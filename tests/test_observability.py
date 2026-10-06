"""Request ids, timing header, access log, metrics and JSON error handlers (``app.observability``)."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app import db, deps, observability, security
from app.common import CONFLICT, ServiceError

HEX12 = re.compile(r"[0-9a-f]{12}")
TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")


class _Capture(logging.Handler):
    """Collects records and their formatted lines."""

    def __init__(self, formatter: logging.Formatter) -> None:
        super().__init__()
        self.setFormatter(formatter)
        self.records: list[logging.LogRecord] = []
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.lines.append(self.format(record))


def make_app(settings: deps.Settings | None = None) -> FastAPI:
    settings = settings or deps.Settings()
    app = FastAPI()
    security.install_auth(app, settings)
    observability.install(app, settings)
    security.install_headers(app, settings)

    @app.get("/whoami")
    def whoami(request: Request):
        return {"request_id": getattr(request.state, "request_id", None)}

    @app.post("/echo")
    def echo(body: dict):
        return body

    @app.get("/boom")
    def boom():
        raise ServiceError(409, "boom", CONFLICT)

    @app.get("/crash")
    def crash():
        raise RuntimeError("kaboom")

    @app.get("/needs-db")
    def needs_db(conn: sqlite3.Connection = Depends(deps.get_conn)):
        return {"one": conn.execute("SELECT 1").fetchone()[0]}

    @app.get("/orders/{order_id}")
    def order(order_id: int):
        return {"id": order_id}

    return app


@pytest.fixture()
def dbpath(tmp_path, monkeypatch):
    path = str(tmp_path / "obs.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    for var in ("STOCKLINE_SEED", "STOCKLINE_POOL_SIZE", "STOCKLINE_POOL_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    deps.reset_state(seed=False)
    observability.METRICS.reset()
    yield path
    deps.reset_state(seed=False)
    observability.METRICS.reset()


@pytest.fixture()
def client(dbpath):
    with TestClient(make_app(), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def capture():
    """Attach a JSON-formatting capture handler to the ``stockline`` logger tree."""
    observability.setup_logging("INFO", "json")
    handler = _Capture(observability.JsonFormatter())
    root = logging.getLogger("stockline")
    root.addHandler(handler)
    yield handler
    root.removeHandler(handler)


def _samples(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        out[name] = float(value)
    return out


# --------------------------------------------------------------------------- request ids and timing
def test_valid_inbound_request_id_is_echoed_and_visible_to_routes(client):
    r = client.get("/whoami", headers={"X-Request-ID": "abc.DEF_123-xyz"})
    assert r.status_code == 200
    assert r.headers.get("x-request-id") == "abc.DEF_123-xyz"
    assert r.json() == {"request_id": "abc.DEF_123-xyz"}


@pytest.mark.parametrize("bad", ["with space", "a" * 65, "semi;colon", "slash/x", "quote\"x", ""])
def test_invalid_inbound_request_id_is_replaced(client, bad):
    r = client.get("/whoami", headers={"X-Request-ID": bad})
    rid = r.headers.get("x-request-id")
    assert rid and HEX12.fullmatch(rid), rid
    assert rid != bad
    assert r.json() == {"request_id": rid}


def test_missing_request_id_is_generated_per_request(client):
    a = client.get("/whoami").headers.get("x-request-id")
    b = client.get("/whoami").headers.get("x-request-id")
    assert a and b and HEX12.fullmatch(a) and HEX12.fullmatch(b)
    assert a != b


def test_response_time_header_is_present(client):
    r = client.get("/whoami")
    value = r.headers.get("x-response-time-ms")
    assert value is not None
    assert float(value) >= 0.0


def test_request_id_on_404_and_405(client):
    r = client.get("/nope", headers={"X-Request-ID": "nf-1"})
    assert r.status_code == 404
    assert r.headers.get("x-request-id") == "nf-1"
    assert r.json() == {"detail": "Not Found", "code": "not_found", "request_id": "nf-1"}
    r = client.post("/whoami", headers={"X-Request-ID": "mna-1"})
    assert r.status_code == 405
    assert r.headers.get("x-request-id") == "mna-1"
    assert "GET" in r.headers.get("allow", "")
    assert r.json() == {"detail": "Method Not Allowed", "code": "method_not_allowed", "request_id": "mna-1"}


# --------------------------------------------------------------------------- logging
def test_json_access_log_line_parses_and_carries_request_id(client, capture):
    client.get("/whoami", headers={"X-Request-ID": "log-1"})
    access = [ln for rec, ln in zip(capture.records, capture.lines, strict=True) if rec.name == "stockline.access"]
    assert access, "no access log line was emitted"
    line = access[-1]
    assert line.startswith("{"), line
    data = json.loads(line)
    assert data["request_id"] == "log-1"
    assert data["method"] == "GET"
    assert data["path"] == "/whoami"
    assert data["status"] == 200
    assert data["level"] == "INFO"
    assert isinstance(data["duration_ms"], int | float)
    assert TS.fullmatch(data["ts"]), data["ts"]
    assert isinstance(data["client"], str) and data["client"]


def test_text_access_log_line(client):
    observability.setup_logging("INFO", "text")
    handler = _Capture(observability.TextFormatter())
    access = logging.getLogger("stockline.access")
    access.addHandler(handler)
    try:
        client.get("/whoami", headers={"X-Request-ID": "log-2"})
    finally:
        access.removeHandler(handler)
    assert handler.lines, "no access log line was emitted"
    line = handler.lines[-1]
    assert not line.startswith("{")
    assert "log-2" in line
    assert "GET /whoami" in line
    assert " 200 " in line
    assert TS.match(line), line


def _own_handlers(logger: logging.Logger) -> list[logging.Handler]:
    # pytest attaches its own capture handlers to non-propagating loggers; only count the one setup_logging installs.
    return [h for h in logger.handlers if getattr(h, "stockline_handler", False)]


def test_setup_logging_is_idempotent_and_switches_format():
    observability.setup_logging("INFO", "json")
    observability.setup_logging("INFO", "json")
    logger = logging.getLogger("stockline")
    assert len(_own_handlers(logger)) == 1
    assert isinstance(_own_handlers(logger)[0].formatter, observability.JsonFormatter)
    assert logger.level == logging.INFO
    assert logger.propagate is False
    observability.setup_logging("DEBUG", "text")
    assert len(_own_handlers(logger)) == 1
    assert isinstance(_own_handlers(logger)[0].formatter, observability.TextFormatter)
    assert logger.level == logging.DEBUG
    assert logging.getLogger("stockline.access").getEffectiveLevel() == logging.DEBUG
    observability.setup_logging("bogus-level", "weird-format")  # unknown choices fall back to INFO / text
    assert logger.level == logging.INFO
    assert isinstance(_own_handlers(logger)[0].formatter, observability.TextFormatter)


def test_unhandled_exception_is_logged_with_traceback(client, capture):
    r = client.get("/crash", headers={"X-Request-ID": "crash-2"})
    assert r.status_code == 500
    errors = [(rec, ln) for rec, ln in zip(capture.records, capture.lines, strict=True) if rec.levelno >= logging.ERROR]
    assert errors, "no error record was logged"
    rec, line = errors[-1]
    assert rec.exc_info is not None
    data = json.loads(line)
    assert data["level"] == "ERROR"
    assert data["request_id"] == "crash-2"
    assert "RuntimeError: kaboom" in data["exc"]
    assert "Traceback" in data["exc"]
    access = [json.loads(ln) for rec, ln in zip(capture.records, capture.lines, strict=True) if rec.name == "stockline.access"]
    assert access and access[-1]["status"] == 500 and access[-1]["request_id"] == "crash-2"


# --------------------------------------------------------------------------- error handlers
def test_unhandled_exception_becomes_json_500_with_headers(client):
    r = client.get("/crash", headers={"X-Request-ID": "crash-1"})
    assert r.status_code == 500
    assert r.headers.get("content-type", "").startswith("application/json")
    assert r.json() == {"detail": "internal error", "code": "internal", "request_id": "crash-1"}
    assert r.headers.get("x-request-id") == "crash-1"
    assert r.headers.get("x-response-time-ms") is not None
    assert r.headers.get("content-security-policy") == security.DEFAULT_CSP
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("cache-control") == "no-store"


def test_service_error_body_includes_request_id(client):
    r = client.get("/boom", headers={"X-Request-ID": "svc-1"})
    assert r.status_code == 409
    assert r.json() == {"detail": "boom", "code": "conflict", "request_id": "svc-1"}
    assert r.headers.get("x-request-id") == "svc-1"


def test_pool_exhausted_becomes_503_with_retry_after(dbpath):
    app = make_app()

    def exhausted():
        raise deps.PoolExhausted("no database connection available within 0.1s (pool size 1)")

    app.dependency_overrides[deps.get_conn] = exhausted
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/needs-db", headers={"X-Request-ID": "pool-1"})
    assert r.status_code == 503
    assert r.headers.get("retry-after") == "1"
    body = r.json()
    assert body["code"] == "pool_exhausted"
    assert body["request_id"] == "pool-1"
    assert isinstance(body["detail"], str) and body["detail"]
    assert r.headers.get("x-request-id") == "pool-1"


def test_validation_error_body_is_machine_readable(client):
    r = client.post("/echo", content=b"not json", headers={"Content-Type": "application/json", "X-Request-ID": "val-1"})
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "validation_error"
    assert body["request_id"] == "val-1"
    assert isinstance(body["detail"], list) and body["detail"]
    assert {"loc", "msg", "type"} <= set(body["detail"][0])
    r = client.get("/orders/not-a-number")
    assert r.status_code == 422
    assert r.json()["code"] == "validation_error"


def test_install_registers_the_exception_handlers():
    app = FastAPI()
    observability.install(app, deps.Settings())
    for exc_type in (ServiceError, deps.PoolExhausted, RequestValidationError):
        assert exc_type in app.exception_handlers, exc_type


# --------------------------------------------------------------------------- metrics
def test_path_template_collapses_numeric_segments():
    assert observability.path_template("/orders/42") == "/orders/{id}"
    assert observability.path_template("/orders/42/cancel") == "/orders/{id}/cancel"
    assert observability.path_template("/inventory/1/2/movements") == "/inventory/{id}/{id}/movements"
    assert observability.path_template("/health") == "/health"
    assert observability.path_template("/stores/12abc") == "/stores/12abc"
    assert observability.path_template("/") == "/"


def test_requests_are_counted_by_method_template_and_status(client):
    observability.METRICS.reset()
    client.get("/whoami")
    client.get("/whoami")
    client.get("/orders/7")
    client.get("/orders/8")
    client.get("/nope")
    client.get("/crash")
    client.post("/echo", json={"a": 1})
    text = observability.METRICS.render_prometheus()
    s = _samples(text)
    assert s.get('stockline_requests_total{method="GET",path="/whoami",status="200"}') == 2
    assert s.get('stockline_requests_total{method="GET",path="/orders/{id}",status="200"}') == 2
    assert s.get('stockline_requests_total{method="GET",path="/nope",status="404"}') == 1
    assert s.get('stockline_requests_total{method="GET",path="/crash",status="500"}') == 1
    assert s.get('stockline_requests_total{method="POST",path="/echo",status="200"}') == 1
    assert s.get("stockline_request_duration_seconds_count") == 7
    assert s.get('stockline_request_duration_seconds_bucket{le="+Inf"}') == 7
    assert s.get("stockline_up") == 1
    assert "# TYPE stockline_requests_total counter" in text
    assert "# TYPE stockline_request_duration_seconds histogram" in text


def test_histogram_buckets_are_cumulative_and_sum_counts():
    m = observability.METRICS
    m.reset()
    m.observe("GET", "/x", 200, 0.003)
    m.observe("GET", "/x", 200, 0.3)
    m.observe("GET", "/x", 200, 5.0)
    s = _samples(m.render_prometheus())
    assert s.get('stockline_request_duration_seconds_bucket{le="0.005"}') == 1
    assert s.get('stockline_request_duration_seconds_bucket{le="0.01"}') == 1
    assert s.get('stockline_request_duration_seconds_bucket{le="0.025"}') == 1
    assert s.get('stockline_request_duration_seconds_bucket{le="0.05"}') == 1
    assert s.get('stockline_request_duration_seconds_bucket{le="0.1"}') == 1
    assert s.get('stockline_request_duration_seconds_bucket{le="0.25"}') == 1
    assert s.get('stockline_request_duration_seconds_bucket{le="0.5"}') == 2
    assert s.get('stockline_request_duration_seconds_bucket{le="1.0"}') == 2
    assert s.get('stockline_request_duration_seconds_bucket{le="+Inf"}') == 3
    assert s.get("stockline_request_duration_seconds_count") == 3
    assert s.get("stockline_request_duration_seconds_sum") == pytest.approx(5.303)
    assert s.get('stockline_requests_total{method="GET",path="/x",status="200"}') == 3
    m.reset()


def test_pool_gauges_and_reset():
    m = observability.METRICS
    m.reset()
    m.observe("GET", "/health", 200, 0.001)
    text = m.render_prometheus({"size": 8, "created": 3, "idle": 2})
    s = _samples(text)
    assert s.get('stockline_pool_connections{state="idle"}') == 2
    assert s.get('stockline_pool_connections{state="created"}') == 3
    assert s.get("stockline_pool_size") == 8
    assert "# TYPE stockline_pool_connections gauge" in text
    without = m.render_prometheus()
    assert "stockline_pool_connections" not in without
    assert s.get('stockline_requests_total{method="GET",path="/health",status="200"}') == 1
    m.reset()
    after = _samples(m.render_prometheus())
    assert 'stockline_requests_total{method="GET",path="/health",status="200"}' not in after
    assert after.get("stockline_request_duration_seconds_count") == 0
    assert after.get('stockline_request_duration_seconds_bucket{le="+Inf"}') == 0
    assert after.get("stockline_request_duration_seconds_sum") == 0
    assert after.get("stockline_up") == 1


def test_metrics_registry_is_thread_safe():
    m = observability.METRICS
    m.reset()

    def work() -> None:
        for _ in range(200):
            m.observe("GET", "/t", 200, 0.001)

    threads = [threading.Thread(target=work) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    s = _samples(m.render_prometheus())
    assert s.get('stockline_requests_total{method="GET",path="/t",status="200"}') == 3200
    assert s.get("stockline_request_duration_seconds_count") == 3200
    assert s.get('stockline_request_duration_seconds_bucket{le="0.005"}') == 3200
    m.reset()


def test_label_values_are_escaped():
    m = observability.METRICS
    m.reset()
    m.observe("GET", '/we"ird\\path', 404, 0.001)
    text = m.render_prometheus()
    assert 'path="/we\\"ird\\\\path"' in text
    m.reset()
