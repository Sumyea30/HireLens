"""HireLens speech-to-text microservice (faster-whisper).

POST /transcribe accepts mono PCM16 (or a small WAV) and returns segments.
Audio is held only for the duration of the request. Nothing is written to disk.
"""
from __future__ import annotations

import io
import logging
import os
import time
import wave
from collections import deque
from threading import Lock

import numpy as np
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse
from faster_whisper import WhisperModel

logging.basicConfig(level=logging.INFO, format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}')
log = logging.getLogger("hirelens.stt")

# Prefer a model already on disk for live rooms. distil-large-v3 is more accurate but
# a mid-session HuggingFace download leaves /health down and the transcript empty.
# Never auto-download a different model mid-call.
MODEL = os.environ.get("STT_MODEL", "small").strip() or "small"
FALLBACK = os.environ.get("STT_FALLBACK_MODEL", MODEL).strip() or MODEL
DEVICE = os.environ.get("STT_DEVICE", "cpu").strip() or "cpu"
COMPUTE = os.environ.get("STT_COMPUTE_TYPE", "int8").strip() or "int8"
# Default English for interview rooms. Set STT_LANGUAGE= (empty) to auto-detect.
_lang = os.environ.get("STT_LANGUAGE", "en").strip()
LANGUAGE = _lang or None
CPU_THREADS = max(1, int(os.environ.get("STT_CPU_THREADS", "4")))
NUM_WORKERS = max(1, int(os.environ.get("STT_NUM_WORKERS", "1")))
RTF_LIMIT = 0.8
RTF_STREAK = 3

app = FastAPI(title="HireLens STT", version="1.0.0")
_lock = Lock()
_model: WhisperModel | None = None
_model_name = MODEL
_fast_mode = False
_loading_fallback = False
_rtf_history: deque[float] = deque(maxlen=RTF_STREAK)
_last_rtf: float | None = None


def _load(name: str) -> WhisperModel:
    log.info("stt_loading", extra={"model": name, "device": DEVICE, "compute_type": COMPUTE})
    return WhisperModel(
        name,
        device=DEVICE,
        compute_type=COMPUTE,
        cpu_threads=CPU_THREADS,
        num_workers=NUM_WORKERS,
    )


@app.on_event("startup")
def startup() -> None:
    global _model, _model_name
    _model = _load(MODEL)
    _model_name = MODEL
    log.info("stt_ready", extra={"model": _model_name})


@app.get("/health")
def health():
    return {
        "status": "ok" if _model is not None else "starting",
        "model": _model_name,
        "fast_mode": _fast_mode,
        "device": DEVICE,
        "compute_type": COMPUTE,
        "last_real_time_factor": _last_rtf,
    }


def _pcm_to_float32(data: bytes, sample_rate: int) -> np.ndarray:
    if len(data) >= 12 and data[:4] == b"RIFF":
        with wave.open(io.BytesIO(data), "rb") as wf:
            frames = wf.readframes(wf.getnframes())
            width = wf.getsampwidth()
            channels = wf.getnchannels()
            rate = wf.getframerate()
        if width != 2:
            raise ValueError("unsupported_wav")
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        if rate != 16000:
            # Simple linear resample to 16 kHz.
            duration = len(samples) / rate
            target = int(duration * 16000)
            x_old = np.linspace(0, 1, num=len(samples), endpoint=False)
            x_new = np.linspace(0, 1, num=max(1, target), endpoint=False)
            samples = np.interp(x_new, x_old, samples).astype(np.float32)
        return samples
    if len(data) % 2:
        data = data[:-1]
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def _maybe_downgrade(rtf: float) -> None:
    """Record lag only. Mid-call model swaps download from HuggingFace and freeze live transcripts."""
    global _last_rtf, _fast_mode
    _last_rtf = rtf
    _rtf_history.append(rtf)
    if _fast_mode or len(_rtf_history) < RTF_STREAK:
        return
    if all(v > RTF_LIMIT for v in _rtf_history):
        _fast_mode = True
        log.warning("stt_slow_cpu", extra={"model": _model_name, "rtf": round(rtf, 3)})


@app.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    prompt: str = Form(""),
    sample_rate: int = Form(16000),
):
    if _model is None:
        return JSONResponse({"detail": "model_not_ready"}, status_code=503)
    raw = await audio.read()
    if not raw:
        return {"segments": [], "model": _model_name, "fast_mode": _fast_mode, "real_time_factor": 0.0}
    # Bound request size (~20 s of 16 kHz mono PCM16 ≈ 640 KB; allow some headroom).
    if len(raw) > 2_000_000:
        return JSONResponse({"detail": "too_large"}, status_code=413)
    try:
        samples = _pcm_to_float32(raw, sample_rate)
    except Exception:
        return JSONResponse({"detail": "bad_audio"}, status_code=400)
    duration = float(len(samples) / 16000) if len(samples) else 0.0
    if duration < 0.05:
        return {"segments": [], "model": _model_name, "fast_mode": _fast_mode, "real_time_factor": 0.0}

    t0 = time.perf_counter()
    with _lock:
        model = _model
        name = _model_name
        segments_iter, _info = model.transcribe(
            samples,
            language=LANGUAGE,
            beam_size=5,
            best_of=5,
            temperature=0.0,
            condition_on_previous_text=False,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=400, speech_pad_ms=400),
            compression_ratio_threshold=2.4,
            no_speech_threshold=0.55,
            # Do not pass job skills here: Whisper often reads initial_prompt aloud as "transcript".
            initial_prompt=(prompt.strip() if prompt and prompt.strip() else None),
            word_timestamps=False,
        )
        segs = list(segments_iter)
    elapsed = time.perf_counter() - t0
    rtf = elapsed / duration if duration > 0 else 0.0
    _maybe_downgrade(rtf)

    out = []
    chars = 0
    for s in segs:
        text = (s.text or "").strip()
        if not text:
            continue
        chars += len(text)
        avg_logprob = float(getattr(s, "avg_logprob", 0.0) or 0.0)
        confidence = max(0.0, min(1.0, 1.0 + avg_logprob))
        out.append({
            "text": text,
            "start": float(s.start or 0),
            "end": float(s.end or 0),
            "confidence": confidence,
            "avg_logprob": avg_logprob,
            "no_speech_prob": float(getattr(s, "no_speech_prob", 0.0) or 0.0),
        })
    log.info(
        "stt_call",
        extra={
            "model": name,
            "audio_s": round(duration, 3),
            "stt_s": round(elapsed, 3),
            "rtf": round(rtf, 3),
            "chars": chars,
            "segments": len(out),
        },
    )
    # Drop raw audio reference as soon as we are done.
    del raw, samples
    return {
        "segments": out,
        "model": name,
        "fast_mode": _fast_mode,
        "real_time_factor": round(rtf, 3),
    }
