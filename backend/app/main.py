"""HireLens API. Run: uvicorn app.main:app --host 0.0.0.0 --port 8000"""
import logging
import uuid
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import assistant, candidates, clientlog, funnel, interviews, misc, room
from app.core.config import Settings, load_settings
from app.graphs import CachedLLM, Deps, build_cv_graph, build_job_post_graph, memory_checkpointer
from app.graphs.common import LLMError
from app.services.blogger import BloggerChannel
from app.services.channels.mcp_channel import McpChannel, mcp_config
from app.services.gemini import GeminiClient, GeminiEmbedder
from app.services.cvtext import extract_cv_text, ocr_cv_text
from app.services.face import FaceService, build_face_engine
from app.services.intake import CVStorage, IntakeService
from app.services.photos import PhotoStorage
from app.services.screening import make_on_queued
from app.services.memory_store import MemoryStore
from app.services.policy import PolicyService

logging.basicConfig(level=logging.INFO, format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}')
log = logging.getLogger("hirelens.app")


@dataclass
class Services:
    settings: Settings
    store: Any
    deps: Deps
    job_graph: Any
    policy: PolicyService
    intake: Any = None
    stack: ExitStack = field(default_factory=ExitStack)
    cv_graph: Any = None
    photos: Any = None
    face: Any = None


class _Unavailable:
    """Stands in when GEMINI_API_KEY is missing: calls fail cleanly instead of crashing startup."""
    def structured(self, **_): raise LLMError("GEMINI_API_KEY is not set")
    def embed(self, *_a, **_k): raise LLMError("GEMINI_API_KEY is not set")


def build_channels(s: Settings) -> dict:
    """Publish approved posts to Blogger. The direct API is used when OAuth is set; otherwise the MCP server."""
    direct = BloggerChannel(s.blogger_blog_id, s.blogger_client_id, s.blogger_client_secret, s.blogger_refresh_token)
    if direct.configured:
        return {"blogger": direct}
    blogger = McpChannel(mcp_config(s))        # raises a clear error if an MCP_* value is malformed
    if not blogger.configured:
        log.warning("No posting channel is configured (Blogger OAuth, MCP_COMMAND, or MCP_URL). Job posts cannot be published.")
        return {}
    return {"blogger": blogger}


def build_services(s: Settings) -> Services:
    stack = ExitStack()
    clientlog.setup_file(s.log_dir)
    if s.gemini_api_key:
        llm = CachedLLM(GeminiClient(s.gemini_api_key, s.gemini_model))
        embedder = GeminiEmbedder(s.gemini_api_key, s.gemini_embedding_model, s.embedding_dims)
    else:
        log.warning("GEMINI_API_KEY is not set: AI features will return errors")
        llm = embedder = _Unavailable()
    if s.database_url:
        from langgraph.checkpoint.postgres import PostgresSaver
        from app.services.pgstore import PgStore
        store = PgStore(s.database_url, embedder, s.embedding_dims, s.apply_email)
        store.init_schema()
        saver = stack.enter_context(PostgresSaver.from_conn_string(s.database_url))
        saver.setup()
    else:
        log.warning("DATABASE_URL is not set: using in-memory storage (data is lost on restart)")
        store, saver = MemoryStore(s.apply_email, None if isinstance(embedder, _Unavailable) else embedder), memory_checkpointer()
    deps = Deps(llm=llm, store=store, repo=store, channels=build_channels(s), apply_email=s.apply_email,
                extract_text=extract_cv_text, ocr_text=ocr_cv_text)
    photos = PhotoStorage(s.photo_dir)
    face = FaceService(build_face_engine(s.face_provider), s.face_match_threshold)
    intake = IntakeService(
        store, CVStorage(s.cv_dir), s.max_upload_mb, on_queued=make_on_queued(s),
        photos=photos, face=face, settings=s,
    )
    policy = PolicyService(store)
    policy.ensure_default_template()
    return Services(s, store, deps, build_job_post_graph(deps, saver), policy, intake, stack,
                    build_cv_graph(deps), photos, face)


def create_app(services: Services | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if services is None:
            settings = load_settings()
            if not settings.auth_secret:
                raise RuntimeError("AUTH_SECRET must be set")
            app.state.services = build_services(settings)
        else:
            app.state.services = services
        yield
        app.state.services.stack.close()

    app = FastAPI(title="HireLens API", version="1.0.0", lifespan=lifespan)
    if services is not None:
        app.state.services = services

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        request.state.cid = uuid.uuid4().hex[:12]
        response = await call_next(request)
        response.headers["x-correlation-id"] = request.state.cid
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        # No stack trace, path, config or prompt text reaches the client (N12).
        logging.getLogger("hirelens.app").error("unhandled", extra={"error_type": type(exc).__name__})
        cid = getattr(request.state, "cid", uuid.uuid4().hex[:12])
        return JSONResponse({"detail": "Internal error"}, status_code=500, headers={"x-correlation-id": cid})

    app.add_middleware(CORSMiddleware, allow_origins=list(load_settings().cors_origins), allow_methods=["*"], allow_headers=["*"])
    app.include_router(misc.router)
    app.include_router(assistant.router)
    app.include_router(funnel.router)
    app.include_router(candidates.router)
    app.include_router(interviews.router)
    app.include_router(room.router)
    return app


app = create_app()
