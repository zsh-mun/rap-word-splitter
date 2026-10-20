FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_ENDPOINT=https://hf-mirror.com \
    HF_HUB_DISABLE_XET=1 \
    HF_HUB_DISABLE_SYMLINKS_WARNING=1 \
    DATA_DIR=/data \
    MODELS_DIR=/models \
    PORT=8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-server.txt ./
RUN pip install --no-cache-dir -r requirements-server.txt

COPY . .

ARG PRELOAD_MODELS=base
RUN mkdir -p /data/jobs /models \
    && if [ -n "$PRELOAD_MODELS" ]; then \
         python download_models.py --models "$PRELOAD_MODELS" --allow-failure; \
       fi

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=8s --start-period=30s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/api/health" || exit 1

CMD ["sh", "-c", "gunicorn --worker-class gthread --workers 1 --threads 4 --timeout 900 --graceful-timeout 30 --bind 0.0.0.0:${PORT:-8000} --access-logfile - --error-logfile - app:app"]
