"""CV collection (F2): validate, store and register CVs that arrive by upload or by email.

Rules:
- Only PDF and DOCX, checked by file CONTENT (not only the extension) and by size (N8).
- Files are stored under a generated name (candidate ID). The client's file name is never used,
  so it cannot cause path tricks and a name inside it never leaks into storage or logs.
- A candidate and its application record are saved atomically. A duplicate email is dropped.
- The CV file is kept only until screening has read it, then deleted.
- An emailed application keeps the sender address when the message names a job. Otherwise the email address written in the CV is kept. Phone numbers and other contact lines are not stored. The email body is never stored.
"""
import io
import logging
import pathlib
import uuid
import zipfile

from app.core.auth import valid_email
from app.graphs.common import email_from_text
from app.services.cvtext import extract_cv_text
from app.services.memory_store import DuplicateApplication

log = logging.getLogger("hirelens.intake")
ALLOWED = {".pdf": "PDF", ".docx": "DOCX"}


class CVRejected(ValueError):
    """Raised with a reason that is safe to show to the user."""


def validate_cv(filename: str, data: bytes, max_bytes: int) -> str:
    """Return the safe extension, or raise CVRejected."""
    ext = pathlib.PurePath(filename or "").suffix.lower()
    if ext not in ALLOWED:
        raise CVRejected("Only PDF and DOCX files are accepted.")
    if not data:
        raise CVRejected("The file is empty.")
    if len(data) > max_bytes:
        raise CVRejected(f"The file is larger than {max_bytes // (1024 * 1024)} MB.")
    if ext == ".pdf" and not data.startswith(b"%PDF-"):
        raise CVRejected("The file is not a valid PDF.")
    if ext == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = z.namelist()
                if "word/document.xml" not in names or sum(i.file_size for i in z.infolist()) > 100 * 1024 * 1024:
                    raise CVRejected("The file is not a valid DOCX.")
        except zipfile.BadZipFile:
            raise CVRejected("The file is not a valid DOCX.") from None
    return ext


class CVStorage:
    """Writes CV files into one directory (a Docker volume in production)."""

    def __init__(self, root: str):
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, candidate_id: str, ext: str, data: bytes) -> str:
        path = self.root / f"{candidate_id}{ext}"
        path.write_bytes(data)
        return str(path)

    def delete(self, path: str | None) -> bool:
        """Remove a stored CV. Refuses anything outside the storage directory."""
        if not path:
            return False
        p = pathlib.Path(path).resolve()
        if p.parent != self.root.resolve():
            log.warning("refused_delete_outside_storage")
            return False
        try:
            p.unlink()
            return True
        except FileNotFoundError:
            return False


class IntakeService:
    def __init__(self, store, storage: CVStorage, max_upload_mb: int = 10, on_queued=None):
        self.store, self.storage, self.max_bytes = store, storage, max_upload_mb * 1024 * 1024
        self.on_queued = on_queued          # optional: ask the worker to screen this candidate

    def accept(self, *, job_id: str, filename: str, data: bytes, source: str, actor_role: str = "system",
               sender_email: str | None = None, message_id_hash: str | None = None) -> dict:
        """Register one CV. Always returns {"filename", "status": "queued"|"rejected", "reason", "candidate_id"}."""
        def result(status, reason=None, cid=None):
            return {"filename": filename, "status": status, "reason": reason, "candidate_id": cid}

        job = self.store.get_job(job_id)
        if not job:
            return result("rejected", "This job does not exist.")
        if job["status"] != "open":
            return result("rejected", "This job is not open for applications.")
        try:
            ext = validate_cv(filename, data, self.max_bytes)
        except CVRejected as exc:
            return result("rejected", str(exc))
        cid = f"cand-{uuid.uuid4().hex[:8]}"
        path = self.storage.save(cid, ext, data)
        address = (sender_email or "").strip().lower()
        if not valid_email(address):
            address = ""
        if not address:
            try:
                address = email_from_text(extract_cv_text(path)) or ""
            except Exception:
                address = ""
        candidate = {"id": cid, "job_id": job_id, "email": address or None, "source": source, "file_path": path}
        try:
            self.store.create_candidate(candidate, {"message_id_hash": message_id_hash} if message_id_hash else None)
        except DuplicateApplication:
            self.storage.delete(path)
            return result("rejected", "This email was already processed.")
        except Exception:
            self.storage.delete(path)          # never leave an orphan file behind
            log.error("candidate_save_failed", extra={"job_id": job_id})
            return result("rejected", "The CV could not be saved. Please try again.")
        self.store.add_audit(actor_role, "cv_received", "candidate", cid)
        log.info("cv_received", extra={"job_id": job_id, "candidate_id": cid, "source": source})
        if self.on_queued:
            try:
                self.on_queued(cid)
            except Exception:
                log.error("screen_enqueue_failed", extra={"candidate_id": cid})
        return result("queued", None, cid)

    def delete_candidate(self, cid: str, actor_role: str) -> bool:
        """N19: remove the candidate, everything linked to them, and the stored file. The audit record stays."""
        path = self.store.delete_candidate(cid)
        if path is None:
            return False
        self.storage.delete(path)
        self.store.add_audit(actor_role, "candidate_deleted", "candidate", cid)
        return True
