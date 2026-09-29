"""Seed a demo dataset: 3 stores, 12 products, opening stock, a few orders."""
from __future__ import annotations

import sqlite3

from . import schemas, service

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


def seed(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0]:
        return
    stores = [service.create_store(conn, schemas.StoreIn(code=c, name=n, region=r)) for c, n, r in STORES]
    products = [
        service.create_product(conn, schemas.ProductIn(sku=s, name=n, category=c, price_cents=p, reorder_point=rp))
        for s, n, c, p, rp in PRODUCTS
    ]
    for si, st in enumerate(stores):
        for pi, p in enumerate(products):
            qty = 5 + ((si * 7 + pi * 11) % 40)
            service.adjust_stock(conn, st["id"], p["id"], schemas.StockAdjust(delta=qty, reason="receipt", reference="opening-stock"))
    # a few orders so reports have velocity
    for i in range(6):
        st = stores[i % 3]
        lines = [schemas.OrderLineIn(product_id=products[(i + k) % 12]["id"], quantity=1 + (i + k) % 3) for k in range(2)]
        service.place_order(conn, schemas.OrderIn(store_id=st["id"], lines=lines), idempotency_key=f"seed-{i}")
    service.cancel_order(conn, 2)
    service.fulfil_order(conn, 1)
