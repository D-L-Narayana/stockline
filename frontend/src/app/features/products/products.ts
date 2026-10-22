import { Component, inject, OnInit, signal } from '@angular/core';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { CatalogService } from '../../core/catalog.service';
import { ApiError, Product } from '../../core/models';
import { MoneyPipe } from '../../shared/money.pipe';
import { ErrorBanner } from '../../shared/error-banner';

/** Product catalogue: searchable, paginated list plus a reactive create form that mirrors the Pydantic rules. */
@Component({
  selector: 'app-products',
  imports: [ReactiveFormsModule, MoneyPipe, ErrorBanner],
  templateUrl: './products.html',
})
export class Products implements OnInit {
  private readonly catalog = inject(CatalogService);
  private readonly fb = inject(FormBuilder);

  readonly pageSize = 20;
  readonly items = signal<Product[]>([]);
  readonly total = signal(0);
  readonly offset = signal(0);
  readonly q = signal('');
  readonly includeInactive = signal(false);
  readonly loading = signal(false);
  readonly error = signal<ApiError | null>(null);
  readonly saved = signal<Product | null>(null);
  readonly showForm = signal(false);

  /** Same constraints as `schemas.ProductIn`: SKU `^[A-Z0-9-]+$` 3–32, price >= 0, reorder point >= 0. */
  readonly form = this.fb.nonNullable.group({
    sku: ['', [Validators.required, Validators.minLength(3), Validators.maxLength(32), Validators.pattern(/^[A-Z0-9-]+$/)]],
    name: ['', [Validators.required, Validators.maxLength(120)]],
    category: ['', [Validators.required, Validators.maxLength(40)]],
    price_rupees: [0, [Validators.required, Validators.min(0)]],
    reorder_point: [10, [Validators.required, Validators.min(0)]],
  });

  ngOnInit(): void {
    this.load();
  }

  load(): void {
    this.loading.set(true);
    this.error.set(null);
    this.catalog.listProducts({ q: this.q(), includeInactive: this.includeInactive(), limit: this.pageSize, offset: this.offset() }).subscribe({
      next: (p) => { this.items.set(p.items); this.total.set(p.total); this.loading.set(false); },
      error: (e: ApiError) => { this.error.set(e); this.loading.set(false); },
    });
  }

  search(value: string): void {
    this.q.set(value.trim());
    this.offset.set(0);
    this.load();
  }

  toggleInactive(checked: boolean): void {
    this.includeInactive.set(checked);
    this.offset.set(0);
    this.load();
  }

  page(delta: number): void {
    const next = Math.max(0, this.offset() + delta * this.pageSize);
    if (next >= this.total() && delta > 0) return;
    this.offset.set(next);
    this.load();
  }

  submit(): void {
    if (this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }
    const v = this.form.getRawValue();
    this.error.set(null);
    this.catalog
      .createProduct({ sku: v.sku, name: v.name, category: v.category, price_cents: Math.round(v.price_rupees * 100), reorder_point: v.reorder_point })
      .subscribe({
        next: (p) => { this.saved.set(p); this.form.reset({ sku: '', name: '', category: '', price_rupees: 0, reorder_point: 10 }); this.load(); },
        error: (e: ApiError) => this.error.set(e),
      });
  }

  deactivate(p: Product): void {
    if (!globalThis.confirm?.(`Deactivate ${p.sku}? It stays in history but cannot be ordered.`)) return;
    this.catalog.deactivateProduct(p.id).subscribe({ next: () => this.load(), error: (e: ApiError) => this.error.set(e) });
  }

  invalid(name: keyof typeof this.form.controls): boolean {
    const c = this.form.controls[name];
    return c.invalid && (c.touched || c.dirty);
  }
}
