import { Component, computed, inject, OnInit, signal } from '@angular/core';
import { FormArray, FormBuilder, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { CatalogService } from '../../core/catalog.service';
import { ApiError, Product, Store } from '../../core/models';
import { newIdempotencyKey, OrdersService } from '../../core/orders.service';
import { MoneyPipe } from '../../shared/money.pipe';
import { ErrorBanner } from '../../shared/error-banner';

type LineGroup = FormGroup<{ product_id: import('@angular/forms').FormControl<number>; quantity: import('@angular/forms').FormControl<number> }>;

/**
 * Place-order form: a store select plus a FormArray of lines (product, quantity).
 * Validation mirrors `schemas.OrderIn`: 1–50 lines, quantity 1–1000, no duplicate products.
 * The request carries a fresh `Idempotency-Key`, which is reused on retry so a double-click cannot create two orders.
 */
@Component({
  selector: 'app-order-form',
  imports: [ReactiveFormsModule, RouterLink, MoneyPipe, ErrorBanner],
  templateUrl: './order-form.html',
})
export class OrderForm implements OnInit {
  private readonly fb = inject(FormBuilder);
  private readonly catalog = inject(CatalogService);
  private readonly orders = inject(OrdersService);
  private readonly router = inject(Router);

  readonly stores = signal<Store[]>([]);
  readonly products = signal<Product[]>([]);
  readonly error = signal<ApiError | null>(null);
  readonly submitting = signal(false);
  /** One key per checkout attempt; kept until the order succeeds so retries replay instead of duplicating. */
  readonly idempotencyKey = signal(newIdempotencyKey());
  private readonly lineValues = signal<{ product_id: number; quantity: number }[]>([]);

  readonly form = this.fb.nonNullable.group({
    store_id: [0, [Validators.required, Validators.min(1)]],
    lines: this.fb.array<LineGroup>([], [Validators.minLength(1), Validators.maxLength(50), uniqueProducts]),
  });

  get lines(): FormArray<LineGroup> {
    return this.form.controls.lines;
  }

  readonly total = computed(() => {
    const prices = new Map(this.products().map((p) => [p.id, p.price_cents]));
    return this.lineValues().reduce((sum, l) => sum + (prices.get(l.product_id) ?? 0) * (l.quantity || 0), 0);
  });

  ngOnInit(): void {
    this.catalog.listStores().subscribe({ next: (s) => this.stores.set(s), error: (e: ApiError) => this.error.set(e) });
    this.catalog.listProducts({ limit: 100 }).subscribe({ next: (p) => this.products.set(p.items), error: (e: ApiError) => this.error.set(e) });
    this.addLine();
    this.form.valueChanges.subscribe(() => this.lineValues.set(this.lines.getRawValue()));
  }

  addLine(): void {
    this.lines.push(this.fb.nonNullable.group({
      product_id: [0, [Validators.required, Validators.min(1)]],
      quantity: [1, [Validators.required, Validators.min(1), Validators.max(1000)]],
    }) as LineGroup);
    this.lineValues.set(this.lines.getRawValue());
  }

  removeLine(i: number): void {
    this.lines.removeAt(i);
    this.lineValues.set(this.lines.getRawValue());
  }

  lineTotal(i: number): number {
    const l = this.lines.at(i).getRawValue();
    return (this.products().find((p) => p.id === l.product_id)?.price_cents ?? 0) * (l.quantity || 0);
  }

  submit(): void {
    if (this.form.invalid || this.submitting()) { this.form.markAllAsTouched(); return; }
    this.submitting.set(true);
    this.error.set(null);
    const v = this.form.getRawValue();
    this.orders.place({ store_id: v.store_id, lines: v.lines }, this.idempotencyKey()).subscribe({
      next: ({ order, replayed }) => {
        this.submitting.set(false);
        void this.router.navigate(['/orders', order.id], { state: { replayed } });
      },
      error: (e: ApiError) => { this.error.set(e); this.submitting.set(false); },
    });
  }
}

/** Cross-field validator: the API rejects duplicate `product_id`s in one order with 422. */
export function uniqueProducts(control: import('@angular/forms').AbstractControl): import('@angular/forms').ValidationErrors | null {
  const ids = (control.value as { product_id: number }[] | null)?.map((l) => l.product_id).filter((id) => id > 0) ?? [];
  return new Set(ids).size === ids.length ? null : { duplicateProducts: true };
}
