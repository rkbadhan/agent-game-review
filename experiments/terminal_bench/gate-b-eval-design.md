# Gate B head-to-head — pre-registered evaluation design

> **This file is written BEFORE any comparison results are produced.** The
> launch plan (Phase 5) requires: *"Pre-write the decision rule for 'meaningful
> improvement' before seeing results."* Nothing below may be edited after runs
> start except by appending a dated addendum that says what changed and why.
> Status: **REGISTERED 2026-08-24, no comparison executed yet.**

## The question

Does AGR provide a meaningful improvement over Harbor's viewer (including its
AI failure summaries) on real failed Terminal-Bench runs?

## Scope — reviewable failed runs in the store

All failed runs with trajectories (`agr runs`, FAILED 0/1). That is **6 runs /
5 tasks** today — fewer than the plan's 10–15 target. We run with all 6 now and
record the shortfall honestly; GO on n=6 is weaker than GO on n≥10 and must be
labelled as such.

| # | run | task | failure shape |
|---|---|---|---|
| 1 | `harbor__terminal-bench/raman-fitting__raman-fittin` | raman-fitting | timeout after pivot into window-scan forensics |
| 2 | `harbor__terminal-bench/raman-fitting__19c58efb-114` | raman-fitting | timeout after axis-spacing investigation (~h27→) |
| 3 | `harbor__terminal-bench/make-mips-interpreter__5426bb92-ddf` | make-mips-interpreter | timeout @1800s mid-build, vm.js never written |
| 4 | `harbor__terminal-bench/make-mips-interpreter__make-mips-in` | make-mips-interpreter | same task, mini-swe-agent, ~138 steps, timeout |
| 5 | `harbor__terminal-bench/nginx-request-logging__4e941d41-a66` | nginx-request-logging | submitted, verifier reward 0 |
| 6 | `harbor__terminal-bench/nginx-request-logging__nginx-reques` (crashed capture `capture_4ed0683d92b3`) | nginx-request-logging | agent crashed before acting |
| 7 | `harbor__terminal-bench/polyglot-c-py__6c3b4b8b-991` | polyglot-c-py | ended without submission signal |

## Ground truth — gold annotations

Gold decisive moments are drafted from the **raw trajectories and task
metadata only**, never from either tool's output (Harbor's narrative exists for
run 1; it is *not* used as gold — it is one of the two contestants). Drafts in
`gold-drafts/` are marked `status: draft`; each needs human adjudication
(checklist inside each file) before scores count. Unadjudicated scores are
reported as provisional.

## Contestants — same model both sides

- **AGR:** `agr review <run>` — reviewer kimi-k3 via Fireworks ($0).
- **Harbor:** `harbor analyze` pointed at the same kimi-k3/Fireworks endpoint
  (`OPENAI_BASE_URL=https://api.fireworks.ai/inference/v1`,
  `PYTHONUTF8=1 PYTHONIOENCODING=utf-8`; do not shell-timeout it).

If Harbor cannot complete a run through Fireworks, record it as an
infrastructure result for that run (not a content loss) and substitute
stealth/ox-alpha on **both** sides for that run rather than mixing models.

## Scoring sheet (per run, per contestant)

The brief's 8 questions, each scored ✅ / ⚠ / ❌ with one-line justification:

1. What was the agent attempting?
2. Which decisions/actions are worth inspecting?
3. Where did strategy change (or drift)?
4. Did the agent recover from an earlier failure?
5. Successful-run divergence (where sibling captures exist)
6. Agent vs harness/tool/env/verifier attribution
7. Evidence inspectability (event/step links, not prose ranges)
8. Appropriate abstention when evidence is insufficient

Plus recorded measures:

- **decisive-moment recall@3 / precision@3** against the adjudicated gold
  (via `agr.reviewer_eval.evaluate_run` — step-overlap matching);
- time-to-understand (minutes, reader stopwatch, single reader noted);
- unsupported/misleading claim count;
- attribution overclaim rate (harness metric).

## Decision rule (fixed in advance)

For each run, AGR "wins" the run iff it matches or beats Harbor on
decisive-moment recall AND commits no attribution overclaim or fabricated
evidence where Harbor also committed none. Ties count to Harbor (conservative).

- **GO** — AGR wins ≥ 60% of runs AND wins question 7 (evidence linking) on a
  majority AND has zero honesty failures (fabricated anchors/causal claims).
- **HOLD** — split verdict, or GO margins achieved only with unadjudicated gold.
- **REVISE / NO-GO** — Harbor wins a majority, or AGR shows any fabricated-
  evidence failure mode.

A GO on n=6 upgrades to a confirmed GO only after backfilling to ≥10 failed
runs under this same registered rule. Any change to this rule after results
are seen converts the outcome to HOLD regardless of numbers.
