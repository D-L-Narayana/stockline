/** TypeScript mirrors of the FastAPI/Pydantic schemas in `app/schemas.py`. */

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface Store {
  id: number;
  code: string;
  name: string;
  region: string;
}

export interface Product {
  id: number;
  sku: string;
  name: string;
  category: string;
  price_cents: number;
  reorder_point: number;
  active: boolean;
  updated_at: string | null;
}

export interface ProductIn {
  sku: string;
  name: string;
  category: string;
  price_cents: number;
  reorder_point: number;
}

export interface InventoryRow {
  store_id: number;
  store_code: string;
  product_id: number;
  sku: string;
  name: string;
  on_hand: number;
  reorder_point: number;
  version: number;
  below_reorder: boolean;
  price_cents: number;
  value_cents: number;
}

export interface ReceiptIn {
  reference: string;
  lines: { product_id: number; quantity: number }[];
}

export type OrderStatus = 'placed' | 'cancelled' | 'fulfilled';

export interface OrderLine {
  product_id: number;
  sku: string;
  quantity: number;
  unit_price_cents: number;
  line_total_cents: number;
}

export interface Order {
  id: number;
  store_id: number;
  status: OrderStatus;
  total_cents: number;
  idempotency_key: string | null;
  created_at: string;
  lines: OrderLine[];
}

export interface OrderIn {
  store_id: number;
  lines: { product_id: number; quantity: number }[];
}

export interface SummaryReport {
  stores: { store_id: number; store_code: string; store_name: string; skus: number; units: number; value_cents: number; low_stock: number }[];
  totals: { skus: number; units: number; value_cents: number; low_stock: number };
  orders: { placed: number; fulfilled: number; cancelled: number };
  revenue_cents: { placed: number; fulfilled: number };
  products: { active: number; inactive: number };
  generated_at: string;
}

/** Normalised shape of the API's `{detail, code, request_id}` error body (see `ApiErrorInterceptor`). */
export interface ApiError {
  status: number;
  code: string;
  detail: string;
  requestId: string | null;
}
