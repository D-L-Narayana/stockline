FROM python:3.12-slim

LABEL org.opencontainers.image.title="StockLine" \
      org.opencontainers.image.description="Multi-store inventory & order API: SQLite ledger, idempotent orders, optimistic locking" \
      org.opencontainers.image.source="https://github.com/D-L-Narayana/stockline" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STOCKLINE_DB=/data/stockline.db \
    STOCKLINE_LOG_FORMAT=json \
    PORT=8000

WORKDIR /app

# Non-root runtime user (no shell, no home) that owns the data volume.
RUN groupadd --system --gid 10001 stockline \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin stockline \
    && mkdir -p /data \
    && chown stockline:stockline /data

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
COPY public ./public

USER 10001
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:'+__import__('os').environ.get('PORT','8000')+'/health')"

# One uvicorn worker by design: SQLite + the in-process connection pool serialise writers.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --workers 1"]
