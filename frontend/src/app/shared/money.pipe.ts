import { Pipe, PipeTransform } from '@angular/core';

/** Formats integer paise/cents as rupees: 64900 -> "₹649.00". */
@Pipe({ name: 'money' })
export class MoneyPipe implements PipeTransform {
  transform(cents: number | null | undefined, currency = '₹'): string {
    if (cents === null || cents === undefined || Number.isNaN(cents)) return '—';
    const sign = cents < 0 ? '-' : '';
    const abs = Math.abs(cents);
    const rupees = Math.floor(abs / 100).toLocaleString('en-IN');
    const paise = String(abs % 100).padStart(2, '0');
    return `${sign}${currency}${rupees}.${paise}`;
  }
}
