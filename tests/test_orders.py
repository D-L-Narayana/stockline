"""Order flows against the assembled application (``client`` / ``seeded`` fixtures from ``conftest.py``).

These are the v0.1 behaviours that must keep working unchanged. The v0.2 additions — fingerprinted
idempotency, ``updated_at``, inactive-product semantics, the CSV export, typed pages without an N+1 —
are covered on a private router app in ``test_orders_router.py`` and ``test_idempotency.py``.
"""
import threading


def _order(seeded, qty1=2, qty2=1):
    return {"store_id": seeded["s1"]["id"], "lines": [
        {"product_id": seeded["p1"]["id"], "quantity": qty1},
        {"product_id": seeded["p2"]["id"], "quantity": qty2},
    ]}


def test_place_order_reserves_stock_and_prices_server_side(seeded, client):
    r = client.post("/orders", json=_order(seeded))
    assert r.status_code == 201
    o = r.json()
    assert o["status"] == "placed" and o["total_cents"] == 2 * 1000 + 1 * 2500
    assert client.get(f"/inventory/{seeded['s1']['id']}/{seeded['p1']['id']}").json()["on_hand"] == 18
    assert client.get(f"/inventory/{seeded['s1']['id']}/{seeded['p2']['id']}").json()["on_hand"] == 2


def test_insufficient_stock_rolls_back_whole_order(seeded, client):
    r = client.post("/orders", json=_order(seeded, qty1=1, qty2=99))  # p2 only has 3
    assert r.status_code == 409
    # p1 must NOT have been decremented (all-or-nothing)
    assert client.get(f"/inventory/{seeded['s1']['id']}/{seeded['p1']['id']}").json()["on_hand"] == 20
    assert client.get("/orders").json()["total"] == 0


def test_idempotency_key_replays_same_order(seeded, client):
    h = {"Idempotency-Key": "abc-123"}
    a = client.post("/orders", json=_order(seeded), headers=h)
    b = client.post("/orders", json=_order(seeded), headers=h)
    assert a.status_code == 201 and b.status_code == 200
    assert a.json()["id"] == b.json()["id"]
    assert b.headers["Idempotent-Replayed"] == "true"
    assert client.get(f"/inventory/{seeded['s1']['id']}/{seeded['p1']['id']}").json()["on_hand"] == 18  # only once


def test_cancel_restocks_and_is_idempotent(seeded, client):
    oid = client.post("/orders", json=_order(seeded)).json()["id"]
    c1 = client.post(f"/orders/{oid}/cancel")
    c2 = client.post(f"/orders/{oid}/cancel")
    assert c1.json()["status"] == "cancelled" and c2.json()["status"] == "cancelled"
    assert client.get(f"/inventory/{seeded['s1']['id']}/{seeded['p1']['id']}").json()["on_hand"] == 20
    assert client.post(f"/orders/{oid}/fulfil").status_code == 409


def test_fulfilled_cannot_cancel(seeded, client):
    oid = client.post("/orders", json=_order(seeded)).json()["id"]
    assert client.post(f"/orders/{oid}/fulfil").json()["status"] == "fulfilled"
    assert client.post(f"/orders/{oid}/cancel").status_code == 409


def test_order_validation(seeded, client):
    bad = {"store_id": seeded["s1"]["id"], "lines": []}
    assert client.post("/orders", json=bad).status_code == 422
    dup = {"store_id": seeded["s1"]["id"], "lines": [{"product_id": 1, "quantity": 1}, {"product_id": 1, "quantity": 1}]}
    assert client.post("/orders", json=dup).status_code == 422
    assert client.get("/orders/999").status_code == 404


def test_list_orders_filters(seeded, client):
    a = client.post("/orders", json=_order(seeded, 1, 1)).json()["id"]
    client.post("/orders", json=_order(seeded, 1, 1))
    client.post(f"/orders/{a}/cancel")
    assert client.get("/orders", params={"status": "cancelled"}).json()["total"] == 1
    assert client.get("/orders", params={"status": "placed"}).json()["total"] == 1
    assert client.get("/orders", params={"status": "bogus"}).status_code == 422


def test_concurrent_orders_never_oversell(seeded, client):
    """20 threads each try to buy 2 of a product with stock 20 -> exactly 10 succeed."""
    s, p = seeded["s1"]["id"], seeded["p1"]["id"]
    results = []

    def buy():
        r = client.post("/orders", json={"store_id": s, "lines": [{"product_id": p, "quantity": 2}]})
        results.append(r.status_code)

    threads = [threading.Thread(target=buy) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(201) == 10 and results.count(409) == 10
    assert client.get(f"/inventory/{s}/{p}").json()["on_hand"] == 0
    assert client.get("/integrity").json()["ok"] is True
