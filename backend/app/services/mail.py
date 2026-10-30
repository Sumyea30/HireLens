"""Email intake: turn an application email into a candidate.

Which job? The job code (job-xxxxxxxx) in the subject, or in a plus-address such as hiring+job-xxxxxxxx@...
The published job post tells applicants to put it in the subject.
One CV per email: the first valid attachment is used. The body is never read for content or stored.

Replies to incompleteness requests (subject token [HL-...] or In-Reply-To) replace the CV
for that candidate instead of creating a new application.
"""
import contextlib
import hashlib
import imaplib
import logging
import re
import threading
import time
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr

from app.services.completion import find_completion_token

log = logging.getLogger("hirelens.mail")
JOB_CODE = re.compile(r"\bjob-[a-z0-9]{4,32}\b", re.IGNORECASE)
MAX_ATTACHMENTS_SCANNED = 5


@dataclass
class ParsedEmail:
    message_id_hash: str
    message_id: str
    subject: str
    recipients: str
    sender: str | None
    in_reply_to: str
    references: str
    attachments: list[tuple[str, bytes]]


def parse_email(raw: bytes) -> ParsedEmail:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    mid = (msg["Message-ID"] or "").strip()
    digest = hashlib.sha256(mid.encode() if mid else raw).hexdigest()
    recipients = " ".join(str(msg[h] or "") for h in ("To", "Cc", "Delivered-To", "X-Original-To"))
    sender = parseaddr(str(msg["From"] or ""))[1].lower() or None
    attachments: list[tuple[str, bytes]] = []
    for part in msg.iter_attachments():
        data = part.get_payload(decode=True)
        if data:
            attachments.append((part.get_filename() or "", data))
        if len(attachments) >= MAX_ATTACHMENTS_SCANNED:
            break
    return ParsedEmail(
        digest, mid, str(msg["Subject"] or ""), recipients, sender,
        str(msg["In-Reply-To"] or ""), str(msg["References"] or ""), attachments,
    )


def find_job_code(subject: str, recipients: str) -> str | None:
    for text in (subject, recipients):
        m = JOB_CODE.search(text)
        if m:
            return m.group(0).lower()
    return None


class MailProcessor:
    def __init__(self, intake, store):
        self.intake, self.store = intake, store

    def _reject(self, p: ParsedEmail, job_id: str | None, reason: str) -> dict:
        if self.store.record_rejected_application(p.message_id_hash, job_id, reason):
            self.store.add_audit("system", "email_rejected", "application", p.message_id_hash[:12], "denied")
        log.info("email_rejected", extra={"reason": reason})
        return {"outcome": "rejected", "reason": reason}

    def _find_incomplete(self, p: ParsedEmail):
        token = find_completion_token(p.subject)
        if token:
            found = self.store.get_candidate_by_completion_token(token)
            if found and found.get("status") == "incomplete":
                return found
        for header in (p.in_reply_to, p.references):
            found = self.store.get_candidate_by_completion_thread(header)
            if found and found.get("status") == "incomplete":
                return found
        return None

    def process(self, raw: bytes) -> dict:
        p = parse_email(raw)
        if self.store.application_seen(p.message_id_hash):
            return {"outcome": "duplicate"}

        incomplete = self._find_incomplete(p)
        if incomplete:
            for filename, data in p.attachments:
                res = self.intake.replace_cv(
                    candidate_id=incomplete["id"], filename=filename, data=data,
                    sender_email=p.sender, message_id_hash=p.message_id_hash,
                )
                if res["status"] in ("queued", "incomplete"):
                    return {"outcome": "accepted", "candidate_id": res["candidate_id"], "status": res["status"]}
                if res["reason"] == "This email was already processed.":
                    return {"outcome": "duplicate"}
            return self._reject(p, incomplete.get("job_id"), "no_valid_attachment")

        code = find_job_code(p.subject, p.recipients)
        if not code:
            return self._reject(p, None, "no_job_code")
        job = self.store.get_job(code)
        if not job:
            return self._reject(p, None, "unknown_job")
        if job["status"] != "open":
            return self._reject(p, job["id"], "job_not_open")
        for filename, data in p.attachments:           # one CV per email: the first valid one wins
            res = self.intake.accept(
                job_id=job["id"], filename=filename, data=data, source="email",
                message_id_hash=p.message_id_hash, sender_email=p.sender,
            )
            if res["status"] in ("queued", "incomplete"):
                return {"outcome": "accepted", "candidate_id": res["candidate_id"], "status": res["status"]}
            if res["reason"] == "This email was already processed.":
                return {"outcome": "duplicate"}
        return self._reject(p, job["id"], "no_valid_attachment")


class ImapSession:
    def __init__(self, conn: imaplib.IMAP4_SSL):
        self.conn = conn

    def fetch_unseen(self, limit: int = 25) -> list[tuple[str, bytes]]:
        typ, data = self.conn.uid("SEARCH", None, "UNSEEN")
        uids = data[0].split()[:limit] if typ == "OK" and data and data[0] else []
        out = []
        for uid in uids:
            typ, parts = self.conn.uid("FETCH", uid, "(BODY.PEEK[])")     # PEEK: does not mark as read yet
            if typ == "OK" and parts and isinstance(parts[0], tuple):
                out.append((uid.decode(), parts[0][1]))
        return out

    def mark_seen(self, uid: str) -> None:
        self.conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")


class ImapMailbox:
    def __init__(self, host: str, port: int, user: str, password: str, folder: str = "INBOX"):
        self.host, self.port, self.user, self.password, self.folder = host, port, user, password, folder

    @contextlib.contextmanager
    def session(self):
        conn = imaplib.IMAP4_SSL(self.host, self.port)
        try:
            conn.login(self.user, self.password)
            conn.select(self.folder)
            yield ImapSession(conn)
        finally:
            with contextlib.suppress(Exception):
                conn.logout()


def poll_once(mailbox, processor: MailProcessor, limit: int = 25) -> dict:
    """Fetch unread mail and process it. A message is marked read only after it was handled,
    so a crash means a retry (the message hash makes retries safe). One bad email never stops the rest."""
    counts = {"accepted": 0, "rejected": 0, "duplicate": 0, "error": 0}
    with mailbox.session() as s:
        for uid, raw in s.fetch_unseen(limit):
            try:
                outcome = processor.process(raw)["outcome"]
                counts[outcome] += 1
                s.mark_seen(uid)
            except Exception as exc:
                counts["error"] += 1
                log.error("email_failed", extra={"error_type": type(exc).__name__})
    return counts


# Staff page refresh can hit this often; skip if we just polled.
_CHECK_GAP_S = 15.0
_check_lock = threading.Lock()
_last_check_at = 0.0


def check_inbox(settings, intake, store, *, force: bool = False) -> dict:
    """One IMAP poll for the API (Candidates / Dashboard refresh). Safe if mail-ingest is also running."""
    global _last_check_at
    if not getattr(settings, "imap_configured", False):
        return {"ok": False, "skipped": "not_configured", "counts": {}}
    with _check_lock:
        now = time.monotonic()
        if not force and (now - _last_check_at) < _CHECK_GAP_S:
            return {"ok": True, "skipped": "recent", "counts": {}}
        _last_check_at = now
    mailbox = ImapMailbox(
        settings.imap_host, settings.imap_port, settings.imap_user, settings.imap_password, settings.imap_folder,
    )
    processor = MailProcessor(intake, store)
    try:
        counts = poll_once(mailbox, processor)
        return {"ok": True, "skipped": None, "counts": counts}
    except Exception as exc:
        log.error("mail_check_failed", extra={"error_type": type(exc).__name__})
        return {"ok": False, "skipped": None, "error": type(exc).__name__, "counts": {}}
