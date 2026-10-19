"""Gemini adapters: structured LLM output and embeddings, over plain REST (httpx).

Design choices:
- The response is constrained with responseJsonSchema. The schema is not copied into
  the prompt, so the model cannot spend output tokens on markdown or commentary.
- maxOutputTokens caps every reply. A job post may be long; scores and labels stay short.
- A repeated reference (the same policy text and template) is stored with the Context
  Cache API once it is large enough for Gemini to accept. Later calls send only the
  new question. Smaller prompts keep the reference first so the prefix stays identical.
- The system instruction is sent unchanged and first. Variable text stays after it.
- Temperature 0 for reproducible results. A 429 pauses for a few seconds, then the call is tried again. Server errors are retried too.
- Errors never include prompt text, response text or the API key (N12, N22).
- Free-tier data may be used by Google to improve its products: use synthetic data only.
"""
import hashlib
import threading
import time
from contextvars import ContextVar
from typing import Callable, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.graphs.common import LLMError

T = TypeVar("T", bound=BaseModel)
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 5
EMBED_BATCH_SIZE = 5
EMBED_BATCH_PAUSE_S = 12.0
# Gemini 3.8 Flash will not store a context cache below this many tokens.
MIN_CACHE_TOKENS = 4096
CACHE_TTL_S = 3600
# A full job post needs room. Judgments (scores, labels, matches) do not.
_LONG_OUTPUT = {"DraftOut"}
DEFAULT_MAX_OUTPUT_TOKENS = 2048
LONG_MAX_OUTPUT_TOKENS = 4096
# Set for one evaluation so its replies stay inside the configured output cap.
output_limit: ContextVar[int | None] = ContextVar("hirelens_output_limit", default=None)
# Flash-Lite defaults to this level. It keeps classification and extraction fast.
# Gemini 3.8 Flash rejects MINIMAL, so it is sent only for Flash-Lite models.
THINKING_LEVEL_MINIMAL = "MINIMAL"
# Keywords Gemini's responseJsonSchema accepts. Others (default, $schema) are dropped
# so the request is not rejected and the same model always sends the same schema bytes.
_SCHEMA_KEYS = {
    "$id", "$defs", "$ref", "$anchor", "type", "format", "title", "description",
    "enum", "items", "prefixItems", "minItems", "maxItems", "minimum", "maximum",
    "anyOf", "oneOf", "properties", "additionalProperties", "required", "propertyOrdering",
}


def _client(timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout)


def response_schema(model: type[BaseModel]) -> dict:
    """JSON Schema for Gemini structured output, reduced to the keywords it accepts."""
    return _trim_schema(model.model_json_schema())


# These hold named schemas. Their keys are field names, not JSON Schema keywords.
_SCHEMA_MAPS = {"properties", "$defs"}


def _trim_schema(node):
    if isinstance(node, list):
        return [_trim_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        if key not in _SCHEMA_KEYS:
            continue
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            out[key] = {name: _trim_schema(child) for name, child in value.items()}
        else:
            out[key] = _trim_schema(value)
    return out


def thinking_config(model: str) -> dict | None:
    """Flash-Lite classification and extraction stay on the minimal thinking level."""
    if "flash-lite" in model.lower():
        return {"thinkingLevel": THINKING_LEVEL_MINIMAL}
    return None


def max_output_tokens(schema: type[BaseModel]) -> int:
    """Cap the reply. Drafts of a job post are the only long answer, unless a call sets a tighter cap."""
    cap = output_limit.get()
    if cap:
        return cap
    return LONG_MAX_OUTPUT_TOKENS if schema.__name__ in _LONG_OUTPUT else DEFAULT_MAX_OUTPUT_TOKENS


def _token_estimate(text: str) -> int:
    """Rough input size. Gemini counts about four characters to a token."""
    return len(text) // 4


def retry_wait(status: int, headers, attempt: int) -> float:
    """Seconds to pause before trying again. A 429 waits a few seconds, or Retry-After when Google sends one."""
    if status == 429:
        raw = headers.get("retry-after") if headers is not None else None
        if raw:
            try:
                return min(max(float(raw), 1.0), 30.0)
            except ValueError:
                pass
        return float(min(5 * attempt, 20))
    return float(min(2 ** attempt, 8))


def post_json(http, url: str, *, api_key: str, body: dict, max_attempts: int, sleep: Callable[[float], None]) -> dict:
    """POST JSON. On 429 or a server error, pause and try again instead of failing on the first response."""
    last = 0
    headers = {"x-goog-api-key": api_key}
    for attempt in range(1, max_attempts + 1):
        try:
            res = http.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            last = -1
            if attempt == max_attempts:
                raise LLMError(f"network error: {type(exc).__name__}") from None
            sleep(float(min(2 ** attempt, 8)))
            continue
        if res.status_code == 200:
            return res.json()
        last = res.status_code
        if res.status_code not in RETRY_STATUS or attempt == max_attempts:
            break
        sleep(retry_wait(res.status_code, res.headers, attempt))
    if last in RETRY_STATUS or last == -1:
        raise LLMError(f"provider unavailable ({last})")
    raise LLMError(f"provider returned {last}")


class GeminiClient:
    def __init__(self, api_key: str, model: str, *, http: httpx.Client | None = None,
                 max_attempts: int = MAX_ATTEMPTS, sleep: Callable[[float], None] = time.sleep):
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not set")
        self.api_key, self.model = api_key, model
        self.http = http or _client(60.0)
        self.max_attempts, self.sleep = max_attempts, sleep
        self.last_usage = {"input": 0, "output": 0}
        self._caches: dict[str, tuple[str, float]] = {}
        self._cache_lock = threading.Lock()

    def _post(self, url: str, body: dict) -> dict:
        return post_json(self.http, url, api_key=self.api_key, body=body,
                         max_attempts=self.max_attempts, sleep=self.sleep)

    def _cache_name(self, system: str, context: str) -> str | None:
        """Reuse a server-side cache of this exact system text and reference. None if it is too small."""
        if _token_estimate(system) + _token_estimate(context) < MIN_CACHE_TOKENS:
            return None
        key = hashlib.sha256(f"{self.model}\n{system}\n{context}".encode("utf-8")).hexdigest()
        now = time.time()
        with self._cache_lock:
            hit = self._caches.get(key)
            if hit and hit[1] > now:
                return hit[0]
        try:
            created = self._post(f"{BASE_URL}/cachedContents", {
                "model": f"models/{self.model}",
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": context}]}],
                "ttl": f"{CACHE_TTL_S}s",
            })
            name = created["name"]
        except (LLMError, KeyError, TypeError):
            return None
        with self._cache_lock:
            self._caches[key] = (name, now + CACHE_TTL_S - 60)
        return name

    def structured(self, *, schema: type[T], system: str, user: str, prompt_version: str, context: str = "") -> T:
        # system and context are the stable prefix. user is the part that changes per call.
        config = {
            "temperature": 0,
            "maxOutputTokens": max_output_tokens(schema),
            "responseMimeType": "application/json",
            "responseJsonSchema": response_schema(schema),
        }
        level = thinking_config(self.model)
        if level:
            config["thinkingConfig"] = level
        cached = self._cache_name(system, context) if context else None
        if cached:
            body = {
                "cachedContent": cached,
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": config,
            }
        else:
            text = f"{context}\n{user}" if context else user
            body = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": text}]}],
                "generationConfig": config,
            }
        data = self._post(f"{BASE_URL}/models/{self.model}:generateContent", body)
        meta = data.get("usageMetadata") or {}
        try:
            self.last_usage = {
                "input": int(meta.get("promptTokenCount") or 0),
                "output": int(meta.get("candidatesTokenCount") or 0),
            }
        except (TypeError, ValueError):
            self.last_usage = {"input": 0, "output": 0}
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = next(p["text"] for p in parts if not p.get("thought") and p.get("text"))
        except (KeyError, IndexError, TypeError, StopIteration):
            raise LLMError("empty or blocked response") from None
        try:
            return schema.model_validate_json(text)
        except ValidationError:
            raise LLMError("response did not match the schema") from None


class GeminiEmbedder:
    def __init__(self, api_key: str, model: str, dims: int = 768, *, http: httpx.Client | None = None,
                 max_attempts: int = MAX_ATTEMPTS, sleep: Callable[[float], None] = time.sleep):
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not set")
        self.api_key, self.model, self.dims = api_key, model, dims
        self.http = http or _client(60.0)
        self.max_attempts, self.sleep = max_attempts, sleep

    def embed(self, texts: list[str], task: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
        """task: RETRIEVAL_DOCUMENT for stored text, RETRIEVAL_QUERY for search queries.

        Texts go out in small batches. A pause between batches lets the per-minute window clear.
        """
        out: list[list[float]] = []
        sent = 0
        for i in range(0, len(texts), EMBED_BATCH_SIZE):
            if sent:
                self.sleep(EMBED_BATCH_PAUSE_S)
            batch = texts[i:i + EMBED_BATCH_SIZE]
            sent += 1
            body = {"requests": [{
                "model": f"models/{self.model}",
                "content": {"parts": [{"text": t}]},
                "taskType": task,
                "outputDimensionality": self.dims,
            } for t in batch]}
            data = post_json(self.http, f"{BASE_URL}/models/{self.model}:batchEmbedContents",
                             api_key=self.api_key, body=body, max_attempts=self.max_attempts, sleep=self.sleep)
            try:
                out.extend(e["values"] for e in data["embeddings"])
            except (KeyError, TypeError):
                raise LLMError("bad embedding response") from None
        if len(out) != len(texts):
            raise LLMError("embedding count mismatch")
        return out
