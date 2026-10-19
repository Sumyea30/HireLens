"""F1 + F2 publishing through the MCP (Blogger) channel, end to end, with a real MCP server process."""
import json
import pathlib
import sys

from fastapi.testclient import TestClient

from app.graphs import Deps, build_job_post_graph, memory_checkpointer
from app.main import Services, build_channels, create_app
from app.services.channels.mcp_channel import McpChannel, McpConfig
from app.services.intake import CVStorage, IntakeService
from app.services.memory_store import MemoryStore
from app.services.policy import PolicyService
from tests.helpers_f2 import PDF, settings
from tests.test_api_f1 import POSTING, _llm
from tests.test_mcp_channel import SERVER, calls


def make(tmp_path, mode="live", publish_tool=""):
    log = tmp_path / "calls.log"
    cfg = McpConfig(command=sys.executable, args=[SERVER], env_keys=["FAKE_LOG", "FAKE_MODE"], blog_id="blog-1", publish_tool=publish_tool, timeout_s=30)
    channel = McpChannel(cfg, env={"FAKE_LOG": str(log), "FAKE_MODE": mode})
    store = MemoryStore("hiring@example.com")
    deps = Deps(llm=_llm(), store=store, repo=store, channels={"blogger": channel}, apply_email="hiring@example.com")
    svc = Services(settings(), store, deps, build_job_post_graph(deps, memory_checkpointer()), PolicyService(store),
                   IntakeService(store, CVStorage(str(tmp_path / "cvs")), 10))
    c = TestClient(create_app(svc))
    h = {"Authorization": "Bearer " + c.post("/auth/login", json={"username": "ivy", "password": "pw-ivy"}).json()["token"]}
    job_id = c.post("/jobs/assistant", headers=h, json={"message": "We need a backend developer.\n" + POSTING}).json()["job_id"]
    return c, h, job_id, log


def test_approved_post_is_published_to_blogger_and_cv_collection_opens(tmp_path):
    c, h, job_id, log = make(tmp_path)
    a = c.get(f"/jobs/{job_id}/assistant", headers=h).json()
    assert a["status"] == "awaiting_approval" and a["approval"]["proposed_channels"] == ["blogger"]
    assert calls(log) == []                                                    # nothing published before approval

    done = c.post(f"/jobs/{job_id}/approval", headers=h, json={"action": "approve", "channels": ["blogger"]}).json()
    assert done["status"] == "open" and done["publish_results"][0]["channel"] == "blogger"
    assert done["publish_results"][0]["url"] == "https://blog.example/2026/10/post.html"
    (call,) = calls(log)
    assert f"Application {job_id}" in call["args"]["content"] and "hiring@example.com" in call["args"]["content"]
    assert c.get(f"/jobs/{job_id}", headers=h).json()["channels"] == ["blogger"]

    up = c.post(f"/jobs/{job_id}/cvs", headers=h, files=[("files", ("a.pdf", PDF, "application/pdf"))]).json()   # F2: now open for CVs
    assert up[0]["status"] == "queued"


def test_a_publishing_failure_is_explained_with_a_safe_code_and_nothing_goes_live(tmp_path):
    c, h, job_id, log = make(tmp_path, mode="draft")                           # server creates hidden drafts, no publish tool set
    out = c.post(f"/jobs/{job_id}/approval", headers=h, json={"action": "approve", "channels": ["blogger"]}).json()
    assert out["status"] != "open" and out["publish_results"][0]["error"] == "created_as_draft"
    assert "blogger: created_as_draft" in out["messages"][-1]["content"]
    assert c.get(f"/jobs/{job_id}", headers=h).json()["status"] != "open"
    assert c.post(f"/jobs/{job_id}/cvs", headers=h, files=[("files", ("a.pdf", PDF, "application/pdf"))]).json()[0]["status"] == "rejected"


def test_blogger_is_the_only_channel_and_there_is_no_fallback():
    assert build_channels(settings()) == {}                                        # nothing configured: nothing offered
    s = settings(mcp_command="npx", mcp_args='["-y","some-blogger-mcp@1.0.0"]')
    assert list(build_channels(s)) == ["blogger"]
    direct = settings(blogger_blog_id="12345", blogger_client_id="cid", blogger_client_secret="sec", blogger_refresh_token="ref")
    assert build_channels(direct)["blogger"].blog_id == "12345"


def test_publish_posts_to_the_configured_blog_as_a_live_post(monkeypatch):
    urls = []

    class _Resp:
        def __init__(self, body: bytes):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self._body

    def urlopen(req, timeout=None):
        urls.append(req.full_url)
        body = b'{"access_token":"tok"}' if "oauth2.googleapis.com" in req.full_url else b'{"url":"https://www.blogger.com/p/1"}'
        return _Resp(body)

    monkeypatch.setattr("app.services.blogger.urllib.request.urlopen", urlopen)
    from app.services.blogger import BloggerChannel
    result = BloggerChannel("12345", "cid", "sec", "ref").publish(title="Backend Developer", body="We are hiring.")
    assert result["status"] == "ok"
    assert urls[-1].startswith("https://www.googleapis.com/blogger/v3/blogs/12345/posts?")
    assert "isDraft=false" in urls[-1]
    assert "BLOGGER_BLOG_ID" not in urls[-1]
