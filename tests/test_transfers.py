"""Transfers as idempotent entities: ``POST/GET /transfers`` on the inventory router and its bridge handlers.

The router is mounted on a private FastAPI app (the v0.1 ``app.main`` is untouched during this phase);
stores, products and opening stock are created on a direct connection to the same temporary database.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import common, db, deps, inventory, schemas
from app.common import BridgeCall, ServiceError
from app.routers.inventory import router


@dataclass
class World:
    client: TestClient
    conn: sqlite3.Connection
    s1: int
    s2: int
    s3: int
    p1: int
    p2: int
    p3: int


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(ServiceError, deps.service_error_handler)
    return app


def _store(conn: sqlite3.Connection, code: str) -> int:
    return int(conn.execute("INSERT INTO stores (code, name, region) VALUES (?,?,?)", (code, f"Store {code}", "South")).lastrowid)


def _product(conn: sqlite3.Connection, sku: str, name: str, price_cents: int, reorder_point: int = 5) -> int:
    cur = conn.execute(
        "INSERT INTO products (sku, name, category, price_cents, reorder_point) VALUES (?,?,?,?,?)", (sku, name, "Home", price_cents, reorder_point)
    )
    return int(cur.lastrowid)


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _on_hand(conn: sqlite3.Connection, store_id: int, product_id: int) -> int:
    row = conn.execute("SELECT on_hand FROM inventory WHERE store_id=? AND product_id=?", (store_id, product_id)).fetchone()
    return int(row[0]) if row else 0


def _ok(resp, status: int = 200) -> dict:
    assert resp.status_code == status, f"{resp.request.method} {resp.url} -> {resp.status_code}: {resp.text}"
    return resp.json()


@pytest.fixture()
def world(tmp_path, monkeypatch):
    path = str(tmp_path / "transfers.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    deps.reset_state(seed=False)
    conn = db.connect(path)
    db.init_schema(conn)
    s1, s2, s3 = (_store(conn, code) for code in ("S1", "S2", "S3"))
    p1 = _product(conn, "SKU-1", "Widget", 1000, 5)
    p2 = _product(conn, "SKU-2", "Gadget", 2500, 2)
    p3 = _product(conn, "SKU-3", "Gizmo", 700, 1)
    for store_id, product_id, qty in ((s1, p1, 20), (s1, p2, 3), (s2, p1, 7)):
        inventory.adjust_stock(conn, store_id, product_id, schemas.StockAdjust(delta=qty, reason="receipt", reference="opening-stock"))
    with TestClient(_make_app()) as client:
        yield World(client, conn, s1, s2, s3, p1, p2, p3)
    conn.close()
    deps.reset_state(seed=False)


def _transfer(client: TestClient, from_store: int, to_store: int, product_id: int, quantity: int, key: str | None = None):
    headers = {"Idempotency-Key": key} if key is not None else {}
    body = {"from_store_id": from_store, "to_store_id": to_store, "product_id": product_id, "quantity": quantity}
    return client.post("/transfers", json=body, headers=headers)


def _bridge(method: str, path: str):
    matches = [(pattern, handler) for m, pattern, handler in inventory.BRIDGE_ROUTES if m == method and re.fullmatch(pattern, path)]
    assert len(matches) == 1, f"expected exactly one bridge route for {method} {path}, found {len(matches)}"
    return matches[0]


def _params(pattern: str, path: str) -> dict[str, int]:
    match = re.fullmatch(pattern, path)
    assert match is not None
    return {k: int(v) for k, v in match.groupdict().items()}


# --------------------------------------------------------------------------- create
def test_transfer_creates_entity_with_ledger_references(world):
    r = _transfer(world.client, world.s1, world.s2, world.p1, 5)
    body = _ok(r, 201)
    assert r.headers["Idempotent-Replayed"] == "false"
    assert {"id", "from_store_id", "to_store_id", "product_id", "quantity", "idempotency_key", "created_at", "from", "to"} <= set(body)
    assert "request_hash" not in body
    assert (body["from_store_id"], body["to_store_id"], body["product_id"], body["quantity"]) == (world.s1, world.s2, world.p1, 5)
    assert body["idempotency_key"] is None
    assert body["from"]["on_hand"] == 15 and body["to"]["on_hand"] == 12
    assert body["from"]["value_cents"] == 15 * 1000 and body["to"]["value_cents"] == 12 * 1000
    assert body["from"]["store_code"] == "S1" and body["to"]["store_code"] == "S2"
    reference = f"transfer:{body['id']}"
    rows = world.conn.execute(
        "SELECT store_id, delta, reason, reference, balance_after FROM stock_movements WHERE reference=? ORDER BY id", (reference,)
    ).fetchall()
    assert [tuple(row) for row in rows] == [(world.s1, -5, "transfer_out", reference, 15), (world.s2, 5, "transfer_in", reference, 12)]
    stored = world.conn.execute("SELECT * FROM transfers WHERE id=?", (body["id"],)).fetchone()
    assert stored["quantity"] == 5 and stored["idempotency_key"] is None and stored["created_at"] == body["created_at"]


def test_transfer_replay_returns_200_and_writes_nothing(world):
    first = _ok(_transfer(world.client, world.s1, world.s2, world.p1, 4, key="xfer-1"), 201)
    assert first["idempotency_key"] == "xfer-1"
    moves = _count(world.conn, "stock_movements")
    r = _transfer(world.client, world.s1, world.s2, world.p1, 4, key="xfer-1")
    second = _ok(r, 200)
    assert r.headers["Idempotent-Replayed"] == "true"
    assert second["id"] == first["id"] and second["quantity"] == 4
    assert second["from"]["on_hand"] == 16 and second["to"]["on_hand"] == 11
    assert _count(world.conn, "stock_movements") == moves
    assert _count(world.conn, "transfers") == 1
    row = world.conn.execute("SELECT request_hash FROM transfers WHERE idempotency_key='xfer-1'").fetchone()
    assert row["request_hash"] == common.request_hash({"from": world.s1, "to": world.s2, "product_id": world.p1, "quantity": 4})
    assert "request_hash" not in second


def test_transfer_key_reuse_with_different_body_is_422(world):
    _ok(_transfer(world.client, world.s1, world.s2, world.p1, 4, key="xfer-2"), 201)
    bad = _transfer(world.client, world.s1, world.s2, world.p1, 5, key="xfer-2")
    assert bad.status_code == 422
    assert bad.json() == {"detail": "Idempotency-Key reused with a different request body", "code": "idempotency_key_reuse"}
    # the opposite direction with the same key is a different body too
    assert _transfer(world.client, world.s2, world.s1, world.p1, 4, key="xfer-2").status_code == 422
    assert _count(world.conn, "transfers") == 1
    assert _on_hand(world.conn, world.s1, world.p1) == 16 and _on_hand(world.conn, world.s2, world.p1) == 11


def test_transfer_replay_accepts_legacy_row_without_fingerprint(world):
    first = _ok(_transfer(world.client, world.s1, world.s2, world.p1, 2, key="xfer-3"), 201)
    world.conn.execute("UPDATE transfers SET request_hash = NULL WHERE id=?", (first["id"],))
    again = _transfer(world.client, world.s1, world.s2, world.p1, 9, key="xfer-3")  # body differs, but nothing to compare against
    body = _ok(again, 200)
    assert again.headers["Idempotent-Replayed"] == "true"
    assert body["id"] == first["id"] and body["quantity"] == 2
    assert _on_hand(world.conn, world.s1, world.p1) == 18


def test_transfer_failure_is_atomic_and_leaves_no_entity(world):
    _ok(_transfer(world.client, world.s1, world.s2, world.p1, 5), 201)
    before = (_count(world.conn, "transfers"), _count(world.conn, "stock_movements"))
    r = _transfer(world.client, world.s1, world.s2, world.p1, 100, key="xfer-fail")
    assert r.status_code == 409
    assert r.json()["code"] == "insufficient_stock"
    assert "have 15, need 100" in r.json()["detail"]
    assert (_count(world.conn, "transfers"), _count(world.conn, "stock_movements")) == before
    assert world.conn.execute("SELECT COUNT(*) FROM transfers WHERE idempotency_key='xfer-fail'").fetchone()[0] == 0
    assert _on_hand(world.conn, world.s1, world.p1) == 15 and _on_hand(world.conn, world.s2, world.p1) == 12
    # the key was never consumed, so a feasible retry with the same key creates a transfer
    retry = _ok(_transfer(world.client, world.s1, world.s2, world.p1, 1, key="xfer-fail"), 201)
    assert retry["quantity"] == 1 and retry["from"]["on_hand"] == 14


def test_transfer_validation_and_not_found_codes(world):
    same = _transfer(world.client, world.s1, world.s1, world.p1, 1)
    assert same.status_code == 422
    assert same.json() == {"detail": "from_store_id and to_store_id must differ", "code": "validation_error"}
    missing_from = _transfer(world.client, 999, world.s2, world.p1, 1)
    assert missing_from.status_code == 404 and missing_from.json() == {"detail": "store 999 not found", "code": "not_found"}
    assert _transfer(world.client, world.s1, 998, world.p1, 1).status_code == 404
    missing_product = _transfer(world.client, world.s1, world.s2, 997, 1)
    assert missing_product.status_code == 404 and missing_product.json()["code"] == "not_found"
    assert _transfer(world.client, world.s1, world.s2, world.p1, 0).status_code == 422
    assert _transfer(world.client, world.s1, world.s2, world.p1, 1, key="k" * 65).status_code == 422
    assert _count(world.conn, "transfers") == 0
    assert _ok(_transfer(world.client, world.s1, world.s2, world.p1, 1, key="k" * 64), 201)["idempotency_key"] == "k" * 64


# --------------------------------------------------------------------------- read
def test_get_transfer_and_list_filters(world):
    a = _ok(_transfer(world.client, world.s1, world.s2, world.p1, 5), 201)
    b = _ok(_transfer(world.client, world.s2, world.s3, world.p1, 2), 201)
    c = _ok(_transfer(world.client, world.s1, world.s3, world.p2, 1), 201)
    got = _ok(world.client.get(f"/transfers/{a['id']}"))
    assert got["id"] == a["id"] and got["from"]["store_id"] == world.s1 and got["to"]["store_id"] == world.s2
    assert got["from"]["on_hand"] == 15 and got["to"]["on_hand"] == 10  # S2 received 5, then sent 2 on to S3
    assert "request_hash" not in got
    missing = world.client.get("/transfers/999")
    assert missing.status_code == 404 and missing.json() == {"detail": "transfer 999 not found", "code": "not_found"}

    page = _ok(world.client.get("/transfers"))
    assert (page["total"], page["limit"], page["offset"]) == (3, 20, 0)
    assert [t["id"] for t in page["items"]] == [c["id"], b["id"], a["id"]]  # newest first
    assert page["items"][1]["to"] == {
        "store_id": world.s3,
        "store_code": "S3",
        "product_id": world.p1,
        "sku": "SKU-1",
        "name": "Widget",
        "on_hand": 2,
        "reorder_point": 5,
        "version": 1,
        "below_reorder": True,
        "price_cents": 1000,
        "value_cents": 2000,
    }
    either_side = _ok(world.client.get("/transfers", params={"store_id": world.s2}))
    assert either_side["total"] == 2 and [t["id"] for t in either_side["items"]] == [b["id"], a["id"]]
    by_product = _ok(world.client.get("/transfers", params={"product_id": world.p2}))
    assert [t["id"] for t in by_product["items"]] == [c["id"]]
    both = _ok(world.client.get("/transfers", params={"store_id": world.s3, "product_id": world.p1}))
    assert [t["id"] for t in both["items"]] == [b["id"]]
    paged = _ok(world.client.get("/transfers", params={"limit": 1, "offset": 1}))
    assert [t["id"] for t in paged["items"]] == [b["id"]] and paged["total"] == 3 and paged["limit"] == 1
    for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"store_id": "abc"}):
        assert world.client.get("/transfers", params=params).status_code == 422
    assert _ok(world.client.get("/transfers", params={"limit": 100}))["limit"] == 100


# --------------------------------------------------------------------------- module function
def test_transfer_module_function_returns_created_flag_and_guards_lost_races(world, monkeypatch):
    t = schemas.TransferIn(from_store_id=world.s1, to_store_id=world.s2, product_id=world.p1, quantity=3)
    out, created = inventory.transfer(world.conn, t, "mod-key")
    assert created is True and out["quantity"] == 3 and out["idempotency_key"] == "mod-key" and out["from"]["on_hand"] == 17
    replay, created_again = inventory.transfer(world.conn, t, "mod-key")
    assert created_again is False and replay["id"] == out["id"]
    plain, created_plain = inventory.transfer(world.conn, t)  # no key: always a new entity
    assert created_plain is True and plain["id"] != out["id"] and plain["idempotency_key"] is None
    assert world.conn.execute("SELECT request_hash FROM transfers WHERE id=?", (plain["id"],)).fetchone()[0] is None
    # race lost to an *identical* request: the fast path saw no row, the re-check inside the write lock finds it -> replay, no writes
    real_lookup = inventory._transfer_by_key
    lookups: list[str] = []

    def racing_lookup(conn, key):
        lookups.append(key)
        return None if len(lookups) == 1 else real_lookup(conn, key)

    monkeypatch.setattr(inventory, "_transfer_by_key", racing_lookup)
    moves = world.conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0]
    raced, created_raced = inventory.transfer(world.conn, t, "mod-key")
    assert created_raced is False and raced["id"] == out["id"] and lookups == ["mod-key", "mod-key"]
    assert world.conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0] == moves
    # race lost between the in-lock check and the insert (key never visible to us) -> defensive 409, nothing written
    monkeypatch.setattr(inventory, "_transfer_by_key", lambda conn, key: None)
    with pytest.raises(ServiceError) as exc:
        inventory.transfer(world.conn, t, "mod-key")
    assert (exc.value.status, exc.value.code) == (409, "idempotency_conflict")
    assert world.conn.in_transaction is False
    assert _on_hand(world.conn, world.s1, world.p1) == 14  # neither failed attempt moved anything
    with pytest.raises(ServiceError) as exc:
        inventory.get_transfer(world.conn, 12345)
    assert (exc.value.status, exc.value.code) == (404, "not_found")


# --------------------------------------------------------------------------- bridge
def test_bridge_transfer_handlers_match_router_semantics(world):
    _, create = _bridge("POST", "/transfers")
    body = {"from_store_id": world.s1, "to_store_id": world.s2, "product_id": world.p1, "quantity": 2}
    call = BridgeCall(conn=world.conn, body=body, headers={"idempotency-key": "bridge-1"})
    status, out = create(call)
    assert status == 201 and call.out_headers["Idempotent-Replayed"] == "false" and out["quantity"] == 2
    replay = BridgeCall(conn=world.conn, body=body, headers={"idempotency-key": "bridge-1"})
    status, again = create(replay)
    assert status == 200 and replay.out_headers["Idempotent-Replayed"] == "true" and again["id"] == out["id"]
    with pytest.raises(ServiceError) as exc:
        create(BridgeCall(conn=world.conn, body=body, headers={"idempotency-key": "k" * 65}))
    assert (exc.value.status, exc.value.code) == (422, "validation_error")
    with pytest.raises(ValidationError):
        create(BridgeCall(conn=world.conn, body={"from_store_id": world.s1}))
    with pytest.raises(ServiceError) as exc:
        create(BridgeCall(conn=world.conn, body={**body, "to_store_id": world.s1}))
    assert exc.value.status == 422

    pattern, get_one = _bridge("GET", f"/transfers/{out['id']}")
    status, fetched = get_one(BridgeCall(conn=world.conn, params=_params(pattern, f"/transfers/{out['id']}")))
    assert status == 200 and fetched["id"] == out["id"] and "request_hash" not in fetched

    _, list_handler = _bridge("GET", "/transfers")
    status, page = list_handler(BridgeCall(conn=world.conn, query={"store_id": str(world.s2)}))
    assert status == 200 and page["total"] == 1 and page["limit"] == 20 and page["offset"] == 0
    status, empty = list_handler(BridgeCall(conn=world.conn, query={"product_id": str(world.p3), "limit": "100"}))
    assert status == 200 and empty["items"] == [] and empty["limit"] == 100
    for query in ({"limit": "0"}, {"limit": "101"}, {"offset": "-1"}, {"limit": "abc"}):
        with pytest.raises(ServiceError) as exc:
            list_handler(BridgeCall(conn=world.conn, query=query))
        assert (exc.value.status, exc.value.code) == (422, "validation_error")
