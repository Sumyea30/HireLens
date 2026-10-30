# Speech-to-text service (faster-whisper, CPU int8 by default).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/home/app/.cache/huggingface \
    STT_MODEL=small \
    STT_DEVICE=cpu \
    STT_COMPUTE_TYPE=int8 \
    STT_LANGUAGE=en \
    STT_CPU_THREADS=4 \
    STT_NUM_WORKERS=1 \
    STT_FALLBACK_MODEL=small

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY backend/stt/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY backend/stt/main.py ./main.py
COPY backend/stt/entrypoint.sh /entrypoint.sh

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /home/app/.cache/huggingface \
    && chown -R app:app /app /home/app \
    && sed -i 's/\r$//' /entrypoint.sh \
    && chmod +x /entrypoint.sh

EXPOSE 8090
# Entrypoint runs as root only to fix volume ownership, then drops to app.
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8090"]
