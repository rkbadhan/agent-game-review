# AGR-00 repair baseline (September 5, 2026)

Working checklist for the repair cycle defined in the September 5, 2026 action
plan ("AGR action plan: repair correctness, then demonstrate usefulness").
Tick items off as their owning tickets land; keep the observations here honest —
counts below describe the reviewed snapshot, they are not permanent targets.

## Snapshot identity

- [x] Reviewed public commit: `cc5be32511fdbcadda172a0caed4ba284ce7839a`
      (`agent-game-review`, "Publish curated subset from source main").
- [x] Mapped to the source repo: the mirror's publish commit records its source
      commit — the snapshot is source `6df5f36` (`main`). All repair work starts
      from this commit.
- [x] Original corpus and historical reports preserved (`.agr-store/`,
      `eval-runs/`, `.benchmarks/`, `experiments/` untouched by repair commits).

## Recorded environment (repair run)

- Python 3.13.2 (local repair checkout; CI matrix runs 3.11 on ubuntu + windows)
- Dependency lock: `uv.lock` at `6df5f36`
- Test command: `pytest` via the `dev` dependency group
- Suite state at the start of the repair run: **393 passed, 3 failed, 1 skipped**
  (plan's review environment recorded "382 passed and 3 skipped" — see D-1)

## Demonstrated defects → owning tickets

Each reproduced failure gets a line here and a regression check in its ticket.

- [x] **D-1 — 3 UI test failures at the snapshot** (AGR-00/01, fixed in PR #42)
      `test_compare_surface…` / `test_eval_lesson_lifecycle…`: stale fixtures
      built scripted model moments without `structured_facts`; the `4602654`
      Stage-G hardening correctly drops fact-less moments, so no card rendered.
      `test_entry_preference…`: racy `wait_for_selector` on an already-present
      chip. App behavior verified correct in a live browser.
- [x] **D-2 — wheel omitted CSS and JavaScript** (AGR-01, fixed in PR #42)
      `package-data` shipped only `static/*.html`. Verified pre-fix failure
      mode; post-fix the wheel renders the full demo from a clean venv outside
      the checkout, guarded by the CI `installed-package` job.
- [x] **D-3 — execution identity collision** (AGR-02, adapter 0.6): distinct
      Harbor trial attempts collapsed into one run ID (first-12-chars of a
      task-prefixed session string — the three nginx attempts all began
      "nginx-reque"). Fixed: run id derives from the full Harbor trial UUID
      (result.json `id`) when present, else a hash of the complete
      task+session identity; trial uuid/name/session preserved on the run for
      lineage. Corpus re-ingested into a fresh store (`.agr-store-v2`, old
      store untouched): 22 discoverable trials → 22 distinct executions (was
      16 logical runs); nginx attempts vTNAJM8/h6dA2go/rwPp8Q4 = PASS/FAIL/
      PASS; every discovered directory accounted ingested-or-excluded-with-
      reason. Old→new mapping in `docs/harbor-identity-migration.json`:
      9 old runs mapped unambiguously, 6 ambiguous (collapsed attempts —
      reported, not guessed); no feedback records existed to migrate.
      Note: Harbor records no retry/sibling metadata per trial, so attempts
      of one task are related by task_id + trial_name only — the store does
      not invent relationships the source does not supply.
- [x] **D-4 — evidence-validation gaps** (AGR-03, fixed on this branch): six
      reproduced cases no longer publish as validated findings — phantom
      anchor, phantom affected check, empty quote (matched every text by
      substring), undeclared-artifact absence, partial-filesystem absence
      (now scoped to "not observed in captured evidence"), and the invented-
      database-outage explanation (now labelled interpretation_only with its
      own support status). Model discoveries meet computed capability
      requirements; cards render from the first PASSED fact; verifier-timing
      is honest ("failed the run's final verifier" unless the agent's trace
      shows it observed the failure before the run ended).

- [x] **D-5 — diagnostic input loss** (AGR-04, adapter 0.7): the reviewer saw
      task requirements only as 220-char timeline excerpts and the verifier as
      a single aggregate reward. Fixed: the complete task instruction is a
      dedicated reviewer input field (nginx: full 4,765 chars); verifier
      ctrf.json imports as atomic per-test checks (nginx a2: 8 tests, 8 failing
      incl. test_outputs.py::test_log_file_format) with the aggregate reward
      retained as the run outcome; all verifier checks labelled post_run;
      test-stdout excerpts attached, capped and source-referenced; step
      timestamps and task checksum/ref preserved when the source records them
      (never synthesised); verifier_code capability now honest (partial — the
      trial bundle carries results, not code) with verifier_results complete.
- [x] **D-6 — behavioural-semantics defects** (AGR-05): four reproduced cases
      fixed — (1) any successful command closed a failure episode (failed
      pytest + successful pwd was "good recovery"); resolution now requires a
      success linked to the failed operation (same signature or same primary
      command token). (2) Repeated calls were flagged "no new information"
      regardless of outputs; the claim now requires captured, equivalent
      outputs — a poll whose report changed emits nothing. (3) A passing run
      with no verification action earned positive verification credit; the
      signature row now reports "no positive evidence — a passing outcome is
      not verification evidence". (4) Dedup collapsed any cards sharing an
      affected check; it now runs on issue identity (fact subject), so two
      distinct issues survive even against a shared aggregate check, while
      same-subject duplicates still collapse. Plus: a successful unchanged
      retry is scored a recovery (worded honestly), and the compaction
      placeholder reports "not implemented" instead of faking an evaluated
      no-issue run. Gold fixtures reconciled with dated addenda (fetch_task
      repetition claim contradicted by captured outputs; under-annotated
      second issues in build_task/greeting_report); formal re-publication is
      AGR-07.
- [x] **D-7 — reviewer access and failure states** (AGR-06): five gaps fixed,
      each with a regression test — (1) only packet event texts were redacted;
      a schema-aware traversal now redacts EVERY outbound string (check
      names/expected/observed values, structured facts, revision and expansion
      payloads — the synthetic-token test proves nothing survives). (2) The
      220-char digest truncated everything; traces that fit the 60k-char
      input budget now ship with full event text. (3) No evidence expansion
      existed; one bounded second pass now resolves the model's requested
      event ids against captured evidence (unknown ids rejected explicitly,
      caps on count and size), redacts it, and grants one final answer
      round. (4) No cost records existed; telemetry now captures per-round
      input/output token estimates, latency, requested evidence, and
      rejection reasons by gate. (5) A malformed model response silently
      became "no decisive moment"; it is now an explicit ModelOutputError —
      the pipeline falls back to the deterministic baseline, writes the error
      to review_errors.json, never touches the model's review slot, and the
      UI shows "model review failed" — with valid-empty and no-selection
      reviews named as their own states.
- [x] **D-8 — evaluation-metric defects** (AGR-07): the harness scored a
      positive prediction on a negative gold moment as a full semantic match;
      matching now requires polarity agreement (mismatches earn no precision
      or recall and the gold stays unmatched). Mechanism specificity, claim
      support, and attribution are scored separately per matched pair. The
      top-k cut is pinned to the reviewer's selection ranking. Abstentions
      (incomplete reviews) stay in the denominator with zero credit;
      fabricated findings on clean passes are counted and named.
      `agr eval --manifest` publishes versions, gold hashes, adjudication
      status, and the invocation for reproducibility. The Gate B1 HOLD label
      was reconciled with its frozen rule in a dated addendum (correct label:
      GO fails, no REVISE majority); the README now distinguishes historical
      results, current deterministic checks, and future model results.
## Follow-up observations (no ticket yet, noted during repair)

- When a model review yields zero selected moments, the UI serves an empty
  moments list with no explicit "model review produced no grounded cards"
  state; the deterministic baseline is not surfaced (AGR-06 territory:
  "preserve the deterministic baseline when enrichment fails").
- Transient `syncUrl` writes a stale moment ID under the new run's URL during
  the async fetch (cosmetic).
