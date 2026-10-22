import { Routes } from '@angular/router';

/** Lazy, standalone routes — each feature is its own chunk. */
export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'dashboard' },
  { path: 'dashboard', title: 'StockLine · Dashboard', loadComponent: () => import('./features/dashboard/dashboard').then((m) => m.Dashboard) },
  { path: 'products', title: 'StockLine · Products', loadComponent: () => import('./features/products/products').then((m) => m.Products) },
  { path: 'inventory', title: 'StockLine · Inventory', loadComponent: () => import('./features/inventory/inventory').then((m) => m.Inventory) },
  { path: 'orders', title: 'StockLine · Orders', loadComponent: () => import('./features/orders/orders').then((m) => m.Orders) },
  { path: 'orders/new', title: 'StockLine · Place order', loadComponent: () => import('./features/orders/order-form').then((m) => m.OrderForm) },
  { path: 'orders/:id', title: 'StockLine · Order', loadComponent: () => import('./features/orders/order-detail').then((m) => m.OrderDetail) },
  { path: '**', redirectTo: 'dashboard' },
];
