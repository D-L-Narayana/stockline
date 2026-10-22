import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { API_BASE } from './api-base.token';
import { Order } from './models';
import { newIdempotencyKey, OrdersService } from './orders.service';

const order: Order = { id: 1, store_id: 1, status: 'placed', total_cents: 1000, idempotency_key: 'k', created_at: '2026-01-01T00:00:00.000Z', lines: [] };

describe('OrdersService', () => {
  let svc: OrdersService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting(), { provide: API_BASE, useValue: '/api' }] });
    svc = TestBed.inject(OrdersService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('generates unique ui-prefixed idempotency keys', () => {
    const a = newIdempotencyKey();
    const b = newIdempotencyKey();
    expect(a).toMatch(/^ui-/);
    expect(a).not.toBe(b);
  });

  it('places an order with a fresh key and maps 201 to replayed=false', () => {
    let placed: { replayed: boolean } | undefined;
    svc.place({ store_id: 1, lines: [{ product_id: 3, quantity: 2 }] }).subscribe((r) => (placed = r));
    const req = http.expectOne('/api/orders');
    expect(req.request.headers.get('Idempotency-Key')).toMatch(/^ui-/);
    expect(req.request.body).toEqual({ store_id: 1, lines: [{ product_id: 3, quantity: 2 }] });
    req.flush(order, { status: 201, statusText: 'Created', headers: { 'Idempotent-Replayed': 'false' } });
    expect(placed?.replayed).toBe(false);
  });

  it('filters the list by store and status, omitting empty status', () => {
    svc.list({ storeId: 2, status: '' }).subscribe();
    const req = http.expectOne((r) => r.url === '/api/orders');
    expect(req.request.params.get('store_id')).toBe('2');
    expect(req.request.params.has('status')).toBe(false);
    req.flush({ items: [], total: 0, limit: 20, offset: 0 });
  });

  it('posts to /cancel and /fulfil', () => {
    svc.cancel(5).subscribe();
    http.expectOne('/api/orders/5/cancel').flush({ ...order, status: 'cancelled' });
    svc.fulfil(6).subscribe();
    http.expectOne('/api/orders/6/fulfil').flush({ ...order, status: 'fulfilled' });
  });
});
