import { Component, inject, OnInit, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { ApiError, InventoryRow, SummaryReport } from '../../core/models';
import { InventoryService } from '../../core/inventory.service';
import { ReportsService } from '../../core/reports.service';
import { MoneyPipe } from '../../shared/money.pipe';
import { ErrorBanner } from '../../shared/error-banner';

/** KPI cards from `GET /reports/summary` plus the low-stock rows from `GET /inventory?low_stock=true`. */
@Component({
  selector: 'app-dashboard',
  imports: [RouterLink, MoneyPipe, ErrorBanner],
  templateUrl: './dashboard.html',
})
export class Dashboard implements OnInit {
  private readonly reports = inject(ReportsService);
  private readonly inventory = inject(InventoryService);

  readonly summary = signal<SummaryReport | null>(null);
  readonly lowStock = signal<InventoryRow[]>([]);
  readonly loading = signal(true);
  readonly error = signal<ApiError | null>(null);

  ngOnInit(): void {
    this.reload();
  }

  reload(): void {
    this.loading.set(true);
    this.error.set(null);
    this.reports.summary().subscribe({
      next: (s) => this.summary.set(s),
      error: (e: ApiError) => { this.error.set(e); this.loading.set(false); },
    });
    this.inventory.list({ lowStock: true, limit: 10 }).subscribe({
      next: (p) => { this.lowStock.set(p.items); this.loading.set(false); },
      error: (e: ApiError) => { this.error.set(e); this.loading.set(false); },
    });
  }
}
