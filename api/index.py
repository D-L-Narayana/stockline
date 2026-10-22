"""Vercel entry point: the FastAPI app mounted under /api, next to the static Angular build.

The database lives in the function's /tmp (ephemeral, re-seeded on every cold start) — fine for a demo.
"""
import os

os.environ.setdefault("STOCKLINE_DB", "/tmp/stockline.db")
os.environ.setdefault("STOCKLINE_SEED", "1")
os.environ.setdefault("STOCKLINE_POOL_SIZE", "2")

from starlette.applications import Starlette  # noqa: E402
from starlette.routing import Mount  # noqa: E402

from app.main import app as stockline  # noqa: E402

app = Starlette(routes=[Mount("/api", app=stockline)])
