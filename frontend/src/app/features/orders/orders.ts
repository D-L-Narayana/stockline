import { Component, inject, OnInit, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { CatalogService } from '../../core/catalog.service';
import { ApiError, Order, OrderStatus, Store } from '../../core/models';
import { OrdersService } from '../../core/orders.service';
import { MoneyPipe } from '../../shared/money.pipe';
import { ErrorBanner } from '../../shared/error-banner';

@Component({
  selector: 'app-orders',
  imports: [RouterLink, MoneyPipe, ErrorBanner],
  templateUrl: './orders.html',
})
export class Orders implements OnInit {
  private readonly orders = inject(OrdersService);
  private readonly catalog = inject(CatalogService);

  readonly pageSize = 20;
  readonly items = signal<Order[]>([]);
  readonly stores = signal<Store[]>([]);
  readonly total = signal(0);
  readonly offset = signal(0);
  readonly storeId = signal<number | null>(null);
  readonly status = signal<OrderStatus | ''>('');
  readonly loading = signal(false);
  readonly error = signal<ApiError | null>(null);

  ngOnInit(): void {
    this.catalog.listStores().subscribe({ next: (s) => this.stores.set(s), error: (e: ApiError) => this.error.set(e) });
    this.load();
  }

  load(): void {
    this.loading.set(true);
    this.error.set(null);
    this.orders.list({ storeId: this.storeId(), status: this.status(), limit: this.pageSize, offset: this.offset() }).subscribe({
      next: (p) => { this.items.set(p.items); this.total.set(p.total); this.loading.set(false); },
      error: (e: ApiError) => { this.error.set(e); this.loading.set(false); },
    });
  }

  setStore(value: string): void { this.storeId.set(value ? Number(value) : null); this.offset.set(0); this.load(); }
  setStatus(value: string): void { this.status.set(value as OrderStatus | ''); this.offset.set(0); this.load(); }
  page(delta: number): void {
    const next = Math.max(0, this.offset() + delta * this.pageSize);
    if (delta > 0 && next >= this.total()) return;
    this.offset.set(next);
    this.load();
  }

  storeCode(id: number): string {
    return this.stores().find((s) => s.id === id)?.code ?? `#${id}`;
  }
}
