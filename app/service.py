"""Compatibility facade over the domain modules.

The v0.1 code base imported everything from ``app.service``; v0.2 splits the logic into
``catalog``, ``inventory``, ``orders``, ``reports`` and ``ledger``.  This module keeps the old
import path working — for the seed, the bridge, scripts and tests — by re-exporting the public
functions of every domain module from one place.  New code should import the domain modules
directly.
"""
from __future__ import annotations

import sqlite3

from .catalog import (
    create_product,
    create_store,
    deactivate_product,
    get_product,
    get_store,
    list_products,
    list_stores,
    product_inventory,
    update_product,
    update_store,
)
from .common import ServiceError
from .inventory import (
    adjust_stock,
    export_inventory_csv,
    export_movements_csv,
    get_inventory_row,
    get_transfer,
    list_inventory,
    list_transfers,
    movement_feed,
    movements,
    receive_stock,
    transfer,
)
from .ledger import apply_movement, ledger_integrity, rebuild_balances
from .orders import cancel_order, export_orders_csv, fulfil_order, get_order, list_orders, place_order
from .reports import export_reorder_csv, reorder_report, sales_report, summary_report


def _apply_movement(conn: sqlite3.Connection, store_id: int, product_id: int, delta: int, reason: str, reference: str | None) -> int:
    """v0.1 private helper kept for compatibility: returns only the new balance (``ledger.apply_movement`` returns a tuple)."""
    return apply_movement(conn, store_id, product_id, delta, reason, reference)[0]


__all__ = [
    "ServiceError",
    "adjust_stock",
    "apply_movement",
    "cancel_order",
    "create_product",
    "create_store",
    "deactivate_product",
    "export_inventory_csv",
    "export_movements_csv",
    "export_orders_csv",
    "export_reorder_csv",
    "fulfil_order",
    "get_inventory_row",
    "get_order",
    "get_product",
    "get_store",
    "get_transfer",
    "ledger_integrity",
    "list_inventory",
    "list_orders",
    "list_products",
    "list_stores",
    "list_transfers",
    "movement_feed",
    "movements",
    "place_order",
    "product_inventory",
    "rebuild_balances",
    "receive_stock",
    "reorder_report",
    "sales_report",
    "summary_report",
    "transfer",
    "update_product",
    "update_store",
]
