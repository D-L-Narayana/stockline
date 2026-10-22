import { inject, Injectable } from '@angular/core';
import { map, Observable } from 'rxjs';
import { ApiService } from './api.service';
import { Page, Product, ProductIn, Store } from './models';

@Injectable({ providedIn: 'root' })
export class CatalogService {
  private readonly api = inject(ApiService);

  listStores(): Observable<Store[]> {
    return this.api.get<Store[]>('/stores');
  }

  listProducts(opts: { q?: string; category?: string; includeInactive?: boolean; limit?: number; offset?: number } = {}): Observable<Page<Product>> {
    return this.api.get<Page<Product>>('/products', {
      q: opts.q,
      category: opts.category,
      include_inactive: opts.includeInactive ? true : undefined,
      limit: opts.limit ?? 20,
      offset: opts.offset ?? 0,
    });
  }

  createProduct(body: ProductIn): Observable<Product> {
    return this.api.post<Product>('/products', body).pipe(map((r) => r.body));
  }

  deactivateProduct(id: number): Observable<void> {
    return this.api.delete(`/products/${id}`);
  }
}
