"""AssemblyAI Universal Streaming (v3) for live interview captions.

PCM stays in memory only. One WebSocket session per speaker role.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlencode

log = logging.getLogger("hirelens.stt")

STREAM_URL = "wss://streaming.assemblyai.com/v3/ws"
# English-only streamer — pro models code-switch (Hindi, etc.) even with language_codes=en.
DEFAULT_SPEECH_MODEL = "universal-streaming-english"


@dataclass(frozen=True)
class LiveFinal:
    text: str
    confidence: float
    partial: bool = False


OnFinal = Callable[[LiveFinal], None]


class AssemblyAiLiveSession:
    """Background WebSocket that accepts PCM16@16k and emits finalized turns."""

    def __init__(
        self,
        api_key: str,
        on_final: OnFinal,
        *,
        sample_rate: int = 16000,
        speech_model: str = DEFAULT_SPEECH_MODEL,
        language_code: str = "en",
        label: str = "",
    ):
        self.api_key = (api_key or "").strip()
        self.on_final = on_final
        self.sample_rate = sample_rate
        self.speech_model = speech_model or DEFAULT_SPEECH_MODEL
        # Interview rooms are English; pro models code-switch (e.g. to Hindi) unless locked.
        self.language_code = (language_code or "en").strip() or "en"
        self.label = label or "speaker"
        self._out: queue.Queue[bytes | None] = queue.Queue(maxsize=80)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = threading.Event()

    @property
    def connected(self) -> bool:
        return self._connected.is_set() and not self._stop.is_set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if not self.api_key:
            log.warning("stt_failed", extra={"error_type": "not_configured"})
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"aai-live-{self.label}", daemon=True)
        self._thread.start()

    def push(self, pcm16_mono: bytes) -> None:
        if self._stop.is_set() or not pcm16_mono:
            return
        # Queue even before connect so the first second of speech is not discarded.
        try:
            self._out.put_nowait(pcm16_mono)
        except queue.Full:
            try:
                self._out.get_nowait()
            except queue.Empty:
                pass
            try:
                self._out.put_nowait(pcm16_mono)
            except queue.Full:
                pass

    def close(self) -> None:
        self._stop.set()
        try:
            self._out.put_nowait(None)
        except queue.Full:
            pass
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3.0)
        self._thread = None
        self._connected.clear()

    def _connection_params(self) -> dict[str, str]:
        # Balanced defaults: too-high VAD/EOT left the UI stuck on "Listening" with no turns.
        min_sil = os.environ.get("ASSEMBLYAI_MIN_TURN_SILENCE", "400")
        max_sil = os.environ.get("ASSEMBLYAI_MAX_TURN_SILENCE", "1600")
        vad = os.environ.get("ASSEMBLYAI_VAD_THRESHOLD", "0.4")
        eot = os.environ.get("ASSEMBLYAI_EOT_CONFIDENCE", "0.4")
        params = {
            "sample_rate": str(self.sample_rate),
            "speech_model": self.speech_model,
            "min_turn_silence": str(min_sil),
            "max_turn_silence": str(max_sil),
            "vad_threshold": str(vad),
        }
        if "universal-streaming-english" in self.speech_model:
            params["format_turns"] = "false"
            params["end_of_turn_confidence_threshold"] = str(eot)
        elif "universal-streaming" in self.speech_model:
            params["format_turns"] = "false"
            params["end_of_turn_confidence_threshold"] = str(eot)
            params["language_codes"] = self.language_code
        else:
            params["mode"] = os.environ.get("ASSEMBLYAI_STREAM_MODE", "balanced")
            params["language_codes"] = self.language_code
        return params

    def _run(self) -> None:
        try:
            import websocket
        except ImportError:
            log.warning("stt_failed", extra={"error_type": "websocket_client_missing"})
            return

        url = f"{STREAM_URL}?{urlencode(self._connection_params())}"
        backoff = 0.5
        bytes_per_sec = max(1, self.sample_rate * 2)

        while not self._stop.is_set():
            self._connected.clear()
            ws_holder: dict = {"ws": None}

            def on_open(ws):
                ws_holder["ws"] = ws
                self._connected.set()
                log.info(
                    "aai_stream_open",
                    extra={"speaker": self.label, "model": self.speech_model},
                )

            def on_message(_ws, message):
                if isinstance(message, (bytes, bytearray)):
                    try:
                        message = message.decode("utf-8")
                    except Exception:
                        return
                self._handle_message(message)

            def on_error(_ws, error):
                log.warning(
                    "stt_failed",
                    extra={"error_type": f"aai_ws_{type(error).__name__}"},
                )

            def on_close(_ws, status, msg):
                self._connected.clear()
                log.info(
                    "aai_stream_close",
                    extra={"speaker": self.label, "status": status},
                )

            app = websocket.WebSocketApp(
                url,
                header={"Authorization": self.api_key},
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            runner = threading.Thread(
                target=lambda: app.run_forever(ping_interval=20, ping_timeout=10),
                name=f"aai-ws-{self.label}",
                daemon=True,
            )
            runner.start()

            deadline = time.monotonic() + 8.0
            while not self._stop.is_set() and not self._connected.is_set():
                if time.monotonic() > deadline:
                    break
                time.sleep(0.05)

            sent_audio_s = 0.0
            wall0 = time.monotonic()
            try:
                while not self._stop.is_set() and self._connected.is_set():
                    try:
                        chunk = self._out.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    if chunk is None:
                        try:
                            app.send(json.dumps({"type": "Terminate"}))
                        except Exception:
                            pass
                        self._stop.set()
                        break
                    ws = ws_holder.get("ws")
                    if not ws:
                        continue
                    try:
                        # Prefer ~50–100 ms frames for the streaming API.
                        frame = self.sample_rate // 10 * 2  # 100 ms
                        for i in range(0, len(chunk), frame):
                            piece = chunk[i:i + frame]
                            ws.send(piece, opcode=websocket.ABNF.OPCODE_BINARY)
                            sent_audio_s += len(piece) / bytes_per_sec
                            ahead = sent_audio_s - (time.monotonic() - wall0)
                            if ahead > 0.2:
                                time.sleep(min(ahead, 0.4))
                    except Exception:
                        break
            finally:
                try:
                    app.close()
                except Exception:
                    pass
                runner.join(timeout=2.0)

            if self._stop.is_set():
                break
            time.sleep(backoff)
            backoff = min(8.0, backoff * 1.5)

    def _handle_message(self, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        msg_type = str(data.get("type") or "")
        if msg_type == "Begin":
            log.info("aai_stream_begin", extra={"speaker": self.label})
            return
        if msg_type == "Error":
            log.warning(
                "stt_failed",
                extra={"error_type": f"aai_err_{data.get('error') or data.get('code') or 'unknown'}"},
            )
            return
        if msg_type not in ("Turn", "turn"):
            log.info(
                "aai_stream_msg",
                extra={
                    "speaker": self.label,
                    "msg_type": msg_type or "?",
                    "keys": ",".join(sorted(str(k) for k in data.keys())[:12]),
                },
            )
            return
        text = str(data.get("transcript") or data.get("utterance") or data.get("text") or "").strip()
        end = bool(data.get("end_of_turn"))
        formatted = bool(data.get("turn_is_formatted"))
        if not text:
            return
        # Never surface Devanagari if the model slips.
        if any("\u0900" <= ch <= "\u097f" or "\u0980" <= ch <= "\u09ff" for ch in text):
            log.info("aai_stream_non_english", extra={"speaker": self.label, "chars": len(text)})
            return
        conf = _turn_confidence(data)
        partial = not end and not formatted
        if partial:
            if len(text) < 3:
                return
            # Surface live partials so the UI is not stuck on "Listening…".
            try:
                self.on_final(LiveFinal(text=text, confidence=max(conf, 0.5), partial=True))
            except Exception:
                log.warning("stt_failed", extra={"error_type": "aai_on_partial"})
            return
        if conf < 0.2:
            log.info("aai_stream_low_conf", extra={"speaker": self.label, "chars": len(text)})
            return
        log.info(
            "aai_stream_final",
            extra={"speaker": self.label, "chars": len(text), "confidence": round(conf, 3)},
        )
        try:
            self.on_final(LiveFinal(text=text, confidence=conf, partial=False))
        except Exception:
            log.warning("stt_failed", extra={"error_type": "aai_on_final"})


def _turn_confidence(data: dict) -> float:
    words = data.get("words") or data.get("tokens") or []
    confs: list[float] = []
    for w in words:
        if not isinstance(w, dict):
            continue
        for key in ("confidence", "prob", "probability"):
            if key in w and w[key] is not None:
                try:
                    confs.append(float(w[key]))
                except (TypeError, ValueError):
                    pass
                break
    if confs:
        return max(0.0, min(1.0, sum(confs) / len(confs)))
    for key in ("end_of_turn_confidence", "confidence"):
        raw = data.get(key)
        if raw is not None:
            try:
                return max(0.0, min(1.0, float(raw)))
            except (TypeError, ValueError):
                pass
    return 0.85
