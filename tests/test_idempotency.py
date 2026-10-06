"""Idempotency semantics of ``POST /orders`` (PLAN §4.7) on a private router app and through ``app.orders`` directly.

Replays return the stored order (200 + ``Idempotent-Replayed: true``) and write nothing; the same key with a
different body is a 422 ``idempotency_key_reuse``; legacy rows without a fingerprint replay unconditionally;
the key is re-checked inside the write lock so a concurrent retry can never reserve stock twice.
"""
from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import common, db, deps, ledger, orders
from app.common import IDEMPOTENCY_CONFLICT, IDEMPOTENCY_KEY_REUSE, ServiceError
from app.routers import orders as orders_router
from app.schemas import OrderIn

MISMATCH = "Idempotency-Key reused with a different request body"
OLD = "2020-01-01T00:00:00.000Z"


@dataclass
class Env:
    """A private app over a temporary database plus a direct connection for raw SQL assertions."""

    client: TestClient
    conn: sqlite3.Connection
    path: str
    s1: int = 1
    s2: int = 2
    p1: int = 1
    p2: int = 2


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(orders_router.router)
    app.add_exception_handler(ServiceError, deps.service_error_handler)
    return app


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO stores (code, name, region) VALUES ('S1', 'Store One', 'South'), ('S2', 'Store Two', 'West')")
    conn.execute(
        "INSERT INTO products (sku, name, category, price_cents, reorder_point) VALUES "
        "('SKU-1', 'Widget', 'Home', 1000, 5), ('SKU-2', 'Gadget', 'Electronics', 2500, 2)"
    )
    with db.transaction(conn):
        ledger.apply_movement(conn, 1, 1, 20, "receipt", "opening")
        ledger.apply_movement(conn, 1, 2, 3, "receipt", "opening")
        ledger.apply_movement(conn, 2, 1, 10, "receipt", "opening")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = str(tmp_path / "idem.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    conn = db.connect(path)
    db.migrate(conn)
    _seed(conn)
    deps.reset_state(seed=False)
    yield Env(client=TestClient(_make_app()), conn=conn, path=path)
    conn.close()
    deps.reset_state(seed=False)


def _order(env: Env, q1: int = 2, q2: int = 1, store_id: int | None = None) -> dict:
    return {
        "store_id": env.s1 if store_id is None else store_id,
        "lines": [{"product_id": env.p1, "quantity": q1}, {"product_id": env.p2, "quantity": q2}],
    }


def _single(env: Env, qty: int = 1) -> dict:
    return {"store_id": env.s1, "lines": [{"product_id": env.p1, "quantity": qty}]}


def _place(env: Env, body: dict, key: str | None = None) -> dict:
    headers = {"Idempotency-Key": key} if key is not None else {}
    r = env.client.post("/orders", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _on_hand(conn: sqlite3.Connection, store_id: int, product_id: int) -> int:
    row = conn.execute("SELECT on_hand FROM inventory WHERE store_id=? AND product_id=?", (store_id, product_id)).fetchone()
    return row[0] if row else 0


def _count(conn: sqlite3.Connection, table: str, where: str = "1=1", params: tuple = ()) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]


def _snapshot(conn: sqlite3.Connection) -> tuple[int, list[tuple]]:
    """Everything a replay must leave untouched: the ledger length and every cached balance/version."""
    movements = _count(conn, "stock_movements")
    inventory = [tuple(r) for r in conn.execute("SELECT store_id, product_id, on_hand, version FROM inventory ORDER BY 1, 2")]
    return movements, inventory


def _arm_race(monkeypatch, path: str, competitor_body: dict, key: str) -> sqlite3.Connection:
    """Make the next ``orders.transaction`` call first commit ``competitor_body`` under ``key`` on another connection.

    This recreates the window between the lock-free fast path and ``BEGIN IMMEDIATE`` deterministically: the
    caller's fast path saw no row, yet by the time it holds the write lock the key exists.
    """
    real = orders.transaction
    other = db.connect(path)
    armed = [True]

    @contextlib.contextmanager
    def racing(conn):
        if armed[0]:
            armed[0] = False
            orders.place_order(other, OrderIn.model_validate(competitor_body), key)
        with real(conn):
            yield conn

    monkeypatch.setattr(orders, "transaction", racing)
    return other


# --------------------------------------------------------------------------- replays
def test_replay_returns_200_with_header_and_writes_nothing(env):
    a = env.client.post("/orders", json=_order(env), headers={"Idempotency-Key": "abc-123"})
    assert a.status_code == 201 and a.headers["Idempotent-Replayed"] == "false"
    snap = _snapshot(env.conn)
    b = env.client.post("/orders", json=_order(env), headers={"Idempotency-Key": "abc-123"})
    assert b.status_code == 200 and b.headers["Idempotent-Replayed"] == "true"
    assert b.json() == a.json()
    assert _snapshot(env.conn) == snap
    assert _count(env.conn, "orders") == 1 and _on_hand(env.conn, 1, 1) == 18
    stored = env.conn.execute("SELECT request_hash FROM orders WHERE id = ?", (a.json()["id"],)).fetchone()[0]
    assert stored == common.request_hash({"store_id": env.s1, "lines": [[env.p1, 2], [env.p2, 1]]})


def test_replay_ignores_the_order_of_lines(env):
    body = _order(env)
    shuffled = {"store_id": body["store_id"], "lines": list(reversed(body["lines"]))}
    a = env.client.post("/orders", json=body, headers={"Idempotency-Key": "k-order"})
    b = env.client.post("/orders", json=shuffled, headers={"Idempotency-Key": "k-order"})
    assert (a.status_code, b.status_code) == (201, 200)
    assert b.json()["id"] == a.json()["id"] and b.headers["Idempotent-Replayed"] == "true"
    assert _count(env.conn, "orders") == 1


def test_same_key_with_a_different_body_is_422_key_reuse(env):
    _place(env, _order(env), key="dup")
    snap = _snapshot(env.conn)
    variants = (
        _order(env, q1=3),
        _order(env, store_id=env.s2),
        {"store_id": env.s1, "lines": [{"product_id": env.p1, "quantity": 2}]},
    )
    for other in variants:
        r = env.client.post("/orders", json=other, headers={"Idempotency-Key": "dup"})
        assert r.status_code == 422, other
        assert r.json() == {"detail": MISMATCH, "code": IDEMPOTENCY_KEY_REUSE}
        assert "Idempotent-Replayed" not in r.headers
    assert _snapshot(env.conn) == snap and _count(env.conn, "orders") == 1


def test_replay_with_null_stored_hash_is_accepted_as_legacy(env):
    a = _place(env, _order(env), key="legacy")
    env.conn.execute("UPDATE orders SET request_hash = NULL WHERE id = ?", (a["id"],))  # a row written by v0.1 has no fingerprint
    snap = _snapshot(env.conn)
    r = env.client.post("/orders", json=_order(env, q1=5), headers={"Idempotency-Key": "legacy"})  # even a different body replays
    assert (r.status_code, r.headers["Idempotent-Replayed"]) == (200, "true")
    assert r.json() == a
    assert _snapshot(env.conn) == snap and _count(env.conn, "orders") == 1
    assert env.conn.execute("SELECT request_hash FROM orders WHERE id = ?", (a["id"],)).fetchone()[0] is None  # never backfilled


def test_replay_returns_the_current_state_of_the_order(env):
    a = _place(env, _order(env), key="later")
    env.conn.execute("UPDATE orders SET updated_at = ? WHERE id = ?", (OLD, a["id"]))
    assert env.client.post(f"/orders/{a['id']}/cancel").status_code == 200
    r = env.client.post("/orders", json=_order(env), headers={"Idempotency-Key": "later"})
    assert (r.status_code, r.headers["Idempotent-Replayed"]) == (200, "true")
    body = r.json()
    assert (body["id"], body["status"]) == (a["id"], "cancelled") and body["updated_at"] > OLD
    assert _on_hand(env.conn, 1, 1) == 20  # the replay did not reserve stock again


def test_fast_path_replay_runs_no_write_statements(env):
    body = _order(env)
    first = _place(env, body, key="fast")
    probe = db.connect(env.path)
    statements: list[str] = []
    probe.set_trace_callback(statements.append)
    order, created = orders.place_order(probe, OrderIn.model_validate(body), "fast")
    probe.set_trace_callback(None)
    probe.close()
    assert created is False and order == first
    assert statements and all(s.lstrip().upper().startswith("SELECT") for s in statements), statements


# --------------------------------------------------------------------------- the authoritative check inside the write lock
def test_recheck_inside_the_write_lock_replays_a_concurrent_winner(env, monkeypatch):
    body = _order(env)
    other = _arm_race(monkeypatch, env.path, body, "race-same")
    order, created = orders.place_order(env.conn, OrderIn.model_validate(body), "race-same")
    other.close()
    assert created is False
    assert _count(env.conn, "orders", "idempotency_key = ?", ("race-same",)) == 1
    assert order["id"] == env.conn.execute("SELECT id FROM orders WHERE idempotency_key = ?", ("race-same",)).fetchone()[0]
    assert _on_hand(env.conn, 1, 1) == 18 and _on_hand(env.conn, 1, 2) == 2  # reserved exactly once
    assert env.conn.in_transaction is False


def test_recheck_inside_the_write_lock_rejects_a_different_body(env, monkeypatch):
    other = _arm_race(monkeypatch, env.path, _order(env, q1=1), "race-diff")
    with pytest.raises(ServiceError) as ei:
        orders.place_order(env.conn, OrderIn.model_validate(_order(env, q1=2)), "race-diff")
    other.close()
    assert (ei.value.status, ei.value.code, str(ei.value)) == (422, IDEMPOTENCY_KEY_REUSE, MISMATCH)
    assert _on_hand(env.conn, 1, 1) == 19  # only the competitor's single unit
    assert _count(env.conn, "orders") == 1 and env.conn.in_transaction is False


def test_unique_violation_is_a_defensive_409_idempotency_conflict(env, monkeypatch):
    body = OrderIn.model_validate(_order(env))
    _, created = orders.place_order(env.conn, body, "unique")
    assert created is True
    monkeypatch.setattr(orders, "_find_replay", lambda conn, key, fingerprint: None)  # pretend neither check saw the row
    snap = _snapshot(env.conn)
    with pytest.raises(ServiceError) as ei:
        orders.place_order(env.conn, body, "unique")
    assert (ei.value.status, ei.value.code) == (409, IDEMPOTENCY_CONFLICT)
    assert _snapshot(env.conn) == snap and _count(env.conn, "orders") == 1 and env.conn.in_transaction is False


# --------------------------------------------------------------------------- the header itself
def test_key_longer_than_64_is_rejected_and_requests_without_key_are_independent(env):
    assert env.client.post("/orders", json=_single(env), headers={"Idempotency-Key": "k" * 65}).status_code == 422
    assert _count(env.conn, "orders") == 0
    assert env.client.post("/orders", json=_single(env), headers={"Idempotency-Key": "k" * 64}).status_code == 201
    a = env.client.post("/orders", json=_single(env))
    b = env.client.post("/orders", json=_single(env))
    assert (a.status_code, b.status_code) == (201, 201) and a.json()["id"] != b.json()["id"]
    assert a.json()["idempotency_key"] is None
    assert _count(env.conn, "orders") == 3 and _on_hand(env.conn, 1, 1) == 17


def test_key_is_global_not_per_store(env):
    _place(env, _single(env), key="shared")
    r = env.client.post("/orders", json={"store_id": env.s2, "lines": [{"product_id": env.p1, "quantity": 1}]}, headers={"Idempotency-Key": "shared"})
    assert (r.status_code, r.json()["code"]) == (422, IDEMPOTENCY_KEY_REUSE)
    assert _on_hand(env.conn, 2, 1) == 10
