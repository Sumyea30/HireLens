#!/bin/sh
# Named volumes are root-owned on first create; the app user must own the HF cache.
set -e
CACHE="${HF_HOME:-/home/app/.cache/huggingface}"
mkdir -p "$CACHE"
chown -R app:app "$(dirname "$CACHE")" 2>/dev/null || chown -R app:app "$CACHE" || true
# Drop root before starting uvicorn (setpriv is in util-linux on Debian slim).
exec setpriv --reuid=app --regid=app --init-groups -- "$@"
