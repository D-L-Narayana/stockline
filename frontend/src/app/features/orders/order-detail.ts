import { Component, inject, OnInit, signal } from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { ApiError, Order } from '../../core/models';
import { OrdersService } from '../../core/orders.service';
import { MoneyPipe } from '../../shared/money.pipe';
import { ErrorBanner } from '../../shared/error-banner';

@Component({
  selector: 'app-order-detail',
  imports: [RouterLink, MoneyPipe, ErrorBanner],
  templateUrl: './order-detail.html',
})
export class OrderDetail implements OnInit {
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly orders = inject(OrdersService);

  readonly order = signal<Order | null>(null);
  readonly error = signal<ApiError | null>(null);
  readonly busy = signal(false);
  readonly replayed = signal<boolean>(Boolean((this.router.getCurrentNavigation()?.extras.state as { replayed?: boolean } | undefined)?.replayed));

  ngOnInit(): void {
    const id = Number(this.route.snapshot.paramMap.get('id'));
    this.orders.get(id).subscribe({ next: (o) => this.order.set(o), error: (e: ApiError) => this.error.set(e) });
  }

  cancel(): void { this.act((id) => this.orders.cancel(id)); }
  fulfil(): void { this.act((id) => this.orders.fulfil(id)); }

  private act(fn: (id: number) => import('rxjs').Observable<Order>): void {
    const o = this.order();
    if (!o || this.busy()) return;
    this.busy.set(true);
    this.error.set(null);
    fn(o.id).subscribe({
      next: (updated) => { this.order.set(updated); this.busy.set(false); },
      error: (e: ApiError) => { this.error.set(e); this.busy.set(false); },
    });
  }
}
