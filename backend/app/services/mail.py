"""Email intake: turn an application email into a candidate.

Which job? The job code (job-xxxxxxxx) in the subject, or in a plus-address such as hiring+job-xxxxxxxx@...
The published job post tells applicants to put it in the subject.
One CV per email: the first valid attachment is used. The body is never read for content or stored.
"""
import contextlib
import hashlib
import imaplib
import logging
import re
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr

log = logging.getLogger("hirelens.mail")
JOB_CODE = re.compile(r"\bjob-[a-z0-9]{4,32}\b", re.IGNORECASE)
MAX_ATTACHMENTS_SCANNED = 5


@dataclass
class ParsedEmail:
    message_id_hash: str
    subject: str
    recipients: str
    sender: str | None
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
    return ParsedEmail(digest, str(msg["Subject"] or ""), recipients, sender, attachments)


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

    def process(self, raw: bytes) -> dict:
        p = parse_email(raw)
        if self.store.application_seen(p.message_id_hash):
            return {"outcome": "duplicate"}
        code = find_job_code(p.subject, p.recipients)
        if not code:
            return self._reject(p, None, "no_job_code")
        job = self.store.get_job(code)
        if not job:
            return self._reject(p, None, "unknown_job")
        if job["status"] != "open":
            return self._reject(p, job["id"], "job_not_open")
        for filename, data in p.attachments:           # one CV per email: the first valid one wins
            res = self.intake.accept(job_id=job["id"], filename=filename, data=data, source="email",
                                     sender_email=p.sender, message_id_hash=p.message_id_hash)
            if res["status"] == "queued":
                return {"outcome": "accepted", "candidate_id": res["candidate_id"]}
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
