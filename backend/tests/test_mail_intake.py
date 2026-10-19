"""Email intake: job matching, validation, duplicates, privacy, and the IMAP polling loop."""
import json
import uuid
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

from app.services.intake import CVStorage, IntakeService
from app.services.mail import ImapMailbox, MailProcessor, find_job_code, parse_email, poll_once
from app.services.memory_store import MemoryStore
from tests.helpers_f2 import PDF, make_docx, open_job


def email(subject="Application job-abc12345", attachments=(("cv.pdf", PDF),), to="hiring@example.com",
          msgid=None, sender="Jane Doe <Jane@Example.org>", body="Hello, my CV is attached. SECRET-BODY-TEXT"):
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = subject, sender, to
    m["Message-ID"] = msgid or f"<{uuid.uuid4().hex}@mail.example>"
    m.set_content(body)
    for name, data in attachments:
        m.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
    return m.as_bytes()


def setup(tmp_path, job_open=True):
    store = MemoryStore()
    open_job(store) if job_open else store.create_job("job-abc12345")
    intake = IntakeService(store, CVStorage(str(tmp_path / "cvs")), 10)
    return store, MailProcessor(intake, store), tmp_path / "cvs"


def test_job_code_in_subject_or_plus_address():
    assert find_job_code("Application JOB-ABC12345 please", "") == "job-abc12345"
    assert find_job_code("My CV", "Hiring <hiring+job-abc12345@example.com>") == "job-abc12345"
    assert find_job_code("My CV", "hiring@example.com") is None


def test_accepted_email_creates_a_candidate_and_stores_no_body(tmp_path):
    store, proc, cvdir = setup(tmp_path)
    res = proc.process(email())
    assert res["outcome"] == "accepted"
    cand = store.get_candidate(res["candidate_id"])
    assert cand["email"] == "jane@example.org" and cand["status"] == "queued" and cand["source"] == "email"
    assert [p.name for p in cvdir.iterdir()] == [f"{res['candidate_id']}.pdf"]
    stored = json.dumps([store.candidates, store.applications, store.audit], default=str)
    assert "SECRET-BODY-TEXT" not in stored and "Jane Doe" not in stored                 # the body and display name are never kept


def test_same_email_twice_is_processed_once(tmp_path):
    store, proc, cvdir = setup(tmp_path)
    raw = email(msgid="<fixed@mail.example>")
    assert proc.process(raw)["outcome"] == "accepted"
    assert proc.process(raw)["outcome"] == "duplicate"
    assert len(store.candidates) == 1 and len(list(cvdir.iterdir())) == 1


def test_emails_that_cannot_be_matched_are_recorded_and_not_retried(tmp_path):
    store, proc, _ = setup(tmp_path)
    raw = email(subject="Looking for work")
    assert proc.process(raw) == {"outcome": "rejected", "reason": "no_job_code"}
    assert proc.process(raw)["outcome"] == "duplicate"
    assert proc.process(email(subject="Application job-zzzz9999"))["reason"] == "unknown_job"
    assert store.candidates == {} and any(e["action"] == "email_rejected" for e in store.audit)


def test_closed_job_and_bad_attachments(tmp_path):
    store, proc, cvdir = setup(tmp_path, job_open=False)
    assert proc.process(email())["reason"] == "job_not_open"
    store.update_job("job-abc12345", status="open")
    assert proc.process(email(attachments=(("run.exe", b"MZ"),)))["reason"] == "no_valid_attachment"
    assert proc.process(email(attachments=()))["reason"] == "no_valid_attachment"
    assert proc.process(email(attachments=(("fake.pdf", b"nope"),)))["reason"] == "no_valid_attachment"
    assert store.candidates == {} and list(cvdir.iterdir()) == []


def test_first_valid_attachment_wins_and_only_one_cv_per_email(tmp_path):
    store, proc, _ = setup(tmp_path)
    res = proc.process(email(attachments=(("photo.exe", b"MZ"), ("cv.docx", make_docx()), ("cv2.pdf", PDF))))
    assert res["outcome"] == "accepted" and len(store.candidates) == 1
    assert next(iter(store.candidates.values()))["file_path"].endswith(".docx")


class FakeSession:
    def __init__(self, mails): self.mails, self.seen = mails, []
    def fetch_unseen(self, limit=25): return self.mails[:limit]
    def mark_seen(self, uid): self.seen.append(uid)


class FakeMailbox:
    def __init__(self, mails): self.s = FakeSession(mails)
    def session(self):
        import contextlib
        @contextlib.contextmanager
        def cm():
            yield self.s
        return cm()


def test_poll_marks_handled_mail_read_and_isolates_failures(tmp_path):
    store, proc, _ = setup(tmp_path)
    original = proc.process
    def flaky(raw):
        if b"EXPLODE" in raw:
            raise RuntimeError("boom")
        return original(raw)
    proc.process = flaky
    mails = [("1", email()), ("2", email(subject="EXPLODE job-abc12345")), ("3", email(subject="no code")), ("4", email(msgid="<dup@x>")), ("5", email(msgid="<dup@x>"))]
    box = FakeMailbox(mails)
    counts = poll_once(box, proc)
    assert counts == {"accepted": 2, "rejected": 1, "duplicate": 1, "error": 1}
    assert box.s.seen == ["1", "3", "4", "5"]                                            # the failed one stays unread for a retry
    assert len(store.candidates) == 2


def test_imap_commands_use_peek_and_mark_seen_after_handling(tmp_path):
    store, proc, _ = setup(tmp_path)
    raw = email()
    conn = MagicMock()
    def uid(cmd, *args):
        if cmd == "SEARCH": return "OK", [b"7"]
        if cmd == "FETCH": return "OK", [(b"7 (BODY[] {10}", raw), b")"]
        return "OK", [None]
    conn.uid.side_effect = uid
    with patch("app.services.mail.imaplib.IMAP4_SSL", return_value=conn) as ctor:
        counts = poll_once(ImapMailbox("imap.example.com", 993, "user", "secret", "INBOX"), proc)
    ctor.assert_called_once_with("imap.example.com", 993)
    conn.login.assert_called_once_with("user", "secret")
    conn.select.assert_called_once_with("INBOX")
    assert any(c.args[:3] == ("FETCH", b"7", "(BODY.PEEK[])") for c in conn.uid.call_args_list)
    assert any(c.args[:3] == ("STORE", "7", "+FLAGS") for c in conn.uid.call_args_list)
    conn.logout.assert_called_once()
    assert counts["accepted"] == 1
