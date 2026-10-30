"""Email intake service. From the backend folder: python mail-ingest/main.py
Polls the application mailbox and turns CV emails into candidates. Needs DATABASE_URL and the IMAP_* settings."""
import logging
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import load_settings
from app.services.face import FaceService, build_face_engine
from app.services.intake import CVStorage, IntakeService
from app.services.photos import PhotoStorage
from app.services.screening import make_on_queued
from app.services.mail import ImapMailbox, MailProcessor, poll_once
from app.services.pgstore import PgStore

logging.basicConfig(level=logging.INFO, format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}')
log = logging.getLogger("hirelens.mail_ingest")
running = True


def stop(*_):
    global running
    running = False


def main() -> None:
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    s = load_settings()      # also reads .env from this folder or the repo root
    if not (s.imap_configured and s.database_url):
        log.warning("email intake is not configured (IMAP_HOST, IMAP_USER, IMAP_PASSWORD, DATABASE_URL). Idling.")
        while running:
            time.sleep(5)
        return
    store = PgStore(s.database_url, None, s.embedding_dims, s.apply_email)    # no embeddings needed here
    photos = PhotoStorage(s.photo_dir)
    face = FaceService(build_face_engine(s.face_provider), s.face_match_threshold)
    intake = IntakeService(
        store, CVStorage(s.cv_dir), s.max_upload_mb, on_queued=make_on_queued(s),
        photos=photos, face=face, settings=s,
    )
    processor = MailProcessor(intake, store)
    mailbox = ImapMailbox(s.imap_host, s.imap_port, s.imap_user, s.imap_password, s.imap_folder)
    delay = s.mail_poll_seconds
    log.info("email intake started")
    while running:
        try:
            counts = poll_once(mailbox, processor)
            if any(counts.values()):
                log.info(f"poll done {counts}")
            delay = s.mail_poll_seconds
        except Exception as exc:                       # network or login problem: back off, never crash
            log.error(f"poll failed ({type(exc).__name__}: {exc})")
            delay = min(delay * 2, 600)
        for _ in range(int(delay)):                    # sleep in small steps so shutdown is quick
            if not running:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
