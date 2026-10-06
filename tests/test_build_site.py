"""Tests for ``scripts/build_site.py``, the GitHub Pages site assembler.

The builder is a script, not a package, so it is imported via importlib from ``scripts/``.
Most tests run it against a throw-away repository layout created in ``tmp_path`` (fake
``app/common.py`` with a known ``BROWSER_MODULES`` tuple, fake modules, fake ``public/``),
so they do not depend on the real module list owned by other workers.  One test builds the
real repository to enforce the single-source-of-truth contract (acceptance gate 4).
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sys
from pathlib import Path

import pytest

from app import __version__ as APP_VERSION

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
build_site = importlib.import_module("build_site")

CHARSET = '<meta charset="utf-8">'
SERVER_ONLY = ("main.py", "deps.py", "observability.py", "security.py")
MODULES = ["__init__.py", "common.py", "db.py", "schemas.py", "bridge.py"]
COMMON_SRC = '''"""Fake shared module: only the two literals the builder parses."""
BROWSER_MODULES: tuple[str, ...] = (
    "__init__.py", "common.py", "db.py",
    "schemas.py", "bridge.py",
)
SERVER_ONLY_MODULES: frozenset[str] = frozenset({"main.py", "deps.py", "observability.py", "security.py"})
'''
INDEX_HTML = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
    '<title>fake</title><link rel="stylesheet" href="styles.css"></head>\n'
    '<body><script type="module" src="app.js"></script></body></html>\n'
)
PUBLIC_FILES = {
    "index.html": INDEX_HTML,
    "app.js": "export const x = 1;\n",
    "lib.js": "export const y = 2;\n",
    "styles.css": ":root{}\n",
    "img/logo.svg": "<svg xmlns='http://www.w3.org/2000/svg'/>\n",
}
EXPECTED_CSP_META = (
    '<meta http-equiv="Content-Security-Policy" content="default-src \'self\'; '
    "script-src 'self' https://cdn.jsdelivr.net 'wasm-unsafe-eval' 'unsafe-eval'; "
    "style-src 'self'; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self' https://cdn.jsdelivr.net; worker-src 'self' blob:; "
    "object-src 'none'; base-uri 'self'; form-action 'self'\">"
)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A minimal repository layout: app/ with browser + server-only modules, public/ with assets."""
    root = tmp_path / "repo"
    app_dir = root / "app"
    app_dir.mkdir(parents=True)
    (app_dir / "__init__.py").write_text('"""fake app"""\n__version__ = "9.9.9"\n', encoding="utf-8")
    (app_dir / "common.py").write_text(COMMON_SRC, encoding="utf-8")
    for name in MODULES:
        if name not in ("__init__.py", "common.py"):
            (app_dir / name).write_text(f"# browser module {name}\n", encoding="utf-8")
    for name in SERVER_ONLY:
        (app_dir / name).write_text(f"# server-only module {name}\n", encoding="utf-8")
    for rel, text in PUBLIC_FILES.items():
        path = root / "public" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _files(out: Path) -> set[str]:
    if not out.exists():
        return set()
    return {Path(dirpath, name).relative_to(out).as_posix() for dirpath, _, names in os.walk(out) for name in names}


# ------------------------------------------------------------------ build(): output set


def test_build_output_is_exactly_public_plus_browser_modules(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "site"
    summary = build_site.build(repo, out, "abc1234")
    expected = set(PUBLIC_FILES) | {f"app/{m}" for m in MODULES} | {".nojekyll", "version.json"}
    assert _files(out) == expected
    assert summary["modules"] == MODULES
    for name in SERVER_ONLY:
        assert not (out / "app" / name).exists(), f"{name} must never ship to the browser"
    assert (out / "app" / "db.py").read_text(encoding="utf-8") == "# browser module db.py\n"
    assert (out / "img" / "logo.svg").read_text(encoding="utf-8") == PUBLIC_FILES["img/logo.svg"]


def test_version_json_records_version_sha_and_utc_timestamp(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "site"
    build_site.build(repo, out, "abc1234")
    version_file = out / "version.json"
    assert version_file.is_file(), "version.json was not written"
    data = json.loads(version_file.read_text(encoding="utf-8"))
    assert set(data) == {"version", "sha", "built_at"}
    assert data["version"] == "9.9.9"
    assert data["sha"] == "abc1234"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", data["built_at"]), data["built_at"]


def test_version_json_sha_is_null_when_not_given_and_nojekyll_is_empty(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "site"
    build_site.build(repo, out, None)
    assert (out / "version.json").is_file(), "version.json was not written"
    assert json.loads((out / "version.json").read_text(encoding="utf-8"))["sha"] is None
    assert (out / ".nojekyll").is_file(), ".nojekyll was not written"
    assert (out / ".nojekyll").read_bytes() == b""


# ------------------------------------------------------------------ CSP meta injection


def test_csp_meta_tag_is_the_pages_policy() -> None:
    assert build_site.CSP_META == EXPECTED_CSP_META
    assert build_site.CSP_META == f'<meta http-equiv="Content-Security-Policy" content="{build_site.PAGES_CSP}">'


def test_csp_meta_is_injected_after_charset_in_the_copy_only(repo: Path, tmp_path: Path) -> None:
    source = repo / "public" / "index.html"
    before = source.read_bytes()
    out = tmp_path / "site"
    build_site.build(repo, out, None)
    copy = out / "index.html"
    assert copy.is_file(), "index.html was not copied"
    html = copy.read_text(encoding="utf-8")
    assert html.count(EXPECTED_CSP_META) == 1
    assert html == INDEX_HTML.replace(CHARSET, CHARSET + EXPECTED_CSP_META, 1)
    assert source.read_bytes() == before, "the repository index.html must stay untouched"
    assert "Content-Security-Policy" not in before.decode("utf-8")


def test_inject_csp_inserts_directly_after_the_charset_meta() -> None:
    html = '<html><head><meta charset="utf-8"><title>x</title></head></html>'
    assert build_site.inject_csp(html) == '<html><head><meta charset="utf-8">' + EXPECTED_CSP_META + "<title>x</title></head></html>"


def test_inject_csp_requires_the_charset_meta() -> None:
    with pytest.raises(build_site.BuildError, match="charset"):
        build_site.inject_csp("<html><head><title>x</title></head></html>")


def test_index_without_charset_meta_fails_before_anything_is_written(repo: Path, tmp_path: Path) -> None:
    index = repo / "public" / "index.html"
    index.write_text(index.read_text(encoding="utf-8").replace(CHARSET, ""), encoding="utf-8")
    out = tmp_path / "site"
    with pytest.raises(build_site.BuildError, match="charset"):
        build_site.build(repo, out, None)
    assert not out.exists()


def test_source_index_already_carrying_a_meta_csp_is_rejected(repo: Path, tmp_path: Path) -> None:
    index = repo / "public" / "index.html"
    stray = '<meta http-equiv="Content-Security-Policy" content="default-src \'self\'">'
    index.write_text(index.read_text(encoding="utf-8").replace(CHARSET, CHARSET + stray), encoding="utf-8")
    with pytest.raises(build_site.BuildError, match="Content-Security-Policy"):
        build_site.build(repo, tmp_path / "site", None)


# ------------------------------------------------------------------ failure modes


def test_missing_listed_module_fails_loudly(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "app" / "db.py").unlink()
    out = tmp_path / "site"
    with pytest.raises(build_site.BuildError, match="db.py"):
        build_site.build(repo, out, None)
    assert not out.exists(), "a failed build must not leave a partial site behind"
    assert build_site.main(["--out", str(out), "--repo", str(repo)]) != 0
    assert "db.py" in capsys.readouterr().err


def test_server_only_module_in_the_list_is_rejected(repo: Path, tmp_path: Path) -> None:
    (repo / "app" / "common.py").write_text(COMMON_SRC.replace('"bridge.py",', '"bridge.py", "deps.py",'), encoding="utf-8")
    with pytest.raises(build_site.BuildError, match="deps.py"):
        build_site.build(repo, tmp_path / "site", None)
    assert not (tmp_path / "site").exists()


def test_hard_coded_server_only_names_are_rejected_without_the_frozenset(repo: Path, tmp_path: Path) -> None:
    src = 'BROWSER_MODULES: tuple[str, ...] = ("__init__.py", "common.py", "main.py")\n'
    (repo / "app" / "common.py").write_text(src, encoding="utf-8")
    with pytest.raises(build_site.BuildError, match="main.py"):
        build_site.build(repo, tmp_path / "site", None)


def test_duplicate_module_names_are_rejected(repo: Path, tmp_path: Path) -> None:
    (repo / "app" / "common.py").write_text(COMMON_SRC.replace('"db.py",', '"db.py", "db.py",'), encoding="utf-8")
    with pytest.raises(build_site.BuildError, match="duplicate"):
        build_site.build(repo, tmp_path / "site", None)


def test_missing_common_module_is_reported_precisely(repo: Path, tmp_path: Path) -> None:
    (repo / "app" / "common.py").unlink()
    with pytest.raises(build_site.BuildError, match="app/common.py"):
        build_site.build(repo, tmp_path / "site", None)


def test_rebuild_replaces_a_previous_build_but_refuses_foreign_directories(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "site"
    build_site.build(repo, out, None)
    assert (out / "app").is_dir(), "site/app was not created"
    stale = out / "app" / "stale.py"
    stale.write_text("# left over from an earlier module list\n", encoding="utf-8")
    build_site.build(repo, out, None)
    assert not stale.exists(), "a rebuild must not keep files from a previous build"
    foreign = tmp_path / "not-a-site"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("precious\n", encoding="utf-8")
    with pytest.raises(build_site.BuildError, match="not a previous site build"):
        build_site.build(repo, foreign, None)
    assert (foreign / "keep.txt").read_text(encoding="utf-8") == "precious\n"


# ------------------------------------------------------------------ parsers


def test_parse_browser_modules_handles_the_multiline_literal() -> None:
    src = (
        "X = 1\n"
        "BROWSER_MODULES: tuple[str, ...] = (\n"
        '    "__init__.py", "common.py", "db.py", "schemas.py", "ledger.py", "catalog.py",\n'
        '    "inventory.py", "orders.py", "reports.py", "service.py", "seed.py", "bridge.py",\n'
        ")\n"
        'SERVER_ONLY_MODULES: frozenset[str] = frozenset({"main.py", "deps.py", "observability.py", "security.py"})\n'
        "for name in BROWSER_MODULES:\n    pass\n"
    )
    assert build_site.parse_browser_modules(src) == [
        "__init__.py", "common.py", "db.py", "schemas.py", "ledger.py", "catalog.py",
        "inventory.py", "orders.py", "reports.py", "service.py", "seed.py", "bridge.py",
    ]
    assert build_site.parse_server_only(src) == {"main.py", "deps.py", "observability.py", "security.py"}


def test_parse_browser_modules_accepts_a_single_line_and_requires_the_literal() -> None:
    assert build_site.parse_browser_modules('BROWSER_MODULES: tuple[str, ...] = ("__init__.py", "bridge.py")\n') == ["__init__.py", "bridge.py"]
    with pytest.raises(build_site.BuildError, match="BROWSER_MODULES"):
        build_site.parse_browser_modules("nothing to see here\n")


def test_parse_server_only_handles_multiline_frozenset_and_absence() -> None:
    src = 'SERVER_ONLY_MODULES: frozenset[str] = frozenset({\n    "main.py",\n    "deps.py",\n})\n'
    assert build_site.parse_server_only(src) == {"main.py", "deps.py"}
    assert build_site.parse_server_only('BROWSER_MODULES = ("a.py",)\n') == set()


def test_read_version_parses_dunder_version() -> None:
    assert build_site.read_version('"""doc"""\n__version__ = "0.2.0"\n') == "0.2.0"
    assert build_site.read_version("__version__ = '1.2.3'\n") == "1.2.3"
    with pytest.raises(build_site.BuildError, match="__version__"):
        build_site.read_version("VERSION = 1\n")


# ------------------------------------------------------------------ CLI


def test_main_builds_and_prints_a_summary(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "site"
    assert build_site.main(["--out", str(out), "--repo", str(repo), "--sha", "feedface"]) == 0
    assert (out / "version.json").is_file(), "version.json was not written"
    assert json.loads((out / "version.json").read_text(encoding="utf-8"))["sha"] == "feedface"
    printed = capsys.readouterr().out
    assert str(out) in printed
    assert "feedface" in printed
    assert "bridge.py" in printed
    # an empty --sha (e.g. an unset $GITHUB_SHA) is recorded as null, not ""
    out2 = tmp_path / "site2"
    assert build_site.main(["--out", str(out2), "--repo", str(repo), "--sha", ""]) == 0
    assert json.loads((out2 / "version.json").read_text(encoding="utf-8"))["sha"] is None


# ------------------------------------------------------------------ the real repository (gate 4)


def test_real_repository_builds_from_browser_modules(tmp_path: Path) -> None:
    """The actual tree assembles: shipped modules come from app/common.py and exclude server-only code."""
    source = ROOT / "public" / "index.html"
    before = source.read_bytes()
    out = tmp_path / "site"
    summary = build_site.build(ROOT, out, None)
    assert (out / "version.json").is_file(), "version.json was not written"
    assert json.loads((out / "version.json").read_text(encoding="utf-8"))["version"] == APP_VERSION
    shipped = {p.name for p in (out / "app").iterdir()}
    assert shipped == set(summary["modules"])
    assert {"__init__.py", "common.py", "db.py", "schemas.py", "service.py", "seed.py", "bridge.py"} <= shipped
    assert shipped.isdisjoint(SERVER_ONLY)
    assert (out / "index.html").read_text(encoding="utf-8").count(EXPECTED_CSP_META) == 1
    assert (out / ".nojekyll").is_file()
    assert source.read_bytes() == before, "the repository index.html must stay untouched"
