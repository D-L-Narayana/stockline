"""Reports domain: reorder windowing and netting, summary maths, sales grouping, CSV export and bridge parity.

The reports router runs on a private FastAPI app over a temporary database. Movements and orders are
crafted through a direct connection so their ``created_at`` values are chosen rather than "now".
"""
from __future__ import annotations

import ast
import csv
import io
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import common, db, deps, ledger, reports
from app.common import VALIDATION, BridgeCall, ServiceError
from app.routers import reports as reports_router

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
COLUMNS = [
    "store_id", "store_code", "product_id", "sku", "name", "on_hand", "reorder_point", "days",
    "sold_window", "returned_window", "net_sold", "daily_velocity", "days_of_cover", "suggested_qty",
]
PATHS = ("/reports/reorder", "/reports/reorder.csv", "/reports/summary", "/reports/sales")


def iso(moment: datetime) -> str:
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}Z"


def ago(days: float) -> str:
    return iso(datetime.now(UTC) - timedelta(days=days))


@dataclass
class Harness:
    """Private app client plus a direct connection for crafting rows with chosen timestamps."""

    client: TestClient
    conn: sqlite3.Connection

    def store(self, code: str, region: str = "South") -> int:
        cur = self.conn.execute("INSERT INTO stores (code, name, region) VALUES (?,?,?)", (code, f"Store {code}", region))
        return int(cur.lastrowid)

    def product(self, sku: str, *, price: int = 1000, reorder_point: int = 10, name: str | None = None, active: bool = True) -> int:
        cur = self.conn.execute(
            "INSERT INTO products (sku, name, category, price_cents, reorder_point, active) VALUES (?,?,?,?,?,?)",
            (sku, name or f"Product {sku}", "Cat", price, reorder_point, 1 if active else 0),
        )
        return int(cur.lastrowid)

    def move(self, store_id: int, product_id: int, delta: int, reason: str, reference: str | None = None, *, days_ago: float = 0.0) -> int:
        with db.transaction(self.conn):
            _, movement_id = ledger.apply_movement(self.conn, store_id, product_id, delta, reason, reference)
        self.conn.execute("UPDATE stock_movements SET created_at=? WHERE id=?", (ago(days_ago), movement_id))
        return movement_id

    def order(
        self, store_id: int, lines: list[tuple[int, int, int]], *, status: str = "placed", days_ago: float = 0.0, created_at: str | None = None
    ) -> int:
        """Insert an order with ``(product_id, quantity, unit_price_cents)`` lines; stock is not touched."""
        stamp = created_at or ago(days_ago)
        total = sum(qty * price for _, qty, price in lines)
        cur = self.conn.execute(
            "INSERT INTO orders (store_id, status, idempotency_key, total_cents, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (store_id, status, None, total, stamp, stamp),
        )
        order_id = int(cur.lastrowid)
        self.conn.executemany(
            "INSERT INTO order_lines (order_id, product_id, quantity, unit_price_cents) VALUES (?,?,?,?)",
            [(order_id, pid, qty, price) for pid, qty, price in lines],
        )
        return order_id

    def get(self, path: str, **params):
        return self.client.get(path, params=params)

    def rows(self, path: str, **params) -> list[dict]:
        r = self.get(path, **params)
        assert r.status_code == 200, r.text
        return r.json()


@pytest.fixture()
def h(tmp_path, monkeypatch):
    path = str(tmp_path / "reports.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    deps.reset_state(seed=False)
    app = FastAPI()
    app.include_router(reports_router.router)
    app.add_exception_handler(ServiceError, deps.service_error_handler)
    conn = db.connect(path)
    db.init_schema(conn)
    with TestClient(app) as client:
        yield Harness(client, conn)
    conn.close()
    deps.reset_state(seed=False)


def reorder_scenario(h: Harness) -> dict[str, int]:
    """Two stores, five products; on_hand / windows chosen so every rule of the report is observable."""
    s1, s2 = h.store("S1"), h.store("S2", "West")
    widget = h.product("SKU-W", price=1000, reorder_point=10)
    gadget = h.product("SKU-G", price=2500, reorder_point=5)
    clamp = h.product("SKU-C", price=300, reorder_point=10)
    plenty = h.product("SKU-P", price=100, reorder_point=2)
    retired = h.product("SKU-R", price=100, reorder_point=10, active=False)
    # widget @ S1: 20 - 3 - 4 - 5 = 8 on hand (<= 10); sales 2, 20 and 60 days ago
    h.move(s1, widget, 20, "receipt", "opening", days_ago=90)
    h.move(s1, widget, -3, "sale", "order:1", days_ago=2)
    h.move(s1, widget, -4, "sale", "order:2", days_ago=20)
    h.move(s1, widget, -5, "sale", "order:3", days_ago=60)
    # gadget @ S1: 4 + 1 (walk-in return, not a cancellation) - 4 + 4 (cancelled order) = 5 (== reorder point)
    h.move(s1, gadget, 4, "receipt", "opening", days_ago=90)
    h.move(s1, gadget, 1, "return", "walk-in", days_ago=10)
    h.move(s1, gadget, -4, "sale", "order:5", days_ago=3)
    h.move(s1, gadget, 4, "return", "cancel:5", days_ago=2.9)
    # clamp @ S1: sale outside the 30-day window, its cancellation inside -> net sold clamps at 0
    h.move(s1, clamp, 10, "receipt", "opening", days_ago=90)
    h.move(s1, clamp, -5, "sale", "order:7", days_ago=40)
    h.move(s1, clamp, 5, "return", "cancel:7", days_ago=2)
    # excluded: plenty (above reorder point) and retired (inactive)
    h.move(s1, plenty, 50, "receipt", "opening", days_ago=90)
    h.move(s1, retired, 1, "receipt", "opening", days_ago=5)
    # S2: deeper deficits than S1
    h.move(s2, widget, 2, "receipt", "opening", days_ago=90)
    h.move(s2, gadget, 1, "receipt", "opening", days_ago=90)
    return {"s1": s1, "s2": s2, "widget": widget, "gadget": gadget, "clamp": clamp, "plenty": plenty, "retired": retired}


def _row(rows: list[dict], store_id: int, product_id: int) -> dict:
    found = [r for r in rows if r["store_id"] == store_id and r["product_id"] == product_id]
    assert len(found) == 1, (store_id, product_id, rows)
    return found[0]


# --------------------------------------------------------------------------- reorder report
def test_reorder_rows_have_the_documented_columns_and_defaults(h):
    ids = reorder_scenario(h)
    rows = h.rows("/reports/reorder")
    assert rows, "the report must list SKUs at/below their reorder point"
    for row in rows:
        assert list(row) == COLUMNS
        assert row["days"] == 30
    widget = _row(rows, ids["s1"], ids["widget"])
    assert (widget["store_code"], widget["sku"], widget["name"]) == ("S1", "SKU-W", "Product SKU-W")
    assert (widget["on_hand"], widget["reorder_point"]) == (8, 10)
    assert rows == reports.reorder_report(h.conn)
    assert reports.reorder_report(h.conn) == reports.reorder_report(h.conn, days=30, store_id=None)


def test_reorder_windowing_by_days(h):
    ids = reorder_scenario(h)
    expectations = {
        7: (3, 0.43, 18.6, 12),
        30: (7, 0.23, 34.8, 12),
        90: (12, 0.13, 61.5, 12),
        365: (12, 0.03, 266.7, 12),
    }
    for days, (sold, velocity, cover, suggested) in expectations.items():
        row = _row(h.rows("/reports/reorder", days=days), ids["s1"], ids["widget"])
        assert row["days"] == days
        assert (row["sold_window"], row["returned_window"], row["net_sold"]) == (sold, 0, sold), days
        assert row["daily_velocity"] == velocity, days
        assert row["days_of_cover"] == cover, days
        assert row["suggested_qty"] == suggested, days
        assert row == _row(reports.reorder_report(h.conn, days=days), ids["s1"], ids["widget"])


def test_reorder_nets_cancellations_and_clamps_at_zero(h):
    ids = reorder_scenario(h)
    rows = h.rows("/reports/reorder")
    gadget = _row(rows, ids["s1"], ids["gadget"])
    assert (gadget["on_hand"], gadget["reorder_point"]) == (5, 5)  # on_hand == reorder point is included
    assert (gadget["sold_window"], gadget["returned_window"], gadget["net_sold"]) == (4, 4, 0)  # walk-in return ignored
    assert gadget["daily_velocity"] == 0.0
    assert gadget["days_of_cover"] is None
    assert gadget["suggested_qty"] == 5  # 2 * 5 - 5
    clamp = _row(rows, ids["s1"], ids["clamp"])
    assert (clamp["sold_window"], clamp["returned_window"], clamp["net_sold"]) == (0, 5, 0)
    assert clamp["days_of_cover"] is None and clamp["suggested_qty"] == 10
    older = _row(h.rows("/reports/reorder", days=90), ids["s1"], ids["clamp"])
    assert (older["sold_window"], older["returned_window"], older["net_sold"]) == (5, 5, 0)


def test_reorder_excludes_inactive_and_well_stocked_products(h):
    ids = reorder_scenario(h)
    rows = h.rows("/reports/reorder")
    listed = {(r["store_id"], r["product_id"]) for r in rows}
    assert (ids["s1"], ids["retired"]) not in listed
    assert (ids["s1"], ids["plenty"]) not in listed
    assert listed == {
        (ids["s1"], ids["widget"]), (ids["s1"], ids["gadget"]), (ids["s1"], ids["clamp"]),
        (ids["s2"], ids["widget"]), (ids["s2"], ids["gadget"]),
    }
    h.conn.execute("UPDATE products SET active = 0 WHERE id = ?", (ids["widget"],))
    after = {(r["store_id"], r["product_id"]) for r in h.rows("/reports/reorder")}
    assert after == {(ids["s1"], ids["gadget"]), (ids["s1"], ids["clamp"]), (ids["s2"], ids["gadget"])}


def test_reorder_store_filter_and_ordering(h):
    ids = reorder_scenario(h)
    rows = h.rows("/reports/reorder")
    deficits = [r["reorder_point"] - r["on_hand"] for r in rows]
    assert deficits == sorted(deficits, reverse=True)
    assert [(r["store_code"], r["sku"]) for r in rows[:3]] == [("S2", "SKU-W"), ("S2", "SKU-G"), ("S1", "SKU-W")]
    assert {(r["store_code"], r["sku"]) for r in rows[3:]} == {("S1", "SKU-G"), ("S1", "SKU-C")}
    only_s1 = h.rows("/reports/reorder", store_id=ids["s1"])
    assert {r["store_id"] for r in only_s1} == {ids["s1"]} and len(only_s1) == 3
    only_s2 = h.rows("/reports/reorder", store_id=ids["s2"])
    assert [(r["store_code"], r["sku"]) for r in only_s2] == [("S2", "SKU-W"), ("S2", "SKU-G")]
    assert h.rows("/reports/reorder", store_id=999) == []
    assert reports.reorder_report(h.conn, store_id=ids["s2"]) == only_s2


# --------------------------------------------------------------------------- bounds
@pytest.mark.parametrize("path", ["/reports/reorder", "/reports/reorder.csv", "/reports/sales"])
@pytest.mark.parametrize("days", ["0", "366", "abc", "-1", "1.5"])
def test_days_bounds_are_422(h, path, days):
    reorder_scenario(h)
    assert h.get(path, days=days).status_code == 422
    assert h.get(path, days="365").status_code == 200
    assert h.get(path, days="1").status_code == 200


def test_group_by_and_store_id_validation(h):
    reorder_scenario(h)
    assert h.get("/reports/sales", group_by="bogus").status_code == 422
    assert h.get("/reports/sales", group_by="").status_code == 422
    assert h.get("/reports/reorder", store_id="x").status_code == 422
    assert h.get("/reports/sales", group_by="day").status_code == 200
    assert h.get("/reports/sales", group_by="product").status_code == 200


def test_module_level_validation_errors(h):
    with pytest.raises(ServiceError) as ei:
        reports.sales_report(h.conn, group_by="bogus")
    assert (ei.value.status, ei.value.code) == (422, VALIDATION)
    for bad in (0, 366, -3):
        with pytest.raises(ServiceError) as ei:
            reports.reorder_report(h.conn, days=bad)
        assert (ei.value.status, ei.value.code) == (422, VALIDATION), bad
        with pytest.raises(ServiceError) as ei:
            reports.sales_report(h.conn, days=bad)
        assert (ei.value.status, ei.value.code) == (422, VALIDATION), bad
        with pytest.raises(ServiceError) as ei:
            reports.export_reorder_csv(h.conn, days=bad)
        assert (ei.value.status, ei.value.code) == (422, VALIDATION), bad


# --------------------------------------------------------------------------- summary
def summary_scenario(h: Harness) -> dict[str, int]:
    s1, s2, s3 = h.store("S1"), h.store("S2", "West"), h.store("S3", "North")
    a = h.product("SKU-A", price=1000, reorder_point=5)
    b = h.product("SKU-B", price=2500, reorder_point=2)
    z = h.product("SKU-Z", price=100, reorder_point=50, active=False)
    h.move(s1, a, 20, "receipt", "opening")
    h.move(s1, b, 2, "receipt", "opening")  # == reorder point -> low
    h.move(s1, z, 7, "receipt", "opening")  # inactive -> ignored everywhere
    h.move(s2, a, 3, "receipt", "opening")  # 3 <= 5 -> low
    h.move(s2, b, 1, "receipt", "opening")
    h.move(s2, b, -1, "sale", "order:1")  # 0 on hand -> low, still a SKU row
    h.order(s1, [(a, 1, 1000)], status="placed")
    h.order(s1, [(b, 1, 2500)], status="placed")
    h.order(s2, [(a, 5, 1000)], status="fulfilled")
    h.order(s2, [(a, 1, 700)], status="cancelled")
    return {"s1": s1, "s2": s2, "s3": s3, "a": a, "b": b, "z": z}


def test_summary_report_shape_and_math(h):
    ids = summary_scenario(h)
    before = datetime.now(UTC)
    body = h.rows("/reports/summary")
    assert list(body) == ["stores", "totals", "orders", "revenue_cents", "products", "generated_at"]
    assert body["stores"] == [
        {"store_id": ids["s1"], "store_code": "S1", "store_name": "Store S1", "skus": 2, "units": 22, "value_cents": 25000, "low_stock": 1},
        {"store_id": ids["s2"], "store_code": "S2", "store_name": "Store S2", "skus": 2, "units": 3, "value_cents": 3000, "low_stock": 2},
        {"store_id": ids["s3"], "store_code": "S3", "store_name": "Store S3", "skus": 0, "units": 0, "value_cents": 0, "low_stock": 0},
    ]
    assert body["totals"] == {"skus": 4, "units": 25, "value_cents": 28000, "low_stock": 3}
    assert body["orders"] == {"placed": 2, "fulfilled": 1, "cancelled": 1}
    assert body["revenue_cents"] == {"placed": 3500, "fulfilled": 5000}
    assert body["products"] == {"active": 2, "inactive": 1}
    assert ISO_RE.match(body["generated_at"]), body["generated_at"]
    generated = datetime.strptime(body["generated_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    assert abs((generated - before).total_seconds()) < 5


def test_summary_report_matches_raw_sql(h):
    summary_scenario(h)
    body = reports.summary_report(h.conn)
    units, value, low = h.conn.execute(
        """SELECT COALESCE(SUM(i.on_hand), 0), COALESCE(SUM(i.on_hand * p.price_cents), 0),
                  COALESCE(SUM(i.on_hand <= p.reorder_point), 0)
           FROM inventory i JOIN products p ON p.id = i.product_id WHERE p.active = 1"""
    ).fetchone()
    skus = h.conn.execute("SELECT COUNT(*) FROM inventory i JOIN products p ON p.id = i.product_id WHERE p.active = 1").fetchone()[0]
    assert body["totals"] == {"skus": skus, "units": units, "value_cents": value, "low_stock": low}
    assert body["totals"] == {
        key: sum(store[key] for store in body["stores"]) for key in ("skus", "units", "value_cents", "low_stock")
    }
    by_status = {r[0]: (r[1], r[2]) for r in h.conn.execute("SELECT status, COUNT(*), SUM(total_cents) FROM orders GROUP BY status")}
    assert body["orders"] == {status: by_status.get(status, (0, 0))[0] for status in ("placed", "fulfilled", "cancelled")}
    assert body["revenue_cents"] == {status: by_status.get(status, (0, 0))[1] for status in ("placed", "fulfilled")}
    active, inactive = h.conn.execute("SELECT SUM(active = 1), SUM(active = 0) FROM products").fetchone()
    assert body["products"] == {"active": active, "inactive": inactive}
    assert {s["store_code"] for s in body["stores"]} == {"S1", "S2", "S3"}


def test_summary_report_on_an_empty_database(h):
    body = h.rows("/reports/summary")
    assert body["stores"] == []
    assert body["totals"] == {"skus": 0, "units": 0, "value_cents": 0, "low_stock": 0}
    assert body["orders"] == {"placed": 0, "fulfilled": 0, "cancelled": 0}
    assert body["revenue_cents"] == {"placed": 0, "fulfilled": 0}
    assert body["products"] == {"active": 0, "inactive": 0}
    assert ISO_RE.match(body["generated_at"])


# --------------------------------------------------------------------------- sales
def sales_scenario(h: Harness) -> dict:
    s1, s2 = h.store("S1"), h.store("S2", "West")
    a = h.product("SKU-A", price=1000, name="Alpha")
    b = h.product("SKU-B", price=2500, name="Beta")
    day10, day3, day1, day40 = ago(10), ago(3), ago(1), ago(40)
    h.order(s1, [(a, 2, 1000)], status="placed", created_at=day10)
    h.order(s1, [(b, 1, 2500)], status="fulfilled", created_at=day10)
    h.order(s2, [(a, 1, 1000), (b, 2, 2500)], status="placed", created_at=day3)
    h.order(s1, [(a, 5, 1000)], status="cancelled", created_at=day1)  # excluded everywhere
    h.order(s2, [(b, 4, 2500)], status="fulfilled", created_at=day40)  # outside the default window
    return {"s1": s1, "s2": s2, "a": a, "b": b, "day10": day10[:10], "day3": day3[:10], "day40": day40[:10]}


def test_sales_by_day(h):
    ids = sales_scenario(h)
    rows = h.rows("/reports/sales")
    assert rows == h.rows("/reports/sales", days=30, group_by="day")
    assert rows == [
        {"day": ids["day10"], "product_id": None, "sku": None, "name": None, "orders": 2, "units": 3, "revenue_cents": 4500},
        {"day": ids["day3"], "product_id": None, "sku": None, "name": None, "orders": 1, "units": 3, "revenue_cents": 6000},
    ]
    wide = h.rows("/reports/sales", days=90)
    assert [r["day"] for r in wide] == [ids["day40"], ids["day10"], ids["day3"]]
    assert (wide[0]["orders"], wide[0]["units"], wide[0]["revenue_cents"]) == (1, 4, 10000)
    assert h.rows("/reports/sales", days=2) == []
    assert rows == reports.sales_report(h.conn)


def test_sales_by_product(h):
    ids = sales_scenario(h)
    rows = h.rows("/reports/sales", group_by="product")
    assert rows == [
        {"day": None, "product_id": ids["b"], "sku": "SKU-B", "name": "Beta", "orders": 2, "units": 3, "revenue_cents": 7500},
        {"day": None, "product_id": ids["a"], "sku": "SKU-A", "name": "Alpha", "orders": 2, "units": 3, "revenue_cents": 3000},
    ]
    wide = h.rows("/reports/sales", group_by="product", days=90)
    assert [(r["sku"], r["orders"], r["units"], r["revenue_cents"]) for r in wide] == [("SKU-B", 3, 7, 17500), ("SKU-A", 2, 3, 3000)]
    assert rows == reports.sales_report(h.conn, group_by="product")


def test_sales_store_filter(h):
    ids = sales_scenario(h)
    days = h.rows("/reports/sales", store_id=ids["s1"])
    assert [(r["day"], r["orders"], r["units"], r["revenue_cents"]) for r in days] == [(ids["day10"], 2, 3, 4500)]
    products = h.rows("/reports/sales", store_id=ids["s1"], group_by="product")
    assert [(r["sku"], r["revenue_cents"]) for r in products] == [("SKU-B", 2500), ("SKU-A", 2000)]
    assert h.rows("/reports/sales", store_id=999) == []
    assert h.rows("/reports/sales", store_id=ids["s2"], days=90, group_by="product") == [
        {"day": None, "product_id": ids["b"], "sku": "SKU-B", "name": "Beta", "orders": 2, "units": 6, "revenue_cents": 15000},
        {"day": None, "product_id": ids["a"], "sku": "SKU-A", "name": "Alpha", "orders": 1, "units": 1, "revenue_cents": 1000},
    ]


# --------------------------------------------------------------------------- CSV export
def test_reorder_csv_export_contract(h):
    ids = reorder_scenario(h)
    hostile = h.product("SKU-H", price=10, reorder_point=3, name='=HYPERLINK("http://x")')
    h.move(ids["s1"], hostile, 1, "receipt", "opening", days_ago=1)
    report = h.rows("/reports/reorder")
    r = h.get("/reports/reorder.csv")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    assert r.headers["content-disposition"] == f'attachment; filename="{common.csv_filename("reorder")}"'
    assert r.headers["x-row-count"] == str(len(report)) == "6"
    assert "x-truncated" not in r.headers
    assert r.text.endswith("\n") and "\r" not in r.text
    parsed = list(csv.reader(io.StringIO(r.text)))
    assert parsed[0] == COLUMNS
    assert len(parsed) == len(report) + 1
    for cells, row in zip(parsed[1:], report, strict=True):
        got = dict(zip(COLUMNS, cells, strict=True))
        assert got["store_code"] == row["store_code"] and got["sku"] == row["sku"]
        assert int(got["on_hand"]) == row["on_hand"] and int(got["suggested_qty"]) == row["suggested_qty"]
        assert got["days_of_cover"] == ("" if row["days_of_cover"] is None else str(row["days_of_cover"]))
        assert got["daily_velocity"] == str(row["daily_velocity"])
    names = {cells[3]: cells[4] for cells in parsed[1:]}
    assert names["SKU-H"] == "'=HYPERLINK(\"http://x\")"  # formula-injection guard
    assert names["SKU-W"] == "Product SKU-W"
    text, count, truncated = reports.export_reorder_csv(h.conn)
    assert (text, count, truncated) == (r.text, 6, False)


def test_reorder_csv_filters_and_row_cap(h, monkeypatch):
    ids = reorder_scenario(h)
    filtered = h.get("/reports/reorder.csv", days=7, store_id=ids["s1"])
    assert filtered.status_code == 200
    parsed = list(csv.reader(io.StringIO(filtered.text)))
    report = h.rows("/reports/reorder", days=7, store_id=ids["s1"])
    assert filtered.headers["x-row-count"] == str(len(report)) == "3"
    assert [cells[COLUMNS.index("days")] for cells in parsed[1:]] == ["7", "7", "7"]
    assert {cells[1] for cells in parsed[1:]} == {"S1"}
    monkeypatch.setattr(common, "CSV_ROW_CAP", 2)
    capped = h.get("/reports/reorder.csv")
    assert capped.status_code == 200
    assert capped.headers["x-row-count"] == "2"
    assert capped.headers["x-truncated"] == "true"
    rows = list(csv.reader(io.StringIO(capped.text)))
    assert len(rows) == 3 and rows[0] == COLUMNS
    assert [(c[1], c[3]) for c in rows[1:]] == [("S2", "SKU-W"), ("S2", "SKU-G")]  # first rows of the ordered report
    text, count, truncated = reports.export_reorder_csv(h.conn)
    assert (count, truncated) == (2, True) and text == capped.text
    monkeypatch.setattr(common, "CSV_ROW_CAP", 5)
    exact = h.get("/reports/reorder.csv")
    assert exact.headers["x-row-count"] == "5" and "x-truncated" not in exact.headers


def test_reorder_csv_empty_report_keeps_header(h):
    h.store("S1")
    r = h.get("/reports/reorder.csv")
    assert r.status_code == 200
    assert r.text == ",".join(COLUMNS) + "\n"
    assert r.headers["x-row-count"] == "0"


# --------------------------------------------------------------------------- bridge parity
def _route_for(path: str):
    matches = [(method, pattern, handler) for method, pattern, handler in reports.BRIDGE_ROUTES if re.compile(pattern).match(path)]
    assert len(matches) == 1, (path, matches)
    return matches[0]


def _call(h: Harness, **query: str) -> BridgeCall:
    return BridgeCall(conn=h.conn, params={}, query=dict(query), body={}, headers={}, out_headers={})


def test_bridge_routes_cover_exactly_the_four_report_paths(h):
    assert len(reports.BRIDGE_ROUTES) == 4
    assert {method for method, _, _ in reports.BRIDGE_ROUTES} == {"GET"}
    for path in PATHS:
        method, _, handler = _route_for(path)
        assert method == "GET" and callable(handler)
    for bogus in ("/reports", "/reports/reorderXcsv", "/reports/reorder/1", "/reports/summary/", "/reports/sales.csv"):
        assert not [p for _, p, _ in reports.BRIDGE_ROUTES if re.compile(p).match(bogus)], bogus


def test_bridge_handlers_return_the_router_bodies(h):
    ids = reorder_scenario(h)
    h.order(ids["s1"], [(ids["widget"], 2, 1000)], days_ago=1)
    _, _, reorder = _route_for("/reports/reorder")
    assert reorder(_call(h, days="7")) == (200, h.rows("/reports/reorder", days=7))
    assert reorder(_call(h, store_id=str(ids["s2"]))) == (200, h.rows("/reports/reorder", store_id=ids["s2"]))
    assert reorder(_call(h)) == (200, h.rows("/reports/reorder"))
    _, _, summary = _route_for("/reports/summary")
    status, body = summary(_call(h))
    expected = h.rows("/reports/summary")
    assert status == 200 and ISO_RE.match(body.pop("generated_at"))
    expected.pop("generated_at")
    assert body == expected
    _, _, sales = _route_for("/reports/sales")
    assert sales(_call(h, group_by="product", days="45")) == (200, h.rows("/reports/sales", group_by="product", days=45))
    assert sales(_call(h)) == (200, h.rows("/reports/sales"))


def test_bridge_handlers_enforce_the_same_bounds(h):
    reorder_scenario(h)
    _, _, reorder = _route_for("/reports/reorder")
    _, _, sales = _route_for("/reports/sales")
    _, _, export = _route_for("/reports/reorder.csv")
    for handler in (reorder, sales, export):
        for bad in ("0", "366", "abc", "-1"):
            with pytest.raises(ServiceError) as ei:
                handler(_call(h, days=bad))
            assert (ei.value.status, ei.value.code) == (422, VALIDATION), bad
        with pytest.raises(ServiceError) as ei:
            handler(_call(h, store_id="x"))
        assert ei.value.status == 422
    with pytest.raises(ServiceError) as ei:
        sales(_call(h, group_by="bogus"))
    assert (ei.value.status, ei.value.code) == (422, VALIDATION)


def test_bridge_csv_handler_sets_headers_and_returns_text(h, monkeypatch):
    reorder_scenario(h)
    _, _, export = _route_for("/reports/reorder.csv")
    call = _call(h, days="7")
    status, text = export(call)
    via_http = h.get("/reports/reorder.csv", days=7)
    assert status == 200 and text == via_http.text
    assert call.out_headers["Content-Type"] == "text/csv; charset=utf-8"
    assert call.out_headers["Content-Disposition"] == f'attachment; filename="{common.csv_filename("reorder")}"'
    assert call.out_headers["X-Row-Count"] == via_http.headers["x-row-count"] == "5"  # the window never changes membership
    assert "X-Truncated" not in call.out_headers
    monkeypatch.setattr(common, "CSV_ROW_CAP", 1)
    capped = _call(h)
    status, text = export(capped)
    assert status == 200 and capped.out_headers["X-Truncated"] == "true" and capped.out_headers["X-Row-Count"] == "1"
    assert text == h.get("/reports/reorder.csv").text


# --------------------------------------------------------------------------- documentation / hygiene
def test_openapi_documents_every_report_operation(h):
    spec = h.client.get("/openapi.json").json()
    for path in PATHS:
        op = spec["paths"][path]["get"]
        assert op["tags"] == ["reports"] and op["summary"], path
        assert "200" in op["responses"], path
    assert "text/csv" in spec["paths"]["/reports/reorder.csv"]["get"]["responses"]["200"]["content"]
    for path in ("/reports/reorder", "/reports/reorder.csv", "/reports/sales"):
        assert "422" in spec["paths"][path]["get"]["responses"], path
    reorder = spec["paths"]["/reports/reorder"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert reorder["type"] == "array" and "ReorderRow" in reorder["items"]["$ref"]
    assert set(spec["components"]["schemas"]["ReorderRow"]["properties"]) == set(COLUMNS)
    summary = spec["components"]["schemas"]["SummaryReport"]["properties"]
    assert set(summary) == {"stores", "totals", "orders", "revenue_cents", "products", "generated_at"}
    assert set(spec["components"]["schemas"]["SalesRow"]["properties"]) == {"day", "product_id", "sku", "name", "orders", "units", "revenue_cents"}
    params = {p["name"]: p for p in spec["paths"]["/reports/sales"]["get"]["parameters"]}
    assert params["days"]["schema"]["minimum"] == 1 and params["days"]["schema"]["maximum"] == 365 and params["days"]["schema"]["default"] == 30
    assert params["group_by"]["schema"]["pattern"] == "^(day|product)$"


def test_reports_module_is_framework_free():
    tree = ast.parse(Path(reports.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0] if not node.level else f".{node.module or ''}")
            if node.level and not node.module:
                imported.update(f".{alias.name}" for alias in node.names)
    assert not imported & {"fastapi", "starlette", "uvicorn", "threading", "queue"}
    assert not imported & {".deps", ".main", ".observability", ".security"}
    assert reports.REORDER_COLUMNS == COLUMNS
