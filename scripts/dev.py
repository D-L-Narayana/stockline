#!/usr/bin/env python3
"""StockLine developer task runner (standard library only, Python 3.11+).

    python3 scripts/dev.py [--log FILE] <command> [args...]

Commands
  which                 print the project interpreter that will be used
  install               pip install -r requirements-dev.txt into the project interpreter
  test [pytest args]    run the test suite (default flags: -q -rs)
  cov [pytest args]     test suite with the coverage gate (--cov=app --cov-fail-under=90)
  compile               byte-compile app/, tests/ and scripts/
  lint                  ruff check app tests scripts
  node-test             node --test tests/ui/lib.test.mjs
  site [--out DIR]      build the static demo site with scripts/build_site.py
  smoke                 run scripts/smoke.sh (starts and stops a temporary server)
  serve [uvicorn args]  run the API in the foreground (uvicorn app.main:app)
  py SCRIPT [args]      run a Python script with the project interpreter
  check                 compile, cov, node-test, site and smoke, sequentially

The project interpreter is $STOCKLINE_PYTHON when set, else ./.venv/bin/python
(or .venv/Scripts/python.exe) when present, else the interpreter running this
script.  --log appends a timestamped transcript of every command to FILE, which
keeps evidence of check runs without shell redirection.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMMANDS = ("which", "install", "test", "cov", "compile", "lint", "node-test", "site", "smoke", "serve", "py", "check")


def interpreter() -> str:
    env = os.environ.get("STOCKLINE_PYTHON")
    if env:
        return env
    for cand in (ROOT / ".venv" / "bin" / "python", ROOT / ".venv" / "Scripts" / "python.exe"):
        if cand.exists():
            return str(cand)
    return sys.executable


class Runner:
    """Runs subprocesses in the repo root, streaming output to stdout and an optional log."""

    def __init__(self, log: Path | None) -> None:
        self.log = log
        if log is not None:
            log.parent.mkdir(parents=True, exist_ok=True)

    def run(self, cmd: list[str], *, env: dict[str, str] | None = None) -> int:
        stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        log_fh = self.log.open("a", encoding="utf-8") if self.log is not None else None

        def emit(text: str) -> None:
            sys.stdout.write(text)
            sys.stdout.flush()
            if log_fh is not None:
                log_fh.write(text)
                log_fh.flush()

        try:
            emit(f"== {stamp} $ {' '.join(cmd)}\n")
            try:
                proc = subprocess.Popen(
                    cmd, cwd=ROOT, env=full_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace"
                )
            except FileNotFoundError as exc:
                emit(f"!! cannot start {cmd[0]}: {exc}\n== exit 127\n")
                return 127
            assert proc.stdout is not None
            for line in proc.stdout:
                emit(line)
            code = proc.wait()
            emit(f"== exit {code}\n")
            return code
        finally:
            if log_fh is not None:
                log_fh.close()


def _require(path: Path, what: str) -> bool:
    if path.exists():
        return True
    sys.stdout.write(f"!! {what} not found: {path.relative_to(ROOT)}\n")
    return False


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dev.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", type=Path, help="append a transcript of every command to this file")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("args", nargs=argparse.REMAINDER, help="arguments passed through to the underlying tool")
    ns = parser.parse_args(argv)
    rest = [a for a in ns.args if a != "--"]
    py = interpreter()
    runner = Runner(ns.log)

    if ns.command == "which":
        sys.stdout.write(py + "\n")
        return 0
    if ns.command == "install":
        return runner.run([py, "-m", "pip", "install", "-r", "requirements-dev.txt", *rest])
    if ns.command == "test":
        return runner.run([py, "-m", "pytest", "-q", "-rs", *rest])
    if ns.command == "cov":
        return runner.run([py, "-m", "pytest", "-q", "-rs", "--cov=app", "--cov-report=term-missing", "--cov-fail-under=90", *rest])
    if ns.command == "compile":
        return runner.run([py, "-m", "compileall", "-q", "app", "tests", "scripts"])
    if ns.command == "lint":
        paths = [a for a in rest if not a.startswith("-")] or ["app", "tests", "scripts"]
        flags = [a for a in rest if a.startswith("-")]
        return runner.run([py, "-m", "ruff", "check", *flags, *paths])
    if ns.command == "node-test":
        if not _require(ROOT / "tests" / "ui" / "lib.test.mjs", "node test file"):
            return 2
        return runner.run(["node", "--test", "tests/ui/lib.test.mjs", *rest])
    if ns.command == "site":
        if not _require(ROOT / "scripts" / "build_site.py", "site builder"):
            return 2
        args = list(rest)
        if "--out" not in args:
            args = ["--out", str(ROOT / "site"), *args]
        return runner.run([py, "scripts/build_site.py", *args])
    if ns.command == "smoke":
        if not _require(ROOT / "scripts" / "smoke.sh", "smoke script"):
            return 2
        return runner.run(["bash", "scripts/smoke.sh", *rest], env={"PYTHON": py})
    if ns.command == "serve":
        return runner.run([py, "-m", "uvicorn", "app.main:app", *rest])
    if ns.command == "py":
        if not rest:
            sys.stdout.write("!! usage: dev.py py SCRIPT [args]\n")
            return 2
        return runner.run([py, *rest])
    if ns.command == "check":
        results: list[tuple[str, int]] = []
        for name in ("compile", "cov", "node-test", "site", "smoke"):
            code = main((["--log", str(ns.log)] if ns.log else []) + [name])
            results.append((name, code))
        sys.stdout.write("\n== check summary\n")
        for name, code in results:
            sys.stdout.write(f"   {'PASS' if code == 0 else 'FAIL'}  {name} (exit {code})\n")
        return 0 if all(code == 0 for _, code in results) else 1
    parser.error(f"unknown command {ns.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
