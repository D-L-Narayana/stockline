import { Component, inject, OnInit, signal } from '@angular/core';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { ActivatedRoute, Router } from '@angular/router';
import { CatalogService } from '../../core/catalog.service';
import { InventoryService } from '../../core/inventory.service';
import { ApiError, InventoryRow, Product, Store } from '../../core/models';
import { MoneyPipe } from '../../shared/money.pipe';
import { ErrorBanner } from '../../shared/error-banner';

/** Stock on hand per (store, product) with store / low-stock / search filters, CSV export and a receive-stock form. */
@Component({
  selector: 'app-inventory',
  imports: [ReactiveFormsModule, MoneyPipe, ErrorBanner],
  templateUrl: './inventory.html',
})
export class Inventory implements OnInit {
  private readonly inventory = inject(InventoryService);
  private readonly catalog = inject(CatalogService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly fb = inject(FormBuilder);

  readonly pageSize = 50;
  readonly stores = signal<Store[]>([]);
  readonly products = signal<Product[]>([]);
  readonly rows = signal<InventoryRow[]>([]);
  readonly total = signal(0);
  readonly offset = signal(0);
  readonly storeId = signal<number | null>(null);
  readonly lowStock = signal(false);
  readonly q = signal('');
  readonly loading = signal(false);
  readonly error = signal<ApiError | null>(null);
  readonly notice = signal<string | null>(null);
  readonly showReceipt = signal(false);

  readonly receipt = this.fb.nonNullable.group({
    store_id: [0, [Validators.required, Validators.min(1)]],
    reference: ['', [Validators.required, Validators.maxLength(64)]],
    product_id: [0, [Validators.required, Validators.min(1)]],
    quantity: [1, [Validators.required, Validators.min(1)]],
  });

  ngOnInit(): void {
    const qp = this.route.snapshot.queryParamMap;
    this.storeId.set(qp.get('store_id') ? Number(qp.get('store_id')) : null);
    this.lowStock.set(qp.get('low_stock') === '1' || qp.get('low_stock') === 'true');
    this.catalog.listStores().subscribe({ next: (s) => this.stores.set(s), error: (e: ApiError) => this.error.set(e) });
    this.catalog.listProducts({ limit: 100 }).subscribe({ next: (p) => this.products.set(p.items), error: (e: ApiError) => this.error.set(e) });
    this.load();
  }

  load(): void {
    this.loading.set(true);
    this.error.set(null);
    void this.router.navigate([], { queryParams: { store_id: this.storeId() ?? null, low_stock: this.lowStock() ? 1 : null }, queryParamsHandling: 'merge', replaceUrl: true });
    this.inventory.list({ storeId: this.storeId(), lowStock: this.lowStock(), q: this.q(), limit: this.pageSize, offset: this.offset() }).subscribe({
      next: (p) => { this.rows.set(p.items); this.total.set(p.total); this.loading.set(false); },
      error: (e: ApiError) => { this.error.set(e); this.loading.set(false); },
    });
  }

  setStore(value: string): void { this.storeId.set(value ? Number(value) : null); this.offset.set(0); this.load(); }
  setLowStock(checked: boolean): void { this.lowStock.set(checked); this.offset.set(0); this.load(); }
  search(value: string): void { this.q.set(value.trim()); this.offset.set(0); this.load(); }
  page(delta: number): void {
    const next = Math.max(0, this.offset() + delta * this.pageSize);
    if (delta > 0 && next >= this.total()) return;
    this.offset.set(next);
    this.load();
  }

  csvUrl(): string {
    return this.inventory.exportCsvUrl({ storeId: this.storeId(), lowStock: this.lowStock(), q: this.q() });
  }

  submitReceipt(): void {
    if (this.receipt.invalid) { this.receipt.markAllAsTouched(); return; }
    const v = this.receipt.getRawValue();
    this.error.set(null);
    this.inventory.receive(v.store_id, { reference: v.reference, lines: [{ product_id: v.product_id, quantity: v.quantity }] }).subscribe({
      next: () => { this.notice.set(`Received ${v.quantity} unit(s) against ${v.reference}.`); this.receipt.patchValue({ quantity: 1, reference: '' }); this.load(); },
      error: (e: ApiError) => this.error.set(e),
    });
  }

  invalid(name: keyof typeof this.receipt.controls): boolean {
    const c = this.receipt.controls[name];
    return c.invalid && (c.touched || c.dirty);
  }
}
