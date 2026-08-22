# Terminal-Bench 2.0 → pi → an ingestable AGR run

This is the end-to-end recipe for reviewing a coding agent (here **pi**, on a
small model like **gpt-oss**) that ran the **Terminal-Bench 2.0** benchmark,
using AGR to review the *behaviour* afterwards.

## The mental model first

AGR does not *run* benchmarks — it **reviews the runs**. Terminal-Bench 2.0 is
executed by [**Harbor**](https://www.harborframework.com), its official harness.
Harbor runs your agent against each task, decides pass/fail, and writes a
trajectory. AGR is the stage *after*: it ingests that trajectory plus the
pass/fail result and reviews what the agent did.

There are **two separate model roles** — don't conflate them:

| role | what it is | where it's set |
|------|------------|----------------|
| **agent model** | the model pi uses to *solve* each task (e.g. `gpt-oss`) | Harbor's `-m` flag / pi's config |
| **reviewer model** | the model AGR uses to *review* the trajectory | AGR's `agr review --provider ...` |

`gpt-oss` can play either or both. Both just need an OpenAI-compatible endpoint
(vLLM / Ollama / LM Studio / a gateway).

```
 Harbor (Terminal-Bench 2.0)                       AGR (this repo)
 ┌───────────────────────────┐                    ┌────────────────────────┐
 │ harbor run                │  trial dir/        │ ingest-from --adapter  │
 │   -d terminal-bench-2     │  ├ result.json ───▶│   harbor               │
 │   --agent-import-path pi  │  └ agent/          │   (reward → verifier)  │
 │   -m gpt-oss              │     trajectory.json│                        │
 │        (agent model)      │ ─────────────────▶ │ show / review / serve  │
 └───────────────────────────┘   ATIF-v1.7        └────────────────────────┘
```

## Step 1 — make pi runnable as a Harbor agent

Harbor runs a *custom* agent via `--agent-import-path "module.path:ClassName"`
(see Harbor's "Writing Custom Agents"). This wrapper lives in **pi's** repo (or
any importable module on `PYTHONPATH`), not in AGR — AGR only consumes the
output. A minimal external-agent wrapper looks like:

```python
# pi_harbor_agent.py  (lives with pi, importable on PYTHONPATH)
from harbor.agents.base import BaseAgent

class PiAgent(BaseAgent):
    @staticmethod
    def name() -> str:
        return "pi-coding-agent"

    async def setup(self, environment) -> None:
        # install / configure pi inside the trial container if needed
        ...

    async def run(self, instruction, environment, context) -> None:
        # drive pi against `instruction` in `environment`, using the model
        # Harbor passes through (gpt-oss). Have pi emit its session so the
        # trajectory is captured; Harbor also records its own ATIF trajectory.
        ...
```

> Harbor already emits an **ATIF-v1.7** `trajectory.json` for whatever agent
> runs, so the AGR side (Step 3) works regardless of pi internals. If you would
> rather review pi's *own* session log, use the `pi` adapter on that file
> instead — see [`adapters.md`](./adapters.md).

## Step 2 — run the benchmark on a small model

```bash
# built-in agent, to sanity-check your Harbor setup first:
harbor run -d terminal-bench/terminal-bench-2 -a claude-code -m anthropic/claude-haiku-4-5 -k 5

# your pi wrapper on a small open model as the agent:
harbor run \
  -d terminal-bench/terminal-bench-2 \
  --agent-import-path "pi_harbor_agent:PiAgent" \
  -m gpt-oss \
  -k 5
```

Harbor writes one directory per trial:

```
<job_dir>/<trial>/
  result.json            reward (1.0 pass / 0.0 fail), exception, phase timings
  agent/trajectory.json  the ATIF-v1.7 trajectory
  agent/recording.cast   asciinema recording (AGR does not read this)
  verifier/              verifier outputs
```

## Step 3 — ingest each trial into AGR

The `harbor` adapter reads a trial directory and produces an ingestable run. It
**synthesises the verifier from `result.json`'s reward**, so you don't hand-write
a sidecar — a passing trial ingests as PASSED, a failing one as FAILED, an
errored one as ERROR, and a trial with no reward honestly as UNVERIFIED:

```bash
python3 -m agr ingest-from --adapter harbor \
    <job_dir>/<trial> --task-id <the-terminal-bench-task-id>
```

Ingest a whole Harbor job (every trial) in one loop:

```bash
for trial in <job_dir>/*/; do
  [ -f "$trial/result.json" ] || continue
  task=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['task_id'])" "$trial/result.json")
  python3 -m agr ingest-from --adapter harbor "$trial" --task-id "$task"
done
python3 -m agr runs
```

Pass `--verifier sidecar.json` to override the reward-derived verifier with your
own atomic checks; pass `--sweep-id` / `--configuration-id` to group runs for a
matched comparison (`agr compare-versions`).

## Step 4 — review

```bash
# deterministic review (no model, no key):
python3 -m agr show <run_id>

# Stage F model review — reviewer model can also be gpt-oss:
#   OPENAI_BASE_URL=http://localhost:11434/v1   (in .env)
#   AGR_REVIEW_MODEL=gpt-oss
pip install -e '.[model-openai]'
python3 -m agr review <run_id> --provider openai

# browse the evidence in the SPA:
pip install -e '.[api]'
python3 -m agr serve
```

## τ-bench and other benchmarks

Any benchmark that runs through Harbor produces the same trial tree, so the
`harbor` adapter works unchanged — you only change the `-d` dataset. A benchmark
with a *different* native format needs its own small adapter (one file, per
[`adapters.md`](./adapters.md)); the reward → verifier mapping shown here is the
model to copy.
