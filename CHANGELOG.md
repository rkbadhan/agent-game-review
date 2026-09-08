# Changelog

All notable changes to Agent Game Review are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/); versions before 1.0
do not promise a stable public API.

## [0.2.0] - Unreleased

A hardening pass driven by two independent reviews of the deterministic
core and the model-reviewer safety envelope (commit `82cc113` and this
follow-up), closing out acceptance gaps AGR-01 through AGR-18.

### Fixed

- **AGR-01** — reproducible corpus manifest, counting definitions, and an
  annotation protocol for the real-data pilot corpus.
- **AGR-02** — reconciled same-scope test observations instead of letting a
  stale earlier failure or a later pass silently win; fixed mixed Go/Cargo
  test-output parsing that missed a failing package or a compile error
  alongside a passing summary.
- **AGR-03** — recognized the submission-confirmation control round-trip so
  it is never miscounted as an ordinary tool failure; evidence validation
  now enforces what a fact actually claims.
- **AGR-04** — recovery/mutation credit requires an observed successful,
  relevant change: a missing captured result no longer defaults to success,
  and an unrelated file edit no longer earns credit for an unrelated retry.
- **AGR-05** — episode token usage is computed from each episode's own
  defined window (interval accounting), not the display list, fixing
  systematic overcounting; usage-instrumentation completeness is tracked
  and surfaced instead of assumed.
- **AGR-06** — fixed `repeat_rate` math and made fleet-wide usage a true
  union across episode windows instead of a sum that double-counted
  overlapping episodes.
- **AGR-07** — error-signature selection prefers a meaningful diagnostic
  line over just the first line of captured output; opaque fallback-only
  signature groups are labelled as such instead of presented like a real
  diagnostic.
- **AGR-08** — repeated terminal failures collapse into one supported
  summary instead of one card per occurrence; the aggregate denominator and
  card wording now track the reconciled current checks, not a stale count.
- **AGR-09** — the runner (`agr run-sweep`) now passes the task's own prompt
  as the ingested instruction instead of relying on the transcript to echo
  it back; a crashed or timed-out task preserves its work directory (and
  any partial output the subprocess captured) instead of deleting the only
  evidence of what happened.
- **AGR-10** — the model reviewer's evidence-expansion round returns the
  *full* structured tool input, not a re-truncated copy of the smaller
  always-sent packet excerpt; Stage G quote validation checks an event's
  structured tool input as well as its display text, so an authentic quote
  from an Edit/Write's retained evidence no longer fails recomputation; a
  model-proposed "repeated mutation, no new information" claim can no
  longer validate on identical acknowledgement text alone, matching the
  deterministic detector's own guard.
- **AGR-11** — the model reviewer checks `build_packet`'s own enforced
  budget result before calling the provider; an over-budget packet is never
  submitted, saving real provider spend on a request the operator's own
  budget already declared unacceptable.
- **AGR-12** — sibling selection for the divergence view now filters by
  `configuration_id` before preferring a same-sweep sibling — two runs
  under different configurations are not a valid comparison baseline for
  each other even when they share a task.
- **AGR-13** — sibling timeline alignment now compares full action
  signatures (including structured input identity), so two Edits of the
  same file with different replacements are correctly detected as a
  divergence instead of comparing as an exact match.
- **AGR-14** — the fleet table's header stays visible while scrolling; a
  partial usage count renders as visible text instead of a hover-only
  marker; controlled-vocabulary badges (fallback-signature flag, the
  confirmed/plausible/unrecovered breakdown) and evidence-jump buttons are
  keyboard-focusable with accessible names, not mouse-hover only.
- **AGR-16** — the pilot gallery was re-verified against the corrected
  deterministic pipeline; two deltas were found and disclosed inline rather
  than silently folded into the original prose (see
  `experiments/terminal_bench/gallery.md`'s 2026-09-08 regeneration notes).
- **AGR-17** — the published quick-start corpus counts (trial/run/task-family
  totals) were stale; corrected to match the current `eval-runs/` corpus.

### Known gaps carried forward

- **AGR-15** — no real `.gold.json` labels exist yet; annotation is a
  separate, ongoing workstream (see `gold/real/README.md` and
  `experiments/terminal_bench/annotation-protocol.md`).

## [0.1.0]

Initial deterministic-core release: ingestion adapters (Harbor/Terminal-Bench
2.0, pi sessions, Claude Code traces), the deterministic reviewer envelope
(Stages G/H/I), the evidence-browser SPA, fleet and sibling-divergence views,
matched version comparison, and the optional Stage F model reviewer.
