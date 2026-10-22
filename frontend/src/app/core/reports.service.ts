import { inject, Injectable } from '@angular/core';
import { Observable } from 'rxjs';
import { ApiService } from './api.service';
import { SummaryReport } from './models';

@Injectable({ providedIn: 'root' })
export class ReportsService {
  private readonly api = inject(ApiService);

  summary(): Observable<SummaryReport> {
    return this.api.get<SummaryReport>('/reports/summary');
  }
}
