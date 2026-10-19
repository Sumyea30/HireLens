"""Posting channel that publishes through an MCP server (for example a Blogger MCP server).

Why this is safe:
- It is plain code called by `publish_post` AFTER a human approval. No LLM ever sees or calls these tools.
- Only the tools named in the configuration (create, optional publish) can be called. Anything else is refused.
- Before calling, it checks that the tools exist on the server and that all required arguments are filled.
- Post text is HTML-escaped. Secrets are forwarded to the server only if listed in MCP_ENV_KEYS.
- Errors are short codes. Tool output, prompts and secrets never appear in errors or logs.

Different Blogger MCP servers use different tool names and arguments. Run
    python -m app.services.channels.mcp_channel tools
to list what YOUR server offers, then set MCP_CREATE_TOOL / MCP_CREATE_ARGS (and MCP_PUBLISH_TOOL if the
server creates drafts) to match.
"""
import asyncio
import html
import json
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass, field

from mcp import Client, StdioServerParameters

log = logging.getLogger("hirelens.mcp")
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
URL = re.compile(r"https?://[^\s\"'<>]+")
TEXT_ID = re.compile(r"\b(?:post\s*)?id\b\W{0,4}(\d{6,})", re.IGNORECASE)
TEXT_STATUS = re.compile(r"\bstatus\b\W{0,4}(DRAFT|LIVE|SCHEDULED)\b", re.IGNORECASE)
CREATE_VALUES = {"title", "body_html", "body_text", "blog_id"}
PUBLISH_VALUES = CREATE_VALUES | {"post_id"}


class ChannelError(Exception):
    """A short, safe error code."""


@dataclass
class McpConfig:
    command: str = ""                       # stdio: the program that starts the server, e.g. "npx"
    args: list[str] = field(default_factory=list)
    url: str = ""                           # or: a Streamable HTTP endpoint
    env_keys: list[str] = field(default_factory=list)    # only these variables are passed to the server
    create_tool: str = "create_post"
    create_args: dict = field(default_factory=lambda: {"blogId": "{blog_id}", "title": "{title}", "content": "{body_html}"})
    publish_tool: str = ""                  # set when the server creates DRAFTS and a second call publishes
    publish_args: dict = field(default_factory=lambda: {"blogId": "{blog_id}", "postId": "{post_id}"})
    blog_id: str = ""
    timeout_s: float = 60.0

    @property
    def configured(self) -> bool:
        return bool(self.command or self.url)


def mcp_config(s) -> McpConfig:
    """Build the configuration from Settings. A malformed value stops startup with a clear message."""
    def js(name: str, raw: str, default, kind):
        if not raw.strip():
            return default
        try:
            value = json.loads(raw)
        except ValueError:
            raise ValueError(f"{name} must be valid JSON") from None
        if not isinstance(value, kind):
            raise ValueError(f"{name} has the wrong JSON type")
        return value

    base = McpConfig()
    cfg = McpConfig(
        command=s.mcp_command.strip(), url=s.mcp_url.strip(),
        args=js("MCP_ARGS", s.mcp_args, [], list),
        env_keys=[k.strip() for k in s.mcp_env_keys.split(",") if k.strip()],
        create_tool=s.mcp_create_tool.strip() or base.create_tool,
        create_args=js("MCP_CREATE_ARGS", s.mcp_create_args, base.create_args, dict),
        publish_tool=s.mcp_publish_tool.strip(),
        publish_args=js("MCP_PUBLISH_ARGS", s.mcp_publish_args, base.publish_args, dict),
        blog_id=s.blogger_blog_id.strip(), timeout_s=float(s.mcp_timeout_seconds),
    )
    for name, template, allowed in (("MCP_CREATE_ARGS", cfg.create_args, CREATE_VALUES), ("MCP_PUBLISH_ARGS", cfg.publish_args, PUBLISH_VALUES)):
        unknown = _placeholders(template) - allowed
        if unknown:
            raise ValueError(f"{name} uses unknown placeholders: {', '.join(sorted(unknown))}")
    return cfg


def _placeholders(template) -> set[str]:
    if isinstance(template, str):
        return set(PLACEHOLDER.findall(template))
    if isinstance(template, dict):
        return set().union(*[_placeholders(v) for v in template.values()]) if template else set()
    if isinstance(template, list):
        return set().union(*[_placeholders(v) for v in template]) if template else set()
    return set()


def render(template, values: dict[str, str]):
    if isinstance(template, str):
        for k, v in values.items():
            template = template.replace("{" + k + "}", v)
        return template
    if isinstance(template, dict):
        return {k: render(v, values) for k, v in template.items()}
    if isinstance(template, list):
        return [render(v, values) for v in template]
    return template


def to_html(body: str) -> str:
    """Plain text to safe HTML: everything is escaped, blank lines make paragraphs."""
    paras = [p for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]
    return "".join("<p>" + html.escape(p).replace("\n", "<br>") + "</p>" for p in paras)


def parse_result(res) -> dict:
    """Servers answer in different ways: structured data, JSON text, or a sentence. Accept all three."""
    structured = getattr(res, "structured_content", None)
    if isinstance(structured, dict) and structured:
        inner = structured.get("result")
        if isinstance(inner, dict):
            return inner
        if "result" not in structured:           # {"result": "text"} is just the wrapped text: parse it below
            return structured
    text = "\n".join(getattr(b, "text", "") or "" for b in res.content)[:20000]
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    out: dict = {}
    for key, pattern in (("url", URL), ("id", TEXT_ID), ("status", TEXT_STATUS)):
        m = pattern.search(text)
        if m:
            out[key] = m.group(1) if key != "url" else m.group(0)
    return out


def _safe_url(data: dict) -> str | None:
    u = data.get("url")
    return u if isinstance(u, str) and u.startswith(("http://", "https://")) and len(u) <= 500 else None


def _post_id(data: dict) -> str | None:
    for key in ("id", "postId", "post_id"):
        if isinstance(data.get(key), (str, int)):
            return str(data[key])
    return None


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, ChannelError):
        return str(exc)
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            code = _error_code(inner)
            if code.startswith(("tool_", "missing_", "no_", "created_")):
                return code
        return _error_code(exc.exceptions[0])
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    return f"mcp_failed:{type(exc).__name__}"


class McpChannel:
    name = "blogger"

    def __init__(self, cfg: McpConfig, env: dict[str, str] | None = None):
        self.cfg = cfg
        self.env = env if env is not None else dict(os.environ)

    @property
    def configured(self) -> bool:
        return self.cfg.configured

    def _target(self):
        if self.cfg.url:
            return self.cfg.url
        command = shutil.which(self.cfg.command) or self.cfg.command      # finds npx.cmd on Windows
        return StdioServerParameters(command=command, args=self.cfg.args,
                                     env={k: self.env[k] for k in self.cfg.env_keys if k in self.env})

    async def _tools(self, client) -> dict:
        return {t.name: t for t in (await client.list_tools()).tools}

    async def _call(self, client, tools: dict, name: str, template: dict, values: dict) -> dict:
        if name not in tools:
            raise ChannelError(f"tool_not_found:{name}")
        args = render(template, values)
        required = (tools[name].input_schema or {}).get("required", [])
        missing = [k for k in required if k not in args]
        if missing:
            raise ChannelError("missing_required_args:" + ",".join(missing))
        res = await client.call_tool(name, args)
        if res.is_error:
            raise ChannelError("tool_error")
        return parse_result(res)

    async def _publish(self, title: str, body: str) -> dict:
        values = {"title": title, "body_html": to_html(body), "body_text": body, "blog_id": self.cfg.blog_id}
        async with Client(self._target(), read_timeout_seconds=self.cfg.timeout_s) as client:
            tools = await self._tools(client)
            created = await self._call(client, tools, self.cfg.create_tool, self.cfg.create_args, values)
            url = _safe_url(created)
            if self.cfg.publish_tool:
                post_id = _post_id(created)
                if not post_id:
                    raise ChannelError("no_post_id_in_result")
                published = await self._call(client, tools, self.cfg.publish_tool, self.cfg.publish_args, {**values, "post_id": post_id})
                url = _safe_url(published) or url
            elif str(created.get("status", "")).upper() == "DRAFT":
                raise ChannelError("created_as_draft")          # a hidden draft is not a published job post
        return {"status": "ok", "url": url, "error": None}

    def publish(self, *, title: str, body: str) -> dict:
        if not self.configured:
            return {"status": "error", "url": None, "error": "mcp_not_configured"}
        try:
            return asyncio.run(asyncio.wait_for(self._publish(title, body), self.cfg.timeout_s))
        except BaseException as exc:                        # includes exception groups raised by the transport
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            code = _error_code(exc)
            log.warning("mcp_publish_failed: %s", code)
            return {"status": "error", "url": None, "error": code}

    async def call_tool(self, name: str, args: dict, timeout_s: float = 300.0) -> str:
        """Manual use only (the 'call' command below): run one tool and return its text. Not reachable from the API."""
        async with Client(self._target(), read_timeout_seconds=timeout_s) as client:
            res = await asyncio.wait_for(client.call_tool(name, args), timeout_s)
            return "\n".join(getattr(b, "text", "") or "" for b in res.content)[:4000]

    async def describe(self) -> list[dict]:
        async with Client(self._target(), read_timeout_seconds=self.cfg.timeout_s) as client:
            return [{"name": t.name, "description": (t.description or "")[:160],
                     "required": (t.input_schema or {}).get("required", []),
                     "properties": sorted((t.input_schema or {}).get("properties", {}))}
                    for t in (await client.list_tools()).tools]


if __name__ == "__main__":
    usage = "usage: python -m app.services.channels.mcp_channel tools\n       python -m app.services.channels.mcp_channel call <tool> [JSON arguments]"
    argv = sys.argv[1:]
    if not argv or argv[0] not in ("tools", "call") or (argv[0] == "call" and len(argv) not in (2, 3)):
        sys.exit(usage)
    from app.core.config import load_settings
    channel = McpChannel(mcp_config(load_settings()))
    if not channel.configured:
        sys.exit("Set MCP_COMMAND (and MCP_ARGS) or MCP_URL in .env first.")
    if argv[0] == "tools":
        for tool in asyncio.run(channel.describe()):
            print(f"- {tool['name']}: required {tool['required'] or 'none'}; arguments {tool['properties']}\n    {tool['description']}")
    else:
        print("Waiting up to 5 minutes (a browser window may open for Google sign-in)...")
        print(asyncio.run(channel.call_tool(argv[1], json.loads(argv[2]) if len(argv) == 3 else {})))