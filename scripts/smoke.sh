#!/usr/bin/env bash
# StockLine end-to-end smoke test.
#
# Boots a temporary uvicorn server (seeded demo data, throw-away SQLite database, free
# port) and exercises the public API: health + schema version, reports, catalogue,
# stock adjustment, idempotent order placement (201, then 200 on replay), ledger
# integrity and the CSV export.  Prints one PASS/FAIL line per check and exits 1 if
# any check fails.  The server is always stopped and the temp directory removed.
#
# Usage (from any directory; the script changes to the repository root itself):
#   bash scripts/smoke.sh
#   PYTHON=python3.12 bash scripts/smoke.sh      # pick the interpreter explicitly
#
# Interpreter: $PYTHON if set, else ./.venv/bin/python when executable, else python3.
# HTTP client: curl when available, otherwise a tiny urllib helper run with "$PYTHON".
set -u

cd "$(dirname "$0")/.." || exit 2

if [ -z "${PYTHON:-}" ]; then
  if [ -x ./.venv/bin/python ]; then PYTHON=./.venv/bin/python; else PYTHON=python3; fi
fi

TMP=$(mktemp -d 2>/dev/null || mktemp -d -t stockline-smoke)
SERVER_PID=""
PASSED=0
FAILED=0

cleanup() {
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null
    n=0
    while [ "$n" -lt 20 ] && kill -0 "$SERVER_PID" 2>/dev/null; do
      sleep 0.25
      n=$((n + 1))
    done
    if kill -0 "$SERVER_PID" 2>/dev/null; then kill -9 "$SERVER_PID" 2>/dev/null; fi
    wait "$SERVER_PID" 2>/dev/null
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

pass() { PASSED=$((PASSED + 1)); printf 'PASS  %-24s %s\n' "$1" "$2"; }
fail() { FAILED=$((FAILED + 1)); printf 'FAIL  %-24s %s\n' "$1" "$2"; }
snippet() { head -c 200 "$TMP/body" | tr '\n\r' '  '; }

if command -v curl >/dev/null 2>&1; then HAVE_CURL=1; else HAVE_CURL=0; fi

cat >"$TMP/smoke_http.py" <<'PY'
"""Fallback HTTP client (no curl): prints the status code, writes body and headers to files."""
import sys
import urllib.error
import urllib.request

method, url, body_file, headers_file, body, extra = sys.argv[1:7]
data = body.encode("utf-8") if body else None
req = urllib.request.Request(url, data=data, method=method)
if data is not None:
    req.add_header("Content-Type", "application/json")
if extra:
    name, _, value = extra.partition(":")
    req.add_header(name.strip(), value.strip())
try:
    resp = urllib.request.urlopen(req, timeout=10)
except urllib.error.HTTPError as err:
    resp = err
except (urllib.error.URLError, OSError):
    open(body_file, "wb").close()
    open(headers_file, "w").close()
    print("000")
    sys.exit(0)
with open(body_file, "wb") as fh:
    fh.write(resp.read())
with open(headers_file, "w", encoding="utf-8") as fh:
    for name, value in resp.headers.items():
        fh.write(f"{name}: {value}\n")
print(getattr(resp, "status", None) or resp.getcode())
PY

# http METHOD PATH [JSON_BODY] [EXTRA_HEADER]
# Prints the HTTP status code ("000" when no response); body -> $TMP/body, headers -> $TMP/headers.
http() {
  local method=$1 path=$2 body=${3:-} extra=${4:-}
  : >"$TMP/body"
  : >"$TMP/headers"
  if [ "$HAVE_CURL" = 1 ]; then
    local args=(-s -S --max-time 10 -o "$TMP/body" -D "$TMP/headers" -w '%{http_code}' -X "$method")
    if [ -n "$body" ]; then args+=(-H 'Content-Type: application/json' --data "$body"); fi
    if [ -n "$extra" ]; then args+=(-H "$extra"); fi
    curl "${args[@]}" "$BASE$path" 2>>"$TMP/curl.err" || true
  else
    "$PYTHON" "$TMP/smoke_http.py" "$method" "$BASE$path" "$TMP/body" "$TMP/headers" "$body" "$extra"
  fi
}

# check_status NAME EXPECTED METHOD PATH [JSON_BODY] [EXTRA_HEADER] -> 0 when the status matches
check_status() {
  local name=$1 want=$2 code
  shift 2
  code=$(http "$@")
  if [ "$code" = "$want" ]; then
    pass "$name" "$1 $2 -> $code"
    return 0
  fi
  fail "$name" "$1 $2 -> HTTP $code (expected $want): $(snippet)"
  return 1
}

# ---------------------------------------------------------------- start the server
PORT=$("$PYTHON" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
case $PORT in
  '' | *[!0-9]*) echo "smoke: could not pick a free port with $PYTHON" >&2; exit 2 ;;
esac
BASE="http://127.0.0.1:$PORT"
export STOCKLINE_DB="$TMP/smoke.db" STOCKLINE_SEED=1
unset STOCKLINE_API_KEY  # the smoke test exercises the open (demo) configuration
printf 'smoke: interpreter=%s base=%s client=%s\n' "$PYTHON" "$BASE" "$([ "$HAVE_CURL" = 1 ] && echo curl || echo urllib)"

"$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" >"$TMP/server.log" 2>&1 &
SERVER_PID=$!

ready=0
code=000
i=0
while [ "$i" -lt 30 ]; do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then break; fi
  code=$(http GET /health)
  if [ "$code" = 200 ]; then ready=1; break; fi
  sleep 0.5
  i=$((i + 1))
done
if [ "$ready" != 1 ]; then
  fail health-ready "GET /health on $BASE not answering 200 within 15 s (last status $code)"
  echo "--- server log (last 40 lines) ---"
  tail -n 40 "$TMP/server.log"
  echo "smoke: $PASSED passed, $FAILED failed"
  exit 1
fi
pass health-ready "GET /health -> 200 after $((i + 1)) poll(s)"

# ---------------------------------------------------------------- checks
code=$(http GET /health)
if [ "$code" = 200 ] && grep -Eq '"status": *"ok"' "$TMP/body"; then
  pass health-status "$(snippet)"
else
  fail health-status "HTTP $code: $(snippet)"
fi
if grep -q '"schema_version"' "$TMP/body"; then
  pass health-schema-version "$(grep -Eo '"schema_version": *[0-9]+' "$TMP/body" | head -n 1)"
else
  fail health-schema-version "GET /health body has no \"schema_version\" field: $(snippet)"
fi

check_status reports-summary 200 GET /reports/summary

if check_status products-limit-1 200 GET '/products?limit=1'; then
  if grep -q '"items"' "$TMP/body"; then
    pass products-page-shape "$(grep -Eo '"total": *[0-9]+' "$TMP/body" | head -n 1)"
  else
    fail products-page-shape "no \"items\" in body: $(snippet)"
  fi
else
  fail products-page-shape "prerequisite GET /products?limit=1 failed"
fi

# make sure product 1 at store 1 has stock, then place the same order twice with one key
check_status inventory-adjust 200 POST /inventory/1/1/adjust '{"delta": 5, "reason": "receipt", "reference": "smoke"}'

ORDER='{"store_id": 1, "lines": [{"product_id": 1, "quantity": 1}]}'
first_id=""
second_id=""
if check_status orders-create 201 POST /orders "$ORDER" 'Idempotency-Key: smoke-1'; then
  first_id=$(grep -Eo '"id": *[0-9]+' "$TMP/body" | head -n 1 | grep -Eo '[0-9]+$')
fi
if check_status orders-replay 200 POST /orders "$ORDER" 'Idempotency-Key: smoke-1'; then
  second_id=$(grep -Eo '"id": *[0-9]+' "$TMP/body" | head -n 1 | grep -Eo '[0-9]+$')
fi
if grep -Eiq '^Idempotent-Replayed: *true' "$TMP/headers" && [ -n "$first_id" ] && [ "$first_id" = "$second_id" ]; then
  pass orders-replay-header "Idempotent-Replayed: true, same order id $first_id"
else
  got_header=$(grep -i 'idempotent-replayed' "$TMP/headers" | tr -d '\r')
  fail orders-replay-header "expected Idempotent-Replayed: true with order id '$first_id'; got id '$second_id', header '$got_header'"
fi

code=$(http GET /integrity)
if [ "$code" = 200 ] && grep -Eq '"ok": *true' "$TMP/body"; then
  pass integrity-ok "ledger matches cached balances"
else
  fail integrity-ok "HTTP $code: $(snippet)"
fi

code=$(http GET /inventory/export.csv)
ctype=$(grep -i '^content-type:' "$TMP/headers" | head -n 1 | tr -d '\r')
header_row=$(head -n 1 "$TMP/body" | tr -d '\r')
rows=$(grep -c . "$TMP/body")
csv_type_ok=0
header_ok=0
case $ctype in *text/csv* | *TEXT/CSV* | *Text/CSV*) csv_type_ok=1 ;; esac
case $header_row in *store_id*sku* | *sku*store_id*) header_ok=1 ;; esac
if [ "$code" = 200 ] && [ "$csv_type_ok" = 1 ] && [ "$header_ok" = 1 ]; then
  pass inventory-export-csv "$ctype; header: $header_row; lines=$rows"
else
  fail inventory-export-csv "HTTP $code; '$ctype'; first line: '$header_row' (expected text/csv with a store_id/sku header row)"
fi

# ---------------------------------------------------------------- summary
echo "smoke: $PASSED passed, $FAILED failed"
if [ "$FAILED" -gt 0 ]; then
  echo "--- server log (last 40 lines) ---"
  tail -n 40 "$TMP/server.log"
  exit 1
fi
exit 0
