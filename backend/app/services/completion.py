"""Ask applicants to resend a CV that includes both an email address and a photo."""
from __future__ import annotations

import logging
import uuid
from email.message import EmailMessage

from app.services.invite import InviteError, smtp_account

log = logging.getLogger("hirelens.completion")
MAX_REQUESTS = 2
TOKEN_PREFIX = "HL-"


def completion_subject(job_id: str, token: str) -> str:
    return f"Your CV is not complete for {job_id} [{TOKEN_PREFIX}{token}]"


def completion_body(job_id: str) -> str:
    return (
        f"Dear candidate,\n\n"
        f"Your CV is not complete for job {job_id}. "
        f"Please reply to this email with a CV that includes your photo and email address.\n\n"
        f"Thank you.\nHireLens\n"
    )


def find_completion_token(subject: str) -> str | None:
    import re
    m = re.search(rf"\[{TOKEN_PREFIX}([a-zA-Z0-9]+)\]", subject or "")
    return m.group(1) if m else None


def send_completion_request(settings, to: str, job_id: str, token: str) -> str:
    """Send the incompleteness mail. Returns the Message-ID used (for reply matching)."""
    host, port, user, password = smtp_account(settings)
    if not (user and password and host):
        raise InviteError("not_configured")
    mid = f"<{uuid.uuid4().hex}@hirelens.completion>"
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = completion_subject(job_id, token)
    msg["Message-ID"] = mid
    msg.set_content(completion_body(job_id))
    import smtplib
    try:
        if port == 465:
            smtp = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            smtp = smtplib.SMTP(host, port, timeout=20)
        with smtp:
            if port != 465:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
            smtp.login(user, password)
            smtp.send_message(msg)
    except Exception:
        log.error("completion_mail_failed")
        raise InviteError("send_failed") from None
    return mid
