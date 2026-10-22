import { inject, Injectable } from '@angular/core';
import { map, Observable } from 'rxjs';
import { ApiService } from './api.service';
import { InventoryRow, Page, ReceiptIn } from './models';

@Injectable({ providedIn: 'root' })
export class InventoryService {
  private readonly api = inject(ApiService);

  list(opts: { storeId?: number | null; lowStock?: boolean; q?: string; limit?: number; offset?: number } = {}): Observable<Page<InventoryRow>> {
    return this.api.get<Page<InventoryRow>>('/inventory', {
      store_id: opts.storeId ?? undefined,
      low_stock: opts.lowStock ? true : undefined,
      q: opts.q,
      limit: opts.limit ?? 50,
      offset: opts.offset ?? 0,
    });
  }

  /** `POST /inventory/{store_id}/receipts` — books a delivery (one movement per line). */
  receive(storeId: number, body: ReceiptIn): Observable<unknown> {
    return this.api.post<unknown>(`/inventory/${storeId}/receipts`, body).pipe(map((r) => r.body));
  }

  exportCsvUrl(opts: { storeId?: number | null; lowStock?: boolean; q?: string } = {}): string {
    const qs = this.api.params({ store_id: opts.storeId ?? undefined, low_stock: opts.lowStock ? true : undefined, q: opts.q }).toString();
    return this.api.url('/inventory/export.csv') + (qs ? `?${qs}` : '');
  }
}
