# Terminal-Bench evaluation experiment

Code for the launch-plan evaluation (see `docs/launch-plan.md`). Harbor runs
the benchmark; this driver standardises how we invoke it, classifies outcomes,
and keeps the run ledger.

## Setup (once)

```bash
uv tool install harbor                 # harness (0.22.0 used)
# .env in repo root must contain the OpenRouter pair:
#   OPENAI_API_KEY="sk-or-v1-..."
#   OPENAI_BASE_URL="https://openrouter.ai/api/v1"
```

Model roles (do not conflate):

- **Agent model** — `stealth/ox-alpha` (OpenRouter free tier), solves the
  Terminal-Bench tasks for both agents. Wired automatically:
  mini-swe-agent via passthrough `OPENAI_API_KEY`+`OPENAI_BASE_URL`
  (setting `MSWEA_API_KEY` breaks this — see driver docstring), terminus-2
  via litellm `OPENROUTER_API_KEY`.
- **Reviewer model** — Kimi via OpenRouter, used later by `agr review`
  (not by anything here).

## Commands

```bash
python experiments/terminal_bench/run_experiment.py status    # ledger summary
python experiments/terminal_bench/run_experiment.py oracle    # env+verifier check (no model)
python experiments/terminal_bench/run_experiment.py --dry-run pilot   # show commands
python experiments/terminal_bench/run_experiment.py pilot     # one attempt per agent (Gate A)
python experiments/terminal_bench/run_experiment.py pilot --agent terminus2
python experiments/terminal_bench/run_experiment.py ingest    # feed jobs into AGR
python experiments/terminal_bench/run_experiment.py import    # adopt ad-hoc job dirs
python experiments/terminal_bench/run_experiment.py sweep     # locked until Phase 2 tasks set
```

## Outcome taxonomy

| outcome | meaning | counts toward the 36? |
|---|---|---|
| `PASS` / `FAIL` | verifier reward 1.0 / 0.0 | yes |
| `INFRA` | docker/environment/harness failure | no — retried (max 2), logged as write-off |
| `RATE_LIMIT` | free-tier throttling killed the run | no — write-off (plan: accepted infra cost) |
| `AUTH_ERROR` | bad/missing key (incl. `Missing credentials` loops in agent logs) | no — fix `.env` |
| `ERROR` / `UNVERIFIED` | anything else / no reward | no — investigated manually |

## Artifacts

```
eval-runs/                    (gitignored)
  <job>/                      harbor job dir (trial dirs, result.json, verifier/)
  <job>.log                   full harbor stdout for the run
  ledger.jsonl                one record per job: outcomes, durations, tokens,
                              429 counts, trajectory presence
```

Oracle jobs are recorded but never ingested (no trajectory by design).
