"""Ledger core: append-only stock movements with a running balance, integrity audit and cache rebuild.

Browser-safe (standard library only). All functions take an open connection first.
"""
from __future__ import annotations

import sqlite3

from .common import CONFLICT, INSUFFICIENT_STOCK, VALIDATION, ServiceError
from .db import schema_version, transaction

# Every (store, product) pair known to either the cache or the ledger, with both balances.
_PAIRS_SQL = """
SELECT k.store_id, k.product_id,
       COALESCE(i.on_hand, 0) AS on_hand,
       COALESCE(m.ledger, 0)  AS ledger,
       (i.store_id IS NOT NULL) AS cached
FROM (SELECT store_id, product_id FROM inventory
      UNION
      SELECT store_id, product_id FROM stock_movements) k
LEFT JOIN inventory i ON i.store_id = k.store_id AND i.product_id = k.product_id
LEFT JOIN (SELECT store_id, product_id, SUM(delta) AS ledger FROM stock_movements GROUP BY store_id, product_id) m
       ON m.store_id = k.store_id AND m.product_id = k.product_id
ORDER BY k.store_id, k.product_id
"""


def apply_movement(
    conn: sqlite3.Connection, store_id: int, product_id: int, delta: int, reason: str, reference: str | None
) -> tuple[int, int]:
    """Append one ledger row and update the cached balance in the caller's transaction.

    Returns ``(new_on_hand, movement_id)``. Raises ``ServiceError(409, …, insufficient_stock)`` when the
    balance would go negative (nothing is written) and ``RuntimeError`` when called outside ``db.transaction()``.
    """
    if not conn.in_transaction:
        raise RuntimeError("apply_movement must run inside db.transaction()")
    if delta == 0:
        raise ServiceError(422, "delta must be non-zero", VALIDATION)
    row = conn.execute("SELECT on_hand FROM inventory WHERE store_id=? AND product_id=?", (store_id, product_id)).fetchone()
    current = row[0] if row else 0
    new = current + delta
    if new < 0:
        raise ServiceError(
            409, f"insufficient stock for product {product_id} at store {store_id}: have {current}, need {-delta}", INSUFFICIENT_STOCK
        )
    conn.execute(
        """INSERT INTO inventory (store_id, product_id, on_hand, version) VALUES (?,?,?,1)
           ON CONFLICT(store_id, product_id) DO UPDATE SET on_hand = excluded.on_hand, version = version + 1""",
        (store_id, product_id, new),
    )
    cur = conn.execute(
        "INSERT INTO stock_movements (store_id, product_id, delta, reason, reference, balance_after) VALUES (?,?,?,?,?,?)",
        (store_id, product_id, delta, reason, reference, new),
    )
    return new, int(cur.lastrowid)


def _chain_breaks(conn: sqlite3.Connection) -> tuple[list[dict], int]:
    """Walk every pair's movements in id order; ``balance_after`` must equal the running sum of deltas."""
    breaks: list[dict] = []
    missing = 0
    running: dict[tuple[int, int], int] = {}
    for row in conn.execute("SELECT id, store_id, product_id, delta, balance_after FROM stock_movements ORDER BY store_id, product_id, id"):
        key = (row["store_id"], row["product_id"])
        total = running.get(key, 0) + row["delta"]
        running[key] = total
        if row["balance_after"] is None:
            missing += 1
        elif row["balance_after"] != total:
            breaks.append({"store_id": key[0], "product_id": key[1], "movement_id": row["id"], "expected": total, "actual": row["balance_after"]})
    return breaks, missing


def ledger_integrity(conn: sqlite3.Connection) -> dict:
    """Audit the ledger against the cache, the ``balance_after`` chain and order totals.

    ``checked`` counts every (store, product) pair present in ``inventory`` or ``stock_movements``.
    ``negative`` lists pairs whose cached *or* ledger balance is below zero (the schema's CHECK normally
    pins the cache at >= 0, so a negative ledger sum is the realistic case); ``on_hand`` there is the
    cache when it is negative, otherwise the negative ledger balance. ``missing_balance_after`` counts
    legacy rows without a running balance and never affects ``ok``.
    """
    mismatches: list[dict] = []
    negative: list[dict] = []
    checked = 0
    for row in conn.execute(_PAIRS_SQL):
        checked += 1
        on_hand, ledger = row["on_hand"], row["ledger"]
        if on_hand != ledger:
            mismatches.append({"store_id": row["store_id"], "product_id": row["product_id"], "on_hand": on_hand, "ledger": ledger})
        if on_hand < 0 or ledger < 0:
            negative.append({"store_id": row["store_id"], "product_id": row["product_id"], "on_hand": on_hand if on_hand < 0 else ledger})
    chain_breaks, missing = _chain_breaks(conn)
    order_total_mismatches = [
        {"order_id": r["order_id"], "total_cents": r["total_cents"], "lines_total_cents": r["lines_total_cents"]}
        for r in conn.execute(
            """SELECT o.id AS order_id, o.total_cents, COALESCE(SUM(ol.quantity * ol.unit_price_cents), 0) AS lines_total_cents
               FROM orders o LEFT JOIN order_lines ol ON ol.order_id = o.id
               GROUP BY o.id HAVING o.total_cents <> lines_total_cents ORDER BY o.id"""
        )
    ]
    return {
        "ok": not (mismatches or negative or chain_breaks or order_total_mismatches),
        "checked": checked,
        "schema_version": schema_version(conn),
        "mismatches": mismatches,
        "negative": negative,
        "chain_breaks": chain_breaks,
        "order_total_mismatches": order_total_mismatches,
        "missing_balance_after": missing,
    }


def rebuild_balances(conn: sqlite3.Connection) -> dict:
    """Recompute every cached balance from the ledger and backfill missing ``balance_after`` values.

    Runs in one IMMEDIATE transaction. For every (store, product) pair in ``inventory`` or
    ``stock_movements`` the cache is set to ``SUM(delta)`` (a missing row is inserted); ``version`` is
    bumped only for rows whose balance actually changed. ``NULL`` ``balance_after`` values are filled
    with the running sum; existing values are never rewritten (a chain break is evidence, not cache).
    Raises ``ServiceError(409, …, conflict)`` — writing nothing — when a ledger sum is negative, because
    the cache cannot legally hold it.
    """
    fixed: list[dict] = []
    backfilled = 0
    with transaction(conn):
        pairs = conn.execute(_PAIRS_SQL).fetchall()
        for row in pairs:
            if row["ledger"] < 0:
                raise ServiceError(
                    409,
                    f"ledger balance for product {row['product_id']} at store {row['store_id']} is negative ({row['ledger']}); "
                    "repair the movements before rebuilding",
                    CONFLICT,
                )
        for row in pairs:
            store_id, product_id, on_hand, ledger = row["store_id"], row["product_id"], row["on_hand"], row["ledger"]
            if not row["cached"]:
                conn.execute("INSERT INTO inventory (store_id, product_id, on_hand, version) VALUES (?,?,?,1)", (store_id, product_id, ledger))
            elif on_hand != ledger:
                conn.execute(
                    "UPDATE inventory SET on_hand = ?, version = version + 1 WHERE store_id=? AND product_id=?", (ledger, store_id, product_id)
                )
            if on_hand != ledger:
                fixed.append({"store_id": store_id, "product_id": product_id, "before": on_hand, "after": ledger})
        running: dict[tuple[int, int], int] = {}
        updates: list[tuple[int, int]] = []
        for m in conn.execute(
            """SELECT id, store_id, product_id, delta, balance_after FROM stock_movements
               WHERE (store_id, product_id) IN (SELECT store_id, product_id FROM stock_movements WHERE balance_after IS NULL)
               ORDER BY store_id, product_id, id"""
        ):
            key = (m["store_id"], m["product_id"])
            total = running.get(key, 0) + m["delta"]
            running[key] = total
            if m["balance_after"] is None:
                updates.append((total, m["id"]))
        if updates:
            conn.executemany("UPDATE stock_movements SET balance_after = ? WHERE id = ?", updates)
            backfilled = len(updates)
    return {"checked": len(pairs), "fixed": fixed, "backfilled": backfilled, "ok": True}
