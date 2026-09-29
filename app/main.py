"""FastAPI application — thin HTTP layer over ``service``."""
from __future__ import annotations

import os
import queue
import sqlite3
import threading
import time
import uuid
from typing import Generator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from . import __version__, db, schemas, service
from .seed import seed

app = FastAPI(
    title="StockLine Inventory API",
    version=__version__,
    description=(
        "Multi-store inventory and order management. Stock is an append-only ledger "
        "(`stock_movements`) with a cached balance; orders are idempotent via the "
        "`Idempotency-Key` header; adjustments support optimistic locking."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
)

class ConnectionPool:
    """Tiny SQLite connection pool. Each request checks a connection out for its
    whole lifetime (dependency -> endpoint -> teardown), so no two requests
    ever share a connection. Writers still serialise on SQLite's file lock via
    BEGIN IMMEDIATE (see db.transaction)."""

    def __init__(self, size: int = 8) -> None:
        self._q: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue()
        self._size = size
        self._created = 0
        self._lock = threading.Lock()
        self._initialised = False

    def acquire(self) -> sqlite3.Connection:
        try:
            return self._q.get_nowait()
        except queue.Empty:
            with self._lock:
                if self._created < self._size:
                    self._created += 1
                    return self._new()
            return self._q.get(timeout=10)

    def release(self, conn: sqlite3.Connection) -> None:
        if conn.in_transaction:  # defensive: never return a dirty connection
            conn.execute("ROLLBACK")
        self._q.put(conn)

    def _new(self) -> sqlite3.Connection:
        conn = db.connect()
        if not self._initialised:
            db.init_schema(conn)
            if os.environ.get("STOCKLINE_SEED", "1") == "1":
                seed(conn)
            self._initialised = True
        return conn


pool = ConnectionPool()


def reset_state() -> None:
    """Test hook: drop the pool so a new DB path takes effect."""
    global pool
    pool = ConnectionPool()


def get_conn() -> Generator[sqlite3.Connection, None, None]:
    conn = pool.acquire()
    try:
        yield conn
    finally:
        pool.release(conn)


@app.middleware("http")
async def request_meta(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    t0 = time.perf_counter()
    response: Response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    response.headers["X-Response-Time-ms"] = f"{(time.perf_counter() - t0) * 1000:.1f}"
    return response


@app.exception_handler(service.ServiceError)
async def service_error_handler(_: Request, exc: service.ServiceError):
    return JSONResponse(status_code=exc.status, content={"detail": exc.detail})


# ------------------------------------------------------------------ health
@app.get("/health", tags=["system"])
def health(conn: sqlite3.Connection = Depends(get_conn)):
    conn.execute("SELECT 1")
    return {"status": "ok", "version": __version__}


@app.get("/integrity", tags=["system"], summary="Ledger vs cached balance check")
def integrity(conn: sqlite3.Connection = Depends(get_conn)):
    return service.ledger_integrity(conn)


# ------------------------------------------------------------------ stores
@app.post("/stores", response_model=schemas.Store, status_code=201, tags=["stores"])
def create_store(body: schemas.StoreIn, conn: sqlite3.Connection = Depends(get_conn)):
    return service.create_store(conn, body)


@app.get("/stores", response_model=list[schemas.Store], tags=["stores"])
def list_stores(conn: sqlite3.Connection = Depends(get_conn)):
    return service.list_stores(conn)


# ------------------------------------------------------------------ products
@app.post("/products", response_model=schemas.Product, status_code=201, tags=["products"])
def create_product(body: schemas.ProductIn, conn: sqlite3.Connection = Depends(get_conn)):
    return service.create_product(conn, body)


@app.get("/products", response_model=schemas.Page, tags=["products"])
def list_products(
    q: str | None = Query(None, max_length=60),
    category: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
):
    items, total = service.list_products(conn, q, category, limit, offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/products/{product_id}", response_model=schemas.Product, tags=["products"])
def get_product(product_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    return service.get_product(conn, product_id)


# ------------------------------------------------------------------ inventory
@app.get("/inventory", response_model=schemas.Page, tags=["inventory"])
def list_inventory(
    store_id: int | None = None,
    low_stock: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
):
    items, total = service.list_inventory(conn, store_id, low_stock, limit, offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/inventory/{store_id}/{product_id}", response_model=schemas.InventoryRow, tags=["inventory"])
def get_inventory(store_id: int, product_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    return service.get_inventory_row(conn, store_id, product_id)


@app.post("/inventory/{store_id}/{product_id}/adjust", response_model=schemas.InventoryRow, tags=["inventory"],
          summary="Receive / return / adjust stock (optimistic locking via expected_version)")
def adjust(store_id: int, product_id: int, body: schemas.StockAdjust, conn: sqlite3.Connection = Depends(get_conn)):
    return service.adjust_stock(conn, store_id, product_id, body)


@app.get("/inventory/{store_id}/{product_id}/movements", response_model=list[schemas.Movement], tags=["inventory"])
def list_movements(store_id: int, product_id: int, limit: int = Query(50, ge=1, le=500), conn: sqlite3.Connection = Depends(get_conn)):
    return service.movements(conn, store_id, product_id, limit)


@app.post("/transfers", tags=["inventory"], summary="Atomic store-to-store transfer")
def transfer(body: schemas.TransferIn, conn: sqlite3.Connection = Depends(get_conn)):
    return service.transfer(conn, body)


# ------------------------------------------------------------------ orders
@app.post("/orders", response_model=schemas.Order, tags=["orders"],
          summary="Place order (reserves stock atomically; idempotent with Idempotency-Key header)")
def place_order(
    body: schemas.OrderIn,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=64),
    conn: sqlite3.Connection = Depends(get_conn),
):
    order, created = service.place_order(conn, body, idempotency_key)
    response.status_code = 201 if created else 200
    response.headers["Idempotent-Replayed"] = "false" if created else "true"
    return order


@app.get("/orders", response_model=schemas.Page, tags=["orders"])
def list_orders(
    store_id: int | None = None,
    status: str | None = Query(None, pattern="^(placed|cancelled|fulfilled)$"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
):
    items, total = service.list_orders(conn, store_id, status, limit, offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/orders/{order_id}", response_model=schemas.Order, tags=["orders"])
def get_order(order_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    return service.get_order(conn, order_id)


@app.post("/orders/{order_id}/cancel", response_model=schemas.Order, tags=["orders"], summary="Cancel and restock (idempotent)")
def cancel_order(order_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    return service.cancel_order(conn, order_id)


@app.post("/orders/{order_id}/fulfil", response_model=schemas.Order, tags=["orders"])
def fulfil_order(order_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    return service.fulfil_order(conn, order_id)


# ------------------------------------------------------------------ reports
@app.get("/reports/reorder", tags=["reports"], summary="SKUs at/below reorder point with 30-day velocity")
def reorder(conn: sqlite3.Connection = Depends(get_conn)):
    return service.reorder_report(conn)


# ------------------------------------------------------------------ static demo UI
_public = Path(__file__).resolve().parent.parent / "public"
if _public.exists():
    app.mount("/", StaticFiles(directory=str(_public), html=True), name="ui")
