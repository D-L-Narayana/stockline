"""Server-only glue: settings from the environment, the SQLite connection pool and FastAPI wiring.

Never imported by browser modules (see ``common.SERVER_ONLY_MODULES``).
"""
from __future__ import annotations

import math
import os
import queue
import sqlite3
import threading
from collections.abc import AsyncIterator, Generator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import db
from .common import ServiceError
from .seed import seed

_TRUE_WORDS = frozenset({"1", "true", "yes"})
_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})


def _env_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_WORDS


def _env_int(env: Mapping[str, str], key: str, default: int, *, minimum: int) -> int:
    raw = env.get(key)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value >= minimum else default


def _env_float(env: Mapping[str, str], key: str, default: float, *, minimum: float) -> float:
    raw = env.get(key)
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        return default
    if math.isnan(value) or value < minimum:
        return default
    return value


def _env_text(env: Mapping[str, str], key: str) -> str | None:
    raw = env.get(key)
    if raw is None:
        return None
    return raw.strip() or None


@dataclass(frozen=True)
class Settings:
    """Runtime configuration, read once from ``STOCKLINE_*`` environment variables."""

    seed: bool = True  # STOCKLINE_SEED ("1" default): load the demo dataset into an empty database
    pool_size: int = 8  # STOCKLINE_POOL_SIZE
    acquire_timeout: float = 10.0  # STOCKLINE_POOL_TIMEOUT (seconds)
    api_key: str | None = None  # STOCKLINE_API_KEY (unset → writes are open; demo default)
    cors_origins: tuple[str, ...] = ()  # STOCKLINE_CORS_ORIGINS, comma-separated
    log_level: str = "INFO"  # STOCKLINE_LOG_LEVEL
    log_format: str = "text"  # STOCKLINE_LOG_FORMAT: "text" | "json"
    csp: str | None = None  # STOCKLINE_CSP: verbatim Content-Security-Policy override
    hsts: bool = False  # STOCKLINE_HSTS ("1") → Strict-Transport-Security header

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Parse settings from ``env`` (default ``os.environ``); malformed numbers and unknown choices fall back to defaults."""
        env = os.environ if env is None else env
        defaults = cls()
        log_level = (env.get("STOCKLINE_LOG_LEVEL") or defaults.log_level).strip().upper()
        log_format = (env.get("STOCKLINE_LOG_FORMAT") or defaults.log_format).strip().lower()
        origins = tuple(o.strip() for o in env.get("STOCKLINE_CORS_ORIGINS", "").split(",") if o.strip())
        return cls(
            seed=_env_bool(env, "STOCKLINE_SEED", defaults.seed),
            pool_size=_env_int(env, "STOCKLINE_POOL_SIZE", defaults.pool_size, minimum=1),
            acquire_timeout=_env_float(env, "STOCKLINE_POOL_TIMEOUT", defaults.acquire_timeout, minimum=0.0),
            api_key=_env_text(env, "STOCKLINE_API_KEY"),
            cors_origins=origins,
            log_level=log_level if log_level in _LOG_LEVELS else defaults.log_level,
            log_format=log_format if log_format in ("text", "json") else defaults.log_format,
            csp=_env_text(env, "STOCKLINE_CSP"),
            hsts=_env_bool(env, "STOCKLINE_HSTS", defaults.hsts),
        )


class PoolExhausted(RuntimeError):
    """Raised by ``ConnectionPool.acquire`` when no connection becomes free within the timeout."""


class ConnectionPool:
    """Tiny SQLite connection pool. Each request checks a connection out for its whole lifetime
    (dependency → endpoint → teardown), so no two requests ever share a connection. Writers still
    serialise on SQLite's file lock via BEGIN IMMEDIATE (see ``db.transaction``).

    Connections are opened lazily: ``db.DB_PATH`` is read when the first connection is created, and
    the schema migration plus optional seeding run exactly once per pool, under the pool lock.
    """

    def __init__(self, size: int = 8, *, seed: bool = True, acquire_timeout: float = 10.0) -> None:
        self.size = max(1, int(size))
        self.seed = seed
        self.acquire_timeout = acquire_timeout
        self._idle: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue()
        self._lock = threading.Lock()
        self._created = 0
        self._initialised = False

    def acquire(self) -> sqlite3.Connection:
        """Check a connection out; raises ``PoolExhausted`` after ``acquire_timeout`` seconds when all are busy."""
        try:
            return self._idle.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            if self._created < self.size:
                conn = self._new()
                self._created += 1
                return conn
        try:
            return self._idle.get(timeout=self.acquire_timeout)
        except queue.Empty:
            raise PoolExhausted(f"no database connection available within {self.acquire_timeout:g}s (pool size {self.size})") from None

    def release(self, conn: sqlite3.Connection) -> None:
        """Return a connection to the pool, rolling back any transaction it still holds."""
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        except sqlite3.ProgrammingError:  # already closed: never hand it out again
            with self._lock:
                self._created = max(0, self._created - 1)
            return
        self._idle.put(conn)

    def close_all(self) -> None:
        """Close every idle connection (checked-out ones are closed when released after this call is repeated).

        Idempotent and best-effort: the last idle connection runs ``PRAGMA wal_checkpoint(TRUNCATE)`` without
        waiting on other readers. A later ``acquire()`` simply opens fresh connections to the same database.
        """
        drained: list[sqlite3.Connection] = []
        while True:
            try:
                drained.append(self._idle.get_nowait())
            except queue.Empty:
                break
        for index, conn in enumerate(drained):
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                if index == len(drained) - 1:
                    conn.execute("PRAGMA busy_timeout = 0")
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            try:
                conn.close()
            except sqlite3.Error:
                pass
        with self._lock:
            self._created = max(0, self._created - len(drained))

    @property
    def stats(self) -> dict:
        """``{"size", "created", "idle"}`` — capacity, open connections and connections waiting in the pool."""
        return {"size": self.size, "created": self._created, "idle": self._idle.qsize()}

    def _new(self) -> sqlite3.Connection:
        """Open a connection to ``db.DB_PATH`` as it is *now*; migrate and seed on the first one (caller holds the lock)."""
        conn = db.connect()
        if not self._initialised:
            db.init_schema(conn)
            if self.seed:
                seed(conn)
            self._initialised = True
        return conn


def _pool_from_env(seed: bool | None = None) -> ConnectionPool:
    settings = Settings.from_env()
    return ConnectionPool(settings.pool_size, seed=settings.seed if seed is None else seed, acquire_timeout=settings.acquire_timeout)


pool: ConnectionPool = _pool_from_env()


def get_conn() -> Generator[sqlite3.Connection, None, None]:
    """FastAPI dependency: one pooled connection per request, always released (and rolled back if dirty)."""
    current = pool  # bind now so a reset_state() during the request still releases to the right pool
    conn = current.acquire()
    try:
        yield conn
    finally:
        current.release(conn)


def reset_state(*, seed: bool | None = None) -> None:
    """Close the current pool and replace it (tests and the app factory use this to re-point ``db.DB_PATH``).

    ``seed=None`` reads ``STOCKLINE_SEED`` (and the pool size/timeout variables) from the environment *now*.
    """
    global pool
    old = pool
    try:
        old.close_all()
    finally:
        pool = _pool_from_env(seed)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: on shutdown close whichever pool is current."""
    yield
    pool.close_all()


def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
    """Render a ``ServiceError`` as ``{"detail", "code"}`` plus ``request_id`` when the request carries one."""
    body = exc.to_body()
    request_id = getattr(request.state, "request_id", None)
    if request_id is not None:
        body["request_id"] = request_id
    return JSONResponse(status_code=exc.status, content=body)
