import { HttpErrorResponse, HttpInterceptorFn } from '@angular/common/http';
import { catchError, throwError } from 'rxjs';
import { ApiError } from './models';

export const API_KEY_STORAGE = 'stockline.apiKey';

/** Reads the optional API key (`STOCKLINE_API_KEY` on the server) saved by the settings panel. */
export function storedApiKey(): string | null {
  try {
    return globalThis.localStorage?.getItem(API_KEY_STORAGE) ?? null;
  } catch {
    return null;
  }
}

/** Turns a FastAPI error response into an `ApiError`, including 422 validation lists and network failures. */
export function toApiError(err: HttpErrorResponse): ApiError {
  const body = err.error as { detail?: unknown; code?: string; request_id?: string } | null;
  let detail: string;
  if (Array.isArray(body?.detail)) {
    detail = body.detail
      .map((d: { loc?: unknown[]; msg?: string }) => `${(d.loc ?? []).slice(1).join('.')}: ${d.msg ?? ''}`.trim())
      .join('; ');
  } else if (typeof body?.detail === 'string') {
    detail = body.detail;
  } else if (err.status === 0) {
    detail = 'Cannot reach the StockLine API. Is the backend running on port 8000?';
  } else {
    detail = err.message || 'Unexpected error';
  }
  return {
    status: err.status,
    code: body?.code ?? (err.status === 0 ? 'network_error' : `http_${err.status}`),
    detail,
    requestId: body?.request_id ?? err.headers?.get('X-Request-ID') ?? null,
  };
}

/** Adds `Accept` + optional `X-API-Key` headers and normalises every failure to `ApiError`. */
export const apiErrorInterceptor: HttpInterceptorFn = (req, next) => {
  const key = storedApiKey();
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (key) headers['X-API-Key'] = key;
  return next(req.clone({ setHeaders: headers })).pipe(
    catchError((err: unknown) =>
      throwError(() => (err instanceof HttpErrorResponse ? toApiError(err) : err)),
    ),
  );
};
