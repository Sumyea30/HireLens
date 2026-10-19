"""Process-wide services for the screening worker.

Built on first task, not at import, so `celery -A app.workers.celery_app`
can load without opening the database. The API and this worker only share
candidates when DATABASE_URL is set (as it is in Docker Compose).
"""
_services = None


def worker_services():
    global _services
    if _services is None:
        from app.core.config import load_settings
        from app.main import build_services
        _services = build_services(load_settings())
    return _services
