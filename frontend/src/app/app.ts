import { Component, inject, signal } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { API_BASE } from './core/api-base.token';
import { API_KEY_STORAGE, storedApiKey } from './core/api-error.interceptor';

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, RouterLink, RouterLinkActive],
  templateUrl: './app.html',
  styleUrl: './app.css',
})
export class App {
  protected readonly title = signal('StockLine');
  protected readonly apiBase = inject(API_BASE);
  protected readonly apiKey = signal(storedApiKey() ?? '');
  protected readonly menuOpen = signal(false);

  protected readonly links = [
    { path: '/dashboard', label: 'Dashboard' },
    { path: '/products', label: 'Products' },
    { path: '/inventory', label: 'Inventory' },
    { path: '/orders', label: 'Orders' },
  ];

  /** Stores the optional API key (`STOCKLINE_API_KEY`) that the interceptor sends as `X-API-Key`. */
  protected saveApiKey(value: string): void {
    const key = value.trim();
    this.apiKey.set(key);
    try {
      if (key) globalThis.localStorage?.setItem(API_KEY_STORAGE, key);
      else globalThis.localStorage?.removeItem(API_KEY_STORAGE);
    } catch {
      /* storage unavailable (private mode) — key is kept for this session only */
    }
  }
}
