import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { API_BASE } from './api-base.token';
import { ApiService } from './api.service';

describe('ApiService', () => {
  let api: ApiService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting(), { provide: API_BASE, useValue: 'http://api.test/' }],
    });
    api = TestBed.inject(ApiService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('joins the base URL without a double slash', () => {
    expect(api.url('/stores')).toBe('http://api.test/stores');
    expect(api.url('stores')).toBe('http://api.test/stores');
  });

  it('drops empty query values so the API only sees real filters', () => {
    api.get('/inventory', { store_id: 2, low_stock: true, q: '', offset: 0, limit: undefined }).subscribe();
    const req = http.expectOne((r) => r.url === 'http://api.test/inventory');
    expect(req.request.params.keys().sort()).toEqual(['low_stock', 'offset', 'store_id']);
    expect(req.request.params.get('low_stock')).toBe('true');
    req.flush({ items: [], total: 0, limit: 50, offset: 0 });
  });

  it('sends Idempotency-Key and reports a replayed response', () => {
    let result: { body: unknown; replayed: boolean; status: number } | undefined;
    api.post('/orders', { store_id: 1, lines: [] }, 'ui-abc').subscribe((r) => (result = r));
    const req = http.expectOne('http://api.test/orders');
    expect(req.request.method).toBe('POST');
    expect(req.request.headers.get('Idempotency-Key')).toBe('ui-abc');
    req.flush({ id: 7 }, { status: 200, statusText: 'OK', headers: { 'Idempotent-Replayed': 'true' } });
    expect(result).toMatchObject({ replayed: true, status: 200, body: { id: 7 } });
  });
});
