"""Server-side glue in ``app.deps``: settings, connection pool, dependency, lifespan and error handler."""
from __future__ import annotations

import dataclasses
import sqlite3
import threading
import time

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from app import db, deps
from app.common import CONFLICT, NOT_FOUND, ServiceError, not_found


@pytest.fixture()
def dbpath(tmp_path, monkeypatch):
    path = str(tmp_path / "pool.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    for var in ("STOCKLINE_SEED", "STOCKLINE_POOL_SIZE", "STOCKLINE_POOL_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    yield path
    deps.reset_state(seed=False)  # leave a fresh, lazily-opened pool behind


# --------------------------------------------------------------------------- Settings
def test_settings_defaults():
    s = deps.Settings.from_env({})
    assert s == deps.Settings()
    assert (s.seed, s.pool_size, s.acquire_timeout, s.api_key, s.cors_origins) == (True, 8, 10.0, None, ())
    assert (s.log_level, s.log_format, s.csp, s.hsts) == ("INFO", "text", None, False)


def test_settings_parse_every_variable():
    s = deps.Settings.from_env(
        {
            "STOCKLINE_SEED": "0",
            "STOCKLINE_POOL_SIZE": "3",
            "STOCKLINE_POOL_TIMEOUT": "0.5",
            "STOCKLINE_API_KEY": "s3cret",
            "STOCKLINE_CORS_ORIGINS": " https://a.example , ,https://b.example ",
            "STOCKLINE_LOG_LEVEL": "debug",
            "STOCKLINE_LOG_FORMAT": "json",
            "STOCKLINE_CSP": "default-src 'none'",
            "STOCKLINE_HSTS": "1",
        }
    )
    assert s.seed is False
    assert s.pool_size == 3
    assert s.acquire_timeout == 0.5
    assert s.api_key == "s3cret"
    assert s.cors_origins == ("https://a.example", "https://b.example")
    assert s.log_level == "DEBUG"
    assert s.log_format == "json"
    assert s.csp == "default-src 'none'"
    assert s.hsts is True


def test_settings_bad_values_fall_back_to_defaults():
    s = deps.Settings.from_env(
        {
            "STOCKLINE_SEED": "0",
            "STOCKLINE_POOL_SIZE": "lots",
            "STOCKLINE_POOL_TIMEOUT": "soon",
            "STOCKLINE_LOG_FORMAT": "xml",
            "STOCKLINE_API_KEY": "",
            "STOCKLINE_CORS_ORIGINS": " , ,",
            "STOCKLINE_CSP": "",
            "STOCKLINE_HSTS": "maybe",
        }
    )
    assert (s.pool_size, s.acquire_timeout, s.log_format) == (8, 10.0, "text")
    assert (s.api_key, s.cors_origins, s.csp, s.hsts, s.seed) == (None, (), None, False, False)
    s2 = deps.Settings.from_env({"STOCKLINE_POOL_SIZE": "0", "STOCKLINE_POOL_TIMEOUT": "-1"})
    assert (s2.pool_size, s2.acquire_timeout) == (8, 10.0)
    assert deps.Settings.from_env({"STOCKLINE_POOL_TIMEOUT": "0"}).acquire_timeout == 0.0
    assert deps.Settings.from_env({"STOCKLINE_SEED": "true", "STOCKLINE_HSTS": "yes"}) == dataclasses.replace(deps.Settings(), hsts=True)


def test_settings_from_os_environ_and_frozen(monkeypatch):
    monkeypatch.setenv("STOCKLINE_POOL_SIZE", "5")
    monkeypatch.setenv("STOCKLINE_SEED", "0")
    s = deps.Settings.from_env()
    assert (s.pool_size, s.seed) == (5, False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.pool_size = 9  # type: ignore[misc]


# --------------------------------------------------------------------------- ConnectionPool
def test_pool_hands_out_distinct_connections_and_reports_stats(dbpath):
    pool = deps.ConnectionPool(size=2, seed=False)
    assert pool.stats == {"size": 2, "created": 0, "idle": 0}
    a = pool.acquire()
    b = pool.acquire()
    assert a is not b
    assert pool.stats == {"size": 2, "created": 2, "idle": 0}
    pool.release(a)
    pool.release(b)
    assert pool.stats == {"size": 2, "created": 2, "idle": 2}
    assert pool.acquire() is b  # LIFO: the most recently released connection is reused first
    pool.close_all()


def test_pool_reads_db_path_lazily(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "old.db"))
    pool = deps.ConnectionPool(size=1, seed=False)
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "new.db"))
    conn = pool.acquire()
    conn.execute("SELECT 1")
    assert (tmp_path / "new.db").exists()
    assert not (tmp_path / "old.db").exists()
    pool.release(conn)
    pool.close_all()


def test_pool_initialises_schema_and_seeds_exactly_once(dbpath, monkeypatch):
    calls: list[int] = []
    real_init = db.init_schema

    def counting_init(conn: sqlite3.Connection) -> int:
        calls.append(1)
        return real_init(conn)

    monkeypatch.setattr(db, "init_schema", counting_init)
    pool = deps.ConnectionPool(size=3, seed=True)
    conns = [pool.acquire() for _ in range(3)]
    assert calls == [1]
    assert conns[0].execute("SELECT COUNT(*) FROM stores").fetchone()[0] == 3
    assert conns[0].execute("SELECT COUNT(*) FROM products").fetchone()[0] == 12
    assert db.schema_version(conns[0]) == 2
    for c in conns:
        pool.release(c)
    pool.close_all()

    unseeded = deps.ConnectionPool(size=1, seed=False)
    monkeypatch.setattr(db, "DB_PATH", dbpath + ".2")
    c = unseeded.acquire()
    assert c.execute("SELECT COUNT(*) FROM stores").fetchone()[0] == 0
    assert db.schema_version(c) == 2
    unseeded.release(c)
    unseeded.close_all()


def test_pool_rolls_back_dirty_connections_on_release(dbpath):
    pool = deps.ConnectionPool(size=1, seed=False)
    conn = pool.acquire()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO stores (code, name, region) VALUES ('ZZ', 'Zed', 'North')")
    assert conn.in_transaction is True
    pool.release(conn)
    assert conn.in_transaction is False
    again = pool.acquire()
    assert again is conn
    assert again.execute("SELECT COUNT(*) FROM stores").fetchone()[0] == 0
    pool.release(again)
    pool.close_all()


def test_pool_exhaustion_raises_after_timeout(dbpath):
    pool = deps.ConnectionPool(size=1, seed=False, acquire_timeout=0.1)
    held = pool.acquire()
    t0 = time.perf_counter()
    with pytest.raises(deps.PoolExhausted):
        pool.acquire()
    elapsed = time.perf_counter() - t0
    assert 0.05 <= elapsed < 2.0
    assert issubclass(deps.PoolExhausted, RuntimeError)
    pool.release(held)
    assert pool.acquire() is held
    pool.release(held)
    pool.close_all()


def test_pool_close_all_is_idempotent_and_closes_idle_connections(dbpath):
    pool = deps.ConnectionPool(size=2, seed=False)
    a = pool.acquire()
    b = pool.acquire()
    pool.release(a)
    pool.release(b)
    pool.close_all()
    assert pool.stats["idle"] == 0
    for c in (a, b):
        with pytest.raises(sqlite3.ProgrammingError):
            c.execute("SELECT 1")
    pool.close_all()  # second call is a no-op
    fresh = pool.acquire()  # a closed pool re-opens lazily
    assert fresh.execute("SELECT 1").fetchone()[0] == 1
    pool.release(fresh)
    pool.close_all()
    assert pool.stats == {"size": 2, "created": 0, "idle": 0}


def test_pool_is_thread_safe(dbpath):
    pool = deps.ConnectionPool(size=4, seed=False, acquire_timeout=5.0)
    errors: list[Exception] = []

    def work() -> None:
        try:
            for _ in range(20):
                c = pool.acquire()
                c.execute("SELECT 1")
                pool.release(c)
        except Exception as exc:  # collected for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert 1 <= pool.stats["created"] <= 4
    assert pool.stats["idle"] == pool.stats["created"]
    pool.close_all()


# --------------------------------------------------------------------------- module-level state
def test_reset_state_reads_environment_at_call_time(dbpath, monkeypatch):
    monkeypatch.setenv("STOCKLINE_SEED", "0")
    deps.reset_state()
    assert deps.pool.seed is False
    monkeypatch.setenv("STOCKLINE_SEED", "1")
    deps.reset_state()
    assert deps.pool.seed is True
    deps.reset_state(seed=False)  # explicit argument wins over the environment
    assert deps.pool.seed is False
    monkeypatch.setenv("STOCKLINE_POOL_SIZE", "3")
    monkeypatch.setenv("STOCKLINE_POOL_TIMEOUT", "0.25")
    deps.reset_state(seed=False)
    assert deps.pool.stats["size"] == 3
    assert deps.pool.acquire_timeout == 0.25


def test_reset_state_closes_the_previous_pool(dbpath):
    deps.reset_state(seed=False)
    old = deps.pool
    conn = old.acquire()
    old.release(conn)
    deps.reset_state(seed=False)
    assert deps.pool is not old
    assert old.stats["idle"] == 0
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    assert deps.pool.stats == {"size": 8, "created": 0, "idle": 0}


def test_reset_state_seed_true_seeds_the_database(dbpath):
    deps.reset_state(seed=True)
    conn = deps.pool.acquire()
    assert conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0] == 3
    deps.pool.release(conn)


def _app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(ServiceError, deps.service_error_handler)

    @app.get("/ping")
    def ping(conn: sqlite3.Connection = Depends(deps.get_conn)):
        return {"one": conn.execute("SELECT 1").fetchone()[0]}

    @app.get("/dirty")
    def dirty(conn: sqlite3.Connection = Depends(deps.get_conn)):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO stores (code, name, region) VALUES ('ZZ', 'Zed', 'North')")
        return {"in_transaction": conn.in_transaction}

    @app.get("/boom")
    def boom():
        raise ServiceError(409, "boom", CONFLICT)

    @app.get("/boom-with-request-id")
    def boom_with_request_id(request: Request):
        request.state.request_id = "req-123"
        raise not_found("order", 5)

    @app.get("/legacy")
    def legacy():
        raise ServiceError(404, "no inventory record")

    return app


def test_get_conn_dependency_checks_out_and_releases(dbpath):
    deps.reset_state(seed=False)
    client = TestClient(_app())
    assert client.get("/ping").json() == {"one": 1}
    assert deps.pool.stats.get("created") == 1
    assert deps.pool.stats.get("idle") == 1
    assert client.get("/dirty").json() == {"in_transaction": True}
    assert deps.pool.stats.get("idle") == 1
    conn = deps.pool.acquire()
    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0] == 0
    deps.pool.release(conn)


def test_service_error_handler_bodies(dbpath):
    deps.reset_state(seed=False)
    client = TestClient(_app())
    r = client.get("/boom")
    assert r.status_code == 409
    assert r.json() == {"detail": "boom", "code": "conflict"}
    r = client.get("/boom-with-request-id")
    assert r.status_code == 404
    assert r.json() == {"detail": "order 5 not found", "code": NOT_FOUND, "request_id": "req-123"}
    r = client.get("/legacy")
    assert r.status_code == 404
    assert r.json() == {"detail": "no inventory record", "code": "error"}


def test_lifespan_closes_the_current_pool(dbpath):
    app = FastAPI(lifespan=deps.lifespan)

    @app.get("/ping")
    def ping(conn: sqlite3.Connection = Depends(deps.get_conn)):
        return {"one": conn.execute("SELECT 1").fetchone()[0]}

    deps.reset_state(seed=False)
    first = deps.pool
    with TestClient(app) as client:
        assert client.get("/ping").status_code == 200
        assert first.stats["idle"] == 1
        held = first.acquire()
        first.release(held)
        deps.reset_state(seed=False)  # swap the global while the app is running
        second = deps.pool
        conn2 = second.acquire()
        second.release(conn2)
        assert second.stats["idle"] == 1
        assert client.get("/ping").status_code == 200
    # shutdown closed the pool that was current at that moment
    assert second.stats["idle"] == 0
    with pytest.raises(sqlite3.ProgrammingError):
        conn2.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        held.execute("SELECT 1")
