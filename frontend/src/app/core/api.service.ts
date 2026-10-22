import { HttpClient, HttpHeaders, HttpParams, HttpResponse } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { map, Observable } from 'rxjs';
import { API_BASE } from './api-base.token';

type Query = Record<string, string | number | boolean | null | undefined>;

/** Thin typed wrapper over HttpClient: base URL, query-string building, idempotency headers. */
@Injectable({ providedIn: 'root' })
export class ApiService {
  private readonly http = inject(HttpClient);
  private readonly base = inject(API_BASE).replace(/\/$/, '');

  url(path: string): string {
    return `${this.base}${path.startsWith('/') ? path : `/${path}`}`;
  }

  /** Builds HttpParams, dropping null/undefined/empty values so the API sees only real filters. */
  params(query: Query = {}): HttpParams {
    let p = new HttpParams();
    for (const [k, v] of Object.entries(query)) {
      if (v === null || v === undefined || v === '') continue;
      p = p.set(k, String(v));
    }
    return p;
  }

  get<T>(path: string, query?: Query): Observable<T> {
    return this.http.get<T>(this.url(path), { params: this.params(query) });
  }

  post<T>(path: string, body: unknown, idempotencyKey?: string): Observable<{ body: T; replayed: boolean; status: number }> {
    let headers = new HttpHeaders();
    if (idempotencyKey) headers = headers.set('Idempotency-Key', idempotencyKey);
    return this.http
      .post<T>(this.url(path), body, { headers, observe: 'response' })
      .pipe(map((res: HttpResponse<T>) => ({ body: res.body as T, replayed: res.headers.get('Idempotent-Replayed') === 'true', status: res.status })));
  }

  patch<T>(path: string, body: unknown): Observable<T> {
    return this.http.patch<T>(this.url(path), body);
  }

  delete(path: string): Observable<void> {
    return this.http.delete<void>(this.url(path));
  }
}
