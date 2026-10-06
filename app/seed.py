"""Deterministic demo dataset: 3 stores, 12 products, opening stock, 45 days of back-dated orders and two transfers.

The seed only calls façade names on ``service`` (``create_store``, ``create_product``, ``adjust_stock``,
``place_order``, ``cancel_order``, ``fulfil_order``, ``transfer``), so it runs unchanged behind the server pool
and inside the browser bridge. It is driven by ``random.Random(RANDOM_SEED)``: every fresh database receives
the same history. Timestamps are anchored on midnight UTC of the seeding day — orders fall on the calendar
days before it, never on the day itself — so the calendar spread of the history does not depend on the hour
at which the database is seeded. The order history is written through the normal order flow (stock really
moves) and then back-dated, so reports have a realistic 45-day window.
"""
from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from . import schemas, service
from .db import transaction

STORES = [("BLR-01", "Bengaluru Koramangala", "South"), ("HYD-01", "Hyderabad Gachibowli", "South"), ("MUM-01", "Mumbai Andheri", "West")]
PRODUCTS = [
    ("GRO-RICE-5KG", "Basmati Rice 5 kg", "Grocery", 64900, 20),
    ("GRO-OIL-1L", "Sunflower Oil 1 L", "Grocery", 15900, 30),
    ("HH-DET-2KG", "Laundry Detergent 2 kg", "Household", 42500, 15),
    ("HH-TOWEL-SET", "Bath Towel Set", "Household", 89900, 8),
    ("APP-TEE-M", "Cotton T-Shirt (M)", "Apparel", 59900, 12),
    ("APP-JEAN-32", "Slim Jeans (32)", "Apparel", 199900, 6),
    ("ELE-EARBUD", "Wireless Earbuds", "Electronics", 249900, 5),
    ("ELE-CHARGER", "65 W USB-C Charger", "Electronics", 179900, 10),
    ("BTY-SHAMPOO", "Herbal Shampoo 400 ml", "Beauty", 34900, 15),
    ("TOY-BLOCKS", "Building Blocks 200 pc", "Toys", 129900, 4),
    ("HOME-LAMP", "LED Desk Lamp", "Home", 149900, 5),
    ("SPT-YOGA-MAT", "Yoga Mat 6 mm", "Sports", 99900, 6),
]

RANDOM_SEED = 42
ORDER_COUNT = 40  # orders in the demo history
HISTORY_DAYS = 45  # the history spans the last 45 days
BUSY_DAYS = 34  # distinct days that carry at least one order; the remaining orders land on one of those days
CANCELLED_COUNT = 6
FULFILLED_COUNT = 20
KEYED_EVERY = 3  # every third order carries an Idempotency-Key, like a retried checkout
MIN_LEFT = 1  # units the seed always leaves at every (store, product) so the demo scenarios can still order
TRANSFERS = ((0, 1, 7, 6), (1, 2, 6, 5))  # (from store index, to store index, product index, quantity)


def opening_quantity(store_index: int, product_index: int) -> int:
    """Opening stock of the demo, 5..44 units per (store, product) — the v0.1 formula."""
    return 5 + ((store_index * 7 + product_index * 11) % 40)


def _iso(moment: datetime) -> str:
    """The ``common.now_iso()`` text shape for an arbitrary UTC moment."""
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}Z"


@dataclass
class PlannedOrder:
    """One order of the demo history, before it is written."""

    store: int  # store index into STORES
    lines: list[tuple[int, int]]  # (product index into PRODUCTS, quantity)
    day: int  # calendar days before the seeding date (1 = yesterday)
    placed_at: datetime
    status: str = "placed"
    closed_at: datetime | None = None  # cancellation / fulfilment time
    key: str | None = None  # Idempotency-Key


def plan_history(rng: random.Random, now: datetime) -> tuple[list[PlannedOrder], list[tuple[int, int, int, int]]]:
    """Pure planning step (no database): the orders (when, where, which lines, outcome) and the transfers.

    Quantities are capped so that no (store, product) ever drops below ``MIN_LEFT`` units during the
    history, which keeps every order and transfer executable and leaves stock for the demo scenarios.
    Every timestamp lies on a calendar day before the seeding date (UTC), between 08:00 and 22:00, so the
    history is a pure function of the random seed and the date: ``BUSY_DAYS`` distinct days carry orders
    (yesterday and the day before always do, so even a 7-day window shows activity), the remaining orders
    fall on one of those days, and cancellations / fulfilments follow within two or three days, never later
    than yesterday. Today stays empty for the orders the demo user places.
    """
    stock = {(s, p): opening_quantity(s, p) for s in range(len(STORES)) for p in range(len(PRODUCTS))}
    days = [1, 2] + rng.sample(range(3, HISTORY_DAYS), BUSY_DAYS - 2)
    days += [rng.choice(days) for _ in range(ORDER_COUNT - BUSY_DAYS)]
    midnight = datetime.combine(now.date(), time(), tzinfo=UTC)  # start of the seeding day (UTC); the history ends before it
    latest = midnight - timedelta(minutes=1)
    moments = sorted(((day, midnight - timedelta(days=day) + timedelta(hours=rng.uniform(8, 22))) for day in days), key=lambda item: item[1])
    orders: list[PlannedOrder] = []
    for index, (day, moment) in enumerate(moments):
        lines: list[tuple[int, int]] = []
        while not lines:
            store = rng.randrange(len(STORES))
            for product in rng.sample(range(len(PRODUCTS)), rng.randint(1, 3)):
                quantity = min(rng.randint(1, 3), stock[(store, product)] - MIN_LEFT)
                if quantity >= 1:
                    lines.append((product, quantity))
                    stock[(store, product)] -= quantity
        key = f"seed-{index + 1:03d}" if index % KEYED_EVERY == 0 else None
        orders.append(PlannedOrder(store, lines, day, moment, key=key))
    for index in rng.sample(range(ORDER_COUNT), CANCELLED_COUNT):
        order = orders[index]
        order.status = "cancelled"
        order.closed_at = min(order.placed_at + timedelta(hours=rng.uniform(1, 48)), latest)
    settled = [i for i, order in enumerate(orders) if order.status == "placed" and order.day >= 2]  # at least a full day old
    for index in rng.sample(settled, min(FULFILLED_COUNT, len(settled))):
        order = orders[index]
        order.status = "fulfilled"
        order.closed_at = min(order.placed_at + timedelta(hours=rng.uniform(2, 72)), latest)
    transfers = []
    for from_index, to_index, product_index, wanted in TRANSFERS:
        quantity = min(wanted, stock[(from_index, product_index)] - MIN_LEFT)
        if quantity >= 1:
            transfers.append((from_index, to_index, product_index, quantity))
            stock[(from_index, product_index)] -= quantity
    return orders, transfers


def _populate(conn: sqlite3.Connection, rng: random.Random, now: datetime) -> None:
    stores = [service.create_store(conn, schemas.StoreIn(code=c, name=n, region=r)) for c, n, r in STORES]
    products = [
        service.create_product(conn, schemas.ProductIn(sku=s, name=n, category=c, price_cents=p, reorder_point=rp))
        for s, n, c, p, rp in PRODUCTS
    ]
    for store_index, store in enumerate(stores):
        for product_index, product in enumerate(products):
            adjustment = schemas.StockAdjust(delta=opening_quantity(store_index, product_index), reason="receipt", reference="opening-stock")
            service.adjust_stock(conn, store["id"], product["id"], adjustment)

    plans, transfers = plan_history(rng, now)
    placed: list[tuple[PlannedOrder, int]] = []
    for plan in plans:
        order_in = schemas.OrderIn(
            store_id=stores[plan.store]["id"],
            lines=[schemas.OrderLineIn(product_id=products[product_index]["id"], quantity=quantity) for product_index, quantity in plan.lines],
        )
        order, _created = service.place_order(conn, order_in, plan.key)
        placed.append((plan, order["id"]))
    for plan, order_id in placed:
        if plan.status == "cancelled":
            service.cancel_order(conn, order_id)
        elif plan.status == "fulfilled":
            service.fulfil_order(conn, order_id)

    for from_index, to_index, product_index, quantity in transfers:
        transfer_in = schemas.TransferIn(
            from_store_id=stores[from_index]["id"], to_store_id=stores[to_index]["id"], product_id=products[product_index]["id"], quantity=quantity
        )
        res = service.transfer(conn, transfer_in)
        t_out = res[0] if isinstance(res, tuple) else res  # v0.1 façade returns the dict, v0.2 returns (transfer, created)
        if t_out["from"]["on_hand"] < MIN_LEFT:  # the plan guarantees this; fail loudly rather than seed a drained store
            raise RuntimeError(f"seed transfer drained store {from_index} of product {product_index}")

    # Back-date the history in one write transaction: orders, their sale movements and the restocks of cancellations.
    with transaction(conn):
        for plan, order_id in placed:
            created_at = _iso(plan.placed_at)
            updated_at = _iso(plan.closed_at or plan.placed_at)
            conn.execute("UPDATE orders SET created_at=?, updated_at=? WHERE id=?", (created_at, updated_at, order_id))
            conn.execute("UPDATE stock_movements SET created_at=? WHERE reference=?", (created_at, f"order:{order_id}"))
            if plan.status == "cancelled":
                conn.execute("UPDATE stock_movements SET created_at=? WHERE reference=?", (updated_at, f"cancel:{order_id}"))


def seed(conn: sqlite3.Connection) -> None:
    """Populate an empty database with the demo dataset; a no-op when stores already exist."""
    if conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0]:
        return
    previous_sync = conn.execute("PRAGMA synchronous").fetchone()[0]
    conn.execute("PRAGMA synchronous = NORMAL")  # ~100 small transactions: skip the per-commit fsync while seeding
    try:
        _populate(conn, random.Random(RANDOM_SEED), datetime.now(UTC))
    finally:
        conn.execute(f"PRAGMA synchronous = {int(previous_sync)}")
