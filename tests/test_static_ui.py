"""Static checks for the dashboard (no browser): CSP cleanliness, asset references, accessibility
structure, Pyodide pinning, PY_FILES parity with ``common.BROWSER_MODULES`` and JS syntax.

Everything here is derived from the files under ``public/`` with the standard library only.
``node`` is required (CI installs it before pytest); a missing node is a failure, never a skip.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "public"
INDEX = PUBLIC / "index.html"
APP_JS = PUBLIC / "app.js"
LIB_JS = PUBLIC / "lib.js"
STYLES = PUBLIC / "styles.css"
COMMON_PY = ROOT / "app" / "common.py"

PYODIDE_VERSION = "0.27.5"
CDN_HOST = "cdn.jsdelivr.net"
EXTERNAL_LINK_HOSTS = {"github.com", "d-l-narayana.github.io"}
ASSET_ATTRS = {"script": "src", "link": "href", "img": "src", "source": "src", "iframe": "src", "object": "data", "embed": "src", "use": "href"}
PY_FILES_RE = re.compile(r"const PY_FILES = \[([^\]]*)\];")
BROWSER_MODULES_RE = re.compile(r"BROWSER_MODULES\s*:\s*tuple\[str,\s*\.\.\.\]\s*=\s*\(([^)]*)\)", re.S)
SERVER_ONLY = {"main.py", "deps.py", "observability.py", "security.py"}
TOKENS = {
    "--brand": "#CC0000",
    "--brand-ink": "#fff",
    "--ink": "#111827",
    "--muted": "#6b7280",
    "--line": "#e5e7eb",
    "--surface": "#fff",
    "--bg": "#fafafa",
    "--ok": "#15803d",
    "--warn": "#b45309",
    "--danger": "#b91c1c",
    "--info": "#1d4ed8",
    "--radius": "10px",
    "--radius-sm": "6px",
    "--space-1": "4px",
    "--space-2": "8px",
    "--space-3": "12px",
    "--space-4": "16px",
    "--space-5": "24px",
    "--font": "15px/1.5 system-ui,sans-serif",
    "--mono": "ui-monospace,SFMono-Regular,Menlo,monospace",
    "--shadow": "0 1px 2px rgba(0,0,0,.06)",
}
DARK_TOKENS = {"--ink": "#e5e7eb", "--muted": "#9ca3af", "--line": "#1f2937", "--surface": "#111827", "--bg": "#0b1220"}
# Controls the v0.1 page offered; the new page must keep every one of these capabilities.
REQUIRED_IDS = {
    "mode",
    "inv-store",
    "inv-low",
    "inv-q",
    "inventory-table",
    "order-store",
    "order-key",
    "order-out",
    "adjust-store",
    "adjust-product",
    "adjust-delta",
    "adjust-reason",
    "adjust-version",
    "adjust-out",
    "transfer-from",
    "transfer-to",
    "transfer-product",
    "transfer-qty",
    "transfer-out",
    "reorder-table",
    "integrity-out",
    "orders-table",
}
VIEWS = ("inventory", "orders", "transfers", "reports", "catalog", "scenarios", "console")


class _Page(HTMLParser):
    """Collects the facts the tests assert on: elements, inline bodies, labels and button names."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.script_bodies: list[str] = []
        self.ids: list[str] = []
        self.label_for: set[str] = set()
        self.controls: list[tuple[dict[str, str | None], bool]] = []
        self.buttons: list[tuple[dict[str, str | None], str]] = []
        self.h1_count = 0
        self.style_count = 0
        self._script: list[str] | None = None
        self._button: tuple[dict[str, str | None], list[str]] | None = None
        self._label_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        self.elements.append((tag, a))
        if a.get("id") is not None:
            self.ids.append(a["id"] or "")
        if tag == "h1":
            self.h1_count += 1
        elif tag == "style":
            self.style_count += 1
        elif tag == "script":
            self._script = []
        elif tag == "label":
            self._label_depth += 1
            if a.get("for"):
                self.label_for.add(a["for"] or "")
        elif tag in ("input", "select", "textarea"):
            self.controls.append((a, self._label_depth > 0))
        elif tag == "button":
            self._button = (a, [])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script is not None:
            self.script_bodies.append("".join(self._script))
            self._script = None
        elif tag == "label" and self._label_depth:
            self._label_depth -= 1
        elif tag == "button" and self._button is not None:
            self.buttons.append((self._button[0], "".join(self._button[1])))
            self._button = None

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script.append(data)
        if self._button is not None:
            self._button[1].append(data)


def _page() -> _Page:
    assert INDEX.exists(), "public/index.html is missing"
    p = _Page()
    p.feed(INDEX.read_text(encoding="utf-8"))
    p.close()
    return p


def _read(path: Path) -> str:
    assert path.exists(), f"{path.relative_to(ROOT)} is missing"
    return path.read_text(encoding="utf-8")


def _is_external(url: str) -> bool:
    return url.startswith(("http://", "https://", "//"))


def _host(url: str) -> str:
    return url.split("//", 1)[1].split("/", 1)[0]


def _has_bare_class_hyphen(pattern: str) -> bool:
    """Browsers compile the HTML ``pattern`` attribute with the JavaScript ``v`` flag, where a literal ``-`` inside a
    character class must be escaped (only ``a-z`` style ranges may use a bare hyphen); otherwise Chromium logs a console
    SyntaxError on every keystroke and silently drops the constraint."""
    for cls in re.finditer(r"\[((?:\\.|[^\]\\])*)\]", pattern):
        tokens = re.findall(r"\\.|.", cls.group(1))
        for i, tok in enumerate(tokens):
            if tok != "-":
                continue
            if i == 0 or i == len(tokens) - 1 or tokens[i - 1] == "-" or tokens[i + 1] == "-":
                return True
    return False


# --------------------------------------------------------------------------- index.html
def test_index_references_only_local_assets_and_safe_links():
    page = _page()
    seen_assets = 0
    for tag, attrs in page.elements:
        attr = ASSET_ATTRS.get(tag)
        if attr and attrs.get(attr):
            url = attrs[attr] or ""
            if tag == "link" and attrs.get("rel") == "icon" and url.startswith("data:"):
                continue  # empty inline favicon: allowed by img-src 'self' data: and avoids a 404 for /favicon.ico
            assert not _is_external(url), f"<{tag} {attr}={url!r}> is remote; CSP allows self-hosted assets only"
            assert not url.startswith(("data:", "blob:", "javascript:")), f"<{tag}> uses a non-file URL {url!r}"
            target = PUBLIC / url.split("?", 1)[0].split("#", 1)[0]
            assert target.is_file(), f"<{tag} {attr}={url!r}> does not exist under public/"
            seen_assets += 1
        if tag == "a" and attrs.get("href"):
            href = attrs["href"] or ""
            assert not href.lower().startswith("javascript:"), "javascript: URL in a link"
            if _is_external(href):
                assert href.startswith("https://"), f"external link must be https: {href}"
                assert _host(href) in EXTERNAL_LINK_HOSTS, f"unexpected external link host in {href}"
            if attrs.get("target") == "_blank":
                assert "noopener" in (attrs.get("rel") or ""), f"target=_blank link without rel=noopener: {href}"
    assert seen_assets >= 2, "expected at least the stylesheet and the module script to be referenced"


def test_index_is_csp_clean():
    page = _page()
    assert page.style_count == 0, "<style> elements are inline CSS and are blocked by style-src 'self'"
    scripts = [attrs for tag, attrs in page.elements if tag == "script"]
    assert len(scripts) == 1, "exactly one <script> tag (the module entry point) is expected"
    assert scripts[0].get("src") == "app.js" and scripts[0].get("type") == "module", scripts[0]
    assert all(body.strip() == "" for body in page.script_bodies), "inline <script> bodies are blocked by script-src 'self'"
    for tag, attrs in page.elements:
        for name, value in attrs.items():
            assert name != "style", f"<{tag}> carries a style attribute; use classes or CSSOM instead"
            assert not name.startswith("on"), f"<{tag} {name}=…> is an inline event handler"
            assert "javascript:" not in (value or "").lower(), f"<{tag} {name}> contains a javascript: URL"
        if tag == "meta":
            equiv = (attrs.get("http-equiv") or "").lower()
            assert equiv != "content-security-policy", "the source index.html must not carry a meta CSP (the Pages build injects one)"
        if tag == "link":
            assert attrs.get("rel") in {"stylesheet", "icon"}, f"unexpected <link rel={attrs.get('rel')!r}>"
    html = _read(INDEX)
    assert CDN_HOST not in html, "the CDN is only referenced from app.js (Pyodide fallback)"
    assert "fonts.googleapis" not in html and "@import" not in html


def test_index_structure_and_accessibility():
    page = _page()
    html_attrs = next(attrs for tag, attrs in page.elements if tag == "html")
    assert html_attrs.get("lang") == "en"
    assert page.h1_count == 1, f"expected exactly one <h1>, found {page.h1_count}"
    dupes = {i for i in page.ids if page.ids.count(i) > 1}
    assert not dupes, f"duplicate ids: {sorted(dupes)}"
    assert "" not in page.ids, "empty id attribute"
    ids = set(page.ids)
    for attrs, inside_label in page.controls:
        if attrs.get("type") in {"hidden", "submit", "button"}:
            continue
        labelled = inside_label or (attrs.get("id") in page.label_for) or attrs.get("aria-label") or attrs.get("aria-labelledby")
        assert labelled, f"form control without a label: {attrs}"
    for attrs, text in page.buttons:
        assert text.strip() or attrs.get("aria-label"), f"button without an accessible name: {attrs}"
    for attrs, _ in page.controls:
        pattern = attrs.get("pattern")
        if pattern is None:
            continue
        re.compile(f"^(?:{pattern})$")
        assert not _has_bare_class_hyphen(pattern), f"pattern {pattern!r}: escape the literal hyphen inside the character class (\\-)"
    tabs = [attrs for tag, attrs in page.elements if tag == "button" and attrs.get("role") == "tab"]
    panels = [attrs for tag, attrs in page.elements if tag == "section" and attrs.get("role") == "tabpanel"]
    assert len(tabs) == len(VIEWS) == len(panels), (len(tabs), len(panels))
    assert any(attrs.get("role") == "tablist" for _, attrs in page.elements), "tabs must sit inside a role=tablist container"
    for tab in tabs:
        assert tab.get("aria-selected") in {"true", "false"}, tab
        assert tab.get("aria-controls") in ids, f"tab controls a missing panel: {tab}"
    assert sum(1 for tab in tabs if tab.get("aria-selected") == "true") == 1
    for panel in panels:
        assert panel.get("aria-labelledby") in ids, f"panel without a labelling tab: {panel}"
    assert any(attrs.get("aria-live") == "polite" for _, attrs in page.elements), "no aria-live=polite result region"
    header = [tag for tag, _ in page.elements if tag in {"header", "nav", "main"}]
    assert {"header", "nav", "main"} <= set(header)
    assert "mode" in ids, "the mode banner must keep id=mode"


def test_index_keeps_every_v01_capability_and_all_views():
    page = _page()
    ids = set(page.ids)
    missing = REQUIRED_IDS - ids
    assert not missing, f"controls from the v0.1 page are missing: {sorted(missing)}"
    for view in VIEWS:
        assert f"view-{view}" in ids and f"tab-{view}" in ids, f"view {view!r} is not wired as tab + panel"
    actions = {attrs.get("data-action") for _tag, attrs in page.elements if attrs.get("data-action")}
    for action in ("export-inventory", "export-orders", "export-reorder", "integrity", "console-clear", "generate-key"):
        assert action in actions, f"missing data-action={action!r}"
    scenario_buttons = [attrs for tag, attrs in page.elements if tag == "button" and attrs.get("data-action") == "run-scenario"]
    assert len(scenario_buttons) == 5, "five one-click scenarios are expected"
    assert {attrs.get("data-scenario") for attrs in scenario_buttons} == {"oversell", "idempotent", "stale", "transfer", "audit"}


# --------------------------------------------------------------------------- JavaScript
def test_js_has_no_inline_style_eval_or_javascript_urls():
    for path in (APP_JS, LIB_JS):
        src = _read(path)
        rel = path.relative_to(ROOT)
        assert 'style="' not in src and "style='" not in src, f"{rel}: inline style attribute in a template"
        assert "eval(" not in src, f"{rel}: eval is blocked by the CSP"
        assert "new Function" not in src, f"{rel}: new Function is blocked by the CSP"
        assert "javascript:" not in src.lower(), f"{rel}: javascript: URL"
        assert "document.write" not in src, f"{rel}: document.write"
        assert not re.search(r"<[a-zA-Z][^>]*\son[a-z]+\s*=", src), f"{rel}: inline event handler inside an HTML template"
        assert "/home/" not in src, f"{rel}: private path"
    app = _read(APP_JS)
    assert re.search(r"""import\s*\{[^}]*\}\s*from\s*['"]\./lib\.js['"]""", app), "app.js must import lib.js as an ES module"
    delegated = "document.body.addEventListener('click'" in app or 'document.body.addEventListener("click"' in app
    assert delegated, "one delegated click listener on document.body is expected"


def test_js_detects_the_server_with_a_relative_health_fetch():
    app = _read(APP_JS)
    health_probe = re.compile(r"""fetch\(\s*['"]health['"]\s*,\s*\{\s*cache:\s*['"]no-store['"]\s*\}\s*\)""")
    assert health_probe.search(app), "mode detection must be fetch('health', {cache: 'no-store'})"
    assert not re.search(r"""fetch\(\s*['"]/""", app), "fetch() must use relative URLs (the Pages site lives under /stockline/)"
    assert "aria-live" in _read(INDEX)


def test_pyodide_version_is_pinned_and_consistent():
    app = _read(APP_JS)
    src_versions = re.findall(r"https://cdn\.jsdelivr\.net/pyodide/v(\d+\.\d+\.\d+)/full/pyodide\.js", app)
    index_versions = re.findall(r"""indexURL:\s*['"]https://cdn\.jsdelivr\.net/pyodide/v(\d+\.\d+\.\d+)/full/['"]""", app)
    assert src_versions == [PYODIDE_VERSION], f"pyodide.js script src versions: {src_versions}"
    assert index_versions == [PYODIDE_VERSION], f"indexURL versions: {index_versions}"
    assert app.count("pyodide.js") == 1, "the CDN script must be injected from exactly one place"
    assert CDN_HOST not in _read(LIB_JS), "lib.js is pure and must not know about the CDN"
    assert "loadPackage(['pydantic', 'sqlite3'])" in app or 'loadPackage(["pydantic", "sqlite3"])' in app
    assert "STOCKLINE_DB" in app and "from app import bridge" in app and "handle_json" in app


def _py_files() -> list[str]:
    app = _read(APP_JS)
    m = PY_FILES_RE.search(app)
    assert m, "app.js must contain a single-statement `const PY_FILES = [\"…\", …];` literal"
    inner = m.group(1)
    assert re.fullmatch(r'\s*(?:"[^"]+"\s*,\s*)*"[^"]+"\s*,?\s*', inner), f"PY_FILES must be double-quoted strings only: {inner!r}"
    assert len(PY_FILES_RE.findall(app)) == 1
    return re.findall(r'"([^"]+)"', inner)


def test_py_files_literal_parses_and_is_browser_safe():
    files = _py_files()
    assert files[0] == "__init__.py" and files[-1] == "bridge.py", files
    assert len(files) == len(set(files)), "duplicate entries in PY_FILES"
    assert all(f.endswith(".py") for f in files)
    assert not (set(files) & SERVER_ONLY), "server-only modules must never be shipped to the browser"
    for name in ("common.py", "db.py", "schemas.py", "service.py", "seed.py"):
        assert name in files, f"{name} missing from PY_FILES"


def test_py_files_literal_matches_browser_modules():
    assert COMMON_PY.exists(), "app/common.py (BROWSER_MODULES) is missing"
    m = BROWSER_MODULES_RE.search(COMMON_PY.read_text(encoding="utf-8"))
    assert m, "BROWSER_MODULES literal not found in app/common.py"
    modules = re.findall(r'"([^"]+)"', m.group(1))
    assert _py_files() == modules, "PY_FILES in app.js must equal common.BROWSER_MODULES in the same order"


# --------------------------------------------------------------------------- CSS
def test_css_tokens_dark_mode_focus_and_no_remote_resources():
    css = _read(STYLES)
    root = re.search(r":root\s*\{([^}]*)\}", css)
    assert root, "styles.css must declare tokens in :root"
    for name, value in TOKENS.items():
        assert re.search(rf"{re.escape(name)}\s*:\s*{re.escape(value)}\s*;", root.group(1)), f"token {name}:{value} missing from :root"
    dark = re.search(r"@media\s*\(prefers-color-scheme:\s*dark\)\s*\{(.*?)\}\s*\}", css, re.S)
    assert dark, "dark mode must be provided via @media (prefers-color-scheme: dark)"
    for name, value in DARK_TOKENS.items():
        assert re.search(rf"{re.escape(name)}\s*:\s*{re.escape(value)}\s*;", dark.group(1)), f"dark token {name}:{value} missing"
    assert ":focus-visible" in css, "visible focus rings are required"
    for cls in (".badge-ok", ".badge-warn", ".badge-danger", ".badge-info", ".low", ".drawer", ".spark"):
        assert cls in css, f"{cls} class missing"
    assert re.search(r"@media\s*\(max-width:\s*(799px|800px)\)", css), "single-column layout below 800px is required"
    assert "@import" not in css and "@font-face" not in css, "no remote fonts"
    assert not re.search(r"url\(\s*['\"]?\s*(https?:)?//", css), "no remote images or fonts in CSS"
    assert "#CC0000" in css or "#cc0000" in css


def _css_declarations(css: str, selector: str) -> dict[str, str]:
    """Return the ``property -> value`` map of the rule whose selector list is exactly ``selector``."""
    match = re.search(rf"(?m)^{re.escape(selector)}\s*\{{([^}}]*)\}}", css)
    assert match, f"rule {selector!r} not found in styles.css"
    declarations: dict[str, str] = {}
    for part in match.group(1).split(";"):
        if ":" in part:
            prop, value = part.split(":", 1)
            declarations[prop.strip().lower()] = value.strip().lower()
    return declarations


def test_table_wrapper_scrolls_and_is_the_containing_block_for_hidden_cells():
    """Visually hidden ``.sr-only`` header cells are absolutely positioned. Unless the scrolling ``.table-wrap`` wrapper is
    positioned as well, their containing block is the initial containing block, so ``overflow: auto`` on the wrapper cannot
    clip them and the 1 px box at the table's intrinsic x-position extends the document width on narrow viewports
    (390 px: 277 px of horizontal document overflow on the inventory view). ``position: relative`` makes the wrapper their
    containing block so they scroll and clip with the table; the hidden text must stay visually hidden (no ``.sr-only`` change)
    and no global ``overflow: hidden`` may be used instead."""
    rules = _css_declarations(_read(STYLES), ".table-wrap")
    assert rules.get("overflow") == "auto" or rules.get("overflow-x") == "auto", f".table-wrap must scroll horizontally: {rules}"
    assert rules.get("position") == "relative", f".table-wrap must be positioned to contain absolutely positioned .sr-only cells: {rules}"
    sr_only = _css_declarations(_read(STYLES), ".sr-only")
    assert sr_only.get("position") == "absolute", "the .sr-only utility must keep hiding content off-screen via absolute positioning"


# --------------------------------------------------------------------------- node
def test_node_check_passes_for_each_public_script(tmp_path):
    node = shutil.which("node")
    assert node, "node is required for the UI checks (install Node 18+); this test never skips"
    scripts = sorted(PUBLIC.glob("*.js"))
    assert {p.name for p in scripts} >= {"app.js", "lib.js"}, "app.js and lib.js must exist under public/"
    env = {**os.environ, "STOCKLINE_DB": str(tmp_path / "ui.db")}
    for path in scripts:
        proc = subprocess.run([node, "--check", str(path)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f"node --check {path.name} failed:\n{proc.stdout}\n{proc.stderr}"


def test_public_files_are_portable():
    names = {p.name for p in PUBLIC.iterdir()}
    assert names == {"index.html", "app.js", "lib.js", "styles.css"}, f"unexpected files under public/: {sorted(names)}"
    for path in sorted(PUBLIC.iterdir()):
        text = path.read_text(encoding="utf-8")
        assert "/home/" not in text, f"{path.name}: private path"
        assert "localhost:8000" not in text or path.name == "app.js", f"{path.name}: hard-coded host"
