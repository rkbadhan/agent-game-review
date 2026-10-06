# Attribution benchmark — Who&When (EV-1)

**Status:** adapter + scorer implemented (`python -m legacy benchmark who-and-when`).
**Benchmark:** [Who&When](https://github.com/ag2ai/Agents_Failure_Attribution)
(ICML 2025), MIT. Dataset also on Hugging Face as `Kevin355/Who_and_When`.

This is the first EV-1 deliverable from
[`legacy/docs/evaluation-strategy.md`](evaluation-strategy.md): reuse an existing,
externally-labelled **failure-attribution** benchmark instead of inventing new
labels. It answers one of the three questions in that strategy — *did AGR find
the important error?* — and nothing else.

## What Who&When is

184 annotated **failure** tasks from two multi-agent systems:

| Split | System | Records |
| --- | --- | --- |
| Algorithm-generated | AutoGen CaptainAgent | 126 |
| Hand-crafted | Microsoft Magnetic-One | 58 |

Tasks are drawn from GAIA / AssistantBench. Each record labels the
failure-responsible **agent** (`mistake_agent`), the decisive error **step**
(`mistake_step`, a 0-based index into `history`), and a written
**explanation** (`mistake_reason`). The benchmark's own protocol scores agent
accuracy, step accuracy, and both.

## Why this benchmark first

The strategy proposed TRAIL's SWE-Bench split first because AGR already has an
OTel adapter. TRAIL is a **gated** Hugging Face dataset, however, and the token
available in this environment does not grant access, so the adapter could not
be run. Who&When is fully public and carries the most direct label AGR needs —
an explicit decisive step — so it was built first and TRAIL drops in behind the
same interface once access is arranged.

## How AGR maps onto it

Everything that makes the comparison fair is declared in
`WHO_WHEN_MANIFEST` before any run (`legacy/benchmark_whowhen.py`):

- **Single-prediction rule.** Who&When expects one decisive step; AGR proposes
  up to five moments. AGR's submission is its **highest-ranked negative
  selected moment**. Its anchor step is the step, its attributed agent is the
  agent. AGR's other moments never enter the score.
- **Trace → ATIF.** Every `history` entry becomes exactly one ATIF step, in
  order, so `mistake_step = k` maps to source step `s{k+1}` with no ambiguity.
  A terminal-executor turn becomes a `tool_result` (whose literal
  `exitcode: N` text is read into `exit_code`); a `human` turn is
  `task_received`; everything else is a `model_output`. An
  `"Orchestrator (-> X)"` delegation is attributed to the speaking
  **Orchestrator** — the decision-maker, not the delegate.
- **Label → gold.** `mistake_step` becomes a `GoldMoment` anchored on that
  source step (critical, negative, `decision`); `mistake_agent` stays in
  `case.meta`. Reference labels never enter the ATIF document, so they cannot
  reach reviewer input.
- **Agent resolution.** AGR's attributed agent is the actor of the moment's
  anchor when that anchor is an agent turn; otherwise attribution walks back to
  the nearest preceding agent turn (a tool result is attributed to its caller).
- **No verifier is invented.** Who&When records none, so the run ingests as
  "Task success unverified" (`capture_completeness: partial`) and the dataset's
  `is_correct` flag stays a meta outcome label, never a verifier signal.

Scoring: step accuracy is exact source-step overlap; agent accuracy compares
AGR's attributed agent to `mistake_agent`; both is their conjunction. Errors
and abstentions stay in the denominator with zero credit.

## Run it

```bash
# clone the benchmark next to the repo (the data is not vendored here)
git clone --depth 1 https://github.com/ag2ai/Agents_Failure_Attribution

# deterministic baseline (no credentials, no model calls)
python -m python -m legacy benchmark who-and-when \
    --data "./Agents_Failure_Attribution/Who&When/Algorithm-Generated"

# the model reviewer (needs a 'model-*' extra + a key)
python -m python -m legacy benchmark who-and-when \
    --data "./Agents_Failure_Attribution/Who&When/Hand-Crafted" \
    --provider openai --out report.json
```

The output always carries the manifest (version, provenance, rule, limits) and
per-case scores. A `--limit N` gives a quick check.

## Limits (carry these with any number)

These are part of the manifest, not a footnote:

- **Failure-only dataset.** It cannot demonstrate correct abstention on clean
  runs, and missing annotations are never evidence that no valid moment exists.
- **Different domain.** Multi-agent web/QA tasks (GAIA, AssistantBench), not
  coding traces; the decisive step may be a coordination error with no local
  tool failure.
- **No verifier results.** AGR's verifier-driven detectors cannot fire; only
  observed execution findings are available. On the deterministic baseline this
  is a large effect, which is itself a finding.
- **The responsible agent is sometimes not the speaker** at the decisive step,
  so agent accuracy is a harder, partly independent label than step accuracy.

## What the first run showed

On the algorithm-generated split (126 cases), the **deterministic** baseline
abstained on most cases (step accuracy 0, agent accuracy ≈0.11): its detectors
flag the step where a tool *failed*, while the human label often marks the
agent's *reasoning* step one turn earlier. Who&When's decisive errors are
mostly coordination/reasoning errors, which is exactly the model reviewer's
job (GR-1) — so a model-scored run is the meaningful next measurement, and the
deterministic number is the honest floor.

## Not yet done

- TRAIL adapter + scorer (blocked on dataset access).
- A frozen model-scored run with thresholds set before scoring (EV-1 step 4).
- The supplementary successful-run / recovery / no-moment set (EV-1 step 5).
