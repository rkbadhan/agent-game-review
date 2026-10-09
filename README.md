# Agent Game Review

**See *why* your agent eval runs passed or failed.** Capture a run from the
harness you already use — Harbor / [Terminal-Bench](https://www.tbench.ai/),
τ³-bench, Claude Code, pi, OpenTelemetry, Langfuse, LangSmith, or your own
ATIF document — and get a per-run, evidence-linked behavioural review: the
decisive moments, what the agent actually did, and what to fix, including the
failures a green pass-rate hides. Put every attempt of one task side by side
to see where the failing attempts split from the passing ones.

Every review is grounded in a **deterministic core**: it ingests a trajectory,
stores it immutably, derives an event timeline, decomposes the verifier outcome
into atomic checks, builds evidence slices, and runs deterministic detectors,
with **no model calls anywhere**. An optional model reviewer can enrich the
narrative on top, but every structured fact it rests on is recomputed here, so
the interpretation stays bounded by evidence rather than free to hallucinate.

## Quick start (about one minute, no clone)

Requires [uv](https://docs.astral.sh/uv/). Install the CLI as an isolated tool
and open the demo:

```bash
uv tool install 'agent-game-review[api] @ git+https://github.com/rkbadhan/agent-game-review'
agr demo
# open the printed link (http://127.0.0.1:8000/...)
```

The demo is built from **real runs**: 15 Terminal-Bench 2.0 trials across 7
tasks and 16 τ³-bench retail trials across 8 tasks (one passing and one failing
attempt each), with 23 pre-computed model reviews baked in. A small synthetic
slice is added only so the version-comparison screen has two configurations to
compare; those runs are labelled `synthetic_demo`. No credentials, no model
calls. Press `Ctrl+C` to stop.

On pip instead of uv? Clone the repo, then `pip install '.[api]'` (quote the
extra — zsh globs unquoted brackets) and run the same commands.

New here? [`examples/`](./examples/README.md) is a six-run guided tour of exactly
what a review surfaces — a clean pass, a recovered failure, a run that passed the
verifier but still misbehaved, an ignored failure, a partial-credit miss, and a
task whose contract contradicts itself.

## Compare the attempts of one task

`agr task <task_id>` lists every attempt of a task with its verifier
breakdown. On the demo's τ³ retail task 58:

```console
$ agr task sierra-research/tau3-bench__tau3-retail-58
sierra-research/tau3-bench__tau3-retail-58  ·  2 attempts · 1 passed · 1 failed

  [PASSED 1/1]  568f9ef5-e86
        reward basis: DB, NL_ASSERTION
        database: match
        actions: 6 checked, 0 failed
  [FAILED 0/1]  77f01636-574
        reward basis: DB, NL_ASSERTION
        database: MISMATCH
        actions: 6 checked, 1 failed
          - exchange_delivered_order_items (write)
```

The failing attempt left out a required write; the browser shows the turn
where it diverged.

## Review your own run

Capture is the first step, and every source lands in the same review. The
deterministic review still needs no model and no credentials.

From the browser: run `agr serve`, create a local project, choose **Add runs**,
pick your source and files, check the evidence preview, and import. Folder and
ZIP uploads work for Harbor and session sources.

From the command line:

| Source | Command |
|--------|---------|
| Harbor / Terminal-Bench 2.0 (trial dir, `trajectory.json`, or a whole job dir) | `agr ingest-harbor ./my-harbor-run/` |
| τ³-bench run through Harbor | `agr ingest-harbor ./jobs/<job>/` |
| Claude Code session `.jsonl`, or a `claude -p` stream-json / json export | `agr ingest-from --adapter claude ./session.jsonl` |
| pi session `.jsonl` | `agr ingest-pi ./session.jsonl` |
| OpenTelemetry (OTLP JSON) — beta | `agr ingest-from --adapter otel ./trace.json` |
| Langfuse trace export — beta | `agr ingest-from --adapter langfuse ./trace.json` |
| Langfuse, live by trace id — beta | `agr ingest-langfuse-api --trace-id <id>` |
| LangSmith run export — beta | `agr ingest-from --adapter langsmith ./run.json` |
| Your own ATIF document | `agr ingest ./run.atif.json` |

Then `agr runs` lists every run with its outcome, `agr task <task_id>` compares
attempts, and `agr serve` opens the browser at http://127.0.0.1:8000.

**τ³-bench.** The adapter reads `verifier/result.json` and keeps the full reward
breakdown — database check, action checks, NL assertions, communicate checks —
behind **one** pass/fail check, so a single failed task is never counted many
times over. A broken verifier (a missing or invalid runtime log, an evaluator
crash) is recorded as an operational error, not as an agent failure, and a
verifier status that disagrees with its reward gets a warning. The
conversation is rebuilt from `tau3_runtime_state.json`; simulated-customer
turns stay marked as the user's, never the agent's.

**Sessions without a verifier.** Claude Code, pi and trace exports carry no
task result. Without a `--verifier sidecar.json` the run ingests as
`UNVERIFIED`, never a borrowed pass; the behavioural review does not need a
task-level pass/fail.

Task identity, the verifier outcome, and capabilities come from what the harness
recorded; where an adapter has to infer something it says so with a contract
warning, and a run ingested from a bare verifier stays `PROVISIONAL` until you
confirm its contract with `agr confirm`. Nothing is invented.

## Limits and next

What this version does not do yet, stated plainly:

- **OpenTelemetry, Langfuse and LangSmith are beta.** Their field mappings are
  tested against small, hand-instrumented sample traces, not against a large
  body of real production exports. Expect gaps; the adapter warns where it
  cannot map something.
- **Subagents are not reviewed.** Claude Code Task-subagent turns are dropped
  with a warning rather than merged into the main agent's timeline, because the
  timeline has no parent-to-subagent link yet.
- **No export.** Reviews live in the local store (`.agr-store/`); `agr show
  <run_id>` prints one as text, but there is no report or JSON export command
  yet.
- **Images are placeholders.** A screenshot in a Claude Code session is
  recorded as `[image: image/png]` so the turn is visible, but the image itself
  is not kept as evidence.

## Browse the real pilot corpus (no synthetic data)

This repo ships a **published evaluation corpus** — 32 discovered real
Harbor/Terminal-Bench trials (22 eligible and ingested, 10 excluded with a
reason) across 7 task families and two agent configurations, with verifier
outputs, logs, and the honest pilot record. With the tool installed (above),
clone the repo and browse it:

```bash
git clone https://github.com/rkbadhan/agent-game-review.git
cd agent-game-review
agr ingest-harbor eval-runs/      # ingests every published job in one command
agr runs                          # 22 logical runs: passes, failures, sibling sets
agr serve                         # http://127.0.0.1:8000 — click any moment's
                                  # anchor to open the raw trajectory event
```

The annotated pilot review lives in
[`experiments/terminal_bench/gallery.md`](./experiments/terminal_bench/gallery.md)
— labeled a **pilot gallery and failure audit**, including the false-positive
audit that caught our own reviewer fabricating a moment (and the abstention
discipline that now prevents it).

## Use your own model

The deterministic review needs no model. To add the AI layer — taxonomy
verdicts, ranked root causes, better actions — bring **any** OpenAI-compatible
or Anthropic-compatible model: OpenRouter, Together, or a fully local Ollama/
vLLM server.

> **Windows note:** if you installed with pip (not uv tool), stop any running
> `agr serve` before reinstalling extras — pip cannot replace the running
> `agr.exe` and the install can corrupt. uv tool users: just re-run
> `uv tool install --force`.

```bash
uv tool install --force 'agent-game-review[api,model-openai] @ git+https://github.com/rkbadhan/agent-game-review'
# or, from a clone: pip install '.[model-openai]'
agr config --provider openai --model moonshotai/kimi-k3 \
    --base-url https://openrouter.ai/api/v1   # or http://localhost:11434/v1
agr config --test          # one real call: proves key + endpoint + model id
agr review --all           # enrich every run in the store
```

The model never gets the raw trace — a typed, redacted packet — and every fact
it claims is recomputed against the trajectory (Stage G), every attribution
capped, every card selected by the deterministic envelope. Its moments are
visibly labeled model-generated.

## Documentation

- [`docs/terminal-bench.md`](./docs/terminal-bench.md) — a full walkthrough:
  running a model through Harbor and reviewing the result.
- [`docs/adapters.md`](./docs/adapters.md) — the source formats and how to add
  your own adapter.
- [`docs/span-adapters.md`](./docs/span-adapters.md) — the OpenTelemetry,
  Langfuse and LangSmith mappings.
- [`docs/tau3-retail-run-2026-10-07.md`](./docs/tau3-retail-run-2026-10-07.md)
  and [`docs/tau3-retail-test-split-2026-10-08.md`](./docs/tau3-retail-test-split-2026-10-08.md)
  — a full τ³ retail run (train and held-out test split) reviewed end to end.
- [`docs/atif-schema.json`](./docs/atif-schema.json) — the ATIF-shaped ingestion
  contract.

## Install extras

The deterministic core has **no third-party runtime dependencies**. Optional
extras add capabilities:

| Extra | Adds |
|-------|------|
| `.[api]` | FastAPI + uvicorn HTTP transport (needed for `agr serve` / `agr demo`) |
| `.[test]` | pytest + httpx for the test suite |
| `.[model-anthropic]` / `.[model-openai]` | a provider SDK for the optional model reviewer |

## Development

The repo is a [uv](https://docs.astral.sh/uv/) project with a committed
`uv.lock`, so contributors get a reproducible environment in one command:

```bash
uv sync                 # create .venv from the lockfile (dev group: pytest + api deps)
uv run pytest           # run the test suite
uv run agr demo         # try the demo from the synced env

uv sync --all-extras    # add the optional model-reviewer SDKs when you need them
```

Prefer plain pip? That works too — the project is a standard PEP 621 package:

```bash
pip install .[api,test]
python -m pytest
```

## Project layout

```
agr/        The package — ingestion, deterministic derivations, detectors,
            the reviewer envelope, the read layer, and the evidence-browser SPA.
docs/       Adapter guides, Terminal-Bench walkthrough, τ³ run reports, ATIF schema.
examples/   Six-run guided tour of what a review surfaces.
eval-runs/  The published Terminal-Bench pilot corpus (real runs, immutable
            source record — see eval-runs/README.md).
experiments/  The pilot gallery and gate evaluation record.
tests/      Acceptance tests for the deterministic core.
archive/    Synthetic fixtures and gold labels used by the tests and the demo's
            comparison slice.
```
