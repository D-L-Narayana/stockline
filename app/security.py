"""Response security headers, Content-Security-Policy, opt-in API key and CORS (server-only).

``app.main`` wires the stack in this order (private test apps that need request ids on 401s do the same)::

    security.install_auth(app, settings)      # innermost: API-key checks for mutating methods
    observability.install(app, settings)      # request id, timing, access log, metrics, JSON 500s
    security.install_headers(app, settings)   # outermost: CORS (when configured) + security headers

``security.install(app, settings)`` is ``install_auth`` + ``install_headers`` for small private apps.
Both middlewares are pure ASGI, so the headers reach every response: static files, JSON, CSV, the
401/404/422 bodies and the JSON 500s produced by the observability layer.
"""
from __future__ import annotations

import hmac

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .common import UNAUTHORIZED
from .deps import Settings

# Served on every response unless the app set its own policy for that response; see docs/operations.md.
DEFAULT_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
)
# Swagger UI / ReDoc bootstrap from a CDN with inline script and style, so the docs pages get their own policy.
DOCS_CSP = (
    "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.jsdelivr.net; "
    "font-src 'self' https://cdn.jsdelivr.net data:; connect-src 'self'; object-src 'none'; "
    "base-uri 'self'; frame-ancestors 'none'"
)
DOCS_PATHS = ("/docs", "/docs/oauth2-redirect", "/redoc")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
HSTS_VALUE = "max-age=31536000; includeSubDomains"
NO_STORE_CONTENT_TYPES = ("application/json", "text/csv")
PROTECTED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
OPEN_PATHS = frozenset({"/health"})
UNAUTHORIZED_DETAIL = "missing or invalid API key"
CORS_EXPOSE_HEADERS = ("Idempotent-Replayed", "X-Request-ID", "X-Response-Time-ms", "X-Row-Count", "X-Truncated", "Content-Disposition")


def _route_path(scope: Scope) -> str:
    """Request path relative to the application root (ignores a reverse-proxy ``root_path`` prefix)."""
    path = scope.get("path") or ""
    root = scope.get("root_path") or ""
    if root and path.startswith(root):
        path = path[len(root) :] or "/"
    return path


class SecurityHeadersMiddleware:
    """Pure-ASGI middleware adding the security headers to every HTTP response.

    * ``Content-Security-Policy``: ``csp`` (default ``DEFAULT_CSP``); ``DOCS_CSP`` on ``DOCS_PATHS`` regardless of the
      override; an existing policy set by the app for the same response is left untouched (per-route overrides).
    * every ``SECURITY_HEADERS`` entry;
    * ``Cache-Control: no-store`` when the content type starts with ``application/json`` or ``text/csv``;
    * ``Strict-Transport-Security`` when ``hsts`` is enabled (only meaningful behind TLS).
    """

    def __init__(self, app: ASGIApp, *, csp: str | None = None, hsts: bool = False) -> None:
        self.app = app
        self.csp = csp or DEFAULT_CSP
        self.hsts = bool(hsts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        policy = DOCS_CSP if _route_path(scope) in DOCS_PATHS else self.csp
        hsts = self.hsts

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers") or [])
                headers = MutableHeaders(scope=message)
                if "content-security-policy" not in headers:
                    headers["Content-Security-Policy"] = policy
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
                if headers.get("content-type", "").lower().startswith(NO_STORE_CONTENT_TYPES):
                    headers["Cache-Control"] = "no-store"
                if hsts:
                    headers["Strict-Transport-Security"] = HSTS_VALUE
            await send(message)

        await self.app(scope, receive, send_with_headers)


class ApiKeyMiddleware:
    """Pure-ASGI middleware requiring the configured key on ``POST``/``PUT``/``PATCH``/``DELETE``.

    The key may be sent as ``X-API-Key: <key>`` or ``Authorization: Bearer <key>`` (constant-time comparison).
    ``GET``/``HEAD``/``OPTIONS`` and ``/health`` are always open. Failures answer
    ``401 {"detail": "missing or invalid API key", "code": "unauthorized", "request_id"?}`` with ``WWW-Authenticate: Bearer``.
    """

    def __init__(self, app: ASGIApp, *, api_key: str) -> None:
        self.app = app
        self._key = api_key.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "").upper() not in PROTECTED_METHODS or _route_path(scope) in OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        if self._authorised(Headers(scope=scope)):
            await self.app(scope, receive, send)
            return
        body = {"detail": UNAUTHORIZED_DETAIL, "code": UNAUTHORIZED}
        request_id = (scope.get("state") or {}).get("request_id")
        if request_id:
            body["request_id"] = request_id
        response = JSONResponse(status_code=401, content=body, headers={"WWW-Authenticate": "Bearer"})
        await response(scope, receive, send)

    def _authorised(self, headers: Headers) -> bool:
        presented: list[str] = []
        api_key = headers.get("x-api-key")
        if api_key is not None:
            presented.append(api_key.strip())
        scheme, _, token = headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            presented.append(token.strip())
        return any(hmac.compare_digest(candidate.encode("utf-8"), self._key) for candidate in presented)


def install_auth(app: FastAPI, settings: Settings) -> None:
    """Add the API-key middleware when ``settings.api_key`` is set. Call FIRST so it is the innermost layer."""
    if settings.api_key:
        app.add_middleware(ApiKeyMiddleware, api_key=settings.api_key)


def install_headers(app: FastAPI, settings: Settings) -> None:
    """Add CORS (when ``settings.cors_origins`` is non-empty) and then the security-headers middleware.

    Call LAST: the headers middleware becomes the outermost layer and covers every response, including
    CORS preflights and the JSON 500s produced inside the observability layer.
    """
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=list(CORS_EXPOSE_HEADERS),
        )
    app.add_middleware(SecurityHeadersMiddleware, csp=settings.csp, hsts=settings.hsts)


def install(app: FastAPI, settings: Settings) -> None:
    """``install_auth`` + ``install_headers`` for private apps that do not need the observability layer in between."""
    install_auth(app, settings)
    install_headers(app, settings)
