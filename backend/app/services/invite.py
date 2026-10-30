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
JOIN_EARLY_MINUTES = 5
JOIN_LATE_MINUTES = 10


class InviteError(Exception):
    """Safe reason code: not_configured or send_failed."""


def _as_utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def join_window(starts_at: datetime | str) -> tuple[datetime, datetime]:
    """Link opens this many minutes before the start, and stays open this many after."""
    start = _as_utc(starts_at)
    return start - timedelta(minutes=JOIN_EARLY_MINUTES), start + timedelta(minutes=JOIN_LATE_MINUTES)


def join_blocked(starts_at: datetime | str | None, *, status: str | None = None,
                 now: datetime | None = None) -> str | None:
    """Human reason the candidate cannot join yet, or None when the link works."""
    if status == "completed":
        return "This interview has ended."
    if status == "live":
        return None
    if not starts_at:
        return None
    try:
        opens, closes = join_window(starts_at)
    except (TypeError, ValueError):
        return None
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    if clock < opens:
        return (
            f"This interview room opens {JOIN_EARLY_MINUTES} minutes before the scheduled time. "
            "Please try again closer to then."
        )
    if clock > closes:
        return (
            f"This interview link closed {JOIN_LATE_MINUTES} minutes after the scheduled start. "
            "Please contact the recruiter if you still need to join."
        )
    return None


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


def _day(value: datetime | str | None) -> str:
    if not value:
        return "the date you applied"
    try:
        when = _as_utc(value)
    except (TypeError, ValueError):
        return "the date you applied"
    return when.strftime("%d %B %Y")


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
                 duration_minutes: int = INVITE_MINUTES, audience: str = "candidate",
                 job_id: str = "", applied_at: datetime | str | None = None,
                 company_name: str = "", recruiter_email: str = "") -> InviteParts:
    """Plain text, HTML and a calendar invite. Nothing here is a score or a CV."""
    ends_at = starts_at + timedelta(minutes=duration_minutes)
    when = _when(starts_at)
    job_label = (job_id or "").strip() or job_title
    company = (company_name or "").strip() or "the hiring team"
    recruiter = (recruiter_email or "").strip() or organizer
    applied = _day(applied_at)
    window = (
        f"The interview room link opens {JOIN_EARLY_MINUTES} minutes before the scheduled time. "
        f"You can still join up to {JOIN_LATE_MINUTES} minutes after it starts."
    )
    if audience == "staff":
        subject = f"Interview for {job_title}"
        calendar = calendar_link(title=subject, start=starts_at, end=ends_at, room_url=room_url)
        plain = (
            f"Hello,\n\n"
            f"You are scheduled to interview a candidate for {job_title}.\n\n"
            f"When: {when}\n"
            f"Length: {duration_minutes} minutes\n"
            f"Job: {job_label}\n"
            f"Company: {company}\n\n"
            f"Interviewer room:\n{room_url}\n\n"
            f"Add it to Google Calendar:\n{calendar}\n\n"
            f"{window}\n"
            f"A calendar invitation is attached. Please do not share the candidate link.\n"
        )
        html_body = (
            "<p>Hello,</p>"
            f"<p>You are scheduled to interview a candidate for <strong>{html.escape(job_title)}</strong>.</p>"
            "<ul>"
            f"<li>When: {html.escape(when)}</li>"
            f"<li>Length: {duration_minutes} minutes</li>"
            f"<li>Job: {html.escape(job_label)}</li>"
            f"<li>Company: {html.escape(company)}</li>"
            "</ul>"
            f"<p>Interviewer room:<br><a href=\"{html.escape(room_url, quote=True)}\">{html.escape(room_url)}</a></p>"
            f"<p><a href=\"{html.escape(calendar, quote=True)}\">Add it to Google Calendar</a></p>"
            f"<p>{html.escape(window)}</p>"
            "<p>A calendar invitation is attached. Please do not share the candidate link.</p>"
        )
    else:
        subject = f"Congratulations — next-round interview for {job_title}"
        calendar = calendar_link(title=f"Interview for {job_title}", start=starts_at, end=ends_at, room_url=room_url)
        plain = (
            f"Congratulations!\n\n"
            f"Regarding job {job_label} that you applied to on {applied}, "
            f"you have been selected for the next round.\n\n"
            f"Your interview is scheduled for {when}.\n"
            f"Length: {duration_minutes} minutes\n\n"
            f"{window}\n\n"
            f"Interview room:\n{room_url}\n\n"
            f"Add it to Google Calendar:\n{calendar}\n\n"
            f"HR / Company: {company}\n"
            f"Recruiter: {recruiter}\n\n"
            f"A calendar invitation is attached. Please do not share the interview room link.\n"
        )
        html_body = (
            "<p><strong>Congratulations!</strong></p>"
            f"<p>Regarding job <strong>{html.escape(job_label)}</strong> that you applied to on "
            f"{html.escape(applied)}, you have been selected for the next round.</p>"
            f"<p>Your interview is scheduled for <strong>{html.escape(when)}</strong>.</p>"
            "<ul>"
            f"<li>Length: {duration_minutes} minutes</li>"
            f"<li>HR / Company: {html.escape(company)}</li>"
            f"<li>Recruiter: {html.escape(recruiter)}</li>"
            "</ul>"
            f"<p>{html.escape(window)}</p>"
            f"<p>Interview room:<br><a href=\"{html.escape(room_url, quote=True)}\">{html.escape(room_url)}</a></p>"
            f"<p><a href=\"{html.escape(calendar, quote=True)}\">Add it to Google Calendar</a></p>"
            "<p>A calendar invitation is attached. Please do not share the interview room link.</p>"
        )
    people = [attendee, *(extra_attendees or [])]
    ics_summary = f"Interview for {job_title}"
    ics = build_ics(uid=uid, sequence=sequence, starts_at=starts_at, ends_at=ends_at, summary=ics_summary,
                    room_url=room_url, organizer=organizer, attendees=people)
    return InviteParts(subject, plain, html_body, ics)


def build_invite(*, job_title: str, starts_at: datetime, room_url: str, job_id: str = "",
                 applied_at: datetime | str | None = None, company_name: str = "",
                 recruiter_email: str = "") -> tuple[str, str]:
    parts = invite_parts(
        job_title=job_title, starts_at=starts_at, room_url=room_url, uid="interview@hirelens",
        sequence=0, organizer="hiring@example.com", attendee="candidate@example.com",
        job_id=job_id, applied_at=applied_at, company_name=company_name, recruiter_email=recruiter_email,
    )
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
