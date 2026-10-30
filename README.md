# HireLens

Evidence-backed hiring: policy-checked job posts, CV screening, live interviews with server-side transcription, and assessable transcripts.

## Run locally

See `docs/tunnel.md` for remote interview links. Typical local stack:

1. Postgres (pgvector) on `:5434`, Redis on `:6379`
2. API: `cd backend && uvicorn app.main:app --host 127.0.0.1 --port 8000`
3. Celery worker: `celery -A app.workers.celery_app worker --loglevel=INFO --pool=solo`
4. Mail intake: `python mail-ingest/main.py`
5. Frontend: `cd frontend && npm run dev`
6. STT (optional for interviews): `docker compose -f backend/docker-compose.yml up --build stt`

Or `docker compose -f backend/docker-compose.yml up --build` for the full stack.

Copy `backend/.env.example` to `backend/.env` and `frontend/.env.example` to `frontend/.env`. Never commit `.env`.

## Speech transcription (N17, N18)

- Each participant’s microphone is captured in the browser as **mono 16 kHz PCM16 (little-endian), 250 ms / 8000-byte frames**, and sent over the interview WebSocket (unless using browser speech below).
- The **`stt`** service (faster-whisper) or **AssemblyAI** receives audio, returns text, and **does not store audio** by default.
- The API keeps audio **only in memory** until that utterance is transcribed, then discards it. No audio in the database, logs, or sealed JSON (unless `STT_DEBUG_DUMP=1` for short local WAV diagnostics — git-ignored, deleted after 10 minutes).
- Live transcript, notes, co-pilot and evaluation are **interviewer-only**. Candidates see a consent banner, not the transcript. Candidate tokens get **403** on transcript and staff tool endpoints; WebSocket transcript messages use `broadcast_staff` only.
- Optional **`STT_PROVIDER=browser`** (+ `VITE_STT_PROVIDER=browser`): uses the browser **SpeechRecognition** API (`VITE_BROWSER_STT_LANG` / default `en-IN`). **Lower accuracy.** Final phrases are sent as room `speech` events; audio is processed by **Google’s speech service in the browser**, not by HireLens. Labelled in the room UI.
- Accents and code-switching remain imperfect; interviewers can edit turns before the transcript is sealed.

Full detail: [docs/stt.md](docs/stt.md).

## Innovation Feature

**Funnel Doctor** shows, for each job requirement, how many screened candidates were met / partial / unmet, flags bottlenecks and non-discriminating criteria, estimates coverage (not the official score), and offers preferred-only what-if / near-miss views. Staff can request a post-revision suggestion (Gemini, aggregates and post text only; capped per job per day) that feeds the existing job-assistant “changes requested” approval path. See [docs/innovation-rationale.md](docs/innovation-rationale.md).

## Third-party services that may receive data

| Service | Data | Retention in HireLens |
|---------|------|------------------------|
| Google Gemini | Job text, CV text, transcript text for drafting/scoring/eval (synthetic data on free tier) | Not stored at Google by us; see Google’s terms |
| Browser SpeechRecognition (optional) | Live mic audio in Chrome/Edge | Processed by Google’s speech service in-browser; HireLens receives text only |
| Blogger / MCP | Approved job post HTML | Public on the blog |
| Application mailbox (IMAP/SMTP) | Application emails and invites | Mailbox provider |
| AssemblyAI (optional) | Short PCM / streaming audio | See AssemblyAI terms; HireLens does not keep audio |
| `stt` (faster-whisper, self-hosted) | Short PCM utterances | Memory only for the request |

Document any STUN/TURN you add in `VITE_ICE_SERVERS` here as well.
