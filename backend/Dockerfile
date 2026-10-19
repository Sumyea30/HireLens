# Backend image, shared by api, worker and mail-ingest (build context = repo root).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System packages: Tesseract for the OCR fallback on scanned CVs.
# For WeasyPrint PDF export, also add: libpango-1.0-0 libpangoft2-1.0-0
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first so this layer is cached between code changes.
# requirements.txt must contain pinned versions (N11).
COPY backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY backend/app ./app
# COPY backend/mail_ingest ./mail_ingest   # add when the email intake service exists

# Non-root user. The data directory is created before the volume is mounted,
# so the named volume inherits this ownership.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data/stored_cvs \
    && chown -R app:app /app /data
USER app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
