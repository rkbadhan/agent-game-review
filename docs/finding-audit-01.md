# Finding Audit 01 — Recovery Episodes & Execution Quality

Status: P2 audit (spec-amendment-01 §B / direction report Priority 2)
Date: 2026-09-14
Method: static review of the deriving code plus a re-read of the bundled demo
store (`.agr-demo`: 12 runs). The historical sample figures are taken from the
9 September audit cited by the direction report; they are **not** re-derived
here and are labelled as historical.

---

## 1. Finding types and what each surfaces today

| Finding type | Where surfaced | Observation | Evidence pointers | Resource measurement | Limits today |
|---|---|---|---|---|---|
| **Recovery episode** | Patterns representative episodes; run Key moments | `classification` | `failure_event_id`, `resolution_event_id`, `evidence_event_ids` | `episode_window_tokens`, `usage_completeness`, `usage_records` | **was implicit** → now an explicit `limits[]` (§4) |
| **Moment / candidate** | Run Overview + Key moments | `rendered_statement` | `anchor_event_ids`, `validated_facts` | none | `attribution_ceiling`, `evidence_status` |
| **Execution quality** | Run Overview; Patterns matrix | dimension + `violations` | `anchor_event_ids` | `peak_input_tokens` / `max_generation_ms` | capability-gated "not evaluated" |
| **Contract / verifier audit** | Checks chapter | `assessment` | `mapped_checks`, `evidence_event_ids` | none | `evidence_status` |

The gap P2 closes: limits existed only as scattered flags
(`usage_completeness`, `attribution_ceiling`), never as a stated, per-finding
list a reader can read next to the claim.

## 2. Evidence rules (current)

**Recovery episodes** (`agr/recovery.py`)
- A qualifying failure must precede a **linked** success; the link is
  operation-scoped (tool + executable + complete command tail). A different or
  narrowed command yields "later command succeeded", never "the failed
  objective was resolved".
- Three tiers: `good_recovery` (strategy/action changed), `plausibly_resolved`
  (same tool+executable+overlapping target, `attribution_ceiling=hypothesized`),
  `unrecovered_failure`. An unchanged successful retry is
  `retry_succeeded_without_strategy_change` — never promoted to recovery.
- `expected_probe` marks an unrecovered read-only probe whose failure may mean
  "target absent", not "mistake".

**Execution quality** (`agr/detectors.py`)
- `ContextTokenBloat` requires `generation_usage: complete`.
- `ExcessLatency` requires `generation_timestamps: complete`.
- `RepeatedActionNoNewInfo` requires `tool_calls` + `tool_results: complete`.
- A detector whose capability is missing reports `evaluated=False` → "not
  evaluated", never "no issue found" (§6.2).

## 3. Observed denominators (demo store, 12 runs)

Recovery episodes — 6 total:

| classification | count | `usage_completeness` | `error_signature_basis` |
|---|---|---|---|
| `good_recovery` | 2 | unavailable (2) | diagnostic_line |
| `retry_succeeded_without_strategy_change` | 2 | unavailable (2) | diagnostic_line |
| `unrecovered_failure` | 2 | unavailable (2) | diagnostic_line |

Execution quality — per dimension, across 12 runs:

| dimension | evaluated | not evaluated | reason |
|---|---|---|---|
| `redundant_work` | 12 | 0 | — |
| `context_bloat` | 0 | 12 | missing `generation_usage` |
| `latency` | 0 | 12 | missing `generation_timestamps` |

Corrections surfaced by the inventory: **no** episode here carries a usable
resource measurement (6/6 `unavailable`) and **no** execution-quality finding
exists in this store, so any cost claim would be unsupported.

## 4. Corrections made (this change)

**Per-finding `limits[]` on representative recovery episodes.** `agr/recovery.py`
gains `episode_limits()` — a read-model projection over fields the episode
already carries, so it works for stores persisted before it and needs no
backfill. It is derived **per episode**, never merged, so two episodes in one
group are limited differently when their evidence differs. The Patterns
drill-in renders the limits beneath the observation.

Limits emitted:
- `usage_completeness == "unavailable"` → usage never instrumented; no cost
  attributed.
- `usage_completeness == "partial"` → measured total undercounts.
- `attribution_ceiling == "hypothesized"` → link plausible, not confirmed.
- `retry_succeeded_without_strategy_change` → a retry, not a demonstrated
  recovery.
- `unrecovered_failure` → no resolution observed; whether it mattered is not
  established. (`expected_probe` → failure may be the target being absent.)

**`limits[]` extended to moments.** The same contract now rides on
`Candidate.limits` → `ReviewMoment.limits` → the guided-view moment projection,
so a published card states its limits on the card (not only in the expanded
breakdown). A moment whose `attribution_ceiling` is `hypothesized` always adds
"Causal attribution is hypothesized, not established".

**`limits[]` extended to execution-quality dimensions.** `execution_quality_summary`
and `execution_quality_record` attach `limits` to every efficiency dimension (a
read-time projection, so captures persisted before this are covered): a
violation is "a candidate for review, not proof of waste" and independent of
the outcome; an unmeasured dimension is "a coverage gap, not a clean result".
Rendered in the run review's Execution quality table.

**Denominators (§12.1).** Recovery groups now carry `task_count` beside the run
count (`2 runs · 1 task`), so a pattern that repeats within one task is not
mistaken for one spanning several.

**Unresolved-failure limit.** `IgnoredToolFailure` candidates now carry
"Not linked to a verifier check; its effect on the task outcome is not
established" — the finding is still surfaced (the failure is real) but reads as
a candidate for review, not a confirmed problem. This is the evidence-gated
half of the precision fix; the rule still needs re-derivation on a labelled
sample (§5).

## 5. Unsupported-risk cases and recommended corrections

| Risk | Evidence | Recommended correction | Status |
|---|---|---|---|
| Unrecovered-failure precision | Historical audit (9 Sep): only 1 of 5 sampled unresolved-failure findings clearly supported. Not re-derived here. | Narrow/suppress unrecovered findings that lack a shown consequence; keep `expected_probe` episodes out of "needs attention" by default. | **Partly addressed** — the finding now states it is not linked to a check; still needs a re-run on a labelled sample |
| Recovery precision | Historical audit: 4 of 4 sampled recovery episodes supported after a verification-method correction. Small sample. | Keep the operation-scoped link; add the per-episode limits (done) so a pass is not read as proof. | Partly addressed |
| Episode cost read as savings | `usage_availability: unavailable` on 6/6 demo episodes; no measurement at all. | Never present `episode_window_tokens` as a saving; the fleet `usage_note` already states this. Limits now repeat it per episode. | Addressed |
| Overlapping windows counted twice | `overlapping_usage_events` guard exists (`_usage_union`). | Keep union semantics for every new aggregate. | Addressed (existing) |
| "Not evaluated" read as "clean" | `context_bloat`/`latency` are 0/12 evaluated here. | Coverage/`not evaluated` must stay distinct from `0%` — covered by the execution-quality coverage work (PR #73) and P1 taxonomy. | Addressed / in review |

## 6. Trust-rule compliance (amendment §B)

| Rule | Status |
|---|---|
| B1 observation + evidence + interpretation + resource + limits | **Addressed** — all four finding types (recovery, moment, execution quality, audit) carry `limits[]`. |
| B2 suggested change = experiment | Lessons/experiments exist; not yet tied to a measurement. **Open (P4).** |
| B3 no double-counting | Union semantics in place; extend to new aggregates. |
| B4 benign behaviour not auto-flagged; per-type audit | This document is the first per-type audit; the unrecovered-failure rule is **open**. |

## 7. Open items for the next audit

1. Re-derive the "1 of 5 unresolved-failure" precision on a labelled sample and
   land the resulting narrow/suppress rule.
2. ~~Extend `limits[]` to execution-quality findings~~ — done.
3. ~~Add a denominator to every surface~~ — done: recovery groups carry a task
   count, and the execution-quality matrix states its coverage in distinct
   tasks beside runs (`12 of 12 runs` · `6 of 6 tasks`), with the per-bucket
   task counts deduplicated across outcome buckets.
