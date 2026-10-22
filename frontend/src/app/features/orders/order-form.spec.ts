import { provideHttpClient, withInterceptors } from '@angular/common/http';
import { apiErrorInterceptor } from '../../core/api-error.interceptor';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { FormControl } from '@angular/forms';
import { provideRouter, Router } from '@angular/router';
import { vi } from 'vitest';
import { OrderForm, uniqueProducts } from './order-form';

describe('OrderForm', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [OrderForm],
      providers: [provideRouter([]), provideHttpClient(withInterceptors([apiErrorInterceptor])), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  function create() {
    const fixture = TestBed.createComponent(OrderForm);
    fixture.detectChanges();
    http.expectOne((r) => r.url.endsWith('/stores')).flush([{ id: 1, code: 'BLR-01', name: 'Bengaluru', region: 'South' }]);
    http.expectOne((r) => r.url.endsWith('/products')).flush({
      items: [
        { id: 10, sku: 'A-1', name: 'A', category: 'c', price_cents: 500, reorder_point: 1, active: true, updated_at: null },
        { id: 11, sku: 'B-2', name: 'B', category: 'c', price_cents: 250, reorder_point: 1, active: true, updated_at: null },
      ],
      total: 2, limit: 100, offset: 0,
    });
    fixture.detectChanges();
    return fixture;
  }

  it('starts with one empty line and an invalid form', () => {
    const cmp = create().componentInstance;
    expect(cmp.lines.length).toBe(1);
    expect(cmp.form.invalid).toBe(true);
  });

  it('rejects duplicate products and quantities above 1000', () => {
    const cmp = create().componentInstance;
    cmp.addLine();
    cmp.form.patchValue({ store_id: 1 });
    cmp.lines.at(0).setValue({ product_id: 10, quantity: 2 });
    cmp.lines.at(1).setValue({ product_id: 10, quantity: 1 });
    expect(cmp.lines.hasError('duplicateProducts')).toBe(true);
    cmp.lines.at(1).setValue({ product_id: 11, quantity: 1001 });
    expect(cmp.lines.hasError('duplicateProducts')).toBe(false);
    expect(cmp.lines.at(1).controls.quantity.hasError('max')).toBe(true);
  });

  it('computes the running total from product prices', () => {
    const cmp = create().componentInstance;
    cmp.addLine();
    cmp.lines.at(0).setValue({ product_id: 10, quantity: 2 }); // 2 × 500
    cmp.lines.at(1).setValue({ product_id: 11, quantity: 4 }); // 4 × 250
    expect(cmp.total()).toBe(2000);
  });

  it('submits with the Idempotency-Key and navigates to the new order', () => {
    const fixture = create();
    const cmp = fixture.componentInstance;
    const router = TestBed.inject(Router);
    const nav = vi.spyOn(router, 'navigate').mockResolvedValue(true);
    cmp.form.patchValue({ store_id: 1 });
    cmp.lines.at(0).setValue({ product_id: 10, quantity: 2 });
    cmp.submit();
    const req = http.expectOne((r) => r.url.endsWith('/orders'));
    expect(req.request.headers.get('Idempotency-Key')).toBe(cmp.idempotencyKey());
    req.flush({ id: 42, store_id: 1, status: 'placed', total_cents: 1000, idempotency_key: cmp.idempotencyKey(), created_at: '', lines: [] }, { status: 201, statusText: 'Created' });
    expect(nav).toHaveBeenCalledWith(['/orders', 42], { state: { replayed: false } });
  });

  it('surfaces insufficient_stock as an ApiError and keeps the same key for retry', () => {
    const fixture = create();
    const cmp = fixture.componentInstance;
    const key = cmp.idempotencyKey();
    cmp.form.patchValue({ store_id: 1 });
    cmp.lines.at(0).setValue({ product_id: 10, quantity: 999 });
    cmp.submit();
    http.expectOne((r) => r.url.endsWith('/orders')).flush({ detail: 'short by 990', code: 'insufficient_stock' }, { status: 409, statusText: 'Conflict' });
    expect(cmp.error()?.code).toBe('insufficient_stock');
    expect(cmp.idempotencyKey()).toBe(key);
    expect(cmp.submitting()).toBe(false);
  });

  it('uniqueProducts ignores unselected (0) lines', () => {
    const c = new FormControl([{ product_id: 0 }, { product_id: 0 }, { product_id: 3 }]);
    expect(uniqueProducts(c)).toBeNull();
  });
});
