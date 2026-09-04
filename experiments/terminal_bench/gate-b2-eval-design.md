# Gate B2 — Game-review usefulness (REGISTERED, not yet run)

> Registered 2026-08-25 BEFORE execution, per launch-plan Phase 5 discipline.
> Do not edit the decision rule after results are seen.

## Question

On a mixture of failed AND successful runs, does AGR produce the 3–5 moments a
human considers useful — and is it non-inferior to Harbor analyze on that,
while materially better on evidence and attribution?

## Why a separate gate

Gate B1 compared failure summaries only — Harbor's home turf. The original
AGR design equally cares about: good recoveries inside successful runs, strong
diagnostic moves, unnecessary risk in passing runs, missed opportunities visible
through successful siblings, and repeated behavioral signatures. B1 cannot test
those; B2 must.

## Scope

- ≥6 runs: at least 2 passing runs with interesting internals (recovery, risk),
  the 7 failed B1 runs may be reused, plus ≥2 successful-with-sibling pairs.
- Same model both sides (kimi-k3 via Fireworks) — recipe in
  `run_analyze_batch.sh` notes.
- Gold: human-drafted from raw trajectories, blind to both contestants'
  outputs, using the brief's moment types (mistake / strong move / recovery /
  repeated dead end / missed opportunity / premature completion / non-agent
  failure / insufficient evidence).

## Scoring

Per run per contestant:
1. useful-moment recall (gold moments surfaced within top 5 cards)
2. false-positive rate (cards a human rejects)
3. evidence validity (quotes/anchors recompute)
4. attribution ceiling correctness vs gold locus
5. better-action usefulness (human rates 0–2)

## Decision rule (fixed now)

- **B2 PASS:** AGR non-inferior to Harbor on useful-moment recall (within one
  gold moment on ≥70% of runs) AND strictly better on evidence validity +
  attribution correctness in aggregate.
- **FAIL:** AGR inferior on recall, or manufactures cards on clean passes.
- Provisional until gold adjudicated and n≥10 runs total across ≥3 task
  families.
