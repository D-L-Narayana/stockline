import { inject, Injectable } from '@angular/core';
import { map, Observable } from 'rxjs';
import { ApiService } from './api.service';
import { Order, OrderIn, OrderStatus, Page } from './models';

export interface PlacedOrder {
  order: Order;
  /** `true` when the server replayed an earlier order with the same Idempotency-Key (HTTP 200 instead of 201). */
  replayed: boolean;
}

@Injectable({ providedIn: 'root' })
export class OrdersService {
  private readonly api = inject(ApiService);

  list(opts: { storeId?: number | null; status?: OrderStatus | ''; limit?: number; offset?: number } = {}): Observable<Page<Order>> {
    return this.api.get<Page<Order>>('/orders', {
      store_id: opts.storeId ?? undefined,
      status: opts.status || undefined,
      limit: opts.limit ?? 20,
      offset: opts.offset ?? 0,
    });
  }

  get(id: number): Observable<Order> {
    return this.api.get<Order>(`/orders/${id}`);
  }

  /** Places an order atomically; the key makes a retried checkout safe (same key + same body -> same order). */
  place(body: OrderIn, idempotencyKey: string = newIdempotencyKey()): Observable<PlacedOrder> {
    return this.api.post<Order>('/orders', body, idempotencyKey).pipe(map((r) => ({ order: r.body, replayed: r.replayed })));
  }

  cancel(id: number): Observable<Order> {
    return this.api.post<Order>(`/orders/${id}/cancel`, {}).pipe(map((r) => r.body));
  }

  fulfil(id: number): Observable<Order> {
    return this.api.post<Order>(`/orders/${id}/fulfil`, {}).pipe(map((r) => r.body));
  }
}

export function newIdempotencyKey(): string {
  const c = globalThis.crypto as Crypto | undefined;
  return c?.randomUUID ? `ui-${c.randomUUID()}` : `ui-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
