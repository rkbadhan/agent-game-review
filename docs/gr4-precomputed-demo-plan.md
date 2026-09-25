# GR-4 — Pre-computed demo reviews: implementation notes

Status: **implemented** on branch `gr4-precomputed-demo` (off `main`, after GR-1/GR-2/GR-3).
Scope: the dev-work review (2026-09-22), section **GR-4 — Pre-computed demo reviews**.

## 1. Goal

`agr demo` opens on a real failed run with a **model** review, and the read-only
demo never calls a model. The reviews are committed, so no API key is needed.

Source requirements:

- Run the model reviewer over the published real runs and commit the results into
  the demo store, with **reviewer model and date on each review**.
- These runs informed detector and prompt development, so they are demo/development
  cases, not evaluation evidence (see EV-1).
- `agr demo` opens on a real failed run with a model review.

## 2. What existed

- `agr/demo.py` — `build_demo_store` built a **synthetic** two-configuration slice
  (source_type `synthetic_demo`) for the Compare surface. No model review.
- `agr/store.py` — per-reviewer review slots (`reviews/<key>.json`) already existed;
  a real model pass is stored alongside the deterministic baseline.
- `eval-runs/` — the published real corpus (immutable source of record).
- A working `.agr-store` carried **real kimi-k3 model reviews** for 15 real
  Terminal-Bench runs, but those reviews were not committed and not reachable by
  `agr demo`.

## 3. What was built

### Committed dataset — `agr/demo_fixtures/real_demo/`
- `runs/*.atif.json` — the real trajectory **source of record** for each reviewed run.
- `reviews/*.json` — one pre-computed model review per run:
  `run_id`, `task_id`, `reviewer_key`, `model`, `reviewed_at`, `source_hash`,
  `moment_count`, `selected_count`, and the review `moments`.

### `agr/demo.py`
- `find_real_demo_dir()` — resolves the dataset (package data, then source tree).
- `build_real_demo_store(store, real_dir=…, include_comparison=True)` — ingests each
  real run, installs the baked review snapshot into its latest capture (re-keyed to
  that capture so the slot is self-consistent), records `review_meta.json` with the
  reviewer **model and date**, adds the synthetic comparison slice, and picks a
  landing run.
- `_choose_landing_run` — a real **FAILED** run whose model review found moments
  (deterministic ordering), else any non-passing run with moments, else any reviewed
  run.
- `bake_reviews(store, out_dir, model=…)` — the regeneration path: exports the real
  runs and their model reviews into the dataset shape above.

### `agr/store.py`
- `review_slot_path()` — so the bake stamps a review with its real file time rather
  than an invented one.

### `agr/read.py`
- `get_review` surfaces `review_meta`, `review_model` and `reviewed_at` for the
  **served** snapshot. The read layer invents nothing: a live review has no meta.

### CLI / Dockerfile / UI
- `agr demo` / `agr demo-store` build the real demo by default (`--synthetic` forces
  the old slice; `--real-dir` overrides). `agr demo` prints the deep link to the
  landing run.
- `agr bake-reviews --store … --out … [--model …]` regenerates the dataset.
- `Dockerfile` bakes the real demo store into the image (no credentials).
- The review header shows the reviewer model and date (`render.js`, `.shell-meta`).

## 4. Design decisions

1. **Install, but verify the exact source first.** The baked moments already carry
   their validated facts; the demo installs the snapshot rather than re-running the
   model. Before installing, it requires a **matching, non-empty** `source_hash` on
   both sides and **rejects** anything else (so stale quotes/facts are never
   served); every model slot the dataset owns is cleared first, so a rejected
   review cannot remain served from a previous build. A guard test also checks the
   quoted text, not just event ids. The real source is never edited — the watermark
   is cleared via a derived `contract_confirmation.json`, which preserves the source
   hash and capture identity.

   **The demo never claims human confirmation.** Its confirmation record carries
   `demo_override: True`, which the pipeline maps to the distinct `demo_confirmed`
   contract status (not `human_confirmed`); item-level `human_status` is left
   `unconfirmed` (no human decided any item). `read.get_review` exposes
   `contract_demo_override`, and the header shows a "Demo override — contract not
   human-confirmed" chip.
2. **Build from the source of record, not a store snapshot.** Committing the real
   ATIF source docs (not derived records) keeps the dataset canonical and lets the
   deterministic pipeline regenerate everything; only the model reviews are baked
   (they cannot be regenerated without a key).
3. **Bake only a healthy reviewer, and never destroy the dataset.** `bake_reviews`
   picks a model slot only when the store's latest attempt for that reviewer is
   `ok` and it has no active error (a failed/incomplete retry leaves the older slot
   on disk by design). It writes to a staging directory and swaps in only when the
   export is **non-empty**; the previous dataset is vacated to a sibling backup and
   restored if the swap fails, so an empty or failed re-bake cannot wipe it.
3. **The dataset lives in the package** (`agr/demo_fixtures/real_demo/`), so `agr
   demo` works from an installed wheel and the container, not only a clone.
4. **`review_meta.json` is separate from the review slot**, so the slot payload stays
   the moment list its readers already expect.
5. **The landing run is chosen deterministically** from real failed runs with a
   model review, so the demo opens on the same run every build.

## 5. Provenance of the committed reviews

The committed dataset was baked from the real model reviews available in the working
store at bake time: **reviewer model `accounts/fireworks/models/kimi-k3`**,
**15 real Terminal-Bench runs**, review dates **2026-08-23 … 2026-08-25**. It is not
the 22-run `eval-runs/` corpus — those runs had no model reviews in the working store
and cannot be scored here (no provider credential is available in this environment).

To cover the published 22 once a key is available:

```bash
agr --store .agr-reviews ingest-harbor eval-runs/
agr --store .agr-reviews review --all --provider fireworks --model accounts/fireworks/models/kimi-k3
agr --store .agr-reviews bake-reviews --out agr/demo_fixtures/real_demo
```

(`--store` is a **global** option and must precede the subcommand; `bake-reviews`
takes only `--out` and `--model`.)

The baked dataset is a **demo/development** artifact. Because it informed detector
and prompt development, it must never be reported as evaluation evidence (EV-1).

## 6. Tests

`tests/test_demo_real.py`:
- the committed dataset is present and well-formed (runs + reviews, model + date);
- `build_real_demo_store` serves a pre-computed model review, no model call, landing
  run is a real failed run with moments;
- every installed review resolves against its freshly-ingested run (the guard);
- `bake_reviews` → `build_real_demo_store` round trip on a scripted review;
- the `demo-store` CLI builds the real demo offline.

`tests/test_ui.py`: the review header shows the pre-computed reviewer model and date
(browser test; skips without Playwright).

## 7. Out of scope / follow-ups

- Actual scoring of the 22 published runs (needs a provider credential; regeneration
  command above).
- PL-5 (honest demo timestamps): the real runs keep their real execution timestamps;
  a separate "demo loaded" time is still deferred to M5.
- PL-6: whether `eval-runs/` stays in the main repo or moves to a release asset.
