# GR-2 — Grounded alternatives: implementation plan

Status: **not started** (P0-1…P0-6 and GR-1 are merged; `git log` shows six `GR-1:` commits and zero GR-2 references).
Scope of the dev-work review (2026-09-22), section **GR-2 — Grounded alternatives**.

## 1. Goal

A better move on a moment card is a **proposal**; a passing sibling is an **example**.
The screen must keep three kinds of alternative apart and show each under its *actual* status:

| Kind | Source | Label shown |
| --- | --- | --- |
| Suggested alternative | model `better_action` | Suggested alternative |
| Observed alternative | passing sibling at the divergence point (`agr/divergence.py`) | Alternative observed in a comparable passing run, + how the sibling differs (model, temperature, agent config) |
| Validated alternative | linked experiment | Validated by replay *or* Validated by comparable experiment, whichever actually ran |

Two cross-cutting rules:

- **Information cutoff for suggestions** — each suggestion names the decision it replaces and the
  information available immediately before that decision; it is generated from that information plus
  the task instructions, and its factual assumptions are validated against the same input. Later
  evidence may explain the consequence but cannot justify what the agent should already have known.
- **Validation rule** — an alternative is *validated* only when a linked experiment supports a stated
  benefit, records the relevant outcome checks, and discloses comparison limits. Without outcome
  verification it claims only the measured execution improvement. Failed or inconclusive experiments
  stay visible with that status.

## 2. What already exists (build on, do not rebuild)

- `agr/model_reviewer.py` — `Enrichment.better_action` (line ~130 in `reviewer.py`), parsed at
  `_parse_enrichment` (~327), prompted at ~246. Currently a bare string.
- `agr/schema.py` — `ReviewMoment.better_action` (~592); `to_dict()` via `asdict`.
- `agr/read.py` — moment projection exposes `better_action` (~209).
- `agr/static/js/moments.js` — renders one `"Better action"` block (~109-112), label
  `"replay-tested"` / `"hypothesis · not replay-tested"`.
- `agr/divergence.py` — `find_passing_sibling` + `divergence_report` (strict *same* configuration via
  `_same_configuration`, AGR-12); surfaced only as the separate `sibling` Compare view
  (`agr/static/js/sibling.js`), never on a moment card.
- `agr/lessons.py` — `experiment_proposal` from an approved lesson (`_experiment_proposal`,
  `propose_experiment`, `approve_experiment`) with `primary_measure`, `evaluation_design`,
  `guardrails`, `status`; no recorded outcome, no link back to the moment's alternative.
- `agr/reviewer.py` — `attribution_ceiling` machinery (`counterfactually_supported` requires replay
  the package does not generate), fact validation (`validate_facts`), `_linked_slices`.
- GR-1 — six-state review status + per-run drop counts already exist; extend them with alternative
  drops rather than inventing a parallel counter.

**Missing entirely:** an alternative *kind* discriminator, the information-cutoff fields/logic, the
sibling→moment-card integration, experiment outcome recording, the validation rule, and any tests.

## 3. Data model

Add a first-class `Alternative` shape (new dataclass in `agr/schema.py`) and a
`alternatives: list[dict]` field on `ReviewMoment`. Keep `better_action: Optional[str]` as the
string summary for backward compatibility (lesson/compare/correct/CLI all read it); when a
`kind == "suggested"` alternative exists it is the source of truth and `better_action` is derived
from it.

```
Alternative:
  kind: "suggested" | "observed" | "validated"
  label: str                      # display label, exactly as the table above
  proposal: str                   # the better move / observed action
  source: str                     # "model" | "sibling:<run_id>" | "experiment:<lesson_id>"
  # information cutoff (suggested only; None otherwise)
  replaces_decision: {event_id, description} | None
  information_available: [ {event_id, ...} ]        # events at/before the decision
  assumptions: [ structured_fact ]                  # recomputed against the same input
  # observed only
  sibling_diff: {model, temperature, agent_config} | None
  # validated only
  validation: {
      status: "proposed" | "validated_by_replay" | "validated_by_comparable_experiment"
              | "failed" | "inconclusive",
      stated_benefit: str | None,
      outcome_checks: [ ... ] | None,
      comparison_limits: [ ... ] | None,
  } | None
  attribution_ceiling: str
  limits: [str]
  rejected: bool                  # dropped by a mechanical check; never shown when True
  rejection_reason: str | None    # for the drop log
```

`kind` → label mapping is a single function/dict so the three labels cannot drift. Validated labels
resolve from `validation.status` (`validated_by_replay` → "Validated by replay";
`validated_by_comparable_experiment` → "Validated by comparable experiment").

## 4. Workstreams

### WS1 — Model (foundation)
- `agr/schema.py`: add `Alternative` dataclass + `ReviewMoment.alternatives`.
- `agr/read.py`: project `alternatives` onto the served moment; keep `better_action`.
- Drop logging: extend the GR-1 per-run review counts with `alternatives_proposed`,
  `alternatives_dropped`, and a per-reason breakdown.
- **Acceptance:** a deterministic-only review carries `alternatives == []`; served JSON includes the
  field; counts appear in `review_counts`.

### WS2 — Suggested alternative + information cutoff
- `agr/model_reviewer.py` prompt: extend the per-moment JSON with an `alternatives` array; for a
  suggested alternative require `replaces_decision`, `information_available` (event ids **from the
  packet**), and `assumptions` (structured facts). Keep `better_action` mandatory-optional so old
  payloads still parse.
- `_parse_enrichment`/envelope validation: parse alternatives; a malformed alternative is dropped, not
  fatal.
- Cutoff validation (new helper, e.g. `_validate_alternative` in `agr/reviewer.py`):
  - every `information_available` event_id must exist and have `sequence <= ` the decision event's
    sequence — a later event fails the cutoff and drops the alternative (logged reason
    `information_cutoff`);
  - `assumptions` must recompute with the existing `validate_facts` — otherwise drop
    (`assumptions_unvalidated`);
  - the `replaces_decision` event must be real.
- Only then attach a `kind == "suggested"` alternative; set `replaces_decision` + cutoff on the card.
- **Acceptance:** the "oversized-file rejection → smaller read" example works; the "smaller read
  before the first read" example is dropped unless a prior event grounds the size limit; drop is logged.

### WS3 — Observed alternative from a passing sibling
- New deterministic finder (probably in `agr/divergence.py`) that returns the nearest passing sibling
  on the same task **allowing a differing configuration**, plus the config diff
  (`model`, `temperature`, `agent_config`). Keep the existing strict `_same_configuration` path for
  the Compare view unchanged.
- At review-build time (in the reviewer envelope, not just at render), for a failed run attach an
  `observed` alternative whose `proposal` is the sibling's action at the first divergence point, to
  the moment anchored on the failed side of that divergence. If no moment exists yet for that event,
  do **not** invent one — the alternative is only attached to an existing moment.
- Never guess: no sibling → no observed alternative (and no error).
- **Acceptance:** a failed run with a passing sibling yields one observed alternative carrying the
  sibling's divergence action and the config diff; a run with no sibling yields none.

### WS4 — Validated alternative from a linked experiment
- Extend the lesson `experiment_proposal` with an outcome record:
  `stated_benefit`, `outcome_checks`, `comparison_limits`, `result`
  (`validated | failed | inconclusive`), and add `record_experiment_outcome(...)` in `agr/lessons.py`
  with an API route + CLI hook. Replay evidence stays gated off (post-MVP) — the realistic path is
  "comparable experiment".
- Validation rule in one place: mark `validated_by_comparable_experiment` **only** when a linked
  experiment has `result == validated` **and** a non-empty `stated_benefit` **and** non-empty
  `outcome_checks` **and** `comparison_limits` disclosed. Otherwise carry `failed` / `inconclusive` /
  `proposed` visibly (never silently upgraded). "Without outcome verification it claims only the
  measured execution improvement" → attach the measured improvement as the proposal's `limits`.
- Map an experiment back to its source moment via `source_moments`, and attach a `validated`
  alternative on that moment.
- **Acceptance:** an approved experiment with recorded outcome checks shows "Validated by comparable
  experiment"; one without outcome checks stays "proposed" with the measured-improvement limit; a
  failed experiment stays visible as failed.

### WS5 — UI
- `agr/static/js/moments.js`: replace the single "Better action" block with an **Alternatives** section
  that renders each alternative under its resolved label, with the information cutoff shown for
  suggestions (decision + available info), and the sibling/capability diff for observed ones.
  Keep a fallback that renders legacy `better_action` when no structured alternative exists.
- `agr/static/js/correct.js` / `compare.js` / `chapters.js` / `lessons.js`: continue to read
  `better_action` (unchanged) so secondary workflows do not regress.
- **Acceptance:** a browser test asserts the three labels render only when their evidence exists, the
  cutoff is shown on a suggestion, and no alternative is labelled "validated" without outcome checks.

## 5. Tests

- `tests/test_alternatives.py` (new): kind→label mapping; suggested cutoff pass/fail; assumptions
  recompute; malformed alternative dropped not fatal; observed from sibling with config diff; no
  sibling → none; validated only with benefit+outcome checks+limits; failed/inconclusive visible;
  drop counts.
- `tests/test_lessons.py`: `record_experiment_outcome` transitions + validation gate.
- `tests/test_api.py` / `tests/test_read.py`: `alternatives` in the served moment projection.
- `tests/test_divergence.py`: the new permissive finder returns a config diff without breaking the
  strict Compare path.
- Playwright/browser test for the moment card (labels, cutoff, no false "validated").
- `tests/test_model_reviewer.py`: prompt-schema test that the alternatives array is accepted and a
  pre-cutoff suggestion is rejected.

## 6. Sequencing & estimate

Roughly 3–4 engineer-days, inside M2 (GR-1 → GR-2 → GR-3). Order:

1. WS1 foundation (0.5 d)
2. WS2 suggested + information cutoff (1 d) — the credibility core; do first after WS1
3. WS3 observed from sibling (0.75 d)
4. WS4 validated from experiment (0.75 d)
5. WS5 UI + browser test (0.5 d)
6. CHANGELOG entry under `[0.2.0] - Unreleased`

Each workstream lands with its tests; nothing is marked done without the browser assertion for WS5.

## 7. Decisions to confirm before coding

1. **Observed-alternative configuration.** GR-2 wants "how the sibling differs (model, temperature,
   agent config)", but `divergence._same_configuration` (AGR-12) requires an *identical*
   configuration. Plan: add a separate permissive finder for observed alternatives that records the
   diff, and leave the strict Compare path alone. Confirm this is the intended reading.
2. **Validated-alternative source.** Replay is post-MVP, so "Validated by replay" is unreachable in
   this package — only "Validated by comparable experiment" can occur. Confirm the label set is kept
   so replay can slot in later without a schema change.
3. **`better_action` string.** Keep it as a derived compatibility field, or migrate all readers
   (`lessons.js`, `correct.js`, `compare.js`, `chapters.js`, `cli.py`) to `alternatives`? Plan keeps
   it derived to avoid a wide, risky migration.
4. **Where observed alternatives are attached.** Reviewer envelope (so they count in evaluation/drop
   logs) vs. read-time projection. Plan: reviewer envelope.
