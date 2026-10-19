"""A tiny stand-in for a Blogger MCP server, used only by the tests (runs over stdio).
It records every call to the file named in FAKE_LOG, so tests can check exactly what was sent."""
import json
import os
import sys
import time

from mcp.server.mcpserver import MCPServer

server = MCPServer("fake-blogger")
LOG = os.environ.get("FAKE_LOG", "")
MODE = os.environ.get("FAKE_MODE", "live")      # live | draft | error | slow


def record(entry: dict) -> None:
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


@server.tool()
def create_post(blogId: str, title: str, content: str) -> str:
    """Create a post."""
    record({"tool": "create_post", "args": {"blogId": blogId, "title": title, "content": content},
            "has_api_key": "BLOGGER_API_KEY" in os.environ, "has_other_secret": "SECRET_NOT_FORWARDED" in os.environ})
    if MODE == "error":
        raise RuntimeError("internal failure with token abc123")
    if MODE == "slow":
        time.sleep(30)
    return json.dumps({"id": "555", "url": "https://blog.example/2026/10/post.html", "status": "DRAFT" if MODE == "draft" else "LIVE"})


@server.tool()
def publish_post(blogId: str, postId: str) -> str:
    """Publish a draft."""
    record({"tool": "publish_post", "args": {"blogId": blogId, "postId": postId}})
    return json.dumps({"id": postId, "url": "https://blog.example/2026/10/post.html", "status": "LIVE"})


@server.tool()
def delete_post(blogId: str, postId: str) -> str:
    """A tool the channel must never call."""
    record({"tool": "delete_post", "args": {"postId": postId}})
    return "deleted"


if __name__ == "__main__":
    server.run()
