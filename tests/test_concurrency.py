"""Threaded concurrency tests against the assembled application (16 threads, barrier-started, no sleeps).

What they prove (see ``docs/concurrency.md``):

* **idempotency race** — one ``Idempotency-Key`` fired from 16 threads at the same instant creates exactly one
  order: one 201 and fifteen 200 replays with the same id and ``Idempotent-Replayed: true``; stock is reserved once;
* **transfer race on one source** — 16 transfers of 3 units from a store holding 10 succeed exactly three times
  (10 = 3 × 3 + 1): a balance never goes negative and every success is a real transfer row with its two movements;
* **opposing transfers** — concurrent transfers in both directions conserve the total and stay consistent;
* **mixed workload** — orders, cancels, fulfilments, transfers, receipts and reads from 16 threads produce no 5xx,
  conserve units across transfers, leave every balance explainable by the orders that exist, and keep ``/integrity`` green.
"""
from __future__ import annotations

import random
import threading
from concurrent.futures import ThreadPoolExecutor

THREADS = 16  # host budget for this suite: at most 16 threads per test


# --------------------------------------------------------------------------- helpers
def _create(client, path: str, body: dict) -> int:
    r = client.post(path, json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _setup(client, stock: dict[tuple[int, int], int]) -> tuple[list[int], list[int]]:
    """Two stores, three products and the given opening stock (``stock[(store index, product index)] = units``)."""
    stores = [_create(client, "/stores", {"code": f"S{i}", "name": f"Store {i}", "region": "Region"}) for i in (1, 2)]
    products = [
        _create(client, "/products", {"sku": f"SKU-{i}", "name": f"Product {i}", "category": "Test", "price_cents": 1000 * i, "reorder_point": 2})
        for i in (1, 2, 3)
    ]
    for (store_index, product_index), units in stock.items():
        r = client.post(f"/inventory/{stores[store_index]}/{products[product_index]}/adjust", json={"delta": units, "reason": "receipt", "reference": "opening"})
        assert r.status_code == 200, r.text
    return stores, products


def _fan_out(fn, n: int = THREADS) -> list:
    """Run ``fn(i)`` on ``n`` threads released together by a barrier; results in index order."""
    barrier = threading.Barrier(n)

    def run(i):
        barrier.wait(timeout=30)
        return fn(i)

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(run, range(n)))


def _on_hand(client, store: int, product: int) -> int:
    r = client.get(f"/inventory/{store}/{product}")
    return r.json()["on_hand"] if r.status_code == 200 else 0


def _all(client, path: str, **params) -> list[dict]:
    """Every item of a paginated listing."""
    items, offset = [], 0
    while True:
        page = client.get(path, params={**params, "limit": 100, "offset": offset}).json()
        items += page["items"]
        offset += 100
        if offset >= page["total"]:
            return items


def _integrity_ok(client) -> None:
    report = client.get("/integrity").json()
    assert report["ok"] is True, report


# --------------------------------------------------------------------------- the idempotency race (PLAN G1)
def test_idempotency_race_creates_exactly_one_order(client):
    """16 identical keyed requests at once: one 201, fifteen 200 replays of the same order, stock decremented once."""
    (s1, _s2), (p1, _p2, _p3) = _setup(client, {(0, 0): 20})
    body = {"store_id": s1, "lines": [{"product_id": p1, "quantity": 2}]}
    responses = _fan_out(lambda _i: client.post("/orders", json=body, headers={"Idempotency-Key": "race-key-1"}))
    statuses = [r.status_code for r in responses]
    assert statuses.count(201) == 1, statuses
    assert statuses.count(200) == THREADS - 1, statuses
    assert len({r.json()["id"] for r in responses}) == 1, [r.json() for r in responses]
    for r in responses:
        assert r.headers["Idempotent-Replayed"] == ("false" if r.status_code == 201 else "true"), (r.status_code, dict(r.headers))
        assert r.json()["idempotency_key"] == "race-key-1" and r.json()["status"] == "placed"
    assert _on_hand(client, s1, p1) == 18  # reserved exactly once
    assert client.get("/orders").json()["total"] == 1
    assert len(client.get(f"/inventory/{s1}/{p1}/movements").json()) == 2  # opening receipt + one sale
    _integrity_ok(client)


# --------------------------------------------------------------------------- transfer races
def test_transfer_race_on_one_source_never_goes_negative(client):
    """16 threads move 3 units each out of a store holding 10: exactly three succeed and the balance stops at 1."""
    (s1, s2), (p1, _p2, _p3) = _setup(client, {(0, 0): 10})
    body = {"from_store_id": s1, "to_store_id": s2, "product_id": p1, "quantity": 3}
    responses = _fan_out(lambda _i: client.post("/transfers", json=body))
    statuses = [r.status_code for r in responses]
    assert statuses.count(201) == 3 and statuses.count(409) == THREADS - 3, statuses
    for r in responses:
        if r.status_code == 409:
            assert r.json()["code"] == "insufficient_stock", r.json()
    assert (_on_hand(client, s1, p1), _on_hand(client, s2, p1)) == (1, 9)
    transfers = _all(client, "/transfers")
    assert len(transfers) == 3 and {t["id"] for t in transfers} == {r.json()["id"] for r in responses if r.status_code == 201}
    feed = client.get("/movements", params={"product_id": p1, "limit": 500}).json()["items"]
    assert sorted(m["reason"] for m in feed) == ["receipt"] + ["transfer_in"] * 3 + ["transfer_out"] * 3
    assert {m["reference"] for m in feed if m["reason"].startswith("transfer")} == {f"transfer:{t['id']}" for t in transfers}
    assert all(m["balance_after"] >= 0 for m in feed)
    _integrity_ok(client)


def test_opposing_transfers_conserve_units_and_stay_consistent(client):
    """Transfers in both directions at once: no 5xx, both balances ≥ 0, total conserved, every success recorded."""
    (s1, s2), (p1, _p2, _p3) = _setup(client, {(0, 0): 10, (1, 0): 2})

    def transfer(i):
        source, target = (s1, s2) if i % 2 == 0 else (s2, s1)
        return client.post("/transfers", json={"from_store_id": source, "to_store_id": target, "product_id": p1, "quantity": 3})

    responses = _fan_out(transfer)
    assert all(r.status_code in (201, 409) for r in responses), [r.status_code for r in responses]
    assert all(r.json()["code"] == "insufficient_stock" for r in responses if r.status_code == 409)
    on_s1, on_s2 = _on_hand(client, s1, p1), _on_hand(client, s2, p1)
    assert on_s1 >= 0 and on_s2 >= 0 and on_s1 + on_s2 == 12, (on_s1, on_s2)
    transfers = _all(client, "/transfers")
    assert len(transfers) == sum(1 for r in responses if r.status_code == 201)
    out_of_s1 = sum(t["quantity"] for t in transfers if t["from_store_id"] == s1)
    into_s1 = sum(t["quantity"] for t in transfers if t["to_store_id"] == s1)
    assert on_s1 == 10 - out_of_s1 + into_s1
    _integrity_ok(client)


# --------------------------------------------------------------------------- mixed workload
def test_mixed_workload_has_no_5xx_conserves_units_and_keeps_integrity(client):
    """Orders, cancels, fulfilments, transfers, receipts and reads from 16 threads leave a consistent, explainable ledger."""
    (s1, s2), (p_order, p_transfer, p_receipt) = _setup(client, {(0, 0): 40, (1, 0): 40, (0, 1): 12, (1, 1): 12})
    stores = (s1, s2)
    statuses: list[tuple[str, int]] = []
    lock = threading.Lock()

    def record(kind: str, r):
        with lock:
            statuses.append((kind, r.status_code))
        return r

    def worker(i):
        rng = random.Random(i)
        mine: list[int] = []
        created = receipts = 0
        for _ in range(6):
            action = rng.choice(("order", "order", "cancel", "fulfil", "transfer", "receipt", "read"))
            if action == "order":
                r = record("order", client.post("/orders", json={"store_id": rng.choice(stores), "lines": [{"product_id": p_order, "quantity": rng.randint(1, 2)}]}))
                if r.status_code == 201:
                    mine.append(r.json()["id"])
                    created += 1
            elif action in ("cancel", "fulfil") and mine:
                record(action, client.post(f"/orders/{rng.choice(mine)}/{action}"))
            elif action == "transfer":
                source, target = (s1, s2) if rng.random() < 0.5 else (s2, s1)
                record("transfer", client.post("/transfers", json={"from_store_id": source, "to_store_id": target, "product_id": p_transfer, "quantity": 1}))
            elif action == "receipt":
                r = record("receipt", client.post(f"/inventory/{rng.choice(stores)}/{p_receipt}/adjust", json={"delta": 2, "reason": "receipt"}))
                receipts += 1 if r.status_code == 200 else 0
            else:
                record("read", client.get("/inventory", params={"limit": 200}))
        return created, receipts

    results = _fan_out(worker)
    allowed = {"order": {201, 409}, "cancel": {200, 409}, "fulfil": {200, 409}, "transfer": {201, 409}, "receipt": {200}, "read": {200}}
    assert all(status < 500 for _kind, status in statuses), statuses
    assert all(status in allowed[kind] for kind, status in statuses), statuses
    orders = _all(client, "/orders")
    assert len(orders) == sum(created for created, _receipts in results)
    for store in stores:
        reserved = sum(line["quantity"] for o in orders if o["store_id"] == store and o["status"] in ("placed", "fulfilled") for line in o["lines"])
        assert _on_hand(client, store, p_order) == 40 - reserved, store
    assert _on_hand(client, s1, p_transfer) + _on_hand(client, s2, p_transfer) == 24
    assert _on_hand(client, s1, p_receipt) + _on_hand(client, s2, p_receipt) == 2 * sum(receipts for _created, receipts in results)
    _integrity_ok(client)
