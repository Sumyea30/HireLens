"""Browser log intake. Entries are re-sanitised here, so the server never trusts the client's filtering."""
import json
import logging
import logging.handlers
import pathlib
import re

client_log = logging.getLogger("hirelens.client")
BLOCKED_KEY = re.compile(r"(email|name|phone|text|token|password|authorization|cv|transcript|message|content|body|file|note|address)", re.I)
LEVELS = {"debug", "info", "warn", "error"}


def setup_file(log_dir: str) -> None:
    """Write browser logs to <log_dir>/client.log (rotating). Called once at startup."""
    if not log_dir:
        return
    path = pathlib.Path(log_dir)
    path.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(path / "client.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    client_log.addHandler(handler)
    client_log.setLevel(logging.INFO)


def clean(entry: dict, role: str) -> dict | None:
    event, level = entry.get("event"), entry.get("level")
    if not isinstance(event, str) or level not in LEVELS:
        return None
    ctx = entry.get("ctx") if isinstance(entry.get("ctx"), dict) else {}
    safe = {k[:40]: (v[:80] if isinstance(v, str) else v) for k, v in ctx.items()
            if isinstance(k, str) and not BLOCKED_KEY.search(k) and isinstance(v, (str, int, float, bool, type(None)))}
    return {"ts": str(entry.get("ts", ""))[:32], "role": role, "level": level, "event": event[:80], "ctx": safe}


def write(entries: list[dict], role: str) -> int:
    n = 0
    for e in entries:
        c = clean(e, role)
        if c:
            client_log.info(json.dumps(c))
            n += 1
    return n
