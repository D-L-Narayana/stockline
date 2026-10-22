import { HttpClient, provideHttpClient, withInterceptors } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { API_KEY_STORAGE, apiErrorInterceptor } from './api-error.interceptor';
import { ApiError } from './models';

describe('apiErrorInterceptor', () => {
  let http: HttpClient;
  let ctrl: HttpTestingController;

  beforeEach(() => {
    localStorage.removeItem(API_KEY_STORAGE);
    TestBed.configureTestingModule({
      providers: [provideHttpClient(withInterceptors([apiErrorInterceptor])), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpClient);
    ctrl = TestBed.inject(HttpTestingController);
  });

  afterEach(() => ctrl.verify());

  it('normalises the FastAPI {detail, code, request_id} body', () => {
    let err: ApiError | undefined;
    http.post('/orders', {}).subscribe({ error: (e: ApiError) => (err = e) });
    ctrl.expectOne('/orders').flush({ detail: 'Insufficient stock for SKU GRO-RICE-5KG', code: 'insufficient_stock', request_id: 'req-1' }, { status: 409, statusText: 'Conflict' });
    expect(err).toEqual({ status: 409, code: 'insufficient_stock', detail: 'Insufficient stock for SKU GRO-RICE-5KG', requestId: 'req-1' });
  });

  it('flattens 422 validation lists into one readable message', () => {
    let err: ApiError | undefined;
    http.post('/products', {}).subscribe({ error: (e: ApiError) => (err = e) });
    ctrl.expectOne('/products').flush(
      { detail: [{ loc: ['body', 'sku'], msg: 'String should match pattern' }, { loc: ['body', 'price_cents'], msg: 'Input should be >= 0' }], code: 'validation_error' },
      { status: 422, statusText: 'Unprocessable Entity' },
    );
    expect(err?.code).toBe('validation_error');
    expect(err?.detail).toBe('sku: String should match pattern; price_cents: Input should be >= 0');
  });

  it('explains a network failure (status 0)', () => {
    let err: ApiError | undefined;
    http.get('/health').subscribe({ error: (e: ApiError) => (err = e) });
    ctrl.expectOne('/health').error(new ProgressEvent('error'));
    expect(err?.status).toBe(0);
    expect(err?.code).toBe('network_error');
    expect(err?.detail).toContain('port 8000');
  });

  it('adds X-API-Key only when a key is stored', () => {
    http.get('/stores').subscribe();
    expect(ctrl.expectOne('/stores').request.headers.has('X-API-Key')).toBe(false);
    localStorage.setItem(API_KEY_STORAGE, 'secret');
    http.get('/stores').subscribe();
    const req = ctrl.expectOne('/stores');
    expect(req.request.headers.get('X-API-Key')).toBe('secret');
    expect(req.request.headers.get('Accept')).toBe('application/json');
  });
});
