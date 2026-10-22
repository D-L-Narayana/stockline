import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Products } from './products';

describe('Products', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [Products], providers: [provideHttpClient(), provideHttpClientTesting()] }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('loads the first page and renders rows', async () => {
    const fixture = TestBed.createComponent(Products);
    fixture.detectChanges();
    const req = http.expectOne((r) => r.url.endsWith('/products'));
    expect(req.request.params.get('limit')).toBe('20');
    req.flush({ items: [{ id: 1, sku: 'GRO-RICE-5KG', name: 'Basmati Rice 5 kg', category: 'Grocery', price_cents: 64900, reorder_point: 20, active: true, updated_at: null }], total: 1, limit: 20, offset: 0 });
    await fixture.whenStable();
    const rows = (fixture.nativeElement as HTMLElement).querySelectorAll('tbody tr');
    expect(rows.length).toBe(1);
    expect(rows[0].textContent).toContain('₹649.00');
  });

  it('mirrors the SKU pattern and price rules of the API', () => {
    const fixture = TestBed.createComponent(Products);
    fixture.detectChanges();
    http.expectOne((r) => r.url.endsWith('/products')).flush({ items: [], total: 0, limit: 20, offset: 0 });
    const form = fixture.componentInstance.form;
    form.patchValue({ sku: 'bad sku', name: 'X', category: 'Y', price_rupees: -1 });
    expect(form.controls.sku.hasError('pattern')).toBe(true);
    expect(form.controls.price_rupees.hasError('min')).toBe(true);
    form.patchValue({ sku: 'NEW-SKU-1', price_rupees: 12.5 });
    expect(form.valid).toBe(true);
  });

  it('converts rupees to price_cents when creating', () => {
    const fixture = TestBed.createComponent(Products);
    fixture.detectChanges();
    http.expectOne((r) => r.url.endsWith('/products')).flush({ items: [], total: 0, limit: 20, offset: 0 });
    fixture.componentInstance.form.setValue({ sku: 'NEW-SKU-1', name: 'New', category: 'Misc', price_rupees: 12.5, reorder_point: 3 });
    fixture.componentInstance.submit();
    const post = http.expectOne((r) => r.method === 'POST' && r.url.endsWith('/products'));
    expect(post.request.body).toEqual({ sku: 'NEW-SKU-1', name: 'New', category: 'Misc', price_cents: 1250, reorder_point: 3 });
    post.flush({ id: 9, ...post.request.body, active: true, updated_at: null }, { status: 201, statusText: 'Created' });
    http.expectOne((r) => r.method === 'GET' && r.url.endsWith('/products')).flush({ items: [], total: 0, limit: 20, offset: 0 });
    expect(fixture.componentInstance.saved()?.id).toBe(9);
  });
});
