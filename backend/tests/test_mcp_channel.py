"""The MCP channel against a real MCP server process (tests/fake_blogger_server.py), over stdio."""
import json
import pathlib
import sys

import pytest

from app.core.config import Settings
from app.services.channels.mcp_channel import McpChannel, McpConfig, mcp_config, render, to_html

SERVER = str(pathlib.Path(__file__).parent / "fake_blogger_server.py")


def channel(tmp_path, mode="live", publish_tool="", create_args=None, publish_args=None, create_tool="create_post", timeout=30.0):
    log = tmp_path / "calls.log"
    cfg = McpConfig(command=sys.executable, args=[SERVER], env_keys=["BLOGGER_API_KEY", "FAKE_LOG", "FAKE_MODE"], blog_id="blog-1",
                    publish_tool=publish_tool, create_tool=create_tool, timeout_s=timeout)
    if create_args is not None:
        cfg.create_args = create_args
    if publish_args is not None:
        cfg.publish_args = publish_args
    env = {"BLOGGER_API_KEY": "key-1", "SECRET_NOT_FORWARDED": "nope", "FAKE_LOG": str(log), "FAKE_MODE": mode}
    return McpChannel(cfg, env=env), log


def calls(log: pathlib.Path) -> list[dict]:
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


def test_publishes_with_the_configured_tool_and_returns_the_public_link(tmp_path):
    ch, log = channel(tmp_path)
    res = ch.publish(title="Backend Developer", body="Join us.\n\nWe are an equal opportunity employer.\nApply by email.")
    assert res == {"status": "ok", "url": "https://blog.example/2026/10/post.html", "error": None}
    (call,) = calls(log)
    assert call["tool"] == "create_post"
    assert call["args"]["blogId"] == "blog-1" and call["args"]["title"] == "Backend Developer"
    assert call["args"]["content"] == "<p>Join us.</p><p>We are an equal opportunity employer.<br>Apply by email.</p>"
    assert call["has_api_key"] is True and call["has_other_secret"] is False      # only listed variables reach the server


def test_post_text_is_escaped_so_it_cannot_inject_html(tmp_path):
    ch, log = channel(tmp_path)
    ch.publish(title="T", body='<script>alert("x")</script> & <img src=x onerror=y>')
    content = calls(log)[0]["args"]["content"]
    assert "<script" not in content and "<img" not in content and "&lt;script&gt;" in content and "&amp;" in content
    assert to_html("a\n\n\n\nb") == "<p>a</p><p>b</p>"


def test_drafts_are_published_with_a_second_call_when_a_publish_tool_is_configured(tmp_path):
    ch, log = channel(tmp_path, mode="draft", publish_tool="publish_post")
    res = ch.publish(title="T", body="Body")
    assert res["status"] == "ok" and res["url"].startswith("https://blog.example/")
    assert [c["tool"] for c in calls(log)] == ["create_post", "publish_post"]
    assert calls(log)[1]["args"] == {"blogId": "blog-1", "postId": "555"}


def test_a_hidden_draft_is_reported_as_an_error_not_as_published(tmp_path):
    ch, log = channel(tmp_path, mode="draft")
    assert ch.publish(title="T", body="Body") == {"status": "error", "url": None, "error": "created_as_draft"}
    assert [c["tool"] for c in calls(log)] == ["create_post"]


def test_only_configured_tools_are_ever_called(tmp_path):
    ch, log = channel(tmp_path, publish_tool="publish_post")
    ch.publish(title="T", body="Body")
    assert "delete_post" not in [c["tool"] for c in calls(log)]


def test_missing_tool_and_missing_required_arguments_are_caught_before_calling(tmp_path):
    ch, log = channel(tmp_path, create_tool="make_post")
    assert ch.publish(title="T", body="B")["error"] == "tool_not_found:make_post"
    ch, log = channel(tmp_path, create_args={"title": "{title}"})                    # server requires blogId and content too
    assert ch.publish(title="T", body="B")["error"] == "missing_required_args:blogId,content"
    assert calls(log) == []                                                          # nothing was sent


def test_server_failures_become_short_codes_without_leaking_details(tmp_path):
    ch, _ = channel(tmp_path, mode="error")
    res = ch.publish(title="T", body="B")
    assert res["status"] == "error" and res["error"] == "tool_error" and "abc123" not in json.dumps(res)


def test_a_hung_server_times_out_instead_of_blocking(tmp_path):
    ch, _ = channel(tmp_path, mode="slow", timeout=3.0)
    res = ch.publish(title="T", body="B")
    assert res == {"status": "error", "url": None, "error": "timeout"}


def test_not_configured_and_unstartable_servers(tmp_path):
    assert McpChannel(McpConfig()).publish(title="T", body="B") == {"status": "error", "url": None, "error": "mcp_not_configured"}
    res = McpChannel(McpConfig(command="definitely-not-a-real-program-xyz")).publish(title="T", body="B")
    assert res["status"] == "error" and res["error"].startswith("mcp_failed")


def test_describe_lists_what_the_server_offers(tmp_path):
    import asyncio
    ch, _ = channel(tmp_path)
    tools = {t["name"]: t for t in asyncio.run(ch.describe())}
    assert set(tools) == {"create_post", "publish_post", "delete_post"} and tools["create_post"]["required"] == ["blogId", "title", "content"]


def test_settings_are_parsed_and_bad_values_stop_startup_with_a_clear_message():
    s = Settings(mcp_command="npx", mcp_args='["-y", "mcp-blogger@1.0.0"]', mcp_env_keys="BLOGGER_API_KEY, BLOGGER_BLOG_ID",
                 mcp_publish_tool="publish_post", blogger_blog_id="42",
                 mcp_create_args='{"blogId": "{blog_id}", "title": "{title}", "content": "{body_html}", "isDraft": false}')
    cfg = mcp_config(s)
    assert cfg.args == ["-y", "mcp-blogger@1.0.0"] and cfg.env_keys == ["BLOGGER_API_KEY", "BLOGGER_BLOG_ID"]
    assert cfg.create_args["isDraft"] is False and cfg.publish_args == {"blogId": "{blog_id}", "postId": "{post_id}"}
    assert render({"a": ["{title}", 3], "b": {"c": "x {blog_id}"}}, {"title": "T", "blog_id": "9"}) == {"a": ["T", 3], "b": {"c": "x 9"}}
    with pytest.raises(ValueError, match="MCP_ARGS must be valid JSON"):
        mcp_config(Settings(mcp_command="npx", mcp_args="[oops"))
    with pytest.raises(ValueError, match="unknown placeholders: post_id"):
        mcp_config(Settings(mcp_command="npx", mcp_create_args='{"id": "{post_id}"}'))
    assert not mcp_config(Settings()).configured
