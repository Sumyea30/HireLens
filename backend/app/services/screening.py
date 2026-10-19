"""Run Graph A on one queued candidate and enqueue that work for the worker.

Upload and email intake stay fast: they store the file and leave the candidate
queued. Screening happens in the Celery worker (or when a test calls
screen_candidate directly). Without REDIS_URL nothing is enqueued, so a process
that has no worker keeps the candidate queued.
"""
import hashlib
import json
import logging
import pathlib
from concurrent.futures import ThreadPoolExecutor

from app.graphs import build_cv_graph, initial_cv_state
from app.graphs.common import chunk_text, redact_protected
from app.graphs.cv_graph import PROMPT_VERSION

log = logging.getLogger("hirelens.screening")
BATCH_WORKERS = 4


def rubric_key(job: dict) -> str:
    """Identity of the rules a score was made under. A weight or requirement change is a new key."""
    payload = {
        "prompt": PROMPT_VERSION,
        "requirements": [
            {"id": r.get("id"), "text": r.get("text"), "kind": r.get("kind"), "category": r.get("category")}
            for r in job.get("requirements") or []
        ],
        "weights": job.get("weights") or {},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _file_hash(path: str | None) -> str | None:
    if not path:
        return None
    try:
        data = pathlib.Path(path).read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest() if data else None


def _rewrite_refs(result: dict, candidate_id: str) -> dict:
    """Evidence refs name the candidate whose chunks were indexed. Point them at this upload."""
    def fix(refs):
        out = []
        for ref in refs or []:
            kind, _, rest = str(ref).partition(":")
            _old, sep, chunk = rest.rpartition(":")
            out.append(f"cv:{candidate_id}:{chunk}" if kind == "cv" and sep else ref)
        return out

    result["candidate_id"] = candidate_id
    for match in result.get("matches") or []:
        match["evidence_refs"] = fix(match.get("evidence_refs"))
    for score in result.get("dimension_scores") or []:
        score["evidence_refs"] = fix(score.get("evidence_refs"))
    return result


def _remember(store, claimed: dict, job: dict) -> None:
    digest = _file_hash(claimed.get("file_path"))
    if not digest:
        return
    result = store.load_cv_result(claimed["id"], claimed["job_id"])
    if result and result.get("status") == "completed":
        store.save_score_cache(digest, rubric_key(job), result)


def _discard_cv(services, claimed: dict) -> None:
    """The original CV is only needed to read it. It is not kept afterwards."""
    path = claimed.get("file_path")
    storage = getattr(getattr(services, "intake", None), "storage", None)
    if storage is not None and path:
        storage.delete(path)


def _reuse(services, claimed: dict, cached: dict) -> None:
    """Copy a stored judgment onto this candidate and index the file so its evidence still opens."""
    path = claimed.get("file_path") or ""
    try:
        text = services.deps.extract_text(path)
        services.store.index(f"cv:{claimed['id']}", chunk_text(redact_protected(text)))
    except Exception:
        log.error("cache_replay_index_failed", extra={"candidate_id": claimed["id"]})
    result = _rewrite_refs(cached, claimed["id"])
    result.pop("contact", None)
    result["status"] = "completed"
    services.store.save_cv_result(claimed["id"], claimed["job_id"], result)


def screen_candidate(services, candidate_id: str) -> str:
    """Claim one candidate, run Graph A, and return completed, failed, or skipped."""
    store = services.store
    claimed = store.claim_candidate(candidate_id)
    if not claimed:
        return "skipped"
    job = store.get_job(claimed["job_id"])
    if not job or not job.get("requirements"):
        _fail(store, claimed, "cv_screening_failed")
        _discard_cv(services, claimed)
        return "failed"
    digest = _file_hash(claimed.get("file_path"))
    cached = store.load_score_cache(digest, rubric_key(job)) if digest else None
    if cached and cached.get("status") == "completed":
        _reuse(services, claimed, cached)
        store.add_audit("system", "cv_screened", "candidate", claimed["id"], "ok")
        _discard_cv(services, claimed)
        return "completed"
    graph = services.cv_graph or build_cv_graph(services.deps)
    try:
        out = graph.invoke(initial_cv_state(
            candidate_id=claimed["id"], job_id=claimed["job_id"],
            file_path=claimed.get("file_path") or "",
            job_requirements=job["requirements"], weights=job["weights"],
        ))
    except Exception:
        log.error("screen_crashed", extra={"candidate_id": claimed["id"]})
        out = {"status": "failed"}
    status = out.get("status") or "failed"
    current = store.get_candidate(claimed["id"])
    if current and current["status"] == "processing":
        # The graph records its own failure, except when saving the result itself failed.
        _fail(store, claimed, "cv_screening_failed")
        _discard_cv(services, claimed)
        return "failed"
    action = "cv_screened" if status == "completed" else "cv_screening_failed"
    store.add_audit("system", action, "candidate", claimed["id"], "ok" if status == "completed" else "error")
    if status == "completed":
        _remember(store, claimed, job)
    _discard_cv(services, claimed)
    return status


def screen_many(services, candidate_ids: list[str]) -> dict[str, str]:
    """Score several CVs at once. Each CV still gets the same result it would alone."""
    ids = [cid for cid in candidate_ids if cid]
    if not ids:
        return {}

    def one(cid: str) -> tuple[str, str]:
        try:
            return cid, screen_candidate(services, cid)
        except Exception:
            log.error("screen_crashed", extra={"candidate_id": cid})
            return cid, "failed"

    workers = min(BATCH_WORKERS, len(ids))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(one, ids))


def _fail(store, claimed: dict, action: str) -> None:
    store.save_cv_result(claimed["id"], claimed["job_id"], {
        "status": "failed", "candidate_id": claimed["id"], "job_id": claimed["job_id"], "errors": [],
    })
    store.add_audit("system", action, "candidate", claimed["id"], "error")


def enqueue_screening(candidate_id: str, *, redis_url: str) -> None:
    """Hand one candidate to the worker. No Redis means the candidate stays queued."""
    if not redis_url:
        return
    from app.workers.celery_app import screen_candidate_task
    screen_candidate_task.apply_async((candidate_id,), task_id=f"screen-{candidate_id}")


def make_on_queued(settings):
    """Callback for IntakeService.accept. A broker problem must not reject the CV."""
    def on_queued(candidate_id: str) -> None:
        try:
            enqueue_screening(candidate_id, redis_url=settings.redis_url)
        except Exception:
            log.error("screen_enqueue_failed", extra={"candidate_id": candidate_id})
    return on_queued


def enqueue_pending(settings) -> int:
    """Ask the worker to screen candidates still queued after a restart."""
    if not settings.database_url or not settings.redis_url:
        return 0
    from app.services.pgstore import PgStore
    store = PgStore(settings.database_url, None, settings.embedding_dims, settings.apply_email)
    n = 0
    for cid in store.list_pending_screening():
        try:
            enqueue_screening(cid, redis_url=settings.redis_url)
            n += 1
        except Exception:
            log.error("screen_enqueue_failed", extra={"candidate_id": cid})
    return n
