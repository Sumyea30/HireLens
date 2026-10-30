"""Simple role-based auth (enterprise auth is out of scope, BID section 4.2).

- Passwords are stored as scrypt hashes in USERS_JSON, never in plain text.
- Tokens are signed (HMAC-SHA256) with AUTH_SECRET and expire.
- Make a hash:  python -m app.core.auth hash 'my-password'
"""
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time

from fastapi import Depends, HTTPException, Request

ROLES = ("interviewer", "admin", "candidate")
# One @, no empty or doubled dots, a domain label, and a letter TLD of at least two characters.
_VALID_EMAIL = re.compile(
    r"^[a-z0-9](?:[a-z0-9._%+\-]{0,62}[a-z0-9])?"
    r"@[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?)*"
    r"\.[a-z]{2,}$"
)


def valid_email(value: str) -> bool:
    """True for an address that can be a recruiter login or a job contact email."""
    email = (value or "").strip().lower()
    if ".." in email or len(email) > 254:
        return False
    local, _, domain = email.partition("@")
    return bool(local) and len(local) <= 64 and len(domain) <= 253 and bool(_VALID_EMAIL.fullmatch(email))


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt:{salt.hex()}:{digest.hex()}"   # no "$": safe inside .env files


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.replace("$", ":").split(":")   # also accepts the older $ format
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def issue_token(secret: str, user_id: str, name: str, role: str, ttl_minutes: int) -> str:
    body = _b64(json.dumps({"sub": user_id, "name": name, "role": role,
                            "exp": int(time.time()) + ttl_minutes * 60}).encode())
    sig = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_token(secret: str, token: str) -> dict | None:
    try:
        body, sig = token.split(".")
        expected = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return data if data.get("exp", 0) > time.time() and data.get("role") in ROLES else None
    except (ValueError, TypeError):
        return None


def current_user(request: Request) -> dict:
    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else ""
    data = verify_token(request.app.state.services.settings.auth_secret, token) if token else None
    if not data:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return data


def require_roles(*roles: str):
    def dep(user: dict = Depends(current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(status_code=403, detail="Not allowed")
        return user
    return dep


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "hash":
        print(hash_password(sys.argv[2]))
    else:
        print("usage: python -m app.core.auth hash <password>")
