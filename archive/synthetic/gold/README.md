# Reviewer gold set — annotation guide

This directory is the **gold review set** the spec sequences *before* the model
reviewer (spec §15, §20 Milestone 0, principle #11). It is the ground truth the
reviewer evaluation harness (`agr/reviewer_eval.py`) scores any reviewer against
— today the deterministic detector baseline, tomorrow the M4 model reviewer,
through the same harness and the same metrics.

> **Honesty note.** The labels here are *synthetic reference annotations authored
> for the synthetic fixtures* in `../fixtures/`, not expert-adjudicated
> production data. The **schema, protocol, and metrics** are the real deliverable;
> the labels exist to exercise them end-to-end. This is the same honesty the
> README applies to "Harbor ATIF" being a synthetic contract. Real deployment
> replaces these files with expert, double-labelled, adjudicated annotations
> following the protocol below — the schema and harness do not change.

## Files

- `<run_id>.gold.json` — one gold trajectory per logical run, validated by
  `agr.gold.GoldSet.validate` against `agr/gold.py` and against the run's
  immutable source (every step id must exist in the source).

## The record (`agr.gold`)

A `GoldTrajectory` carries one or more `GoldAnnotation`s. More than one means the
trajectory was **double-labelled**; then an `adjudicated` annotation records the
resolved truth used for scoring. Disagreement between annotators is *reported,
not erased* (`agr.gold.disagreement_report`).

Each `GoldMoment` follows the §15.2 protocol:

| Field | Meaning |
|---|---|
| `anchor_type` | `decision` \| `omission` \| `recovery` \| `external` (spec §3.4) |
| `anchor_step_ids` | the **source** step ids the moment is anchored on (immutable; survives derivation-version bumps) |
| `polarity` | `negative` (concern) or `positive` (strength) |
| `behaviour_tags` | controlled tags from the §9.1 behaviour axes |
| `affected_checks` / `affected_requirements` | verifier checks / contract items the moment bears on |
| `evidence_span_step_ids` | the smallest set of source steps that evidences the moment |
| `attribution_ceiling` | strongest attribution language the evidence licenses (`hypothesized` → `counterfactually_supported`). A single rollout without replay caps at `dependency_linked`. |
| `opportunity_ability` / `opportunity_window_step_ids` | the feasible window (spec §6.7); omit when there is none |
| `root_cause_candidates` | ranked `{locus, rank, rationale}` from the §11 loci |
| `acceptable_better_actions` | the smallest plausible better actions at the moment |
| `critical` | a moment whose omission by a reviewer counts as a *missed-critical* error (spec §15.3) |

Set `no_decisive_moment: true` (with a `no_moment_rationale`, and no moments)
when no defensible decisive moment exists — the calibration case the spec
requires (§15.1).

## Sampling coverage (spec §15.1)

| Run | Category |
|---|---|
| `chess_best_move__seed42` | failed run · **omission** (submitted one of two winning moves) · **double-labelled** |
| `build_task__ignored_failure` | failed run · **external** tool failure ignored through submission |
| `solve_task__recovered` | **recovery** (positive) via strategy change |
| `fetch_task__unchanged_retry` | passed · low-value **repeated-action** concern (no material effect) |
| `greeting_report__seed7` | **task/verifier concern** (broken verifier check) + omitted required artifact |
| `greeting_file__clean_pass` | clean pass · **no defensible decisive moment** (calibration case) |

## Protocol summary (spec §15.2)

Experts label top decisive moments, anchor type and evidence spans, behaviour
tags, affected requirements/checks, attribution ceiling, opportunity window,
ranked root-cause candidates, acceptable better actions, whether no decisive
moment can be established, and task/verifier concerns. A subset is independently
double-labelled and adjudicated; disagreement is reported.

## Validate

```bash
python3 -m agr eval            # ingests fixtures, scores the baseline, prints metrics
python3 -c "from agr.gold import load_gold_set; load_gold_set('gold').validate()"
```
