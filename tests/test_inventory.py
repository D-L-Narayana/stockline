def test_adjust_creates_ledger_and_balance(seeded, client):
    s, p = seeded["s1"]["id"], seeded["p1"]["id"]
    inv = client.get(f"/inventory/{s}/{p}").json()
    assert inv["on_hand"] == 20 and inv["version"] == 1 and inv["below_reorder"] is False
    mv = client.get(f"/inventory/{s}/{p}/movements").json()
    assert len(mv) == 1 and mv[0]["delta"] == 20 and mv[0]["reason"] == "receipt"


def test_cannot_go_negative(seeded, client):
    s, p = seeded["s1"]["id"], seeded["p1"]["id"]
    r = client.post(f"/inventory/{s}/{p}/adjust", json={"delta": -21, "reason": "adjustment"})
    assert r.status_code == 409
    assert client.get(f"/inventory/{s}/{p}").json()["on_hand"] == 20  # unchanged


def test_zero_delta_rejected(seeded, client):
    s, p = seeded["s1"]["id"], seeded["p1"]["id"]
    assert client.post(f"/inventory/{s}/{p}/adjust", json={"delta": 0, "reason": "adjustment"}).status_code == 422


def test_optimistic_locking(seeded, client):
    s, p = seeded["s1"]["id"], seeded["p1"]["id"]
    ok = client.post(f"/inventory/{s}/{p}/adjust", json={"delta": 1, "reason": "receipt", "expected_version": 1})
    assert ok.status_code == 200 and ok.json()["version"] == 2
    stale = client.post(f"/inventory/{s}/{p}/adjust", json={"delta": 1, "reason": "receipt", "expected_version": 1})
    assert stale.status_code == 409 and "version conflict" in stale.json()["detail"]


def test_unknown_store_or_product_404(seeded, client):
    assert client.post("/inventory/999/1/adjust", json={"delta": 1, "reason": "receipt"}).status_code == 404
    assert client.post(f"/inventory/{seeded['s1']['id']}/999/adjust", json={"delta": 1, "reason": "receipt"}).status_code == 404


def test_low_stock_filter(seeded, client):
    s, p2 = seeded["s1"]["id"], seeded["p2"]["id"]
    assert client.get("/inventory", params={"low_stock": "true"}).json()["total"] == 0  # 3 > reorder 2
    client.post(f"/inventory/{s}/{p2}/adjust", json={"delta": -1, "reason": "adjustment"})     # now 2 <= 2
    r = client.get("/inventory", params={"low_stock": "true"}).json()
    assert r["total"] == 1 and r["items"][0]["sku"] == "SKU-2" and r["items"][0]["below_reorder"] is True


def test_transfer_is_atomic(seeded, client):
    s1, s2, p = seeded["s1"]["id"], seeded["s2"]["id"], seeded["p1"]["id"]
    r = client.post("/transfers", json={"from_store_id": s1, "to_store_id": s2, "product_id": p, "quantity": 5})
    assert r.status_code == 200
    assert r.json()["from"]["on_hand"] == 15 and r.json()["to"]["on_hand"] == 5
    # insufficient -> 409, and nothing changes on either side
    r = client.post("/transfers", json={"from_store_id": s1, "to_store_id": s2, "product_id": p, "quantity": 100})
    assert r.status_code == 409
    assert client.get(f"/inventory/{s1}/{p}").json()["on_hand"] == 15
    assert client.get(f"/inventory/{s2}/{p}").json()["on_hand"] == 5
    # same store -> 422
    assert client.post("/transfers", json={"from_store_id": s1, "to_store_id": s1, "product_id": p, "quantity": 1}).status_code == 422


def test_ledger_integrity_holds(seeded, client):
    s, p = seeded["s1"]["id"], seeded["p1"]["id"]
    for d in (5, -3, 2, -1):
        client.post(f"/inventory/{s}/{p}/adjust", json={"delta": d, "reason": "adjustment"})
    r = client.get("/integrity").json()
    assert r["ok"] is True and r["mismatches"] == []
