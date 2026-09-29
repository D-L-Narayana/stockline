"""The browser demo router must behave like the HTTP API."""
import json
import pytest
from app import bridge, db


@pytest.fixture()
def br(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "b.db"))
    bridge._conn = None
    yield bridge
    bridge._conn = None


def test_health_and_seed(br):
    assert br.handle("GET", "/health")["status"] == 200
    assert len(br.handle("GET", "/stores")["body"]) == 3
    assert br.handle("GET", "/products?limit=5")["body"]["total"] == 12


def test_order_idempotency_and_409(br):
    body = json.dumps({"store_id": 1, "lines": [{"product_id": 1, "quantity": 1}]})
    r1 = br.handle("POST", "/orders", body, {"Idempotency-Key": "abc"})
    r2 = br.handle("POST", "/orders", body, {"Idempotency-Key": "abc"})
    assert (r1["status"], r2["status"]) == (201, 200)
    assert r1["body"]["id"] == r2["body"]["id"]
    assert r2["headers"]["Idempotent-Replayed"] == "true"
    big = json.dumps({"store_id": 1, "lines": [{"product_id": 1, "quantity": 999}]})
    assert br.handle("POST", "/orders", big)["status"] == 409


def test_validation_and_404(br):
    assert br.handle("POST", "/orders", json.dumps({"store_id": 1, "lines": []}))["status"] == 422
    assert br.handle("POST", "/orders", "{not json")["status"] == 422
    assert br.handle("GET", "/nope")["status"] == 404
    assert br.handle("GET", "/orders/9999")["status"] == 404


def test_adjust_transfer_reports(br):
    r = br.handle("POST", "/inventory/1/1/adjust", json.dumps({"delta": 5, "reason": "receipt"}))
    assert r["status"] == 200 and r["body"]["on_hand"] > 0
    stale = br.handle("POST", "/inventory/1/1/adjust", json.dumps({"delta": 1, "reason": "receipt", "expected_version": 0}))
    assert stale["status"] == 409
    t = br.handle("POST", "/transfers", json.dumps({"from_store_id": 1, "to_store_id": 2, "product_id": 1, "quantity": 1}))
    assert t["status"] == 200 and set(t["body"]) == {"from", "to"}
    assert br.handle("GET", "/reports/reorder")["status"] == 200
    assert br.handle("GET", "/integrity")["body"]["ok"] is True
    assert br.handle("GET", "/inventory?low_stock=true&store_id=1")["status"] == 200
    assert br.handle("GET", "/inventory/1/1/movements")["status"] == 200
    assert br.handle("POST", "/orders/1/cancel")["status"] in (200, 409)


def test_handle_json_roundtrip(br):
    out = json.loads(br.handle_json("GET", "/health", None, json.dumps({"X-Test": "1"})))
    assert out["status"] == 200 and out["body"]["runtime"] == "pyodide"
