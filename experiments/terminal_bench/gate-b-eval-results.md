# Gate B head-to-head — PROVISIONAL results

> **STATUS: PROVISIONAL.** Scored 2026-08-25 against `gold-drafts/` which have
> NOT yet been human-adjudicated. Per `gate-b-eval-design.md`, unadjudicated
> scores can never produce official GO. Scoring performed by the AI coding
> assistant (not the independent second reviewer the launch plan asks for).
> Both contestants used kimi-k3 via Fireworks ($0) — same-model requirement met.

## Per-run scoring

Recall = did the contestant surface the gold decisive pattern?
Specificity = how close to the true root cause, beyond the generic pattern?

| # | run | gold decisive pattern | Harbor | AGR | verdict |
|---|---|---|---|---|---|
| 1 | raman `FpPoN5F` | stuck over-analyzing axis calibration, never fit/wrote results | ✅ names it exactly | ✅ "prolonged investigation without producing deliverable", event-anchored | tie |
| 2 | raman `LMDAdti` | budget spent on format/axis exploration, no fits, timeout | ✅ | ✅ same finding + env root-cause note (numpy/scipy absent) | tie |
| 3 | make-mips `mU7gNZv` | recon-only, never wrote vm.js | ✅ "extensive reconnaissance only" | ✅ strategy_drift + time-boxing action | tie |
| 4 | make-mips `Fj4RpHV` | near-complete VM, instability+OOM near finish, no frame.bmp | ✅ full arc incl. steps 33–35, OOM at 49 | ✅ "over-refinement past a working state" + positive strength card | tie |
| 5 | nginx `SLfAhLz` | submitted while failing; missed requirement | ✅✅ pinpoints shell-quoting bug that ate quotes around `$http_user_agent`; single failing test named | ⚠ correct generic "premature completion w/o reverification"; misses quoting root cause | **Harbor** |
| 6 | nginx crash `h6dA2go` | task_never_attempted (protocol failure) | ✅ | ✅ same finding, grounded | tie |
| 7 | polyglot `CGnNC78` | working polyglot then over-hardening; bignum rewrite broke C side; died before verification | ✅✅ names the rotation bug (`a=next` paired with `a_length=b_length`) | ⚠ catches over-hardening pattern; misses the specific bug | **Harbor** |

## Structural dimensions (not part of win count)

| dimension | Harbor | AGR |
|---|---|---|
| evidence linking | prose step-ranges only | every claim event-anchored + machine-recomputed facts |
| attribution discipline | confident prose, no ceilings | ceiling=hypothesized throughout, observational language enforced |
| abstention | rubric always pass/fail | honest "no candidate" when unsupported |
| narrative fluency | strong | minimal |

## Honesty checks

- Fabricated evidence: **none detected** on either side (all AGR quotes recompute; Harbor summaries consistent with trajectories).
- Attribution overclaim rate: 0 for AGR (ceiling-gated); Harbor prose makes causal-sounding claims ("root cause was a shell-quoting bug") but they happen to be well-supported here.

## Verdict under the frozen decision rule

- Ties counted to Harbor (conservative): **AGR 0 / Harbor 7.**
- GO required AGR ≥60% of runs → **not met.**
- **GATE B1 — Semantic discovery: PROVISIONAL HOLD.** Harbor identified the
  decisive pattern on 7/7 trajectories; AGR identified the broad pattern on
  ~5/7 and was less specific on several runs.

**Accurate statement of the result:** AGR approached broad-pattern recall while
adding validated evidence and calibrated attribution, but Harbor remained
better at concrete diagnosis. "Over-hardening" is less useful than "the bignum
rotation changed `a` without preserving the matching length." Evidence
discipline must not make AGR vague.

## What would change the verdict

1. Human adjudication of gold could shift runs 1–4 if AGR's extra cards
   (positive strength, env root cause) are judged *decisive additions* — then
   those ties become AGR wins (5/7 = 71% ≥ 60% → provisional GO, still
   requiring backfill to n≥10). This is precisely why adjudication must happen
   before any official claim.
2. Backfilling to ≥10 failed runs under the same registered rule.
3. Prompt/packet work targeting root-cause specificity (the gap runs 5 & 7
   exposed: naming the concrete bug, not just the behavioral pattern).

## Scope limit of this experiment

This comparison tested ONE prerequisite: semantic finding quality on failed
runs. It did NOT test the full Agent Game Review product. Per the spec's
original concept (§1–§2, §4), the following remain **implemented but not
evaluated**: sweep triage at scale, matched baseline/candidate comparison,
task-ability signatures, lessons/experiment pipeline, feedback-correction-
disposition workflow, UI evidence reachability, successful-run review (strong
moves, recoveries, risky passes), and injection safety in practice.

Registered follow-ups (before any launch claim):
- **Gate B2 — Game-review usefulness:** mixed pass/fail runs; does each system
  produce the 3–5 moments a human finds useful? AGR non-inferior on useful-
  moment recall + materially better on evidence/attribution. Registered in
  `gate-b2-eval-design.md`.
- **Gate C — Sweep workflow:** realistic 20–50-run sweep; triage, repeated
  patterns, agent-vs-harness separation, matched attempts, dispositions,
  next-experiment quality. Registered in `gate-c-eval-design.md`.

## Caveats

- Scoring by the AI assistant; plan calls for an independent second reviewer.
- Time-to-understand not measured (needs a human reader stopwatch session).
- n=7 < planned 10–15.

---

## ADDENDUM (2026-08-25, after results — superseded by scope-limit section)

~~On re-reading the spec, the framing above mis-weights the comparison...~~
**Correction trail note:** this addendum argued AGR's differentiation on trust
and workflow axes. That instinct was directionally right (spec §1 forbids
trace-summarizer-as-analysis), but it overclaimed "content parity reached" —
it was not: Harbor identified the decisive pattern on 7/7 vs AGR ~5/7, with
higher concrete-mechanism specificity. Kept above for provenance; the
authoritative statements are the Gate B1 verdict and scope-limit sections.
