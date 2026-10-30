"""Blogger posting channel. Publishes an approved job post to a Blogger blog.

Uses the Blogger API v3 with an OAuth refresh token (an API key cannot write posts).
Standard library only. Never logs or returns tokens or response bodies.
"""
import html
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("hirelens.blogger")

TOKEN_URL = "https://oauth2.googleapis.com/token"
POSTS_URL = "https://www.googleapis.com/blogger/v3/blogs/{blog_id}/posts?isDraft=false"
TIMEOUT_SECONDS = 20


def _to_html(text: str) -> str:
    """Plain text -> simple HTML: blank line = new paragraph, newline = <br>."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    return "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paragraphs)


class BloggerChannel:
    name = "blogger"

    def __init__(self, blog_id: str, client_id: str, client_secret: str, refresh_token: str):
        self.blog_id = blog_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token

    @classmethod
    def from_env(cls) -> "BloggerChannel":
        return cls(
            os.environ.get("BLOGGER_BLOG_ID", ""),
            os.environ.get("BLOGGER_CLIENT_ID", ""),
            os.environ.get("BLOGGER_CLIENT_SECRET", ""),
            os.environ.get("BLOGGER_REFRESH_TOKEN", ""),
        )

    @property
    def configured(self) -> bool:
        return all([self.blog_id, self.client_id, self.client_secret, self.refresh_token])

    def _access_token(self) -> str:
        data = urllib.parse.urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        }).encode("utf-8")
        with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data, method="POST"),
                                    timeout=TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))["access_token"]

    def publish(self, *, title: str, body: str) -> dict:
        """Return {"status": "ok" | "error", "url": str | None, "error": str | None}."""
        if not self.configured:
            return {"status": "error", "url": None, "error": "not configured"}
        try:
            token = self._access_token()
            payload = json.dumps({"kind": "blogger#post", "title": title,
                                  "content": _to_html(body)}).encode("utf-8")
            request = urllib.request.Request(
                POSTS_URL.format(blog_id=urllib.parse.quote(self.blog_id)),
                data=payload, method="POST",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            log.error("blogger_publish_failed", extra={"http_status": exc.code})
            return {"status": "error", "url": None, "error": f"HTTP {exc.code}"}
        except Exception as exc:  # network error, timeout, bad JSON, missing token
            log.error("blogger_publish_failed", extra={"error_type": type(exc).__name__})
            return {"status": "error", "url": None, "error": type(exc).__name__}
        return {"status": "ok", "url": data.get("url"), "error": None}