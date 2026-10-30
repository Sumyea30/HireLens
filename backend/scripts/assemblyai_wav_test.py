"""Stream a WAV file to AssemblyAI at real-time pace and print the turns.

Use it on a recording of your own voice to see whether the problem is the model or your
app. Run several models on the same clip and compare the word error rate.

    python scripts/assemblyai_wav_test.py clip.wav --model u3-rt-pro --language en --ref clip.txt --terms "FastAPI,LangGraph"

The WAV must be 16 kHz, mono, 16-bit PCM. Convert with:
    ffmpeg -i in.m4a -ac 1 -ar 16000 -sample_fmt s16 clip.wav
Set ASSEMBLYAI_API_KEY in the environment. Never commit audio or keys.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.assemblyai_stream import AssemblyAISpeakerStream, SpeechTurn


def wer(ref: str, hyp: str) -> float:
    def norm(s: str) -> list[str]:
        return re.sub(r"[^a-z0-9' ]+", " ", s.lower()).split()

    r, h = norm(ref), norm(hyp)
    d = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, hw in enumerate(h, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (rw != hw))
    return d[len(h)] / max(len(r), 1)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--model", default=None, help="speech_model name, e.g. u3-rt-pro, whisper-rt")
    ap.add_argument("--ref", help="text file with the words actually spoken")
    ap.add_argument("--language", default=None, help="pin a language, e.g. en")
    ap.add_argument("--terms", default="", help="comma-separated key terms")
    ap.add_argument("--frame-ms", type=int, default=250)
    args = ap.parse_args()

    key = os.environ.get("ASSEMBLYAI_API_KEY", "")
    if not key:
        print("Set ASSEMBLYAI_API_KEY first.")
        return 2
    with wave.open(args.wav, "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            print("WAV must be 16 kHz, mono, 16-bit. Convert it with ffmpeg (see the file header).")
            return 2
        pcm = w.readframes(w.getnframes())

    turns: list[SpeechTurn] = []

    async def on_turn(t: SpeechTurn) -> None:
        turns.append(t)
        print(f"[{t.start_ms/1000:6.1f}s-{t.end_ms/1000:6.1f}s] {t.text}")

    async def on_status(s: str) -> None:
        print("status:", s)

    session = AssemblyAISpeakerStream(
        api_key=key,
        speaker="test",
        on_turn=on_turn,
        on_status=on_status,
        keyterms=[t for t in args.terms.split(",") if t],
        speech_model=args.model,
        language_code=args.language,
    )
    await session.start()
    step = 16000 * 2 * args.frame_ms // 1000
    t0 = time.time()
    for i in range(0, len(pcm), step):
        session.feed(pcm[i : i + step])
        await asyncio.sleep(args.frame_ms / 1000)  # real-time pace
    await asyncio.sleep(3)  # let the last turn finish
    await session.stop()

    print(f"\nturns: {len(turns)}  dropped frames: {session.dropped_frames}  wall time: {time.time()-t0:.1f}s")
    if args.ref:
        ref = open(args.ref, encoding="utf-8").read()
        hyp = " ".join(t.text for t in turns)
        print(f"word error rate: {wer(ref, hyp):.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
