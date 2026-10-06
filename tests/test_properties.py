"""Model-based randomised test of the stock ledger through the HTTP API.

A plain Python ``ReferenceModel`` predicts the outcome of every operation; the assembled
application (``client`` fixture: temporary database, no demo seed) must agree with it after each
one.  Three seeds × 300 operations over 2 stores × 4 products: receipts, signed adjustments with
and without ``expected_version``, orders with 1–3 lines (keyed and unkeyed), idempotent replays and
key reuse, cancel / fulfil in every order state, store-to-store transfers, product soft delete and
reactivation.  Invariants checked after **every** operation:

* every cached ``on_hand`` and ``version`` equals the model (a pair the model never touched has no row);
* no balance is ever negative;
* a 4xx happens exactly when the model predicts one, with the predicted ``code`` — a shortage
  (409 ``insufficient_stock``), a stale version (409 ``version_conflict``), an inactive product
  (409 ``product_inactive``), an impossible status change (409 ``invalid_state``), a reused key
  (422 ``idempotency_key_reuse``) or an unknown order (404 ``not_found``) — and leaves the ledger untouched.

At the end ``/integrity`` reports ok, the sum of every pair's deltas equals its cached balance, the
``balance_after`` chain is intact, the movement feed pages through exactly the model's movement count,
and every order carries the model's status.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

import pytest

SEEDS = (11, 42, 2026)
OPERATIONS = 300
STORE_COUNT = 2
PRODUCT_COUNT = 4
PLACED, CANCELLED, FULFILLED = "placed", "cancelled", "fulfilled"
UNKNOWN_ORDER_ID = 999_999


@dataclass
class Expect:
    """The status (and error code) the model predicts for one request."""

    status: int
    code: str | None = None


OK = Expect(200)
CREATED = Expect(201)
SHORTAGE = Expect(409, "insufficient_stock")
STALE = Expect(409, "version_conflict")
INACTIVE = Expect(409, "product_inactive")
INVALID_STATE = Expect(409, "invalid_state")
KEY_REUSE = Expect(422, "idempotency_key_reuse")
NOT_FOUND = Expect(404, "not_found")


@dataclass
class ModelOrder:
    """One order as the model remembers it."""

    id: int
    store: int
    lines: dict[int, int]  # product id -> quantity
    status: str
    key: str | None = None


class ReferenceModel:
    """Pure-Python twin of the ledger: balances, versions, product state and orders, plus the outcome rules.

    Rules mirrored from the domain modules: a movement bumps the pair's ``version`` by one and may never take
    ``on_hand`` below zero; an adjustment with a wrong ``expected_version`` is refused before stock is looked at;
    an order is refused when any line names an inactive product (checked before any stock), else when any line
    exceeds the store's balance — all or nothing; cancelling restocks every line once and is idempotent, fulfilling
    moves no stock, and the two are mutually exclusive; a transfer needs the full quantity at the source and does not
    care whether the product is active; an unknown order is a 404.
    """

    def __init__(self, stores: list[int], products: list[int]) -> None:
        self.on_hand = {(s, p): 0 for s in stores for p in products}
        self.version = {(s, p): 0 for s in stores for p in products}
        self.active = dict.fromkeys(products, True)
        self.orders: dict[int, ModelOrder] = {}
        self.movements = 0

    def move(self, store: int, product: int, delta: int) -> None:
        pair = (store, product)
        self.on_hand[pair] += delta
        assert self.on_hand[pair] >= 0, f"model bug: {pair} would go negative"
        self.version[pair] += 1
        self.movements += 1

    def predict_adjust(self, store: int, product: int, delta: int, expected_version: int | None) -> Expect:
        if expected_version is not None and expected_version != self.version[(store, product)]:
            return STALE
        if self.on_hand[(store, product)] + delta < 0:
            return SHORTAGE
        return OK

    def apply_adjust(self, store: int, product: int, delta: int) -> None:
        self.move(store, product, delta)

    def predict_order(self, store: int, lines: dict[int, int]) -> Expect:
        if any(not self.active[product] for product in lines):
            return INACTIVE
        if any(self.on_hand[(store, product)] < quantity for product, quantity in lines.items()):
            return SHORTAGE
        return CREATED

    def apply_order(self, order_id: int, store: int, lines: dict[int, int], key: str | None) -> None:
        for product, quantity in lines.items():
            self.move(store, product, -quantity)
        self.orders[order_id] = ModelOrder(order_id, store, dict(lines), PLACED, key)

    def predict_cancel(self, order_id: int) -> Expect:
        order = self.orders.get(order_id)
        if order is None:
            return NOT_FOUND
        return INVALID_STATE if order.status == FULFILLED else OK

    def apply_cancel(self, order_id: int) -> None:
        order = self.orders[order_id]
        if order.status == PLACED:
            for product, quantity in order.lines.items():
                self.move(order.store, product, quantity)
            order.status = CANCELLED

    def predict_fulfil(self, order_id: int) -> Expect:
        order = self.orders.get(order_id)
        if order is None:
            return NOT_FOUND
        return INVALID_STATE if order.status == CANCELLED else OK

    def apply_fulfil(self, order_id: int) -> None:
        order = self.orders[order_id]
        if order.status == PLACED:
            order.status = FULFILLED

    def predict_transfer(self, source: int, target: int, product: int, quantity: int) -> Expect:
        return SHORTAGE if self.on_hand[(source, product)] < quantity else CREATED

    def apply_transfer(self, source: int, target: int, product: int, quantity: int) -> None:
        self.move(source, product, -quantity)
        self.move(target, product, quantity)

    def set_active(self, product: int, active: bool) -> None:
        self.active[product] = active


# --------------------------------------------------------------------------- the random driver
def _check(response, expect: Expect, context: str) -> None:
    assert response.status_code == expect.status, f"{context}: expected HTTP {expect.status}, got {response.status_code} {response.text}"
    if expect.code is not None:
        assert response.json()["code"] == expect.code, f"{context}: {response.json()}"


class Driver:
    """Draws random operations, sends them through the API and keeps the model in step."""

    def __init__(self, client, seed: int) -> None:
        self.client = client
        self.rng = random.Random(seed)
        self.seed = seed
        self.step = 0
        self.stores = [
            self._created(client.post("/stores", json={"code": f"S{i}", "name": f"Store {i}", "region": "Region"})) for i in range(1, STORE_COUNT + 1)
        ]
        self.prices = {}
        self.products = []
        for i in range(1, PRODUCT_COUNT + 1):
            body = {"sku": f"SKU-{i}", "name": f"Product {i}", "category": "Test", "price_cents": 100 * i, "reorder_point": 5}
            product_id = self._created(client.post("/products", json=body))
            self.products.append(product_id)
            self.prices[product_id] = 100 * i
        self.model = ReferenceModel(self.stores, self.products)
        self.operations = [
            (self.op_receipt, 20),
            (self.op_adjust, 15),
            (self.op_order, 28),
            (self.op_replay, 6),
            (self.op_cancel, 10),
            (self.op_fulfil, 8),
            (self.op_transfer, 10),
            (self.op_toggle_product, 3),
        ]

    @staticmethod
    def _created(response) -> int:
        assert response.status_code == 201, response.text
        return response.json()["id"]

    def context(self, what: str) -> str:
        return f"seed {self.seed} step {self.step} {what}"

    # ------------------------------------------------------------------ operations
    def op_receipt(self) -> str:
        store, product = self.rng.choice(self.stores), self.rng.choice(self.products)
        delta = self.rng.randint(1, 30)
        what = f"receipt +{delta} store {store} product {product}"
        r = self.client.post(f"/inventory/{store}/{product}/adjust", json={"delta": delta, "reason": "receipt", "reference": f"receipt-{self.step}"})
        _check(r, OK, self.context(what))
        self.model.apply_adjust(store, product, delta)
        assert r.json()["on_hand"] == self.model.on_hand[(store, product)], self.context(what)
        return what

    def op_adjust(self) -> str:
        store, product = self.rng.choice(self.stores), self.rng.choice(self.products)
        delta = self.rng.choice([d for d in range(-8, 9) if d])
        current = self.model.version[(store, product)]
        mode = self.rng.random()
        expected_version = None if mode < 0.5 else current if mode < 0.8 else (current + 1 if current == 0 else current - 1)
        body = {"delta": delta, "reason": "adjustment" if delta < 0 else "return"}
        if expected_version is not None:
            body["expected_version"] = expected_version
        what = f"adjust {delta:+d} store {store} product {product} expected_version {expected_version} (model version {current})"
        expect = self.model.predict_adjust(store, product, delta, expected_version)
        r = self.client.post(f"/inventory/{store}/{product}/adjust", json=body)
        _check(r, expect, self.context(what))
        if expect is OK:
            self.model.apply_adjust(store, product, delta)
            assert (r.json()["on_hand"], r.json()["version"]) == (self.model.on_hand[(store, product)], self.model.version[(store, product)]), self.context(what)
        return what

    def _order_body(self, store: int, lines: dict[int, int]) -> dict:
        items = [{"product_id": p, "quantity": q} for p, q in lines.items()]
        self.rng.shuffle(items)
        return {"store_id": store, "lines": items}

    def op_order(self) -> str:
        store = self.rng.choice(self.stores)
        chosen = self.rng.sample(self.products, self.rng.randint(1, 3))
        lines = {p: self.rng.randint(1, 5) for p in chosen}
        key = f"key-{self.seed}-{self.step}" if self.rng.random() < 0.3 else None
        what = f"order store {store} lines {lines} key {key}"
        expect = self.model.predict_order(store, lines)
        r = self.client.post("/orders", json=self._order_body(store, lines), headers={"Idempotency-Key": key} if key else {})
        _check(r, expect, self.context(what))
        if expect is CREATED:
            body = r.json()
            assert body["status"] == PLACED and body["idempotency_key"] == key, self.context(what)
            assert body["total_cents"] == sum(q * self.prices[p] for p, q in lines.items()), self.context(what)
            assert r.headers["Idempotent-Replayed"] == "false", self.context(what)
            self.model.apply_order(body["id"], store, lines, key)
        return what

    def op_replay(self) -> str:
        keyed = [o for o in self.model.orders.values() if o.key]
        if not keyed:
            return self.op_order()
        order = self.rng.choice(keyed)
        if self.rng.random() < 0.7:
            what = f"replay order {order.id} key {order.key}"
            r = self.client.post("/orders", json=self._order_body(order.store, order.lines), headers={"Idempotency-Key": order.key})
            _check(r, OK, self.context(what))
            body = r.json()
            assert (body["id"], body["status"], r.headers["Idempotent-Replayed"]) == (order.id, order.status, "true"), self.context(what)
            return what
        first = next(iter(order.lines))
        changed = {**order.lines, first: order.lines[first] + 1}
        what = f"reuse key {order.key} of order {order.id} with a different body"
        r = self.client.post("/orders", json=self._order_body(order.store, changed), headers={"Idempotency-Key": order.key})
        _check(r, KEY_REUSE, self.context(what))
        return what

    def _pick_order(self) -> int | None:
        if self.rng.random() < 0.05:
            return UNKNOWN_ORDER_ID
        if not self.model.orders:
            return None
        return self.rng.choice(sorted(self.model.orders))

    def op_cancel(self) -> str:
        order_id = self._pick_order()
        if order_id is None:
            return self.op_order()
        what = f"cancel order {order_id}"
        expect = self.model.predict_cancel(order_id)
        r = self.client.post(f"/orders/{order_id}/cancel")
        _check(r, expect, self.context(what))
        if expect is OK:
            self.model.apply_cancel(order_id)
            assert r.json()["status"] == CANCELLED, self.context(what)
        return what

    def op_fulfil(self) -> str:
        order_id = self._pick_order()
        if order_id is None:
            return self.op_order()
        what = f"fulfil order {order_id}"
        expect = self.model.predict_fulfil(order_id)
        r = self.client.post(f"/orders/{order_id}/fulfil")
        _check(r, expect, self.context(what))
        if expect is OK:
            self.model.apply_fulfil(order_id)
            assert r.json()["status"] == FULFILLED and r.json()["updated_at"], self.context(what)
        return what

    def op_transfer(self) -> str:
        source, target = self.rng.sample(self.stores, 2)
        product, quantity = self.rng.choice(self.products), self.rng.randint(1, 5)
        what = f"transfer {quantity} of product {product} from store {source} to {target}"
        expect = self.model.predict_transfer(source, target, product, quantity)
        r = self.client.post("/transfers", json={"from_store_id": source, "to_store_id": target, "product_id": product, "quantity": quantity})
        _check(r, expect, self.context(what))
        if expect is CREATED:
            self.model.apply_transfer(source, target, product, quantity)
            body = r.json()
            assert (body["from"]["on_hand"], body["to"]["on_hand"]) == (self.model.on_hand[(source, product)], self.model.on_hand[(target, product)]), self.context(what)
        return what

    def op_toggle_product(self) -> str:
        product = self.rng.choice(self.products)
        if self.model.active[product]:
            what = f"deactivate product {product}"
            r = self.client.delete(f"/products/{product}")
            assert r.status_code == 204, self.context(what)
            self.model.set_active(product, False)
        else:
            what = f"reactivate product {product}"
            r = self.client.patch(f"/products/{product}", json={"active": True})
            _check(r, OK, self.context(what))
            assert r.json()["active"] is True, self.context(what)
            self.model.set_active(product, True)
        return what

    # ------------------------------------------------------------------ invariants
    def assert_balances(self, what: str) -> None:
        rows = self.client.get("/inventory", params={"limit": 200}).json()["items"]
        actual = {(row["store_id"], row["product_id"]): row for row in rows}
        assert set(actual) <= set(self.model.on_hand), self.context(what)
        for pair, expected in self.model.on_hand.items():
            row = actual.get(pair)
            if row is None:
                assert (expected, self.model.version[pair]) == (0, 0), f"{self.context(what)}: {pair} has no row, model says {expected}"
                continue
            assert row["on_hand"] >= 0, f"{self.context(what)}: negative balance {row}"
            assert row["on_hand"] == expected, f"{self.context(what)}: {pair} on_hand {row['on_hand']} != model {expected}"
            assert row["version"] == self.model.version[pair], f"{self.context(what)}: {pair} version {row['version']} != model {self.model.version[pair]}"

    def assert_ledger_consistent(self) -> None:
        integrity = self.client.get("/integrity").json()
        assert integrity["ok"] is True, integrity
        touched = [pair for pair in self.model.on_hand if self.model.version[pair] > 0]
        assert integrity["checked"] == len(touched), integrity
        for store, product in touched:
            movements = self.client.get(f"/inventory/{store}/{product}/movements", params={"limit": 500}).json()
            assert len(movements) == self.model.version[(store, product)], (store, product)
            running = 0
            for movement in reversed(movements):  # oldest first
                running += movement["delta"]
                assert movement["balance_after"] == running, movement
            assert running == self.model.on_hand[(store, product)], (store, product)
        seen, before_id = 0, None
        while True:
            params = {"limit": 100} if before_id is None else {"limit": 100, "before_id": before_id}
            feed = self.client.get("/movements", params=params).json()
            seen += len(feed["items"])
            before_id = feed["next_before_id"]
            if before_id is None:
                break
        assert seen == self.model.movements

    def assert_orders_match(self) -> None:
        items, offset = [], 0
        while True:
            page = self.client.get("/orders", params={"limit": 100, "offset": offset}).json()
            items += page["items"]
            offset += 100
            if offset >= page["total"]:
                break
        assert {o["id"]: o["status"] for o in items} == {o.id: o.status for o in self.model.orders.values()}

    def run(self, operations: int) -> None:
        ops = [op for op, _weight in self.operations]
        weights = [weight for _op, weight in self.operations]
        for step in range(operations):
            self.step = step
            what = self.rng.choices(ops, weights)[0]()
            self.assert_balances(what)
        self.assert_ledger_consistent()
        self.assert_orders_match()


@pytest.mark.parametrize("seed", SEEDS)
def test_random_operations_match_the_reference_model(client, seed):
    """300 random operations per seed; the API must agree with the model after every one and leave a consistent ledger."""
    Driver(client, seed).run(OPERATIONS)
