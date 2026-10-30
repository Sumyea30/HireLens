#!/usr/bin/env python3
"""Measure word error rate and real-time factor for the STT service.

Usage (from repo root, with the stt service running):

    python scripts/stt_benchmark.py path/to/clips

Each clip is a .wav file with a matching .txt reference transcript
(same stem). Recordings must stay out of git (see .gitignore).

Environment:
    STT_URL          default http://127.0.0.1:8090
    STT_MODEL_LABEL  optional label printed in the table
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import wave
from pathlib import Path

import httpx


def wer(ref: str, hyp: str) -> float:
    r = ref.lower().split()
    h = hyp.lower().split()
    if not r:
        return 0.0 if not h else 1.0
    # Classic Levenshtein on word lists.
    dp = [[0] * (len(h) + 1) for _ in range(len(r) + 1)]
    for i in range(len(r) + 1):
        dp[i][0] = i
    for j in range(len(h) + 1):
        dp[0][j] = j
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            cost = 0 if r[i - 1] == h[j - 1] else 1
            dp[i][j] = min(dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + cost)
    return dp[-1][-1] / len(r)


def load_wav_pcm16(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wf:
        return wf.readframes(wf.getnframes())


def main() -> int:
    parser = argparse.ArgumentParser(description="WER + RTF benchmark against HireLens STT")
    parser.add_argument("folder", type=Path, help="Folder of .wav + matching .txt files")
    parser.add_argument("--url", default=os.environ.get("STT_URL", "http://127.0.0.1:8090"))
    args = parser.parse_args()
    folder: Path = args.folder
    if not folder.is_dir():
        print(f"Not a folder: {folder}", file=sys.stderr)
        return 2
    wavs = sorted(folder.glob("*.wav"))
    if not wavs:
        print("No .wav files found.", file=sys.stderr)
        return 2
    label = os.environ.get("STT_MODEL_LABEL", "")
    rows = []
    with httpx.Client(timeout=300.0) as client:
        health = client.get(f"{args.url.rstrip('/')}/health").json()
        model = label or health.get("model") or "?"
        print(f"STT model: {model}  device={health.get('device')}  compute={health.get('compute_type')}")
        for wav in wavs:
            ref_path = wav.with_suffix(".txt")
            if not ref_path.is_file():
                print(f"skip {wav.name}: no matching .txt")
                continue
            ref = ref_path.read_text(encoding="utf-8").strip()
            pcm = load_wav_pcm16(wav)
            t0 = time.perf_counter()
            res = client.post(
                f"{args.url.rstrip('/')}/transcribe",
                files={"audio": (wav.name, pcm, "application/octet-stream")},
                data={"prompt": "", "sample_rate": "16000"},
            )
            wall = time.perf_counter() - t0
            res.raise_for_status()
            data = res.json()
            hyp = " ".join(s["text"] for s in data.get("segments") or []).strip()
            rtf = data.get("real_time_factor")
            score = wer(ref, hyp)
            rows.append((wav.name, score, rtf, wall, hyp[:60]))
            print(f"{wav.name:40}  WER={score:6.3f}  RTF={rtf}  wall={wall:.2f}s  hyp={hyp[:60]!r}")
    if rows:
        avg = sum(r[1] for r in rows) / len(rows)
        avg_rtf = sum((r[2] or 0) for r in rows) / len(rows)
        print(f"\nAverage WER={avg:.3f}  Average RTF={avg_rtf:.3f}  n={len(rows)}  model={model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
