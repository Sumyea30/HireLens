"""Celery application used by `docker compose` (`celery -A app.workers.celery_app`).

The attribute must be named `app` or `celery`: that is what the celery command
loads from this module. `celery_app` is the same object, for imports.
"""
import logging

from celery import Celery
from celery.signals import worker_ready

from app.core.config import load_settings

log = logging.getLogger("hirelens.worker")


def _broker() -> str:
    return load_settings().redis_url or "redis://localhost:6379/0"


app = Celery("hirelens", broker=_broker())
celery_app = app
app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_ignore_result=True,
    broker_connection_retry_on_startup=True,
)


@app.task(name="hirelens.screen_candidate")
def screen_candidate_task(candidate_id: str) -> str:
    from app.services.screening import screen_candidate
    from app.workers.runtime import worker_services
    return screen_candidate(worker_services(), candidate_id)


@worker_ready.connect
def sweep_pending(**_kwargs):
    """Pick up CVs that were stored before this worker was up."""
    try:
        from app.services.screening import enqueue_pending
        enqueue_pending(load_settings())
    except Exception:
        log.error("screen_sweep_failed")
