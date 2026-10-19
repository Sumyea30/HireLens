"""Email a candidate and an interviewer their own room link and a calendar invite.

The candidate link carries a random token. Only a hash of that token is stored.
The interviewer link is the logged-in room and has no token. A text/calendar
REQUEST and an invite.ics file travel with the message. Google Calendar's
template link stays as a fallback. No Calendar API and no extra OAuth scopes.
"""
import html
import logging
import smtplib
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

log = logging.getLogger("hirelens.invite")
INVITE_MINUTES = 60


class InviteError(Exception):
    """Safe reason code: not_configured or send_failed."""


def calendar_link(*, title: str, start: datetime, end: datetime, room_url: str) -> str:
    def stamp(value: datetime) -> str:
        return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    query = urllib.parse.urlencode({
        "action": "TEMPLATE",
        "text": title,
        "dates": f"{stamp(start)}/{stamp(end)}",
        "details": f"Join the HireLens interview room: {room_url}",
        "location": room_url,
    })
    return "https://calendar.google.com/calendar/render?" + query


def _when(start: datetime) -> str:
    offset = start.strftime("%z") or "+0000"
    zone = f"{offset[:3]}:{offset[3:]}"
    return start.strftime("%d %B %Y at %H:%M") + f" (UTC{zone})"


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ics_text(value: str) -> str:
    return (value.replace("\\", "\\\\").replace("\r\n", "\\n").replace("\n", "\\n")
            .replace(",", "\\,").replace(";", "\\;"))


def build_ics(*, uid: str, sequence: int, starts_at: datetime, ends_at: datetime, summary: str,
              room_url: str, organizer: str, attendees: list[str]) -> str:
    """One VEVENT. UID stays stable for the interview. SEQUENCE grows when the time changes."""
    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//HireLens//Interview//EN",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"SEQUENCE:{int(sequence)}",
        f"DTSTAMP:{_stamp(datetime.now(timezone.utc))}",
        f"DTSTART:{_stamp(starts_at)}",
        f"DTEND:{_stamp(ends_at)}",
        f"SUMMARY:{_ics_text(summary)}",
        f"LOCATION:{_ics_text(room_url)}",
        f"DESCRIPTION:{_ics_text('Join the HireLens interview room: ' + room_url)}",
        f"ORGANIZER:mailto:{organizer}",
    ]
    for person in attendees:
        if person:
            lines.append(f"ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{person}")
    lines += [
        "BEGIN:VALARM",
        "TRIGGER:-PT15M",
        "ACTION:DISPLAY",
        "DESCRIPTION:Interview reminder",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


@dataclass(frozen=True)
class InviteParts:
    subject: str
    plain: str
    html: str
    ics: str


def invite_parts(*, job_title: str, starts_at: datetime, room_url: str, uid: str, sequence: int,
                 organizer: str, attendee: str, extra_attendees: list[str] | None = None,
                 duration_minutes: int = INVITE_MINUTES) -> InviteParts:
    """Plain text, HTML and a calendar invite. Nothing here is a score or a CV."""
    ends_at = starts_at + timedelta(minutes=duration_minutes)
    subject = f"Interview for {job_title}"
    calendar = calendar_link(title=subject, start=starts_at, end=ends_at, room_url=room_url)
    when = _when(starts_at)
    plain = (
        f"Hello,\n\n"
        f"You are invited to an interview for {job_title}.\n\n"
        f"When: {when}\n"
        f"Length: {duration_minutes} minutes\n\n"
        f"Interview room:\n{room_url}\n\n"
        f"Add it to Google Calendar:\n{calendar}\n\n"
        f"A calendar invitation is attached. Open the interview room link when it is time to join. "
        f"Please do not share that link.\n"
    )
    html_body = (
        "<p>Hello,</p>"
        f"<p>You are invited to an interview for <strong>{html.escape(job_title)}</strong>.</p>"
        "<ul>"
        f"<li>When: {html.escape(when)}</li>"
        f"<li>Length: {duration_minutes} minutes</li>"
        "</ul>"
        f"<p>Interview room:<br><a href=\"{html.escape(room_url, quote=True)}\">{html.escape(room_url)}</a></p>"
        f"<p><a href=\"{html.escape(calendar, quote=True)}\">Add it to Google Calendar</a></p>"
        "<p>A calendar invitation is attached. Open the interview room link when it is time to join. "
        "Please do not share that link.</p>"
    )
    people = [attendee, *(extra_attendees or [])]
    ics = build_ics(uid=uid, sequence=sequence, starts_at=starts_at, ends_at=ends_at, summary=subject,
                    room_url=room_url, organizer=organizer, attendees=people)
    return InviteParts(subject, plain, html_body, ics)


def build_invite(*, job_title: str, starts_at: datetime, room_url: str) -> tuple[str, str]:
    parts = invite_parts(job_title=job_title, starts_at=starts_at, room_url=room_url, uid="interview@hirelens",
                         sequence=0, organizer="hiring@example.com", attendee="candidate@example.com")
    return parts.subject, parts.plain


def smtp_account(settings) -> tuple[str, int, str, str]:
    """SMTP_HOST wins. Otherwise the mailbox that receives applications, on Gmail's SMTP server."""
    user = (settings.smtp_user or settings.imap_user or "").strip()
    password = settings.smtp_password or settings.imap_password or ""
    host = (settings.smtp_host or "").strip()
    if not host and "gmail" in (settings.imap_host or "").lower():
        host = "smtp.gmail.com"
    port = int(settings.smtp_port or 587)
    return host, port, user, password


def _message(sender: str, to: str, subject: str, body: str, html_body: str | None, ics: str | None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    if not ics:
        return msg
    mixed = EmailMessage()
    mixed["From"] = sender
    mixed["To"] = to
    mixed["Subject"] = subject
    mixed.make_mixed()
    mixed.attach(msg)
    calendar = EmailMessage()
    calendar.set_content(ics, subtype="calendar", charset="utf-8")
    calendar.replace_header("Content-Type", 'text/calendar; method=REQUEST; charset="utf-8"')
    mixed.attach(calendar)
    mixed.add_attachment(ics.encode("utf-8"), maintype="text", subtype="calendar", filename="invite.ics")
    for part in mixed.iter_attachments():
        if part.get_content_subtype() == "calendar":
            part.replace_header("Content-Type", 'text/calendar; method=REQUEST; charset="utf-8"; name="invite.ics"')
    return mixed


def send_invite(settings, to: str, subject: str, body: str, *, html: str | None = None, ics: str | None = None) -> None:
    """Send through the configured SMTP server, or the application mailbox when SMTP_HOST is unset."""
    host, port, user, password = smtp_account(settings)
    if not (user and password and host):
        raise InviteError("not_configured")
    message = _message(user, to, subject, body, html, ics)
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
            smtp.send_message(message)
    except Exception as exc:
        log.error("invite_failed", extra={"error_type": type(exc).__name__})
        raise InviteError("send_failed") from None
