# Server-side speech-to-text

HireLens transcribes interview audio on the API process. Browsers send ~250 ms frames of mono PCM16 at 16 kHz over the room WebSocket. The API buffers per speaker, cuts on pauses (or at a hard cap), calls the configured STT provider, stores text turns, and shows them to interviewers only.

## Providers

| `STT_PROVIDER` | Backend | Notes |
|----------------|---------|-------|
| `assemblyai` (default) | [AssemblyAI](https://www.assemblyai.com) Universal Streaming | Set `ASSEMBLYAI_API_KEY`. Continuous live captions (not short upload clips). |
| `faster_whisper` | Compose service `stt` (`--profile whisper`) | Self-hosted; set `STT_URL`. |
| `fake` | In-process | Tests only. |

Raw audio is held in memory until the utterance is transcribed, then discarded. Nothing is written to local disk. With AssemblyAI, bytes are uploaded to their API for that request only.

Speaker labels come from the authenticated WebSocket role. There is no separate diarisation step.

## AssemblyAI

```env
STT_PROVIDER=assemblyai
ASSEMBLYAI_API_KEY=your_key_here
STT_LANGUAGE=en
STT_TIMEOUT_SECONDS=90
```

Get a key from the AssemblyAI dashboard. Restart the API after changing `.env`.

## Optional local Whisper

```bash
docker compose -f backend/docker-compose.yml --project-directory . --profile whisper up -d stt
```

```env
STT_PROVIDER=faster_whisper
STT_URL=http://127.0.0.1:8090
STT_LANGUAGE=en
```

| Variable | Default | Notes |
|----------|---------|-------|
| `STT_MODEL` | `small` | Avoid mid-call model swaps |
| `STT_DEVICE` | `cpu` | |
| `STT_COMPUTE_TYPE` | `int8` | |
| `STT_SILENCE_MS` | `500` | End utterance after quiet (API) |
| `STT_MAX_UTTERANCE_S` | `8` | Hard cap per utterance (API) |

## Accuracy

- Mixed Bangla–English and strong accents are harder than clear studio English.
- Low-confidence turns are shown lighter in the interviewer UI; interviewers can edit before seal.
- Browser SpeechRecognition is not used.
