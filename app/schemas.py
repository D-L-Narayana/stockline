from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class StoreIn(BaseModel):
    code: str = Field(min_length=2, max_length=16, pattern=r"^[A-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=80)
    region: str = Field(min_length=1, max_length=40)


class Store(StoreIn):
    id: int


class ProductIn(BaseModel):
    sku: str = Field(min_length=3, max_length=32, pattern=r"^[A-Z0-9-]+$")
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=40)
    price_cents: int = Field(ge=0)
    reorder_point: int = Field(default=10, ge=0)


class Product(ProductIn):
    id: int
    active: bool = True


class StockAdjust(BaseModel):
    delta: int = Field(description="Positive to receive stock, negative to remove")
    reason: Literal["receipt", "return", "adjustment"]
    reference: str | None = Field(default=None, max_length=64)
    expected_version: int | None = Field(default=None, ge=0, description="Optimistic lock; 409 if stale")

    @field_validator("delta")
    @classmethod
    def non_zero(cls, v: int) -> int:
        if v == 0:
            raise ValueError("delta must be non-zero")
        return v


class InventoryRow(BaseModel):
    store_id: int
    store_code: str
    product_id: int
    sku: str
    name: str
    on_hand: int
    reorder_point: int
    version: int
    below_reorder: bool


class TransferIn(BaseModel):
    from_store_id: int
    to_store_id: int
    product_id: int
    quantity: int = Field(gt=0)


class OrderLineIn(BaseModel):
    product_id: int
    quantity: int = Field(gt=0, le=1000)


class OrderIn(BaseModel):
    store_id: int
    lines: list[OrderLineIn] = Field(min_length=1, max_length=50)

    @field_validator("lines")
    @classmethod
    def unique_products(cls, v: list[OrderLineIn]) -> list[OrderLineIn]:
        ids = [line.product_id for line in v]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate product_id in lines")
        return v


class OrderLine(BaseModel):
    product_id: int
    sku: str
    quantity: int
    unit_price_cents: int
    line_total_cents: int


class Order(BaseModel):
    id: int
    store_id: int
    status: Literal["placed", "cancelled", "fulfilled"]
    total_cents: int
    idempotency_key: str | None
    created_at: str
    lines: list[OrderLine]


class Movement(BaseModel):
    id: int
    store_id: int
    product_id: int
    delta: int
    reason: str
    reference: str | None
    created_at: str


class Page(BaseModel):
    items: list
    total: int
    limit: int
    offset: int
