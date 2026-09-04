# Agent Game Review

**See *why* your agent eval runs passed or failed.** Point Agent Game Review at
a [Terminal-Bench](https://www.tbench.ai/) / [Harbor](https://www.harborframework.com)
sweep (or any agent trajectory) and get a per-run, evidence-linked behavioural
review: the decisive moments, what the agent actually did, and what to fix —
including the failures a green pass-rate hides.

Every review is grounded in a **deterministic core** — it ingests a trajectory,
stores it immutably, derives an event timeline, decomposes the verifier outcome
into atomic checks, builds evidence slices, and runs deterministic detectors,
with **no model calls anywhere**. An optional model reviewer can enrich the
narrative on top, but every structured fact it rests on is recomputed here, so
the interpretation stays bounded by evidence rather than free to hallucinate.

## Quick start (about five minutes)

```bash
pip install .[api]        # or: uv pip install .[api]
agr demo
# open http://127.0.0.1:8000
```

That installs the package, builds a synthetic demo store (12 runs across 5
tasks), and starts the evidence-browser SPA. No credentials, no model calls,
no watermarks. Press `Ctrl+C` to stop.

New here? [`examples/`](./examples/README.md) is a six-run guided tour of exactly
what a review surfaces — a clean pass, a recovered failure, a run that passed the
verifier but still misbehaved, an ignored failure, a partial-credit miss, and a
task whose contract contradicts itself.

## Review your own run

The demo is synthetic. To review a **real** run, ingest it and open the same
browser — still no model calls or credentials for the deterministic review.

**A Harbor / Terminal-Bench 2.0 run** — point `ingest-harbor` at one trial
directory, a `trajectory.json`, or a whole `harbor run` job directory:

```bash
agr ingest-harbor ./my-harbor-run/   # trial dir, trajectory.json, or a job dir
agr runs                             # list runs with their outcomes
agr serve                            # browse at http://127.0.0.1:8000
```

**A pi session** — a pi `.jsonl` transcript ingests the same way:

```bash
agr ingest-pi ./session.jsonl
agr serve
```

Task identity, the verifier outcome, and capabilities come from what the harness
recorded; where an adapter has to infer something it says so with a contract
warning, and a run ingested from a bare verifier stays `PROVISIONAL` until you
confirm its contract with `agr confirm`. Nothing is invented — a run with no
verifier sidecar ingests as `UNVERIFIED`, never a vacuous pass.

## Browse the real pilot corpus (no synthetic data)

This repo ships a **published evaluation corpus** — 25 real Harbor/Terminal-Bench
trials across 6 task families and two agent configurations, with verifier
outputs, logs, and the honest pilot record. Browse it in three commands:

```bash
agr ingest-harbor eval-runs/      # ingests every published job in one command
agr runs                          # 16 logical runs: passes, failures, sibling sets
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

```bash
pip install '.[model-openai]'
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
docs/       Adapter guide, Terminal-Bench walkthrough, ATIF schema.
examples/   Six-run guided tour of what a review surfaces.
eval-runs/  The published Terminal-Bench pilot corpus (real runs, immutable
            source record — see eval-runs/README.md).
experiments/  The pilot gallery and gate evaluation record.
tests/      Acceptance tests for the deterministic core.
archive/    Synthetic fixtures and gold labels used by the demo and tests.
```
