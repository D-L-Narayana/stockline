"""System router (``app.routers.system``): /health, /integrity, /integrity/rebuild, /metrics."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import __version__, db, deps, ledger, observability, security
from app.common import ServiceError
from app.routers import system

KEY = "rebuild-key"


def make_app(settings: deps.Settings | None = None) -> FastAPI:
    settings = settings or deps.Settings()
    app = FastAPI()
    security.install_auth(app, settings)
    observability.install(app, settings)
    security.install_headers(app, settings)
    app.add_exception_handler(ServiceError, deps.service_error_handler)
    app.include_router(system.router)
    return app


@pytest.fixture()
def dbpath(tmp_path, monkeypatch):
    path = str(tmp_path / "sys.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    for var in ("STOCKLINE_SEED", "STOCKLINE_POOL_SIZE", "STOCKLINE_POOL_TIMEOUT", "STOCKLINE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    deps.reset_state(seed=False)
    observability.METRICS.reset()
    yield path
    deps.reset_state(seed=False)


@pytest.fixture()
def client(dbpath):
    with TestClient(make_app(), raise_server_exceptions=False) as c:
        yield c


def _seed_pair(path: str) -> None:
    """One store, one product and a 10-unit receipt written directly to the database file."""
    conn = db.connect(path)
    try:
        db.init_schema(conn)
        conn.execute("INSERT INTO stores (code, name, region) VALUES ('S1', 'One', 'South')")
        conn.execute("INSERT INTO products (sku, name, category, price_cents, reorder_point) VALUES ('SKU-1', 'Widget', 'Home', 1000, 5)")
        with db.transaction(conn):
            ledger.apply_movement(conn, 1, 1, 10, "receipt", "receipt:t1")
    finally:
        conn.close()


def _force_mismatch(path: str, on_hand: int) -> None:
    conn = db.connect(path)
    try:
        conn.execute("UPDATE inventory SET on_hand = ? WHERE store_id = 1 AND product_id = 1", (on_hand,))
    finally:
        conn.close()


# --------------------------------------------------------------------------- /health
def test_health_shape(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"status", "version", "schema_version", "runtime", "uptime_s"}
    assert body["status"] == "ok"
    assert body["version"] == __version__ == "0.2.0"
    assert body["schema_version"] == 2
    assert body["runtime"] == "server"
    assert isinstance(body["uptime_s"], int | float)
    assert body["uptime_s"] >= 0
    assert round(body["uptime_s"], 1) == body["uptime_s"]
    assert r.headers.get("cache-control") == "no-store"
    assert r.headers.get("x-request-id")


def test_health_uptime_grows_and_is_not_cached(client):
    first = client.get("/health").json()["uptime_s"]
    second = client.get("/health").json()["uptime_s"]
    assert second >= first


def test_health_returns_503_when_the_pool_is_exhausted(client, monkeypatch):
    monkeypatch.setenv("STOCKLINE_POOL_SIZE", "1")
    monkeypatch.setenv("STOCKLINE_POOL_TIMEOUT", "0.05")
    deps.reset_state(seed=False)
    held = deps.pool.acquire()
    try:
        r = client.get("/health", headers={"X-Request-ID": "busy-1"})
    finally:
        deps.pool.release(held)
    assert r.status_code == 503
    assert r.headers.get("retry-after") == "1"
    assert r.json()["code"] == "pool_exhausted"
    assert r.json()["request_id"] == "busy-1"
    assert client.get("/health").status_code == 200
    monkeypatch.delenv("STOCKLINE_POOL_SIZE")
    monkeypatch.delenv("STOCKLINE_POOL_TIMEOUT")
    deps.reset_state(seed=False)


# --------------------------------------------------------------------------- /integrity
def test_integrity_report_on_an_empty_database(client):
    r = client.get("/integrity")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"ok", "checked", "schema_version", "mismatches", "negative", "chain_breaks", "order_total_mismatches", "missing_balance_after"}
    assert body["ok"] is True
    assert body["checked"] == 0
    assert body["schema_version"] == 2
    assert body["mismatches"] == [] and body["negative"] == [] and body["chain_breaks"] == [] and body["order_total_mismatches"] == []
    assert body["missing_balance_after"] == 0


def test_integrity_detects_a_forced_cache_mismatch(client, dbpath):
    _seed_pair(dbpath)
    assert client.get("/integrity").json()["ok"] is True
    _force_mismatch(dbpath, 7)
    body = client.get("/integrity").json()
    assert body["ok"] is False
    assert body["checked"] == 1
    assert body["mismatches"] == [{"store_id": 1, "product_id": 1, "on_hand": 7, "ledger": 10}]
    assert body["negative"] == []


def test_integrity_detects_a_chain_break(client, dbpath):
    _seed_pair(dbpath)
    conn = db.connect(dbpath)
    try:
        conn.execute("UPDATE stock_movements SET balance_after = 99 WHERE id = 1")
    finally:
        conn.close()
    body = client.get("/integrity").json()
    assert body["ok"] is False
    assert body["chain_breaks"] == [{"store_id": 1, "product_id": 1, "movement_id": 1, "expected": 10, "actual": 99}]


# --------------------------------------------------------------------------- /integrity/rebuild
def test_rebuild_repairs_a_forced_mismatch(client, dbpath):
    _seed_pair(dbpath)
    _force_mismatch(dbpath, 7)
    r = client.post("/integrity/rebuild")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"checked", "fixed", "backfilled", "ok"}
    assert body["ok"] is True
    assert body["checked"] == 1
    assert body["backfilled"] == 0
    assert body["fixed"] == [{"store_id": 1, "product_id": 1, "before": 7, "after": 10}]
    assert client.get("/integrity").json()["ok"] is True
    conn = db.connect(dbpath)
    try:
        row = conn.execute("SELECT on_hand, version FROM inventory WHERE store_id = 1 AND product_id = 1").fetchone()
    finally:
        conn.close()
    assert (row["on_hand"], row["version"]) == (10, 2)
    again = client.post("/integrity/rebuild").json()
    assert again["fixed"] == [] and again["ok"] is True


def test_rebuild_backfills_missing_balance_after(client, dbpath):
    _seed_pair(dbpath)
    conn = db.connect(dbpath)
    try:
        conn.execute("UPDATE stock_movements SET balance_after = NULL WHERE id = 1")
    finally:
        conn.close()
    assert client.get("/integrity").json()["missing_balance_after"] == 1
    body = client.post("/integrity/rebuild").json()
    assert body["backfilled"] == 1
    assert client.get("/integrity").json()["missing_balance_after"] == 0


def test_rebuild_is_write_protected_when_an_api_key_is_set(dbpath):
    _seed_pair(dbpath)
    with TestClient(make_app(deps.Settings(api_key=KEY)), raise_server_exceptions=False) as c:
        assert c.get("/integrity").status_code == 200
        r = c.post("/integrity/rebuild")
        assert r.status_code == 401
        assert r.json()["code"] == "unauthorized"
        assert c.post("/integrity/rebuild", headers={"X-API-Key": KEY}).status_code == 200
        assert c.post("/integrity/rebuild", headers={"Authorization": f"Bearer {KEY}"}).status_code == 200


# --------------------------------------------------------------------------- /metrics
def test_metrics_exposition_after_requests(client):
    observability.METRICS.reset()
    client.get("/health")
    client.get("/health")
    client.get("/integrity")
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers.get("content-type") == "text/plain; version=0.0.4; charset=utf-8"
    text = r.text
    assert 'stockline_requests_total{method="GET",path="/health",status="200"} 2' in text
    assert 'stockline_requests_total{method="GET",path="/integrity",status="200"} 1' in text
    assert "stockline_request_duration_seconds_bucket{le=\"0.005\"}" in text
    assert "stockline_request_duration_seconds_bucket{le=\"+Inf\"} 3" in text
    assert "stockline_request_duration_seconds_count 3" in text
    assert "stockline_request_duration_seconds_sum " in text
    assert "stockline_up 1" in text
    assert 'stockline_pool_connections{state="created"} 1' in text
    assert 'stockline_pool_connections{state="idle"} 1' in text
    assert r.headers.get("content-security-policy") == security.DEFAULT_CSP


def test_metrics_reads_the_current_pool_and_never_opens_a_connection(client):
    client.get("/health")
    assert 'stockline_pool_connections{state="created"} 1' in client.get("/metrics").text
    deps.reset_state(seed=False)
    text = client.get("/metrics").text
    assert 'stockline_pool_connections{state="created"} 0' in text
    assert 'stockline_pool_connections{state="idle"} 0' in text
    assert deps.pool.stats["created"] == 0
    observability.METRICS.reset()
    assert "stockline_requests_total{" not in client.get("/metrics").text


# --------------------------------------------------------------------------- OpenAPI
def test_system_routes_are_documented(client):
    spec = client.get("/openapi.json").json()
    ops = {
        ("get", "/health"),
        ("get", "/integrity"),
        ("post", "/integrity/rebuild"),
        ("get", "/metrics"),
    }
    for method, path in ops:
        op = spec["paths"][path][method]
        assert op.get("summary"), (method, path)
        assert op.get("tags") == ["system"], (method, path)
    schemas = spec["components"]["schemas"]
    for name in ("Health", "IntegrityReport", "RebuildReport", "ErrorBody"):
        assert name in schemas, name
    assert set(schemas["Health"]["properties"]) == {"status", "version", "schema_version", "runtime", "uptime_s"}
    assert {"ok", "checked", "schema_version", "mismatches", "negative", "chain_breaks", "order_total_mismatches", "missing_balance_after"} <= set(
        schemas["IntegrityReport"]["properties"]
    )
    assert {"checked", "fixed", "backfilled", "ok"} <= set(schemas["RebuildReport"]["properties"])
    assert "text/plain" in spec["paths"]["/metrics"]["get"]["responses"]["200"]["content"]
    rebuild = spec["paths"]["/integrity/rebuild"]["post"]["responses"]
    assert "401" in rebuild and "409" in rebuild
