"""Email intake: job matching, validation, duplicates, privacy, and the IMAP polling loop."""
import json
import uuid
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

from app.services.mail import ImapMailbox, MailProcessor, find_job_code, parse_email, poll_once
from app.services.memory_store import MemoryStore
from tests.helpers_f2 import PDF, make_cv_docx, make_docx, open_job, wired_intake


def email(subject="Application job-abc12345", attachments=None, to="hiring@example.com",
          msgid=None, sender="Jane Doe <Jane@Example.org>", body="Hello, my CV is attached. SECRET-BODY-TEXT"):
    if attachments is None:
        attachments = (("cv.docx", make_cv_docx()),)
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
    intake, _, _ = wired_intake(tmp_path, store)
    return store, MailProcessor(intake, store), tmp_path / "cvs"


def test_job_code_in_subject_or_plus_address():
    assert find_job_code("Application JOB-ABC12345 please", "") == "job-abc12345"
    assert find_job_code("My CV", "Hiring <hiring+job-abc12345@example.com>") == "job-abc12345"
    assert find_job_code("My CV", "hiring@example.com") is None


def test_accepted_email_creates_a_candidate_and_stores_no_body(tmp_path):
    store, proc, cvdir = setup(tmp_path)
    cv = make_cv_docx(name="Applicant Name", email="applicant@example.com")
    res = proc.process(email(attachments=(("cv.docx", cv),), sender="Jane Doe <sender@example.org>"))
    assert res["outcome"] == "accepted"
    cand = store.get_candidate(res["candidate_id"])
    assert cand["email"] == "applicant@example.com" and cand["status"] == "queued" and cand["source"] == "email"
    assert cand["name"] == "Applicant Name"
    assert [p.name for p in cvdir.iterdir()] == [f"{res['candidate_id']}.docx"]
    stored = json.dumps([store.applications, store.audit], default=str)
    assert "SECRET-BODY-TEXT" not in stored
    assert "sender@example.org" not in stored                                           # message sender is never kept as contact


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
    res = proc.process(email(attachments=(("photo.exe", b"MZ"), ("cv.docx", make_cv_docx()), ("cv2.pdf", PDF))))
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


def test_check_inbox_skips_when_not_configured(tmp_path):
    from types import SimpleNamespace
    from app.services.mail import check_inbox
    store, _proc, _ = setup(tmp_path)
    settings = SimpleNamespace(imap_configured=False)
    assert check_inbox(settings, None, store)["skipped"] == "not_configured"


def test_check_inbox_debounces_rapid_calls(tmp_path):
    from types import SimpleNamespace
    from app.services import mail as mail_mod
    store, proc, _ = setup(tmp_path)
    settings = SimpleNamespace(
        imap_configured=True, imap_host="imap.example.com", imap_port=993,
        imap_user="u", imap_password="p", imap_folder="INBOX",
    )
    mail_mod._last_check_at = 0.0
    with patch.object(mail_mod, "poll_once", return_value={"accepted": 1, "rejected": 0, "duplicate": 0, "error": 0}) as poll:
        with patch.object(mail_mod, "ImapMailbox"):
            with patch.object(mail_mod, "MailProcessor", return_value=proc):
                first = mail_mod.check_inbox(settings, None, store)
                second = mail_mod.check_inbox(settings, None, store)
    assert first["ok"] and first["skipped"] is None and first["counts"]["accepted"] == 1
    assert second["skipped"] == "recent"
    assert poll.call_count == 1


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
