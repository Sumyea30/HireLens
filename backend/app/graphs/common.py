"""Shared types, dependency interfaces and helpers for all HireLens graphs.

Conventions (see docs/langgraph-agent-design.md):
- Every LLM output is a Pydantic model. Judgments carry a justification and
  evidence_refs.
- Evidence refs: cv:<candidate_id>:<chunk_id>, tr:<session_id>:<turn_id>,
  pol:<doc_id>:<chunk_id>, req:<requirement_id>
- Nodes return partial state updates. Failures are recorded in `errors` and
  routed to a failure node. They are never raised to the worker.
- Logs contain IDs and outcomes only, never CV, transcript or policy text.
"""
import functools
import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol, TypedDict, TypeVar

from langgraph.errors import GraphInterrupt
from pydantic import BaseModel

log = logging.getLogger("hirelens.graphs")

MAX_RETRIES = 2

# TODO: placeholder defaults. Weights must be configurable per job profile (F2).
DEFAULT_WEIGHTS = {"technical_skills": 0.5, "experience": 0.3, "qualifications": 0.2}

T = TypeVar("T", bound=BaseModel)


# --------------------------------------------------------------------------
# Types and dependency interfaces
# --------------------------------------------------------------------------
class ErrorRecord(TypedDict):
    node: str
    code: str
    message: str  # always generic (N12)


class LLMError(Exception):
    """Raised by LLM clients on provider failure, timeout or invalid output.
    Messages must never contain prompt text, response text or keys."""


class LLMClient(Protocol):
    def structured(
        self, *, schema: type[T], system: str, user: str, prompt_version: str, context: str = ""
    ) -> T:
        """Return a validated instance of `schema`. Raise on provider failure."""
        ...


class VectorStore(Protocol):
    def index(self, collection: str, items: list[dict]) -> None:
        """items: [{"id": str, "text": str}]. Collections: cv:<candidate_id>,
        req:<job_id>, policy. Keep policy chunks separate from CV chunks."""
        ...

    def search(self, collection: str, query: str, k: int) -> list[dict]:
        """Return [{"id": str, "text": str, "score": float}], best first."""
        ...


class Repository(Protocol):
    def save_cv_result(self, candidate_id: str, job_id: str, result: dict) -> None: ...
    def load_cv_result(self, candidate_id: str, job_id: str) -> dict | None: ...
    def load_transcript(self, session_id: str) -> list[dict]: ...
    def save_qa_result(self, session_id: str, result: dict) -> None: ...
    def load_qa_result(self, session_id: str) -> dict | None: ...
    def save_assessment(self, candidate_id: str, session_id: str, report: dict) -> None: ...
    def save_job_post(self, job_id: str, record: dict) -> None: ...
    def save_posting_decision(self, job_id: str, decision: dict) -> None: ...
    def mark_job_open(self, job_id: str) -> None: ...


class PostingChannel(Protocol):
    name: str

    def publish(self, *, title: str, body: str) -> dict:
        """Return {"status": "ok" | "error", "url": str | None, "error": str | None}."""
        ...


@dataclass
class Deps:
    llm: LLMClient
    store: VectorStore
    repo: Repository
    extract_text: Callable[[str], str] | None = None   # file path -> text
    ocr_text: Callable[[str], str] | None = None       # file path -> text
    render_report: Callable[[dict], str] | None = None  # report -> HTML
    channels: dict[str, PostingChannel] = field(default_factory=dict)
    apply_email: str = "applications@example.com"


# --------------------------------------------------------------------------
# Evidence references
# --------------------------------------------------------------------------
def cv_ref(candidate_id: str, chunk_id: str) -> str:
    return f"cv:{candidate_id}:{chunk_id}"


def tr_ref(session_id: str, turn_id: str) -> str:
    return f"tr:{session_id}:{turn_id}"


def pol_ref(item_id: str) -> str:
    return f"pol:{item_id}"


def req_ref(requirement_id: str) -> str:
    return f"req:{requirement_id}"


# --------------------------------------------------------------------------
# Text helpers (deterministic)
# --------------------------------------------------------------------------
_PROTECTED_LINE = re.compile(
    r"^\s*(date of birth|dob|birth\s*date|age|gender|sex|nationality|religion|"
    r"marital status|ethnicity|race|photo|passport)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_CONTACT_LINE = re.compile(
    r"^\s*(e-?mail|phone|mobile|tel|telephone|address|linkedin|contact)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)


def email_from_text(text: str) -> str | None:
    """The first real email address in CV text. Always used as the candidate's contact address."""
    from app.core.auth import valid_email
    for match in _EMAIL.finditer(text or ""):
        address = match.group(0).strip(".,;:").lower()
        if valid_email(address):
            return address
    return None


_NAME_LABEL = re.compile(r"^\s*(?:full\s*)?name\s*[:\-]\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_NAME_SKIP = re.compile(
    r"^(curriculum|resume|cv|objective|summary|profile|experience|education|skills|"
    r"projects|certifications|employment|work\s+history|contact|email|phone|address)\b",
    re.IGNORECASE,
)


def name_from_text(text: str) -> str | None:
    """Best-effort display name from CV text. Never sent to scoring prompts or the vector store."""
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    labeled = _NAME_LABEL.search(raw)
    if labeled:
        candidate = " ".join(labeled.group(1).split()).strip(" ,;|")
        if 1 < len(candidate) <= 80 and not _EMAIL.search(candidate):
            return candidate
    for line in raw.split("\n")[:12]:
        line = " ".join(line.split()).strip(" ,;|")
        if not line or len(line) > 80 or _EMAIL.search(line) or _PHONE.search(line):
            continue
        if _NAME_SKIP.match(line) or _CONTACT_LINE.match(line) or _PROTECTED_LINE.match(line):
            continue
        words = line.split()
        if 1 <= len(words) <= 5 and all(re.fullmatch(r"[A-Za-z][A-Za-z'.\-]*", w) for w in words):
            return line
    return None


def redact_protected(text: str) -> str:
    """Remove protected attributes and contact details before anything is stored or scored.

    What remains is the job-relevant text: skills, experience, qualifications, and projects.
    """
    cleaned = _PROTECTED_LINE.sub("", text)
    cleaned = _CONTACT_LINE.sub("", cleaned)
    cleaned = _EMAIL.sub("", cleaned)
    cleaned = _PHONE.sub("", cleaned)
    return cleaned


def scrub_text(value: str) -> str:
    """Drop contact details from a stored sentence. Skills and reasons stay."""
    return " ".join(redact_protected(value or "").split())


def scrub_parsed(parsed: dict | None) -> dict:
    parsed = parsed or {}
    out = {"total_experience_years": parsed.get("total_experience_years")}
    for key in ("skills", "education", "work_history", "certifications", "projects"):
        out[key] = [item for item in (scrub_text(str(x)) for x in parsed.get(key) or []) if item]
    return out


def chunk_text(text: str, max_chars: int = 600) -> list[dict]:
    """Group paragraphs into chunks. TODO: split oversized paragraphs and
    chunk by CV section for better evidence spans."""
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for part in parts:
        if buf and len(buf) + len(part) + 2 > max_chars:
            chunks.append(buf)
            buf = part
        else:
            buf = f"{buf}\n\n{part}" if buf else part
    if buf:
        chunks.append(buf)
    return [{"id": f"c{i:03d}", "text": c} for i, c in enumerate(chunks, start=1)]


def weighted_average(scores: list[dict], weights: dict[str, float]) -> float:
    """Aggregation is plain code, never done by the LLM (reproducibility)."""
    total = sum(weights.get(s["dimension"], 0.0) for s in scores)
    if total <= 0:
        return 0.0
    value = sum(s["score"] * weights.get(s["dimension"], 0.0) for s in scores) / total
    return round(value, 2)


def profile_to_requirements(profile: dict) -> list[dict]:
    """Turn an approved job profile into the requirement list used by Graph A/B/C."""
    reqs: list[dict] = []

    def add(text: str, kind: str, category: str) -> None:
        reqs.append({"id": f"R{len(reqs) + 1}", "text": text, "kind": kind, "category": category})

    for skill in profile.get("required_skills", []):
        add(skill, "required", "skill")
    for skill in profile.get("preferred_skills", []):
        add(skill, "preferred", "skill")
    for qual in profile.get("qualifications", []):
        add(qual, "required", "qualification")
    years = profile.get("min_experience_years")
    if years is not None:
        add(f"At least {years} years of relevant experience", "required", "experience")
    for other in profile.get("other_criteria", []):
        add(other, "required", "other")
    return reqs


# --------------------------------------------------------------------------
# LLM helpers
# --------------------------------------------------------------------------
def error_record(node: str, exc: Exception) -> ErrorRecord:
    # Only the exception type is kept: no message text, which could hold content.
    return {"node": node, "code": type(exc).__name__, "message": "processing step failed"}


def safe_structured(
    llm: LLMClient,
    node: str,
    schema: type[T],
    *,
    system: str,
    user: str,
    prompt_version: str,
    context: str = "",
) -> tuple[T | None, list[ErrorRecord]]:
    """Call the LLM. Return (output, []) or (None, [error]). Never raises.

    context is reference text shared by many calls (a policy, a template). It stays
    out of user so a context cache can keep it instead of sending it every time.
    """
    try:
        out = llm.structured(
            schema=schema, system=system, user=user, prompt_version=prompt_version, context=context,
        )
        return out, []
    except Exception as exc:  # provider error, timeout, invalid JSON, schema failure
        return None, [error_record(node, exc)]


class CachedLLM:
    """Cache wrapper for reproducible scores and lower cost (R3, R4).

    Key = hash(schema + prompt_version + system + user). The default cache is an
    in-memory dict. TODO: back it with Redis.
    """

    def __init__(self, inner: LLMClient, cache: dict[str, str] | None = None):
        self.inner = inner
        self.cache = cache if cache is not None else {}
        self.last_usage = {"input": 0, "output": 0}
        self._lock = threading.Lock()

    def structured(self, *, schema: type[T], system: str, user: str, prompt_version: str, context: str = "") -> T:
        raw = f"{schema.__name__}|{prompt_version}|{system}|{context}|{user}"
        key = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        with self._lock:
            cached = self.cache.get(key)
        if cached is not None:
            self.last_usage = {"input": 0, "output": 0}
            return schema.model_validate_json(cached)
        out = self.inner.structured(
            schema=schema, system=system, user=user, prompt_version=prompt_version, context=context,
        )
        usage = getattr(self.inner, "last_usage", None) or {"input": 0, "output": 0}
        self.last_usage = {"input": int(usage.get("input") or 0), "output": int(usage.get("output") or 0)}
        with self._lock:
            self.cache[key] = out.model_dump_json()
        return out


# --------------------------------------------------------------------------
# Graph helpers
# --------------------------------------------------------------------------
def traced(graph: str, node: str):
    """Structured log line per node: graph, node, entity ID, duration, outcome."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(state):
            entity = state.get("candidate_id") or state.get("session_id") or state.get("job_id")
            started = time.perf_counter()
            try:
                update = fn(state)
            except GraphInterrupt:
                raise
            except Exception as exc:
                log.error(
                    "node_crashed",
                    extra={"graph": graph, "node": node, "entity_id": entity,
                           "error_type": type(exc).__name__},
                )
                raise
            log.info(
                "node_done",
                extra={
                    "graph": graph,
                    "node": node,
                    "entity_id": entity,
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                    "outcome": "error" if update.get("errors") else "ok",
                },
            )
            return update

        return wrapper

    return decorator


def route_after_validate(state) -> str:
    """Return "ok", the retry target node name, or "failed".

    The validate node sets `validation_errors`, `retry_target` and increments
    `retry_count`. Routers cannot update state, so counting happens in the node.
    """
    if not state.get("validation_errors"):
        return "ok"
    if state.get("retry_count", 0) <= MAX_RETRIES:
        return state["retry_target"]
    return "failed"
