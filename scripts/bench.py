#!/usr/bin/env python3
"""StockLine micro-benchmark: throughput and latency percentiles of three hot endpoints.

    python3 scripts/bench.py [--n 200] [--concurrency 4] [--url http://127.0.0.1:8000] [--json FILE]

Default (no ``--url``): the application runs in-process behind a ``TestClient`` on a throw-away seeded
SQLite file (``STOCKLINE_DB`` points into a temporary directory that is removed afterwards), so the
numbers include the whole ASGI stack — security headers, request context, routing, pydantic, SQLite —
but no network hop and no uvicorn.  With ``--url`` the same workload is sent over HTTP with httpx to a
running server, which must be seeded (``STOCKLINE_SEED=1``) and must not require an API key.

Workload — ``--n`` requests per endpoint spread over ``--concurrency`` threads:

    POST /orders           one line, quantity 1, from a product that is stocked up front for the run
    GET  /inventory        first page (limit 50)
    GET  /reports/summary  stock valuation, order and product counts

Output: one line per endpoint with ops/s, p50 / p95 / p99 latency in milliseconds and the status
histogram; ``--json FILE`` also writes the raw numbers.  Exit code 1 when any request answered 5xx
or could not be sent, 0 otherwise.  Standard library + httpx only (httpx is a dev dependency).
In-process mode lowers ``STOCKLINE_LOG_LEVEL`` to ``WARNING`` unless it is set, so the access log
does not flood the output; export it explicitly to keep the per-request lines.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import statistics
import sys
import tempfile
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_N = 200
DEFAULT_CONCURRENCY = 4


def percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile of an ascending list (``pct`` in 0..100)."""
    if not sorted_values:
        return math.nan
    rank = max(1, math.ceil(pct / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def _request(client, method: str, path: str, **kwargs) -> tuple[int, float]:
    """``(status, latency_ms)``; status 0 when the request could not be completed."""
    started = time.perf_counter()
    try:
        response = client.request(method, path, **kwargs)
        status = response.status_code
    except Exception:  # connection refused, timeout, … — counted as a failure, never raised
        status = 0
    return status, (time.perf_counter() - started) * 1000


def measure(client, name: str, method: str, path: str, n: int, concurrency: int, make_kwargs=None) -> dict:
    """Send ``n`` requests over ``concurrency`` threads and summarise statuses and latencies."""
    make_kwargs = make_kwargs or (lambda _i: {})
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(lambda i: _request(client, method, path, **make_kwargs(i)), range(n)))
    wall = time.perf_counter() - started
    latencies = sorted(ms for _status, ms in results)
    histogram: dict[str, int] = {}
    for status, _ms in results:
        key = str(status) if status else "failed"
        histogram[key] = histogram.get(key, 0) + 1
    return {
        "endpoint": name,
        "requests": n,
        "concurrency": concurrency,
        "wall_s": round(wall, 4),
        "ops_per_s": round(n / wall, 1) if wall else math.inf,
        "p50_ms": round(percentile(latencies, 50), 2),
        "p95_ms": round(percentile(latencies, 95), 2),
        "p99_ms": round(percentile(latencies, 99), 2),
        "mean_ms": round(statistics.fmean(latencies), 2) if latencies else math.nan,
        "max_ms": round(latencies[-1], 2) if latencies else math.nan,
        "statuses": dict(sorted(histogram.items())),
        "server_errors": sum(1 for status, _ms in results if status >= 500 or status == 0),
    }


def prepare(client, n: int) -> tuple[int, int]:
    """Pick a store and a product from the seeded catalogue and stock enough units for ``n`` single-unit orders."""
    stores = client.get("/stores")
    products = client.get("/products", params={"limit": 100})
    if stores.status_code != 200 or products.status_code != 200 or not stores.json() or not products.json()["items"]:
        raise SystemExit(f"bench: the server has no seeded stores/products (GET /stores → {stores.status_code}, GET /products → {products.status_code})")
    store_id = stores.json()[0]["id"]
    product_id = min(p["id"] for p in products.json()["items"] if p.get("active", True))
    receipt = client.post(f"/inventory/{store_id}/{product_id}/adjust", json={"delta": n + 5, "reason": "receipt", "reference": "bench"})
    if receipt.status_code != 200:
        raise SystemExit(f"bench: could not stock store {store_id} / product {product_id}: HTTP {receipt.status_code} {receipt.text[:200]}")
    return store_id, product_id


def run(client, n: int, concurrency: int, label: str) -> tuple[list[dict], int]:
    """The whole benchmark against ``client``; returns ``(results, exit_code)``."""
    store_id, product_id = prepare(client, n)
    order_body = {"store_id": store_id, "lines": [{"product_id": product_id, "quantity": 1}]}
    results = [
        measure(client, "POST /orders", "POST", "/orders", n, concurrency, lambda _i: {"json": order_body}),
        measure(client, "GET /inventory", "GET", "/inventory", n, concurrency, lambda _i: {"params": {"limit": 50}}),
        measure(client, "GET /reports/summary", "GET", "/reports/summary", n, concurrency),
    ]
    print(f"StockLine bench — {label} · n={n} per endpoint · concurrency={concurrency}")
    print(f"{'endpoint':<22}{'ops/s':>9}{'p50 ms':>10}{'p95 ms':>10}{'p99 ms':>10}{'max ms':>10}  statuses")
    for row in results:
        statuses = ", ".join(f"{status}×{count}" for status, count in row["statuses"].items())
        print(f"{row['endpoint']:<22}{row['ops_per_s']:>9.1f}{row['p50_ms']:>10.2f}{row['p95_ms']:>10.2f}{row['p99_ms']:>10.2f}{row['max_ms']:>10.2f}  {statuses}")
    errors = sum(row["server_errors"] for row in results)
    if errors:
        print(f"bench: {errors} request(s) answered 5xx or failed", file=sys.stderr)
    return results, (1 if errors else 0)


def run_in_process(n: int, concurrency: int) -> tuple[list[dict], int]:
    """Benchmark the application in-process on a temporary seeded database."""
    tmp = tempfile.mkdtemp(prefix="stockline-bench-")
    os.environ["STOCKLINE_DB"] = os.path.join(tmp, "bench.db")
    os.environ["STOCKLINE_SEED"] = "1"
    # One access-log line per request would dominate the output and the timings; errors are still logged.
    os.environ.setdefault("STOCKLINE_LOG_LEVEL", "WARNING")
    for name in ("STOCKLINE_API_KEY", "STOCKLINE_CORS_ORIGINS", "STOCKLINE_CSP", "STOCKLINE_HSTS"):
        os.environ.pop(name, None)
    # Starlette >= 1.7 prefers the httpx2 package for its TestClient; classic httpx keeps working (same filter as pyproject.toml).
    warnings.filterwarnings("ignore", message=r"Using .httpx. with .starlette.testclient. is deprecated")
    sys.path.insert(0, str(ROOT))
    try:
        from fastapi.testclient import TestClient  # imported late: the environment above must be set first

        from app import main as app_main

        with TestClient(app_main.app) as client:
            return run(client, n, concurrency, "in-process TestClient (temporary seeded database)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_against_url(url: str, n: int, concurrency: int) -> tuple[list[dict], int]:
    """Benchmark a running server over HTTP."""
    import httpx

    with httpx.Client(base_url=url.rstrip("/"), timeout=30.0) as client:
        return run(client, n, concurrency, f"HTTP against {url}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench.py", description="StockLine throughput / latency micro-benchmark.")
    parser.add_argument("--n", type=int, default=DEFAULT_N, help=f"requests per endpoint (default {DEFAULT_N})")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help=f"parallel client threads (default {DEFAULT_CONCURRENCY})")
    parser.add_argument("--url", default=None, help="benchmark a running server instead of the in-process application")
    parser.add_argument("--json", type=Path, default=None, help="also write the raw results to this JSON file")
    ns = parser.parse_args(argv)
    if ns.n < 1 or ns.concurrency < 1:
        parser.error("--n and --concurrency must be positive")
    results, code = run_against_url(ns.url, ns.n, ns.concurrency) if ns.url else run_in_process(ns.n, ns.concurrency)
    if ns.json:
        ns.json.write_text(json.dumps({"mode": ns.url or "in-process", "n": ns.n, "concurrency": ns.concurrency, "results": results}, indent=2) + "\n", encoding="utf-8")
        print(f"results written to {ns.json}")
    return code


if __name__ == "__main__":
    sys.exit(main())
