#!/usr/bin/env bash
# Gate B head-to-head — batch Harbor analyze, kimi-k3 via Fireworks (same model
# as AGR's reviewer; per gate-b-eval-design.md).
#
# Working invocation notes (learned 2026-08-25):
#   - agent must be terminus-2 (default claude-code needs Anthropic login and
#     ignores OPENAI_* entirely)
#   - model needs the openai/ prefix for litellm pass-through:
#       openai/accounts/fireworks/models/kimi-k3
#   - terminus-2 calls the LLM from the HOST harbor process, so OPENAI_API_KEY /
#     OPENAI_BASE_URL must be exported in THIS shell (--agent-env is useless here)
set -u
cd "$(dirname "$0")/../.."
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
set -a; source .env 2>/dev/null; set +a
export OPENAI_BASE_URL="https://api.fireworks.ai/inference/v1"

TRIALS=(
  "eval-runs/pilot-terminus2/make-mips-interpreter__mU7gNZv"
  "eval-runs/pilot-miniswe/make-mips-interpreter__Fj4RpHV"
  "eval-runs/swpB2-nginx-request-logging-a2/nginx-request-logging__SLfAhLz"
  "eval-runs/swpB-nginx-request-logging-a2/nginx-request-logging__h6dA2go"
  "eval-runs/swpC2-polyglot-c-py-a1/polyglot-c-py__CGnNC78"
  "eval-runs/swpA2-raman-fitting-a1/raman-fitting__LMDAdti"   # re-run: earlier attempt predates fix
)
LOGDIR="experiments/terminal_bench/analyze-batch-logs"
mkdir -p "$LOGDIR"

i=0
for t in "${TRIALS[@]}"; do
  i=$((i+1))
  name=$(basename "$t")
  echo "[$(date +%H:%M:%S)] START $name" | tee -a "$LOGDIR/batch.log"
  harbor analyze "$t" \
    --agent terminus-2 \
    --model "openai/accounts/fireworks/models/kimi-k3" \
    > "$LOGDIR/$name.log" 2>&1
  rc=$?
  echo "[$(date +%H:%M:%S)] DONE  $name (exit $rc)" | tee -a "$LOGDIR/batch.log"
done
echo "[$(date +%H:%M:%S)] BATCH COMPLETE" | tee -a "$LOGDIR/batch.log"
