#!/usr/bin/env python3
"""Assemble the static GitHub Pages demo site (standard library only, Python 3.11+).

    python3 scripts/build_site.py --out DIR [--sha SHA] [--repo ROOT]

The demo runs the Python service layer in the browser (Pyodide), so the site is the UI
from ``public/`` plus the Python modules the browser loads.  The list of shipped modules
has exactly one source of truth: the ``BROWSER_MODULES`` tuple in ``app/common.py``.
It is parsed from the *source text* with a regular expression -- nothing under ``app``
is imported, so the build never needs the server dependencies installed.

What the build produces in ``DIR``::

    <every file under public/>      copied as-is, except index.html (see below)
    app/<module>.py                 exactly the modules listed in BROWSER_MODULES
    .nojekyll                       keeps GitHub Pages from ignoring __init__.py-style paths
    version.json                    {"version": app.__version__, "sha": SHA|null, "built_at": ISO-8601 UTC}

The copied ``index.html`` gets a ``Content-Security-Policy`` ``<meta>`` tag inserted
directly after ``<meta charset="utf-8">``; the repository copy stays untouched because the
FastAPI server sends the (stricter) policy as a response header instead.

The build fails loudly (non-zero exit, one clear message, nothing written) when a listed
module is missing, when a server-only module is listed, when the literal is malformed, or
when ``index.html`` has no charset meta tag to anchor the injection.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Policy served by GitHub Pages (no response headers there, hence the meta tag).  Pyodide
#: needs the jsDelivr CDN for its runtime, blob workers and WebAssembly/eval execution.
PAGES_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.jsdelivr.net 'wasm-unsafe-eval' 'unsafe-eval'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self' https://cdn.jsdelivr.net; "
    "worker-src 'self' blob:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)
CSP_META = f'<meta http-equiv="Content-Security-Policy" content="{PAGES_CSP}">'

#: Modules that must never ship to the browser even if the frozenset in common.py is absent.
ALWAYS_SERVER_ONLY = frozenset({"main.py", "deps.py", "observability.py", "security.py"})

_BROWSER_MODULES_RE = re.compile(r"^BROWSER_MODULES\s*(?::[^=\n]*)?=\s*\(([^)]*)\)", re.MULTILINE)
_SERVER_ONLY_RE = re.compile(r"^SERVER_ONLY_MODULES\s*(?::[^=\n]*)?=\s*frozenset\(\s*\{([^}]*)\}", re.MULTILINE)
_VERSION_RE = re.compile(r"""^__version__\s*=\s*(['"])([^'"\n]+)\1""", re.MULTILINE)
_STRING_RE = re.compile(r'"([^"\n]*)"')
_CHARSET_RE = re.compile(r"""<meta\s+charset\s*=\s*["']?utf-8["']?\s*/?>""", re.IGNORECASE)
_CSP_TAG_RE = re.compile(r"""<meta\s[^>]*http-equiv\s*=\s*["']?content-security-policy""", re.IGNORECASE)
_MODULE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.py")


class BuildError(RuntimeError):
    """A contract violation that must stop the build (reported as one clear message)."""


def parse_browser_modules(src: str) -> list[str]:
    """Return the module names listed in the ``BROWSER_MODULES`` tuple literal of ``src``.

    The literal must be a module-level assignment whose parentheses contain only
    double-quoted strings, commas and whitespace (one or more lines).
    """
    match = _BROWSER_MODULES_RE.search(src)
    if match is None:
        raise BuildError("BROWSER_MODULES tuple literal not found in app/common.py (expected `BROWSER_MODULES: tuple[str, ...] = (...)`)")
    inner = match.group(1)
    leftovers = _STRING_RE.sub("", inner).replace(",", "").strip()
    if leftovers:
        raise BuildError(f"BROWSER_MODULES literal must contain only double-quoted module names; unexpected content: {leftovers!r}")
    return _STRING_RE.findall(inner)


def parse_server_only(src: str) -> set[str]:
    """Return the names in the ``SERVER_ONLY_MODULES = frozenset({...})`` literal (empty set when absent)."""
    match = _SERVER_ONLY_RE.search(src)
    if match is None:
        return set()
    return set(_STRING_RE.findall(match.group(1)))


def read_version(src: str) -> str:
    """Return the ``__version__`` string assigned in ``app/__init__.py`` source."""
    match = _VERSION_RE.search(src)
    if match is None:
        raise BuildError("__version__ assignment not found in app/__init__.py")
    return match.group(2)


def inject_csp(html: str) -> str:
    """Insert the Pages CSP meta tag directly after the ``<meta charset="utf-8">`` tag."""
    match = _CHARSET_RE.search(html)
    if match is None:
        raise BuildError('index.html has no <meta charset="utf-8"> tag to anchor the Content-Security-Policy meta tag')
    return html[: match.end()] + CSP_META + html[match.end() :]


def _validate_modules(modules: list[str], server_only: set[str], app_dir: Path) -> None:
    if not modules:
        raise BuildError("BROWSER_MODULES is empty; nothing would be shipped to the browser")
    duplicates = sorted({m for m in modules if modules.count(m) > 1})
    if duplicates:
        raise BuildError(f"duplicate entries in BROWSER_MODULES: {', '.join(duplicates)}")
    invalid = [m for m in modules if not _MODULE_NAME_RE.fullmatch(m)]
    if invalid:
        raise BuildError(f"BROWSER_MODULES entries must be plain *.py file names, got: {', '.join(invalid)}")
    leaked = [m for m in modules if m in server_only]
    if leaked:
        raise BuildError(f"server-only modules must not be listed in BROWSER_MODULES: {', '.join(leaked)}")
    missing = [m for m in modules if not (app_dir / m).is_file()]
    if missing:
        raise BuildError(f"modules listed in BROWSER_MODULES are missing from app/: {', '.join(missing)}")


def _prepare_out(out: Path) -> None:
    """Create ``out``; replace a previous build; refuse to wipe anything that is not one."""
    if out.exists():
        if not out.is_dir():
            raise BuildError(f"--out {out} exists and is not a directory")
        if any(out.iterdir()):
            if not ((out / "version.json").is_file() and (out / ".nojekyll").is_file()):
                raise BuildError(f"--out {out} is not empty and not a previous site build (no version.json + .nojekyll); refusing to overwrite it")
            shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)


def build(repo: Path, out: Path, sha: str | None = None) -> dict:
    """Assemble the site from ``repo`` into ``out`` and return a summary dict.

    All validation happens before anything is written, so a failed build leaves no
    partial site behind.
    """
    repo, out = Path(repo).resolve(), Path(out)
    app_dir, public = repo / "app", repo / "public"
    common, init, index = app_dir / "common.py", app_dir / "__init__.py", public / "index.html"
    if not public.is_dir():
        raise BuildError(f"public/ directory not found under {repo}")
    for path, what in ((common, "shared module"), (init, "package init"), (index, "UI entry point")):
        if not path.is_file():
            raise BuildError(f"{what} not found: {path.relative_to(repo).as_posix()}")
    resolved_out = out.resolve()
    if resolved_out in (app_dir, public) or public in resolved_out.parents or app_dir in resolved_out.parents:
        raise BuildError("--out must not point inside the repository's app/ or public/ directories")

    common_src = common.read_text(encoding="utf-8")
    modules = parse_browser_modules(common_src)
    _validate_modules(modules, parse_server_only(common_src) | ALWAYS_SERVER_ONLY, app_dir)
    version = read_version(init.read_text(encoding="utf-8"))
    try:
        html = index.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BuildError(f"public/index.html is not valid UTF-8: {exc}") from exc
    if _CSP_TAG_RE.search(html):
        raise BuildError("public/index.html already carries a Content-Security-Policy meta tag; the Pages policy is injected at build time only")
    injected = inject_csp(html)

    _prepare_out(out)
    shutil.copytree(public, out, dirs_exist_ok=True)
    (out / "index.html").write_bytes(injected.encode("utf-8"))
    (out / "app").mkdir(exist_ok=True)
    for name in modules:
        shutil.copy2(app_dir / name, out / "app" / name)
    (out / ".nojekyll").write_bytes(b"")
    built_at = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    info = {"version": version, "sha": sha or None, "built_at": built_at}
    (out / "version.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    files = sum(len(names) for _, _, names in os.walk(out))
    return {"out": str(out), **info, "modules": modules, "files": files}


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(prog="build_site.py", description="Assemble the static StockLine demo site for GitHub Pages.")
    parser.add_argument("--out", required=True, type=Path, help="output directory (created; a previous build there is replaced)")
    parser.add_argument("--sha", default=None, help="commit SHA recorded in version.json (empty/omitted -> null)")
    parser.add_argument("--repo", type=Path, default=ROOT, help="repository root (default: the parent of scripts/)")
    ns = parser.parse_args(argv)
    try:
        summary = build(ns.repo, ns.out, ns.sha or None)
    except BuildError as exc:
        print(f"build_site: error: {exc}", file=sys.stderr)
        return 1
    print(f"site built: {summary['out']}  version={summary['version']} sha={summary['sha'] or '-'} files={summary['files']}")
    print("browser modules: " + ", ".join(summary["modules"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
