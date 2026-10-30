"""All configuration comes from environment variables (N7). Nothing is hard-coded."""
import json
import logging
import os
import pathlib
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    database_url: str = ""
    redis_url: str = ""              # Celery broker for CV screening; empty keeps candidates queued
    # LLM and embeddings (Gemini free tier by default; check current model names and limits)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    gemini_embedding_model: str = "gemini-embedding-001"
    embedding_dims: int = 768
    apply_email: str = "applications@example.com"
    public_base_url: str = "http://localhost:5173"
    # Auth
    auth_secret: str = ""
    token_ttl_minutes: int = 480
    users_json: str = "{}"               # {"name": {"hash": "...", "role": "interviewer"}}
    cors_origins: tuple[str, ...] = field(default_factory=tuple)
    # CV collection
    cv_dir: str = "stored_cvs"
    photo_dir: str = "stored_photos"
    max_upload_mb: int = 10
    face_match_threshold: float = 0.35
    face_provider: str = "auto"  # auto | insightface | hash (tests / no model)
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    mail_poll_seconds: int = 60
    # MCP posting channel (for example a Blogger MCP server)
    mcp_command: str = ""
    mcp_args: str = ""                  # JSON list, e.g. ["-y", "mcp-blogger@1.2.3"]
    mcp_url: str = ""
    mcp_env_keys: str = ""              # comma-separated variable names to pass to the server
    mcp_create_tool: str = "create_post"
    mcp_create_args: str = ""           # JSON, placeholders: {title} {body_html} {body_text} {blog_id}
    mcp_publish_tool: str = ""
    mcp_publish_args: str = ""          # JSON, placeholders as above plus {post_id}
    blogger_blog_id: str = ""
    blogger_client_id: str = ""
    blogger_client_secret: str = ""
    blogger_refresh_token: str = ""
    mcp_timeout_seconds: int = 60
    log_dir: str = ""                    # folder for the browser log file; empty means no file
    # Outbound interview mail. Empty SMTP_HOST falls back to the application mailbox.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    transcript_dir: str = ""             # optional git-ignored folder for a copy of the transcript JSON
    stt_provider: str = "faster_whisper"  # assemblyai | faster_whisper | fake
    stt_url: str = "http://127.0.0.1:8090"
    stt_timeout_seconds: int = 90
    assemblyai_api_key: str = ""
    assemblyai_base_url: str = "https://api.assemblyai.com"
    stt_language: str = "en"
    copilot_max_calls: int = 15
    copilot_input_tokens: int = 3000
    copilot_output_tokens: int = 600
    copilot_per_minute: int = 5
    copilot_daily_tokens: int = 100_000
    eval_max_runs: int = 2
    eval_input_tokens: int = 12_000
    eval_output_tokens: int = 2_000
    # Funnel Doctor (F11) — estimate-only analytics; does not change official scores
    funnel_bottleneck_max: float = 0.2
    funnel_flat_min: float = 0.9
    funnel_shortlist_n: int = 5
    funnel_suggest_max_per_day: int = 3

    @property
    def imap_configured(self) -> bool:
        return bool(self.imap_host and self.imap_user and self.imap_password)

    @property
    def users(self) -> dict:
        try:
            return json.loads(self.users_json)
        except ValueError:
            return {}


def load_env() -> None:
    """Load .env from the current folder or its parent (the repo root). Real environment variables win."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        logging.getLogger("hirelens.config").warning("python-dotenv is not installed, so .env was NOT read. Run: pip install -r requirements.txt")
        return
    here = pathlib.Path.cwd()
    for folder in (here, here.parent):
        if (folder / ".env").is_file():
            load_dotenv(folder / ".env", override=False)
            return


def load_settings() -> Settings:
    load_env()
    return Settings(
        database_url=os.environ.get("DATABASE_URL", ""),
        redis_url=os.environ.get("REDIS_URL", ""),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-3.8-flash"),
        gemini_embedding_model=os.environ.get("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001"),
        embedding_dims=_int("EMBEDDING_DIMS", 768),
        apply_email=os.environ.get("APPLY_EMAIL", "applications@example.com"),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", "http://localhost:5173").rstrip("/"),
        auth_secret=os.environ.get("AUTH_SECRET", ""),
        token_ttl_minutes=_int("TOKEN_TTL_MINUTES", 480),
        users_json=os.environ.get("USERS_JSON", "{}"),
        cors_origins=tuple(o for o in os.environ.get("CORS_ORIGINS", "").split(",") if o),
        cv_dir=os.environ.get("CV_DIR", "stored_cvs"),
        photo_dir=os.environ.get("PHOTO_DIR", "stored_photos"),
        max_upload_mb=_int("MAX_UPLOAD_MB", 10),
        face_match_threshold=_float("FACE_MATCH_THRESHOLD", 0.35),
        face_provider=os.environ.get("FACE_PROVIDER", "auto"),
        imap_host=os.environ.get("IMAP_HOST", ""),
        imap_port=_int("IMAP_PORT", 993),
        imap_user=os.environ.get("IMAP_USER", ""),
        imap_password=os.environ.get("IMAP_PASSWORD", ""),
        imap_folder=os.environ.get("IMAP_FOLDER", "INBOX"),
        mail_poll_seconds=_int("MAIL_POLL_INTERVAL_SECONDS", 60),
        mcp_command=os.environ.get("MCP_COMMAND", ""),
        mcp_args=os.environ.get("MCP_ARGS", ""),
        mcp_url=os.environ.get("MCP_URL", ""),
        mcp_env_keys=os.environ.get("MCP_ENV_KEYS", ""),
        mcp_create_tool=os.environ.get("MCP_CREATE_TOOL", "create_post"),
        mcp_create_args=os.environ.get("MCP_CREATE_ARGS", ""),
        mcp_publish_tool=os.environ.get("MCP_PUBLISH_TOOL", ""),
        mcp_publish_args=os.environ.get("MCP_PUBLISH_ARGS", ""),
        blogger_blog_id=os.environ.get("BLOGGER_BLOG_ID", ""),
        blogger_client_id=os.environ.get("BLOGGER_CLIENT_ID", ""),
        blogger_client_secret=os.environ.get("BLOGGER_CLIENT_SECRET", ""),
        blogger_refresh_token=os.environ.get("BLOGGER_REFRESH_TOKEN", ""),
        mcp_timeout_seconds=_int("MCP_TIMEOUT_SECONDS", 60),
        log_dir=os.environ.get("LOG_DIR", "logs"),
        smtp_host=os.environ.get("SMTP_HOST", ""),
        smtp_port=_int("SMTP_PORT", 587),
        smtp_user=os.environ.get("SMTP_USER", ""),
        smtp_password=os.environ.get("SMTP_PASSWORD", ""),
        transcript_dir=os.environ.get("TRANSCRIPT_DIR", ""),
        stt_provider=os.environ.get("STT_PROVIDER", "faster_whisper"),
        stt_url=os.environ.get("STT_URL", "http://127.0.0.1:8090"),
        stt_timeout_seconds=_int("STT_TIMEOUT_SECONDS", 90),
        assemblyai_api_key=os.environ.get("ASSEMBLYAI_API_KEY", ""),
        assemblyai_base_url=os.environ.get("ASSEMBLYAI_BASE_URL", "https://api.assemblyai.com"),
        stt_language=os.environ.get("STT_LANGUAGE", "en"),
        copilot_max_calls=_int("COPILOT_MAX_CALLS", 15),
        copilot_input_tokens=_int("COPILOT_INPUT_TOKENS", 3000),
        copilot_output_tokens=_int("COPILOT_OUTPUT_TOKENS", 600),
        copilot_per_minute=_int("COPILOT_PER_MINUTE", 5),
        copilot_daily_tokens=_int("COPILOT_DAILY_TOKENS", 100_000),
        eval_max_runs=_int("EVAL_MAX_RUNS", 2),
        eval_input_tokens=_int("EVAL_INPUT_TOKENS", 12_000),
        eval_output_tokens=_int("EVAL_OUTPUT_TOKENS", 2000),
        funnel_bottleneck_max=_float("FUNNEL_BOTTLENECK_MAX", 0.2),
        funnel_flat_min=_float("FUNNEL_FLAT_MIN", 0.9),
        funnel_shortlist_n=_int("FUNNEL_SHORTLIST_N", 5),
        funnel_suggest_max_per_day=_int("FUNNEL_SUGGEST_MAX_PER_DAY", 3),
    )
