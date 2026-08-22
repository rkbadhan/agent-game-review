#!/usr/bin/env bash
# ============================================================================
# verify-five-minute-path.sh
#
# Cold-start verification of the eval-game five-minute path.
#
# Simulates what a stranger experiences: creates a temporary Python virtual
# environment, installs the package from source (with the [api] extra), runs
# ``agr demo-store``, starts ``agr serve`` briefly, curls every key endpoint,
# then tears everything down.
#
# Usage:
#   bash scripts/verify-five-minute-path.sh
#
# Returns 0 if every step passes, 1 if any step fails.
# ============================================================================

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR=$(mktemp -d -t eval-game-verify-XXXXXX)
STORE_DIR="$VENV_DIR/agr-store"
PASS=0
FAIL=0
SERVER_PID=""

cleanup() {
    set +e
    [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null && wait "$SERVER_PID" 2>/dev/null
    rm -rf "$VENV_DIR"
}
trap cleanup EXIT

pass()  { PASS=$((PASS + 1)); echo "  ✓ $1"; }
fail()  { FAIL=$((FAIL + 1)); echo "  ✗ $1"; }

header() {
    echo ""
    echo "━━━ $1 ━━━"
}

# ------------------------------------------------------------------
# 1. Create a fresh virtual environment (cold start simulation)
# ------------------------------------------------------------------
header "1. Creating virtual environment"
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --quiet --upgrade pip
echo "  venv at $VENV_DIR"

# ------------------------------------------------------------------
# 2. Install the package from source with the [api] extra
# ------------------------------------------------------------------
header "2. pip install .[api]"
pip install --quiet "$ROOT_DIR[api]" 2>&1 | tail -2
# The 'agr' console_scripts entry point should now be on PATH
if command -v agr &>/dev/null; then
    pass "agr command found on PATH"
else
    fail "agr command not on PATH  (try: pip install -e .[api])"
    # Fallback: use python -m agr for the rest
    AGR="python -m agr"
fi
AGR="${AGR:-agr}"
echo "  using: $AGR"

# ------------------------------------------------------------------
# 3. Verify help works
# ------------------------------------------------------------------
header "3. agr --help"
$AGR --help >/dev/null 2>&1 && pass "help works" || fail "help failed"

# ------------------------------------------------------------------
# 4. Build the demo store
# ------------------------------------------------------------------
header "4. agr demo-store"
rm -rf "$STORE_DIR"
$AGR --store "$STORE_DIR" demo-store 2>&1 | head -5
if [ -d "$STORE_DIR/runs" ]; then
    RUN_COUNT=$(ls "$STORE_DIR/runs" | wc -l)
    echo "  runs created: $RUN_COUNT"
    [ "$RUN_COUNT" -eq 12 ] && pass "12 runs created" || fail "expected 12 runs, got $RUN_COUNT"
else
    fail "demo-store did not create runs/ directory"
fi

# ------------------------------------------------------------------
# 5. Verify runs list via CLI
# ------------------------------------------------------------------
header "5. agr runs (CLI)"
OUT=$($AGR --store "$STORE_DIR" runs 2>&1)
LINE_COUNT=$(echo "$OUT" | grep -c "^")
echo "$OUT" | head -5
echo "  ... ($LINE_COUNT lines)"
[ "$LINE_COUNT" -eq 12 ] && pass "12 runs listed" || fail "expected 12, got $LINE_COUNT"

# ------------------------------------------------------------------
# 6. Start the server and test API endpoints
# ------------------------------------------------------------------
header "6. agr serve → HTTP endpoints"
$AGR --store "$STORE_DIR" serve &
SERVER_PID=$!
sleep 3  # give uvicorn time to bind

ENDPOINT_PASS=0
ENDPOINT_FAIL=0
endpoint_pass() { ENDPOINT_PASS=$((ENDPOINT_PASS+1)); echo "    ✓ $1"; }
endpoint_fail() { ENDPOINT_FAIL=$((ENDPOINT_FAIL+1)); echo "    ✗ $1"; }

# 6a. GET /  →  SPA HTML
SPA=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/)
[ "$SPA" = "200" ] && endpoint_pass "GET /  200" || endpoint_fail "GET /  $SPA"

# 6b. GET /healthz
HZ=$(curl -s http://127.0.0.1:8000/healthz)
echo "$HZ" | grep -q '"status": *"ok"' && endpoint_pass "GET /healthz  ok" || endpoint_fail "GET /healthz  unexpected response: $HZ"

# 6c. GET /sweep  →  total_runs = 12, both sweeps
SWEEP=$(curl -s http://127.0.0.1:8000/sweep)
TOTAL=$(echo "$SWEEP" | python3 -c "import sys,json; print(json.load(sys.stdin)['total_runs'])")
[ "$TOTAL" = "12" ] && endpoint_pass "GET /sweep  total_runs=12" || endpoint_fail "GET /sweep  expected 12, got $TOTAL"
echo "$SWEEP" | python3 -c "import sys,json; b=json.load(sys.stdin)['sweep_ids']; assert 'sweep_141' in b and 'sweep_142' in b" 2>/dev/null && \
    endpoint_pass "GET /sweep  both sweep IDs present" || endpoint_fail "GET /sweep  missing sweep id"

# 6d. GET /runs  →  list of 12
RUNS=$(curl -s http://127.0.0.1:8000/runs)
RUN_COUNT_HTTP=$(echo "$RUNS" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
[ "$RUN_COUNT_HTTP" = "12" ] && endpoint_pass "GET /runs  12 runs" || endpoint_fail "GET /runs  expected 12, got $RUN_COUNT_HTTP"

# 6e. GET /runs/{run_id}  →  contract is human_confirmed
REVIEW=$(curl -s http://127.0.0.1:8000/runs/chess_best_move__seed42__b)
STATUS=$(echo "$REVIEW" | python3 -c "import sys,json; print(json.load(sys.stdin)['contract']['status'])" 2>/dev/null || echo "ERROR")
[ "$STATUS" = "human_confirmed" ] && endpoint_pass "GET /runs/{id}  contract=human_confirmed" || endpoint_fail "GET /runs/{id}  expected human_confirmed, got $STATUS"

# 6f. GET /configurations  →  two configs with default_pair
CONFIGS=$(curl -s http://127.0.0.1:8000/configurations)
CONFIG_COUNT=$(echo "$CONFIGS" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['configurations']))" 2>/dev/null || echo "0")
[ "$CONFIG_COUNT" = "2" ] && endpoint_pass "GET /configurations  2 configurations" || endpoint_fail "GET /configurations  expected 2, got $CONFIG_COUNT"

# 6g. GET /comparisons/preview  →  matched comparison
PREVIEW=$(curl -s "http://127.0.0.1:8000/comparisons/preview?baseline=sweep_id%3Dsweep_141&candidate=sweep_id%3Dsweep_142&axis=evaluation_harness")
PAIRS=$(echo "$PREVIEW" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['pairs']))" 2>/dev/null || echo "0")
[ "$PAIRS" = "5" ] && endpoint_pass "GET /comparisons/preview  5 matched tasks" || endpoint_fail "GET /comparisons/preview  expected 5, got $PAIRS"

# 6h. POST /comparisons  →  save a comparison
SAVED=$(curl -s -X POST http://127.0.0.1:8000/comparisons \
    -H "Content-Type: application/json" \
    -d '{"baseline":{"sweep_id":"sweep_141"},"candidate":{"sweep_id":"sweep_142"},"axis":"evaluation_harness","name":"verify test"}')
CID=$(echo "$SAVED" | python3 -c "import sys,json; print(json.load(sys.stdin)['comparison_id'])" 2>/dev/null || echo "FAILED")
[ "$CID" != "FAILED" ] && endpoint_pass "POST /comparisons  saved as $CID" || endpoint_fail "POST /comparisons  could not save"

# 6i. Load the saved comparison back
LOADED=$(curl -s "http://127.0.0.1:8000/comparisons/$CID")
LOADED_PAIRS=$(echo "$LOADED" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['pairs']))" 2>/dev/null || echo "0")
[ "$LOADED_PAIRS" = "5" ] && endpoint_pass "GET /comparisons/{id}  5 matched tasks on reload" || endpoint_fail "GET /comparisons/{id}  mismatch"

# 6j. GET /runs/{run_id}/next
NEXT=$(curl -s http://127.0.0.1:8000/runs/chess_best_move__seed42__b/next)
NEXT_ID=$(echo "$NEXT" | python3 -c "import sys,json; print(json.load(sys.stdin)['next_unhandled'])" 2>/dev/null || echo "null")
[ "$NEXT_ID" != "null" ] && endpoint_pass "GET /runs/{id}/next  returns next run" || endpoint_fail "GET /runs/{id}/next  no next run"

# 6k. GET /metrics  →  derived product measures (empty, but endpoint exists)
METRICS=$(curl -s http://127.0.0.1:8000/metrics)
METRICS_EVENTS=$(echo "$METRICS" | python3 -c "import sys,json; print(json.load(sys.stdin)['events'])" 2>/dev/null || echo "ERROR")
[ "$METRICS_EVENTS" = "0" ] && endpoint_pass "GET /metrics  0 events (fresh store)" || endpoint_fail "GET /metrics  unexpected: $METRICS_EVENTS"

# ------------------------------------------------------------------
echo ""
echo "━━━ Summary ━━━"
echo "  CLI tests:  $PASS passed, $FAIL failed"
echo "  HTTP tests: $ENDPOINT_PASS passed, $ENDPOINT_FAIL failed"
echo ""

TOTAL_PASS=$((PASS + ENDPOINT_PASS))
TOTAL_FAIL=$((FAIL + ENDPOINT_FAIL))

if [ "$TOTAL_FAIL" -eq 0 ]; then
    echo "  ✓ Five-minute path VERIFIED"
    exit 0
else
    echo "  ✗ Five-minute path FAILED  ($TOTAL_FAIL test(s) failed)"
    exit 1
fi
