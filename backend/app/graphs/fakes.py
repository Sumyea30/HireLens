"""In-memory fakes for development and tests. Do not use in production.

Use FakeLLM to build and test the graphs without API keys or cost (R3).
"""
import hashlib
import math
import re
from typing import Callable

from pydantic import BaseModel


class FakeLLM:
    """canned: schema class name -> dict, or a function(user_text) -> dict."""

    def __init__(self, canned: dict[str, dict | Callable[[str], dict]]):
        self.canned = canned
        self.calls: list[str] = []
        self.users: list[str] = []
        self.contexts: list[str] = []

    def structured(self, *, schema: type[BaseModel], system: str, user: str, prompt_version: str, context: str = ""):
        self.calls.append(schema.__name__)
        self.users.append(user)
        self.contexts.append(context)
        data = self.canned[schema.__name__]
        if callable(data):
            data = data(user)
        return schema.model_validate(data)


class FakeVectorStore:
    DIM = 256

    def __init__(self):
        self._data: dict[str, list[dict]] = {}

    @classmethod
    def _vec(cls, text: str) -> list[float]:
        v = [0.0] * cls.DIM
        for tok in re.findall(r"[a-z0-9+#]+", text.lower()):
            v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % cls.DIM] += 1.0
        return v

    @staticmethod
    def _cos(a: list[float], b: list[float]) -> float:
        na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
        return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0

    def index(self, collection: str, items: list[dict]) -> None:
        self._data[collection] = [{**it, "vec": self._vec(it["text"])} for it in items]

    def search(self, collection: str, query: str, k: int) -> list[dict]:
        return self.search_many(collection, [query], k)[0]

    def search_many(self, collection: str, queries: list[str], k: int) -> list[list[dict]]:
        out = []
        for query in queries:
            q = self._vec(query)
            rows = [{"id": it["id"], "text": it["text"], "score": self._cos(q, it["vec"])}
                    for it in self._data.get(collection, [])]
            out.append(sorted(rows, key=lambda r: r["score"], reverse=True)[:k])
        return out


class FakeRepo:
    def __init__(self):
        self.cv: dict[tuple[str, str], dict] = {}
        self.qa: dict[str, dict] = {}
        self.transcripts: dict[str, list[dict]] = {}
        self.assessments: dict[tuple[str, str], dict] = {}
        self.job_posts: dict[str, dict] = {}
        self.decisions: dict[str, dict] = {}
        self.open_jobs: set[str] = set()

    def save_cv_result(self, candidate_id, job_id, result): self.cv[(candidate_id, job_id)] = result
    def load_cv_result(self, candidate_id, job_id): return self.cv.get((candidate_id, job_id))
    def load_transcript(self, session_id): return self.transcripts.get(session_id, [])
    def save_qa_result(self, session_id, result): self.qa[session_id] = result
    def load_qa_result(self, session_id): return self.qa.get(session_id)
    def save_assessment(self, candidate_id, session_id, report): self.assessments[(candidate_id, session_id)] = report
    def save_job_post(self, job_id, record): self.job_posts[job_id] = record
    def save_posting_decision(self, job_id, decision): self.decisions[job_id] = decision
    def mark_job_open(self, job_id): self.open_jobs.add(job_id)


class FakeChannel:
    def __init__(self, name: str, fail: bool = False):
        self.name = name
        self.fail = fail
        self.published: list[dict] = []

    def publish(self, *, title: str, body: str) -> dict:
        if self.fail:
            raise ConnectionError("channel down")
        self.published.append({"title": title, "body": body})
        return {"status": "ok", "url": f"https://example.invalid/{self.name}", "error": None}
