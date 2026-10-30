"""CV collection (F2): validate, store and register CVs that arrive by upload or by email.

Rules:
- Only PDF and DOCX, checked by file CONTENT (not only the extension) and by size (N8).
- Files are stored under a generated name (candidate ID). The client's file name is never used,
  so it cannot cause path tricks and a name inside it never leaks into storage or logs.
- A candidate and its application record are saved atomically. A duplicate email is dropped.
- The CV file is kept only until screening has read it, then deleted. The face crop is kept.
- Contact email and display name are taken from the CV text. A face photo must also be present.
- Message sender is used only to bootstrap incompleteness emails when the CV has no email yet.
  The email body is never stored.
"""
import io
import logging
import pathlib
import uuid
import zipfile

from app.graphs.common import email_from_text, name_from_text
from app.services.completion import MAX_REQUESTS, send_completion_request
from app.services.cvphoto import pick_face_jpeg
from app.services.cvtext import extract_cv_text
from app.services.invite import InviteError
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
    def __init__(self, store, storage: CVStorage, max_upload_mb: int = 10, on_queued=None,
                 photos=None, face=None, settings=None):
        self.store, self.storage, self.max_bytes = store, storage, max_upload_mb * 1024 * 1024
        self.on_queued = on_queued          # optional: ask the worker to screen this candidate
        self.photos = photos
        self.face = face
        self.settings = settings

    def _extract_identity(self, path: str, candidate_id: str) -> dict:
        try:
            text = extract_cv_text(path)
        except Exception:
            text = ""
        address = email_from_text(text) or ""
        name = name_from_text(text)
        photo_path, embedding = None, None
        if self.photos and self.face:
            jpeg, emb = pick_face_jpeg(path, self.face)
            if jpeg and emb is not None:
                photo_path = self.photos.save(candidate_id, jpeg)
                embedding = emb
        return {"name": name, "email": address or None, "photo_path": photo_path, "face_embedding": embedding}

    def _is_complete(self, identity: dict) -> bool:
        return bool(identity.get("email") and identity.get("photo_path") and identity.get("face_embedding"))

    def _reachable(self, identity: dict, sender_email: str | None) -> str:
        from app.core.auth import valid_email
        for addr in (identity.get("email"), (sender_email or "").strip().lower()):
            if addr and valid_email(addr):
                return addr
        return ""

    def _maybe_request_completion(self, cid: str, job_id: str, to: str, requests_so_far: int) -> int:
        if requests_so_far >= MAX_REQUESTS or not to or not self.settings:
            return requests_so_far
        cand = self.store.get_candidate(cid) or {}
        token = cand.get("completion_token") or uuid.uuid4().hex[:12]
        try:
            mid = send_completion_request(self.settings, to, job_id, token)
        except InviteError:
            log.error("completion_request_not_sent", extra={"candidate_id": cid})
            self.store.update_candidate(cid, completion_token=token)
            return requests_so_far
        self.store.update_candidate(
            cid,
            completion_token=token,
            completion_thread_id=mid,
            completion_requests=requests_so_far + 1,
        )
        self.store.add_audit("system", "cv_completion_requested", "candidate", cid)
        return requests_so_far + 1

    def _enqueue_if_ready(self, cid: str) -> None:
        if not self.on_queued:
            return
        try:
            self.on_queued(cid)
        except Exception:
            log.error("screen_enqueue_failed", extra={"candidate_id": cid})

    def accept(self, *, job_id: str, filename: str, data: bytes, source: str, actor_role: str = "system",
               message_id_hash: str | None = None, sender_email: str | None = None) -> dict:
        """Register one CV. Status is queued when complete, incomplete when email or photo is missing."""
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
        identity = self._extract_identity(path, cid)
        complete = self._is_complete(identity)
        status = "queued" if complete else "incomplete"
        candidate = {
            "id": cid, "job_id": job_id, "name": identity.get("name"), "email": identity.get("email"),
            "source": source, "file_path": path, "photo_path": identity.get("photo_path"),
            "face_embedding": identity.get("face_embedding"), "status": status,
            "completion_requests": 0, "completion_token": None, "completion_thread_id": None,
        }
        try:
            self.store.create_candidate(candidate, {"message_id_hash": message_id_hash} if message_id_hash else None)
        except DuplicateApplication:
            self.storage.delete(path)
            if identity.get("photo_path") and self.photos:
                self.photos.delete(identity["photo_path"])
            return result("rejected", "This email was already processed.")
        except Exception:
            self.storage.delete(path)
            if identity.get("photo_path") and self.photos:
                self.photos.delete(identity["photo_path"])
            log.error("candidate_save_failed", extra={"job_id": job_id})
            return result("rejected", "The CV could not be saved. Please try again.")
        self.store.add_audit(actor_role, "cv_received", "candidate", cid)
        log.info("cv_received", extra={"job_id": job_id, "candidate_id": cid, "source": source, "status": status})
        if complete:
            self._enqueue_if_ready(cid)
            return result("queued", None, cid)
        to = self._reachable(identity, sender_email if source == "email" else None)
        self._maybe_request_completion(cid, job_id, to, 0)
        return result("incomplete", "The CV needs an email address and a photo.", cid)

    def replace_cv(self, *, candidate_id: str, filename: str, data: bytes,
                   sender_email: str | None = None, message_id_hash: str | None = None) -> dict:
        """Replace the CV for an incomplete candidate (completion-email reply)."""
        def result(status, reason=None):
            return {"filename": filename, "status": status, "reason": reason, "candidate_id": candidate_id}

        cand = self.store.get_candidate(candidate_id)
        if not cand or cand.get("status") != "incomplete":
            return result("rejected", "This application is not waiting for a CV update.")
        if message_id_hash and self.store.application_seen(message_id_hash):
            return result("rejected", "This email was already processed.")
        try:
            ext = validate_cv(filename, data, self.max_bytes)
        except CVRejected as exc:
            return result("rejected", str(exc))
        old_path = cand.get("file_path")
        old_photo = cand.get("photo_path")
        path = self.storage.save(candidate_id, ext, data)
        identity = self._extract_identity(path, candidate_id)
        if message_id_hash and not self.store.record_completion_reply(message_id_hash, cand["job_id"], candidate_id):
            self.storage.delete(path)
            if identity.get("photo_path") and self.photos:
                self.photos.delete(identity["photo_path"])
            return result("rejected", "This email was already processed.")
        complete = self._is_complete(identity)
        fields = {
            "name": identity.get("name"),
            "email": identity.get("email"),
            "file_path": path,
            "photo_path": identity.get("photo_path"),
            "face_embedding": identity.get("face_embedding"),
            "status": "queued" if complete else "incomplete",
        }
        self.store.update_candidate(candidate_id, **fields)
        if old_path and old_path != path:
            self.storage.delete(old_path)
        if old_photo and old_photo != identity.get("photo_path") and self.photos:
            self.photos.delete(old_photo)
        self.store.add_audit("system", "cv_replaced", "candidate", candidate_id)
        if complete:
            self._enqueue_if_ready(candidate_id)
            return result("queued")
        requests = int(cand.get("completion_requests") or 0)
        to = self._reachable(identity, sender_email)
        self._maybe_request_completion(candidate_id, cand["job_id"], to, requests)
        return result("incomplete", "The CV still needs an email address and a photo.")

    def delete_candidate(self, cid: str, actor_role: str) -> bool:
        """N19: remove the candidate, everything linked to them, and the stored files. The audit record stays."""
        paths = self.store.delete_candidate(cid)
        if paths is None:
            return False
        if isinstance(paths, str):
            self.storage.delete(paths)
        else:
            self.storage.delete(paths.get("file_path"))
            if self.photos:
                self.photos.delete(paths.get("photo_path"))
        self.store.add_audit(actor_role, "candidate_deleted", "candidate", cid)
        return True
