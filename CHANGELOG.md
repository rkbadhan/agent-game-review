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

### Fixed (follow-up review of commit 5782f1b)

A second review reproduced three further correctness gaps and flagged
several acceptance details the first pass left unfinished.

- **AGR-02** — a contract with NO author-declared
  (`stated_requirement`/`environment_precondition`) required items — the
  normal shape for a Claude trace, whose task carries only a free-text
  instruction and never a structured requirements list — vacuously produced
  an empty `coverage_gaps`, identical to "every declared item is covered".
  Those are not the same: with zero declared items there is nothing to
  check a smoke test's coverage against, so coverage is unestablished, not
  established. `outcome()` now demotes an in-session-only PASS to
  UNDETERMINED in that case too, with a `coverage_unknown` flag
  distinguishing it from an actual per-item gap list. **Blast radius
  checked**: this only fires when every current check is in-session-
  synthesized (`agr synthesize-verifier`) — the published 22-run
  `eval-runs/` corpus is entirely Harbor-ingested with `native_structured`
  checks, so none of it is affected; the gallery and README headline counts
  are unchanged.
- **AGR-11** — the initial-packet budget gate did not protect the SECOND
  (expanded evidence) provider call, which can add up to its own
  `_EXPANSION_MAX_CHARS` on top of a packet already close to budget. The
  expanded request is now measured and gated the same way; an over-budget
  expansion round raises before reaching the provider.
- **AGR-12** — the configuration filter still treated a target's MISSING
  `configuration_id` as a wildcard matching any candidate, including one
  with a specific, different, known configuration. Simplified to one
  exact-equality comparison: **missing `configuration_id` is its own value,
  not absence of a constraint** — a target with none matches a candidate
  that ALSO declares none (both sides genuinely unknown), never a candidate
  with a known, different configuration. This is a deliberate choice, not
  a stricter "missing means no match at all" reading: it keeps the common
  real case working (two Harbor imports, which never stamp
  `configuration_id`, remain comparable) while still rejecting the
  cross-configuration pairing AGR-12 exists to catch.
- **AGR-09** — a timeout now ingests its partial transcript as its own
  stored, reviewable run (not just a preserved workdir a human has to find
  and convert by hand), with no fabricated verifier outcome (empty checks →
  honest UNVERIFIED) and an explicit `run_timed_out` terminal step — the
  same vocabulary the Harbor adapter uses for a genuine deadline exception
  — instead of a weaker "completion not observed" gap for a fact the
  runner is not actually in doubt about.
- Fleet aggregation (`EpisodeGroup`/`FleetUsageSummary`) only ever counted
  episodes with `usage_completeness == "unavailable"`; an individually
  `"partial"` episode contributed to neither the group nor fleet-wide
  qualifier, so a group or fleet made entirely of partial episodes read as
  plain "measured". Added `usage_partial_count`/`usage_partial_episode_count`
  and folded them into `usage_availability` at both levels.

### Changed (SPA intuitiveness pass)

- **Plain-language evidence panel.** The right-hand panel's headings and the
  causal-strength card read in plain words now, with the precise term kept on
  hover and in the glossary: "Situation & action — source" → "What the agent
  did", "Consequence & outcome" → "What resulted", "Interpretation boundary" →
  "Fact vs. interpretation", "Provenance" → "Where this came from", and the
  "Attribution" trust card → "Cause" (the glossary and orientation overlay now
  read "Cause … (attribution)" so the controlled term stays discoverable).
- **The header run position is a stepper.** "Run 1 of 12" in the run header is
  now walkable: `‹`/`›` step through the current queue run-by-run without going
  back to the queue list (the arrows disable at the ends). It complements the
  bottom bar's "Next unhandled", which skips already-handled runs.
- **One coherent run view-bar.** The run's navigation used to mix chapter tabs,
  a Trace tab, a "More analysis" dropdown, and a right-floating Source button on
  one rail with no grouping. It now reads as two clear zones on one bar: the
  three review chapters as tabs on the left (Overview · Key moments · Checks),
  and every other way to view the run — Trace · Source · More analysis · Compare
  — gathered into one tools cluster on the right. The tabs now mean "the review";
  the tools sit together.
- **One "Compare" control.** The three separate, similarly-named comparison
  entry points — "Compare with a passing run" (in the tab row), "Compare
  reviewer outputs" (a reviewer-selector chip), and "Compare versions" (the app
  bar) — are gathered into a single `Compare` menu beside the chapter tabs:
  Against a passing run · Between reviewers · Across versions. Comparison is now
  one idea in one place. Each destination and its keyboard shortcut are
  unchanged; the reviewer chips still switch which snapshot is served.
- **A run reads in one pass.** A run now opens on the Overview summary by
  default (was: the first key moment), so a first-time reader lands on the
  synthesis — what happened, the main finding, the final state — instead of
  dropping into moment 1. The fast repeat-triage path is still one switch away
  under "Open at → First key moment", and that choice is remembered.
- **Persistent plain-language verdict.** A one-line verdict now sits directly
  under the run title on every chapter — e.g. "Failed — 1 of 1 checks failed:
  C1 (Binary exists at /app/bin)." It mirrors the Overview "What happened"
  logic and only restates validated check facts (no invented impact or intent).
- **Primary chapter navigation reads as a tab bar.** Overview / Key moments /
  Checks / Trace now sit on a shared underline rail with the active chapter
  marked by an underline; the per-chapter status square was replaced by a small
  status glyph (viewed / corrected / unavailable) so the strip no longer looks
  like a row of checkboxes. Structure and class names are unchanged.
- **Responsive appbar.** On narrow screens the labelled global-nav buttons
  (Runs · Patterns · Compare versions · Glossary · Help · Theme) collapse into a
  single "⋯" overflow menu instead of overflowing and clipping; the brand text
  no longer wraps and the breadcrumb trail is dropped first. The header stays a
  single 60px row so the panel overlays keep their offset.
- **Evidence panel peek.** When closed on narrow screens the panel now slides
  fully off-screen, leaving only its vertical "Evidence" tab handle — the
  reading column is no longer covered by stubs of the panel's text.
- **Theme choice persists.** The light/dark toggle is remembered in
  `localStorage` and reapplied before first paint, so a reload no longer resets
  it (and no theme flash on load).
- Added a favicon (inline SVG), removing the console 404 on every page load.

### Changed (workflow UI revamp)

- **Runs / Patterns / Compare versions are workspaces, not a sidebar glued to
  whatever run is open.** These three destinations now render full-width with
  no persistent queue sidebar or evidence panel — reclaiming the space those
  panels occupied — while opening a run still switches into the familiar
  three-pane investigation shell. A new `body.workspace-mode` toggle drives the
  layout; `#main.wide` widens the ~900px reading column the run-review
  chapters otherwise keep for prose.
- **Browser Back/Forward move between actual destinations.** Chapter, moment,
  filter, and evidence changes still use `history.replaceState` (no history
  spam per click), but switching between Runs, Patterns, Compare versions, and
  a given run's investigation now pushes a real history entry, so Back/Forward
  restores the destination a reader actually meant to leave, not just the last
  query-string edit.
- **A full-width Runs table.** The triage queue is now also a dedicated table
  surface with task/run identity, outcome, main finding, review status,
  duration, and cost columns; a client-side search over task name and run id;
  and explicit filter chips for passed/undetermined outcomes and
  in-progress/handled review status (previously only failed/needs-attention
  and unreviewed were reachable as chips). Returning from a run restores the
  search text, filters, sort, and scroll position.
- **Cost/duration sort no longer conflates "unavailable" with "free."** A run
  with no captured cost or duration now sorts consistently to the end of that
  sort, instead of tying with (and, once negated for descending order, sorting
  ahead of) a run that genuinely measured zero.
- **Patterns groups are inspectable, not just summarized.** Each group's row
  now expands a "representative episodes" drilldown (which run, how it
  resolved, which signature tier) and, when grouped by tool + error together,
  an "argument shapes" drilldown reading item 31's key/type distribution among
  the group's failing calls — both were computed by the backend already but
  had no surface in the fleet view.
- **The Overview chapter's main finding is now the most prominent element on
  the page**, not the task identifier above it: the run-header title shrank to
  a label and the main-finding headline grew, toned by polarity like the
  existing plain-language verdict. A new "View evidence →" action opens Key
  moments and focuses the evidence panel on that finding in one click, instead
  of requiring a separate manual step after "Open in Key moments."

### Changed (Runs / Patterns / run-review redesign)

A follow-up design pass across the three surfaces above, prioritized
Overview + evidence first (the run-review synthesis a reader sees before
anything else), then Patterns, then Runs.

- **Tool-failure findings name the tool and its own diagnostic**, not just an
  anonymous event id: a moment's summary now reads e.g. `shell failed:
  ModuleNotFoundError: No module named 'numpy'` instead of "a tool failure at
  evt_012 was left unresolved before submission." `RecoveryEpisode` carries a
  new `failure_diagnostic` field — the same failure text `error_signature`
  already derives its (masked, cross-run-groupable) signature from, kept
  UNMASKED so a single run's finding shows the real numbers/paths a reader
  wants, not `<N>`/`<PATH>` placeholders. Both `IgnoredToolFailure` and
  `SuccessfulRecoveryViaStrategyChange` carry `tool`/`error_signature`/
  `failure_diagnostic` through to the rendered card.
- **Tool failure, recovery, and task outcome are visually separate claims.**
  The Overview chapter and each tool-failure moment card now show a dedicated
  "Recovery:" line — "Confirmed" or "Not observed before submission" — that
  never states or implies the run's eventual task outcome; task outcome is
  its own "Task outcome:" line read from the same reconciled narrative the
  header verdict uses. The old always-present, always-generic "What
  happened" sentence and a separate "Main finding" card are now one card:
  the specific, evidence-backed finding headline leads, with a single
  "View {Tool} request and response →" (or "View evidence →") action.
- **The full-trace drawer shows a tool call and its result together.** A
  synchronized panel for a `tool_result` now also shows its paired
  `tool_call` request (and vice versa) beneath it — matched the same way
  `agr._util.paired_call`/`paired_result` already match them elsewhere, now
  exposed per source step as `paired_step_id` — instead of requiring a
  second click to see the other half of the same exchange. Panels with
  nothing to say about the selected step collapse to their header instead of
  a permanent "—" body, and the capability badges row moved behind a
  "Capture capabilities" disclosure instead of occupying space above every
  trace opened.
- **Patterns leads with a compact overview and compact rows.** A new stat
  row (affected runs · episodes · recorded usage, large numbers formatted as
  `203.1M`) and one coverage line ("Recorded usage is partial — …") replace
  several always-visible paragraphs about the counting methodology, which
  moved behind a "How this is counted" disclosure. Each pattern's row is now
  Pattern · Affected runs · Episodes · Recovery · Recorded usage; repeat
  rate, avg turns, wall time, argument shapes, and representative episodes
  moved into a per-row expandable detail so opening one pattern's examples
  never reflows the others. A pattern's title is now derived from its
  diagnostic text (e.g. `bash — ModuleNotFoundError: No module named
  '<STR>'`); a group where every episode's signature came from the opaque
  fallback tier (no traceback, no recognised diagnostic marker — the bare
  `---`/`}`/`===` case) is labelled **Unclassified tool failures** instead
  of presenting that raw text as if it identified a real common cause — the
  raw text is still shown, just not as the headline.
- **Session identity reads as a name, not a UUID.** The Runs table's
  identity cell now shows the captured task as the title, one shortened
  session-id fragment with a copy button (Patterns' representative-episode
  links use the same shortened, copyable form instead of the full
  `namespace/task__uuid` string), and — when the source captured it — the
  working directory (`RunSource.cwd`, currently only the Claude Code
  adapter) and a relative capture time.
- **Runs finding cells truncate at a sentence/clause boundary**, never
  mid-word or mid-enumeration; the full statement is unchanged and still
  shown in full once the run is opened.
- **Filter chips are grouped, not a ten-chip wall.** The shared filter row
  (Runs table and the investigation sidebar) now reads as two labelled
  groups — Outcome, Review status — with the remaining behavioural flags
  (Needs attention, Recovered, Plausible recovery, Verifier concern) behind
  a "More filters" disclosure that opens itself when one of those is active.
  The "Undetermined" chip is relabelled "Undetermined / Unverified" since it
  already matched both outcome statuses; each run's own badge still shows
  the exact one.
- **Cost/Duration columns disappear when the current run set never captured
  either value**, instead of a column of permanent "—" cells; a missing
  value is still never shown as a misleading 0.

### Known gaps carried forward

- **AGR-15** — no real `.gold.json` labels exist yet; annotation is a
  separate, ongoing workstream (see `gold/real/README.md` and
  `experiments/terminal_bench/annotation-protocol.md`).

## [0.1.0]

Initial deterministic-core release: ingestion adapters (Harbor/Terminal-Bench
2.0, pi sessions, Claude Code traces), the deterministic reviewer envelope
(Stages G/H/I), the evidence-browser SPA, fleet and sibling-divergence views,
matched version comparison, and the optional Stage F model reviewer.
