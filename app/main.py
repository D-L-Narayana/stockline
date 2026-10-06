"""FastAPI application: settings, middleware stack, routers and the static demo UI.

Middleware order matters (innermost → outermost): API-key auth → request context (ids, access log,
metrics, JSON 500s) → CORS (when configured) → security headers. Starlette wraps each newly added
middleware around the previous ones, so the install calls below run in exactly that order.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.routing import Match, Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__, deps, observability, security
from .routers import catalog, inventory, orders, reports, system

settings = deps.Settings.from_env()
observability.setup_logging(settings.log_level, settings.log_format)

app = FastAPI(
    title="StockLine Inventory API",
    version=__version__,
    description=(
        "Multi-store inventory and order management. Stock is an append-only ledger "
        "(`stock_movements`) with a cached balance and a running `balance_after`; orders and "
        "transfers are idempotent via the `Idempotency-Key` header; adjustments support optimistic "
        "locking. Errors are `{detail, code, request_id}`."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=deps.lifespan,
)

security.install_auth(app, settings)
observability.install(app, settings)
security.install_headers(app, settings)

for router in (system.router, catalog.router, inventory.router, orders.router, reports.router):
    app.include_router(router)


def _declared_routes(routes: list) -> list[Route]:
    """Every HTTP route the application declares, including the operations of included routers.

    FastAPI keeps each ``include_router`` call as a wrapper object (``original_router`` holds the router)
    rather than copying its operations into ``app.routes``; older versions place the routes directly.
    """
    found: list[Route] = []
    for route in routes:
        if isinstance(route, Route):  # APIRoute subclasses starlette's Route
            found.append(route)
            continue
        if isinstance(route, Mount):
            continue
        inner = getattr(route, "original_router", None) or getattr(route, "router", None)
        nested = getattr(inner, "routes", None) if inner is not None else getattr(route, "routes", None)
        if nested:
            found.extend(_declared_routes(list(nested)))
    return found


class _DashboardWithMethodGuard:
    """Serve the dashboard for every path that is not an API path; answer API paths with a wrong method properly.

    The static files are mounted at ``/`` and therefore match every request. Without this guard a request such as
    ``GET /integrity/rebuild`` or ``PUT /orders`` would be answered by the file server (404, or 405 without an
    ``Allow`` header). Here the declared routes are consulted first: a path they know with a method they do not
    accept is a ``405`` carrying ``Allow`` with the declared methods, rendered like every other error as
    ``{"detail", "code": "method_not_allowed", "request_id"}``.
    """

    def __init__(self, static: ASGIApp, routes: list[Route]) -> None:
        self.static = static
        # Not named ``routes``: starlette's ``Mount.routes`` returns the mounted app's ``routes`` attribute, and the
        # mount must not advertise the API operations a second time (route walkers and schema tools read it).
        self.api_routes = routes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            allowed: set[str] = set()
            for route in self.api_routes:
                match, _child = route.matches(scope)
                if match is not Match.NONE:
                    allowed.update(route.methods or ())
            if allowed:
                raise HTTPException(status_code=405, headers={"Allow": ", ".join(sorted(allowed))})
        await self.static(scope, receive, send)


# Static demo UI last, so every API path above wins over the catch-all mount.
_public = Path(__file__).resolve().parent.parent / "public"
if _public.exists():
    app.mount("/", _DashboardWithMethodGuard(StaticFiles(directory=str(_public), html=True), _declared_routes(list(app.routes))), name="ui")

# Compatibility aliases: tests/conftest.py calls main.reset_state(); v0.1 code imported these from main.
ConnectionPool = deps.ConnectionPool
get_conn = deps.get_conn
reset_state = deps.reset_state
