"""HTTP surface of the catalogue domain — ``/stores`` and ``/products`` — as thin wrappers over :mod:`app.catalog`.

Every operation documents its error bodies (``ErrorBody``) and uses the typed models from the domain
module; query bounds here are mirrored exactly by ``catalog.BRIDGE_ROUTES`` for the browser demo.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query, Response

from .. import catalog, schemas
from ..common import ErrorBody, page
from ..deps import get_conn

router = APIRouter(tags=["catalog"])

_404 = {404: {"model": ErrorBody, "description": "No such store or product"}}
_409 = {409: {"model": ErrorBody, "description": "Duplicate store code or SKU"}}
_422 = {422: {"model": ErrorBody, "description": "Validation error"}}


# --------------------------------------------------------------------------- stores
@router.post("/stores", response_model=schemas.Store, status_code=201, summary="Create a store", responses={**_409, **_422})
def create_store(body: schemas.StoreIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return catalog.create_store(conn, body)


@router.get("/stores", response_model=list[schemas.Store], summary="List stores")
def list_stores(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
    return catalog.list_stores(conn)


@router.get("/stores/{store_id}", response_model=schemas.Store, summary="Get a store", responses={**_404, **_422})
def get_store(store_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return catalog.get_store(conn, store_id)


@router.patch(
    "/stores/{store_id}",
    response_model=schemas.Store,
    summary="Update a store's name and/or region (the code is immutable; an empty patch is a 422)",
    responses={**_404, **_422},
)
def update_store(store_id: int, body: catalog.StorePatch, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return catalog.update_store(conn, store_id, body)


# --------------------------------------------------------------------------- products
@router.post("/products", response_model=catalog.ProductOut, status_code=201, summary="Create a product", responses={**_409, **_422})
def create_product(body: schemas.ProductIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return catalog.create_product(conn, body)


@router.get("/products", response_model=catalog.ProductPage, summary="List products (active only unless include_inactive)", responses=_422)
def list_products(
    q: str | None = Query(None, max_length=60, description="Case-insensitive substring match on name or SKU"),
    category: str | None = Query(None, description="Exact category"),
    include_inactive: bool = Query(False, description="Also return soft-deleted (inactive) products"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict:
    items, total = catalog.list_products(conn, q, category, limit, offset, include_inactive=include_inactive)
    return page(items, total, limit, offset)


@router.get("/products/{product_id}", response_model=catalog.ProductOut, summary="Get a product (inactive products included)", responses={**_404, **_422})
def get_product(product_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return catalog.get_product(conn, product_id)


@router.patch(
    "/products/{product_id}",
    response_model=catalog.ProductOut,
    summary="Update product fields (the SKU is immutable; sets updated_at; an empty patch is a 422)",
    responses={**_404, **_422},
)
def update_product(product_id: int, body: catalog.ProductPatch, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
    return catalog.update_product(conn, product_id, body)


@router.delete(
    "/products/{product_id}",
    status_code=204,
    response_class=Response,
    summary="Deactivate a product (soft delete: active=false, idempotent)",
    responses={**_404, **_422},
)
def deactivate_product(product_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> Response:
    catalog.deactivate_product(conn, product_id)
    return Response(status_code=204)


@router.get(
    "/products/{product_id}/inventory",
    response_model=list[catalog.ProductInventoryRow],
    summary="Stock of one product across all stores, valued at the current price",
    responses={**_404, **_422},
)
def product_inventory(product_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
    return catalog.product_inventory(conn, product_id)
