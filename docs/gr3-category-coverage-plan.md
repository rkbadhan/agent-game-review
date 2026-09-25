# GR-3 — Cover the idea's categories: implementation plan

Status: **implemented** on branch `gr3-category-coverage` (off `main`, which now
includes GR-1 and GR-2 via PR #91). Scope of the dev-work review (2026-09-22),
section **GR-3 — Cover the idea's categories**.

## 1. Goal

The reviewer must be able to produce a finding for **each of the four categories in the idea**,
and must be able to say which categories it actually covered on a run. The category is a
*deterministic view over taxonomy tags* — the model still emits tags, never categories.

Source table (verbatim):

| Category from the idea | Taxonomy tag | Detected today by | Needed |
| --- | --- | --- | --- |
| Mistake in planning | `poor_decomposition`, `failed_to_replan`, `premature_commitment` | Model only | GR-1 plus per-tag prompt guidance |
| Bad query in a tool call | `poor_query`, `invalid_arguments` | Partly: `argument_shapes` | Model judgment of the query against later results, linked to argument-shape stats |
| Claiming victory before verifying | `premature_submission`, `skipped_verification` | `unresolved_requirement_at_submission` | Keep; the timed-out-run false positive is already fixed |
| Good recovery from a failed plan | `good_recovery`, `effective_replan` | `successful_recovery_via_strategy_change` | Shown with a supported-recovery label |

Two cross-cutting rules from GR-2 still apply, and GR-3 must not weaken them:

- **Judging ≠ suggesting.** A *judgment* about a past action ("this query was bad") may use the later
  result that revealed the consequence. The **information cutoff only constrains a *suggestion***.
- **No label without the support behind it.** A supported-recovery label is only shown when the
  deterministic recovery classification supports it.

## 2. What already existed

- `agr/taxonomy.py` — `BEHAVIOUR_TAG_GUIDANCE` (GR-1), every tag covered. Planning and query tags
  already in the vocabulary.
- `agr/model_reviewer.py` — `_behaviour_tag_guidance()` renders all guidance into `SYSTEM_PROMPT`.
- `agr/reviewer.py` — `validate_facts`, tag filtering, `_cutoff_context` (GR-2, suggestions only).
- `agr/argument_shapes.py` — fleet-wide `argument_shapes(store)`; not in the packet.
- `agr/detectors.py` — the submission and recovery detectors; neither set a vocabulary tag.
- `agr/read.py` — moment projection; `review_counts` (GR-1/GR-2).

**Missing:** the category registry and coverage report; category-targeted prompt framing;
run-scoped argument-shape stats and a link to them; the mechanical supported-recovery label.

## 3. Design decisions

1. **Categories are a deterministic view over tags, not a new axis.** Coverage is computed from what
   was found, never asserted by the model.
2. **Coverage is reported, never forced.** An uncovered category is absent; abstention stays valid.
3. **Judging may use later evidence; only suggestions are cutoff-bound.** Stated in the prompt.
4. **The recovery label's basis is mechanical**, from the deterministic recovery classification.
5. **The argument-shape link is deterministic** (see deviation below), so a statistic can never be
   invented.

## 4. Workstreams — as implemented

### WS1 — Category registry + coverage report (the spine)
- `agr/taxonomy.py`: `IDEA_CATEGORIES`, `DETECTOR_BEHAVIOUR_TAGS`, `_TAG_TO_CATEGORY`,
  `category_for_tag()`, `category_for_tags()`, `category_coverage()`, plus import-time invariants
  (tags real and guided, ≥1 route per category, no tag in two categories, detector tags in-vocabulary).
- `agr/read.py`: `_apply_moment_enrichment()` adds `category`, `category_label`, `categories` and
  `basis` to every served moment; `get_review` adds `review_counts["categories"]`
  (`{"covered": [...], "counts": {...}}`) from the served moments.
- Tests: `tests/test_taxonomy.py`; `tests/test_read.py` coverage + uncategorised-moment tests.

### WS2 — Planning category: category-targeted prompt framing
- `agr/model_reviewer.py`: a **CATEGORY COVERAGE** block in `SYSTEM_PROMPT` names the four
  categories and their tags, states the judging-vs-suggesting rule, and re-states the abstention
  guard ("never pad a category"). `_BUDGET_RESERVE_CHARS` bumped 15_000 → 16_000.
- Tests: prompt names every category tag and both rules (`tests/test_model_reviewer.py`).

### WS3 — Bad query: later-result judgment + argument-shape link
- `agr/argument_shapes.py`: `_run_groups`/`_groups_to_list` factored out; new
  `argument_shapes_for_events(events)` and `argument_shape_link_for_event(events, event_id)`
  (resolves either side of the failing pair; structure only).
- `agr/model_packet.py`: the packet carries `argument_shapes` (top 10 run-scoped groups).
- `agr/read.py`: `_apply_moment_enrichment` attaches `argument_shape_link` deterministically when a
  moment anchors a genuinely failing call.
- Tests: `tests/test_argument_shapes.py` (run-scoped parity, link, no value leak, None cases);
  `tests/test_model_reviewer.py` (packet carries shapes).

### WS4 — Recovery category: supported label with mechanical basis
- `agr/read.py`: a recovery detector moment gets `detector_tags = ["good_recovery"]` (+
  `"effective_replan"` when the state_transition fact's `strategy_changed` is true), the category
  *Good recovery from a failed plan*, `label = "Supported recovery"`, `basis = "mechanical"`. Basis
  is tracked per CATEGORY (`category_bases`), so a model tag for another category never inherits the
  mechanical basis.
- The submission detector gets the neutral label *Requirement unresolved at submission* and no
  category: it proves a submission and a failing check, not that the agent claimed victory
  (*Claiming victory before verifying* stays model-only, with the detector recorded as `surfaced_by`).
- Tests: `tests/test_read.py` (recovery label/category/basis; submission neutral label; a model
  category never inherits a mechanical basis).
- UI: `agr/static/js/moments.js` `.moment-category` / `.moment-shape` rows; `agr/static/app.css`;
  browser test `test_moment_card_shows_supported_recovery_category_and_basis` (skips without
  Playwright).

### WS5 — Row 3 regression guard
- `tests/test_detectors.py`: `test_good_recovery_category_keeps_the_timed_out_false_positive_fixed`
  — the submission detector stays silent on a timed-out run, the terminal-failure detector covers it.

## 5. Implementation deviations from the draft plan

1. **Argument-shape link is deterministic and read-time, not a model-emitted fact.** The draft
   proposed a `{type: "argument_shape", tool, error_signature, shape_index}` fact the model would
   emit and Stage G would recompute. In the packet the model sees *redacted* trace text, so the
   `error_signature` it echoed could be a redacted form that then fails recomputation against the
   unredacted events. The system now computes the link itself from the moment's own failing call, so
   the statistic is always real and the model cannot invent one — strictly stronger than
   recomputation. It is not added to `supported_fact_types` because it is not a model fact at all.
2. **`category_label` is sent to the UI** so the registry lives in one place (`taxonomy.py`) rather
   than being duplicated in JavaScript.
3. **`model_guided` in `category_coverage()` is "the category's tags have prompt guidance"**, not
   "a model detection route was declared" — recovery is detector-first but its tags are still guided.
4. **PR #92 review — no mechanical victory claim.** The submission detector originally mapped to
   `premature_submission` with a mechanical basis, but it only proves a submission and a failing
   check; an honest "I could not finish" fails the same check. It now gets a neutral label and no
   category, and is recorded as `surfaced_by` rather than a tag route.
5. **PR #92 review — per-category basis, every category shown.** `basis` moved from per-moment to
   per-category (`category_bases`); the card renders every category the moment covers
   (`category_details`), each with its own label and basis. A model tag for one category keeps its
   own `model` basis, and no category is counted toward coverage while staying invisible.

## 6. Verification

- Full suite green: `python -m pytest -q` (browser tests skip locally without Playwright).
- Demonstrated on real fixtures: `solve_task__recovered` covers *Good recovery from a failed plan*
  with `good_recovery` + `effective_replan`, *Supported recovery*, `basis: mechanical`;
  `build_task__ignored_failure` / `chess_best_move__seed42` cover *Claiming victory before verifying*.

## 7. Open follow-ups (not in GR-3 scope)

- An aggregate category-coverage report over the development set for M2 reporting (the per-run
  `review_counts.categories` is the building block; no new storage needed).
- RS-2 (M5) formalises the full `basis`/label vocabulary; GR-3 only adds the mechanical basis the
  recovery row needs.
