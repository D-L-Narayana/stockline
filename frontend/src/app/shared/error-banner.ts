import { Component, input } from '@angular/core';
import { ApiError } from '../core/models';

@Component({
  selector: 'app-error-banner',
  template: `
    @if (error(); as e) {
      <div class="banner error" role="alert">
        <strong>{{ e.code }}</strong> ({{ e.status }}): {{ e.detail }}
        @if (e.requestId) { <span class="muted"> · request {{ e.requestId }}</span> }
      </div>
    }
  `,
})
export class ErrorBanner {
  readonly error = input<ApiError | null>(null);
}
