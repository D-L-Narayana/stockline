"""Structured logging, request ids, timing, in-process metrics and JSON error handlers (server-only).

``app.main`` wires the middleware stack in this order (private test apps do the same)::

    security.install_auth(app, settings)      # innermost: API-key checks for mutating methods
    observability.install(app, settings)      # request id, timing, access log, metrics, JSON 500s
    security.install_headers(app, settings)   # outermost: CORS (when configured) + security headers

``RequestContextMiddleware`` is pure ASGI, so every response — static files, JSON, CSV, the API-key
401s, validation 422s and unhandled exceptions — carries ``X-Request-ID`` and ``X-Response-Time-ms``,
is written to the ``stockline.access`` logger and counted in ``METRICS``.

Logging: ``setup_logging(level, fmt)`` configures the ``stockline`` logger tree once (idempotent) with a
single stderr handler; ``fmt="json"`` emits one JSON object per line, ``fmt="text"`` a readable line.
Metrics: ``METRICS`` is a thread-safe registry rendered by ``GET /metrics`` in the Prometheus text format.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import deps
from .common import CONFLICT, INTERNAL, METHOD_NOT_ALLOWED, NOT_FOUND, POOL_EXHAUSTED, UNAUTHORIZED, VALIDATION, ServiceError

LOGGER_NAME = "stockline"
ACCESS_LOGGER_NAME = "stockline.access"
PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
HISTOGRAM_BUCKETS: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0)
MAX_SERIES = 1000  # distinct (method, path, status) counters kept before new paths collapse into "other"
INTERNAL_ERROR_DETAIL = "internal error"
POOL_EXHAUSTED_DETAIL = "no database connection available, retry shortly"

logger = logging.getLogger(LOGGER_NAME)
access_logger = logging.getLogger(ACCESS_LOGGER_NAME)

_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_NUMERIC_SEGMENT_RE = re.compile(r"/\d+(?=/|$)")
_RECORD_FIELDS = ("request_id", "method", "path", "status", "duration_ms", "client")
_HTTP_STATUS_CODES = {401: UNAUTHORIZED, 404: NOT_FOUND, 405: METHOD_NOT_ALLOWED, 409: CONFLICT, 422: VALIDATION}


# --------------------------------------------------------------------------- logging
def _timestamp(created: float) -> str:
    moment = datetime.fromtimestamp(created, UTC)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}Z"


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ``ts, level, logger, msg`` plus the request fields when present and ``exc`` on errors."""

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {"ts": _timestamp(record.created), "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        for key in _RECORD_FIELDS:
            value = record.__dict__.get(key)
            if value is not None:
                data[key] = value
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """``ts LEVEL logger [request_id] message`` with the traceback appended on errors."""

    def format(self, record: logging.LogRecord) -> str:
        prefix = f"{_timestamp(record.created)} {record.levelname} {record.name}"
        request_id = record.__dict__.get("request_id")
        if request_id:
            prefix += f" [{request_id}]"
        line = f"{prefix} {record.getMessage()}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class _StderrHandler(logging.StreamHandler):
    """Stream handler that resolves ``sys.stderr`` at emit time (survives stream replacement, e.g. test capture)."""

    stockline_handler = True

    @property
    def stream(self):  # type: ignore[override]
        return sys.stderr

    @stream.setter
    def stream(self, value) -> None:  # the base class assigns a stream in __init__; we always use the current one
        return None


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    """Configure the ``stockline`` logger tree: one stderr handler with a ``json`` or ``text`` formatter.

    Idempotent — calling it again only swaps the formatter and level, it never stacks handlers.
    ``stockline`` does not propagate to the root logger, so lines are never duplicated by other configs.
    """
    formatter: logging.Formatter = JsonFormatter() if str(fmt).strip().lower() == "json" else TextFormatter()
    numeric = logging.getLevelName(str(level).strip().upper())
    if not isinstance(numeric, int):
        numeric = logging.INFO
    handler = next((h for h in logger.handlers if getattr(h, "stockline_handler", False)), None)
    if handler is None:
        handler = _StderrHandler()
        logger.addHandler(handler)
    handler.setFormatter(formatter)
    logger.setLevel(numeric)
    logger.propagate = False


# --------------------------------------------------------------------------- metrics
def path_template(path: str) -> str:
    """Collapse numeric path segments (``/orders/42/cancel`` → ``/orders/{id}/cancel``) to bound label cardinality."""
    return _NUMERIC_SEGMENT_RE.sub("/{id}", path)


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class MetricsRegistry:
    """Thread-safe in-process request metrics rendered in the Prometheus text exposition format.

    * ``stockline_requests_total{method,path,status}`` — counter per method, path template and status.
    * ``stockline_request_duration_seconds`` — histogram with buckets ``HISTOGRAM_BUCKETS`` (+Inf), ``_sum``, ``_count``.
    * ``stockline_up`` — always 1 while the process serves.
    * ``stockline_pool_connections{state="created"|"idle"}`` and ``stockline_pool_size`` when pool stats are passed in.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str, int], int] = {}
        self._buckets: list[int] = [0] * len(HISTOGRAM_BUCKETS)
        self._count = 0
        self._sum = 0.0

    def reset(self) -> None:
        """Drop every counter and histogram value (tests and the demo use this)."""
        with self._lock:
            self._requests = {}
            self._buckets = [0] * len(HISTOGRAM_BUCKETS)
            self._count = 0
            self._sum = 0.0

    def observe(self, method: str, path: str, status: int, duration_s: float) -> None:
        """Count one request and add its duration (seconds) to the histogram."""
        duration = max(0.0, float(duration_s))
        with self._lock:
            key = (str(method), str(path), int(status))
            if key not in self._requests and len(self._requests) >= MAX_SERIES:
                key = (key[0], "other", key[2])
            self._requests[key] = self._requests.get(key, 0) + 1
            for index, bound in enumerate(HISTOGRAM_BUCKETS):
                if duration <= bound:
                    self._buckets[index] += 1
            self._count += 1
            self._sum += duration

    def snapshot(self) -> dict:
        """Copy of the raw values: ``{"requests", "buckets", "count", "sum"}``."""
        with self._lock:
            return {"requests": dict(self._requests), "buckets": list(self._buckets), "count": self._count, "sum": self._sum}

    def render_prometheus(self, pool_stats: dict | None = None) -> str:
        """Render the registry (and optional ``ConnectionPool.stats``) as Prometheus text format 0.0.4."""
        snap = self.snapshot()
        lines = [
            "# HELP stockline_requests_total HTTP requests by method, path template and status.",
            "# TYPE stockline_requests_total counter",
        ]
        for (method, path, status), count in sorted(snap["requests"].items()):
            lines.append(f'stockline_requests_total{{method="{_label(method)}",path="{_label(path)}",status="{status}"}} {count}')
        lines += [
            "# HELP stockline_request_duration_seconds HTTP request duration in seconds.",
            "# TYPE stockline_request_duration_seconds histogram",
        ]
        for bound, count in zip(HISTOGRAM_BUCKETS, snap["buckets"], strict=True):
            lines.append(f'stockline_request_duration_seconds_bucket{{le="{bound}"}} {count}')
        lines.append(f'stockline_request_duration_seconds_bucket{{le="+Inf"}} {snap["count"]}')
        lines.append(f"stockline_request_duration_seconds_sum {snap['sum']:.6f}")
        lines.append(f"stockline_request_duration_seconds_count {snap['count']}")
        lines += ["# HELP stockline_up 1 while the process is serving requests.", "# TYPE stockline_up gauge", "stockline_up 1"]
        if pool_stats is not None:
            lines += [
                "# HELP stockline_pool_connections SQLite connections in the in-process pool by state.",
                "# TYPE stockline_pool_connections gauge",
            ]
            for state in ("created", "idle"):
                lines.append(f'stockline_pool_connections{{state="{state}"}} {int(pool_stats.get(state, 0))}')
            lines += [
                "# HELP stockline_pool_size Capacity of the SQLite connection pool.",
                "# TYPE stockline_pool_size gauge",
                f"stockline_pool_size {int(pool_stats.get('size', 0))}",
            ]
        return "\n".join(lines) + "\n"


METRICS = MetricsRegistry()


# --------------------------------------------------------------------------- request context middleware
def resolve_request_id(inbound: str | None) -> str:
    """Keep a well-formed inbound ``X-Request-ID`` (``^[A-Za-z0-9._-]{1,64}$``); otherwise mint ``uuid4().hex[:12]``."""
    if inbound and _REQUEST_ID_RE.fullmatch(inbound):
        return inbound
    return uuid.uuid4().hex[:12]


def _client(scope: Scope) -> str:
    client = scope.get("client")
    if not client:
        return "-"
    return f"{client[0]}:{client[1]}"


class RequestContextMiddleware:
    """Pure-ASGI middleware: request id, ``X-Response-Time-ms``, access log, metrics and JSON 500s.

    The request id lives in ``scope["state"]["request_id"]`` (``request.state.request_id`` in routes and
    exception handlers). Exceptions escaping the inner app are logged with their traceback and answered
    with ``500 {"detail": "internal error", "code": "internal", "request_id"}`` unless the response had
    already started, in which case the exception is re-raised after logging.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = resolve_request_id(Headers(scope=scope).get("x-request-id"))
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()
        status_seen: list[int] = []

        async def send_with_context(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_seen.append(int(message["status"]))
                message["headers"] = list(message.get("headers") or [])
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers["X-Response-Time-ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
            await send(message)

        failed = False
        try:
            try:
                await self.app(scope, receive, send_with_context)
            except Exception as exc:
                failed = True
                logger.error(
                    "unhandled exception during %s %s", scope.get("method", "-"), scope.get("path", ""), exc_info=exc, extra={"request_id": request_id}
                )
                if status_seen:  # headers already on the wire: nothing more can be sent
                    raise
                body = {"detail": INTERNAL_ERROR_DETAIL, "code": INTERNAL, "request_id": request_id}
                await JSONResponse(status_code=500, content=body)(scope, receive, send_with_context)
        finally:
            duration = time.perf_counter() - started
            status = 500 if failed else (status_seen[0] if status_seen else 500)
            self._record(scope, request_id, status, duration)

    @staticmethod
    def _record(scope: Scope, request_id: str, status: int, duration: float) -> None:
        method = scope.get("method", "-")
        path = scope.get("path", "")
        METRICS.observe(method, path_template(path), status, duration)
        duration_ms = round(duration * 1000, 2)
        client = _client(scope)
        access_logger.log(
            logging.WARNING if status >= 500 else logging.INFO,
            "%s %s %s %.1fms client=%s",
            method,
            path,
            status,
            duration_ms,
            client,
            extra={"request_id": request_id, "method": method, "path": path, "status": status, "duration_ms": duration_ms, "client": client},
        )


# --------------------------------------------------------------------------- exception handlers
def _error_body(request: Request, detail: Any, code: str) -> dict:
    body: dict[str, Any] = {"detail": detail, "code": code}
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        body["request_id"] = request_id
    return body


async def pool_exhausted_handler(request: Request, exc: deps.PoolExhausted) -> JSONResponse:
    """``503 pool_exhausted`` with ``Retry-After: 1`` when no pooled connection became free in time."""
    logger.warning("database pool exhausted: %s", exc, extra={"request_id": getattr(request.state, "request_id", None)})
    return JSONResponse(status_code=503, content=_error_body(request, POOL_EXHAUSTED_DETAIL, POOL_EXHAUSTED), headers={"Retry-After": "1"})


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """``422 validation_error`` carrying pydantic's error list and the request id."""
    return JSONResponse(status_code=422, content=_error_body(request, jsonable_encoder(exc.errors()), VALIDATION))


async def http_exception_handler(request: Request, exc: HTTPException) -> Response:
    """Framework ``HTTPException`` (404 unknown path, 405 wrong method, …) in the same ``{detail, code, request_id}`` shape."""
    headers = getattr(exc, "headers", None)
    if exc.status_code < 200 or exc.status_code in (204, 205, 304):
        return Response(status_code=exc.status_code, headers=headers)
    code = _HTTP_STATUS_CODES.get(exc.status_code, "error")
    return JSONResponse(status_code=exc.status_code, content=_error_body(request, exc.detail, code), headers=headers)


def install(app: FastAPI, settings: deps.Settings | None = None) -> None:
    """Add ``RequestContextMiddleware`` and the JSON error handlers.

    Call it after ``security.install_auth`` and before ``security.install_headers``. ``settings`` is accepted
    so every install function has the same signature; logging itself is configured by ``setup_logging``.
    """
    app.add_middleware(RequestContextMiddleware)
    app.add_exception_handler(ServiceError, deps.service_error_handler)
    app.add_exception_handler(deps.PoolExhausted, pool_exhausted_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
