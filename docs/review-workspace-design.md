# Review workspace: implementation and validation

17 October 2026 · Implemented after product benchmarking and explicit user authorization · Usability study not yet run

The primary job is to triage a collection of traces, inspect the evidence, record a decision, and move to the next run without losing position. The queue should own the review workspace's left column, rather than receive the space left under application navigation.

## Existing product baseline and implemented changes

The current Runs page is the baseline: Session, Outcome, Main finding, human Review status, AI review, optional Duration/Cost, search, grouped filter controls, sorting, 50-row paging and return-position restoration. The review stepper and N already use a stable `queue_view_id`. These are existing features, not benefits attributed to the concept.

The first commit added redacted Langfuse input excerpts and named views to that page. Awaiting review means human `unreviewed` or `in_progress`; Failed checks uses the existing failed-outcome filter. Custom definitions now store search, filters, sort, grouping and AI status per project in this browser. Benchmark instructions remain shared task context, so Harbor-only worklists keep their existing columns. Stable task names remain in the sidebar; reject the conditional time-first layout switch. Keep the independent Home/pinned-filter fixes.

The production inspector now opens from a row, showing the main finding, linked evidence, bounded redacted input/output, captured metadata, and current human review state. Inspection performs no workflow write. Open full review displays the fetched evidence immediately and moves unreviewed work to in progress in the background; queue refresh does not block display. A disposition waits for that opening write before using its workflow version. Mark reviewed requires a disposition and advances to the next unreviewed run in focused mode. Unverified/undetermined outcomes are neutral throughout; operational errors remain a warning, distinct from agent failures.

Start review snapshots the exact selected or filtered run IDs in display order, including search, AI state, pattern/import scope, sort and optional grouping. The focused list, stepper and N share that snapshot. The Next API checks live human state against explicit order slices without changing it. Both worklist and focused queue initially render 50 runs per group; a distant focused selection opens its containing window. Find searches the complete queue and leaves its saved order intact. Queue details explain the originating view; returning to Runs restores the original worklist and inspector.

Named views and the twenty most recent focused queue snapshots are saved per project in this browser. Queue links restore here after reload; another browser or cleared storage receives an explicit recovery message and a route back to Runs. Storage failures retain the current queue in memory and explain the reload limitation. Queue-load failures preserve the current inspection, show Retry, and prevent starting a queue from stale results. These are browser navigation definitions, not shared server queue records.

Home now leads with Continue review or Review pending runs and shows human backlog, retryable AI jobs, running AI jobs and saved next actions when present. Add runs and AI configuration are secondary once runs exist. Read-only demos allow browsing and focused queues while disabling workflow changes. Human dispositions retain optimistic version checks, and asynchronous selection/write guards prevent late responses from changing a different run. Leaving a pending run restores the previous loaded selection; late successes and failures cannot reopen it or pair old findings with the pending ID.

Langfuse adapter 0.5 retains root input/output and captured userId, plus whether a task assignment was explicit. The inspector redacts those values before returning previews. Existing captures remain readable; re-importing with the newer adapter is required to obtain metadata that older captures did not retain. Re-importing an adapter 0.4 trace creates a new immutable capture revision because the adapter version and source document changed. Old captures stay unchanged and the inspector explains re-import for those missing fields.

## Usability comparison (not yet run)

Recruit five engineers who regularly review Langfuse traces. Compare the pre-change Runs page, commit `4fc9e28` (input/views), and the implemented workbench/inspector. Rotate condition order across participants and use equivalent task sets so no condition always benefits from practice. Include repeated names, mixed outcomes, missing inputs, handled/unreviewed/in-progress states, and a dataset exceeding 50 rows.

Tasks: find a specified input, identify a failed check, distinguish an unverified task from an unreviewed run, record a required disposition, and resume a deep-list position. Predeclare acceptance: at least four of five participants complete each task unaided; zero wrong-run dispositions or exposures of redacted content; no confusion between human review and task outcome. Use a median task-time improvement of at least 20% over the pre-change Runs page, without reduced accuracy, as the design-performance target. Implementation alone does not establish that this target has been met. Record time, completion, errors and assistance; report per-participant results alongside medians. These are proposed thresholds, not measured findings.

## Recommended structure

The [product benchmark](review-ux-benchmark.md) revises the initial recommendation. Start with a Runs workbench: labelled application navigation, a broad searchable worklist, optional saved views, and an inspector that opens beside the selected run at desktop widths and as a dedicated view below 1100px. Keep input/context, outcome, finding, and human-review state separately scannable. Optional columns can expose captured session/user/time/configuration; do not manufacture missing values.

Allow the inspector to expand into focused review. That mode gives the queue a full-height column below the project bar and keeps evidence and review actions in the main area. Returning to the workbench restores its selection, filters, sorting and scroll position. “Start review” uses the selected runs or the current filtered result, with an explicit queue count.

The existing rail/top-navigation mock-ups remain exploratory focused-review concepts. Their local browser checks do not validate them as the preferred product design. The research does not establish a navigation winner. Compare labelled navigation for discovery with a compact shell for sustained review; assess task completion and discoverability, not only available row height.

For the focused-review concept, keep the short-window capacity target: search, Filter/count and at least six complete two-line rows at 1280×600 with filters closed, and at least three with filters open. Keep the evidence panel collapsed until requested. Below 900px, queue and run become separate views with Back to queue, preserving selection, filter, group expansion and position. At 390px, actions must remain reachable without horizontal scrolling.

## Queue: scan for meaning, then open evidence

Default to an ungrouped worklist. Offer task/workflow and session grouping when those relationships help the user. For optional task groups in ordinary captures, use the source's meaningful task identity. For Langfuse captures whose task IDs are generated from trace IDs, use the captured trace name, scoped to the source connection/project. Do not group those captures by their generated task ID. Explicit task assignments take precedence. Importing a differently named trace adds a group; it never changes existing row layouts.

When grouping is enabled, a group heading shows its name and matching count and is a native collapsible control. Open the selected run's group automatically. Remember expansion per project/group, and preserve it through filters. A long name wraps in the heading and remains available in full in run details.

Each row has:

1. A compact context line: recorded capture time and session/user when available; otherwise the recorded model/configuration. Include the labelled task outcome and an independent review-state circle.
2. A meaningful summary: a linked failed check name, a recorded main finding, or a short input excerpt. Do not infer task failure from an execution concern. Display a recorded success finding when appropriate; use “No finding recorded” when evidence is absent.

IDs and step counts move to run details. When multiple rows have identical captured context and findings, retain a short ID as a last-resort discriminator. Never invent timestamps, sessions, users, attempt numbers, or clearer findings than the evidence supports. Full findings and IDs remain accessible.

Filter/count and search stay outside the scrolling row list. An empty result says “0 runs match” and offers Clear filters. An unavailable source says “Couldn't load runs” with Retry, while keeping the last loaded selection visible. Do not present a load failure as an empty project.

Without grouping, the selected sort applies across the worklist. With grouping, group order is stable by task/workflow label and the chosen sort applies within each group. The stepper follows that same flattened order, including runs in collapsed groups. Review actions update counts without silently reordering the queue. A filter change creates a new queue; an open run excluded by it remains in the detail panel with an “Outside this filter” label. Clearing the filter reveals that run again.

For 1,000 runs, initially show 50 rows per expanded group and an explicit Load more action. Selection via stepper/search reveals the necessary window automatically. Search and counts cover the complete loaded dataset, including unloaded row windows. Measure this design with representative data before choosing production windowing or paging.

## Home: one next step, then attention items

With a saved position, the only filled primary action is Continue review, naming the last opened task and position. Without one, it is Review pending runs. Add runs becomes a secondary action in the project bar once runs exist.

Below that, show plain actionable rows for runs awaiting human review, AI reviews requiring retry, and saved investigations when present. Each opens the relevant work. Omit empty categories. Do not turn “Unverified” into a human-review backlog: task evaluation, AI execution, and human review are separate states. Show an empty project's explanation and a single Add runs primary action. When nothing needs attention, show completion and secondary links rather than manufacturing urgency.

Home progress comes from actual workflow records, not from how many traces have been opened. Resume position is separate per-project navigation state. Restoring a stale position offers the nearest remaining run and explains the change.

## Consistent status language

| Axis | Examples | Presentation everywhere |
|---|---|---|
| Task outcome | Passed / Failed / Unverified / Undetermined / Operational error | Success / failure / neutral / neutral / warning, with text labels |
| Execution finding | Recorded tool failure, unresolved check, recovery | Evidence-specific finding; does not change an unverified task into a failure |
| Human review | Awaiting review / In progress / Reviewed | Empty / partial / checked circle, with an accessible label |
| AI review | Not run / Running / Completed / Retry needed | Separate text; retry is an actionable warning |

An Unverified run header says “Unverified · No task evaluation” in the same neutral treatment as its queue row. No `0/0` appears without checks or tests. The review screen's primary action is Mark reviewed, always visible in a footer; Run AI review, Trace, and Source remain secondary. Its menu requires a disposition before changing human workflow state and never changes the captured outcome.

## Keyboard and recovery

- Tab follows native controls: navigation, queue search/filter, group headings/rows, then run actions. Enter/Space opens a row or toggles a group. Focus stays visible.
- Alt+Down / Alt+Up selects the next/previous run; existing J/K moment navigation remains unchanged. Ignore navigation shortcuts while editing text. Escape closes filters and restores focus to their trigger.
- The header's Previous/Next buttons have the same order as the queue. Changes reveal the selected row without stealing keyboard focus. Routine re-renders preserve the reader's scroll position.
- Selecting a run expands its group. Returning from Home restores the same run and queue position. Mobile selection moves focus to the run heading; Back to queue restores focus to its selected row.

## Review criteria and data fidelity

| Case | Must demonstrate |
|---|---|
| 1 run | No unnecessary group chrome; meaningful context; disabled stepper endpoints |
| 50 identical names | Meaningful input/context and findings, optional stable grouping, selected run 14, no `0/0` |
| Import one different name | Existing group and rows keep their shape; a separate group appears |
| 1,000 runs | Counts/search cover all runs; bounded initial rendering; navigating past row 50 reveals it |
| Long names | Full content remains reachable; no overlap or horizontal page scroll |
| No runs / no matches | Different copy and actions; one primary action at most |
| Load or AI error | Visible recovery action; current evidence and position preserved |
| 1280×800 / 1280×600 / 390px | Queue capacity, navigation, focus and controls remain usable |
| Light / dark / keyboard | Consistent outcome colours, readable labels and visible focus |

The default mock-up uses the 350 published leaderboard runs bundled in `agr/demo_fixtures/tb2_leaderboard`, with their real recorded outcomes, configurations and findings. They cover seven configurations and ten tasks. Their absent capture timestamps are left absent. The 1/50/1,000-run, long-name, empty and error views are explicitly illustrative layout scenarios. The Langfuse scenario's recorded tool-error wording is based on the bundled Langfuse toy export; session/user labels and workflow/AI states in that scenario are illustrative. They are not additional real captures.

The earlier mock-ups change local preview state only. The mock-up can test selection, filters, grouping, paging, review completion, Home routing and keyboard use; it cannot establish backend integration or real service recovery. No user testing has been claimed. The production implementation is now tested separately from those previews; its validation and remaining study work are recorded below.

## Earlier mock-up verification

Chromium checks passed for 12 groups of interactions, including both navigation alternatives, the 350 real runs, the 1/50/1,000-run scenarios, search across unrendered rows, filter recovery, keyboard movement, 390px/320px layouts, and light/dark appearance. The short-window mock-up fits six complete rows below the task heading with filters closed, and at least three with filters open. No runtime errors were observed. These checks validate the proposed local interactions and layout; service integration and user validation remain part of the later implementation work.

## Production verification

The implementation was exercised against ingested copies of the bundled Langfuse toy export and Harbor fixtures, plus the bundled public leaderboard demo. Browser journeys cover inspector retry and stale responses, exact filtered/selected queues, required dispositions, opening in progress, frozen Next order, reload and return, optional grouping, Home attention, mobile actions, and failed queue retrieval. A separate 1,000-row read-model fixture verifies bounded paging, search beyond rendered rows, and selecting a real capture at the end of a focused queue; it does not claim ingestion or usability testing of 1,000 real traces.

Visual checks at 1280×600 fit six complete queue rows with details closed and four with them open. Inspector and review actions remain reachable at 1280, 1024, 760, 390 and 320px with no horizontal page overflow. Light/dark views were inspected. No real-user study, production Langfuse account testing, or measured task-time improvement is claimed.


### PR review follow-up validation

Deterministically held workflow and queue responses verify that fetched review
content appears before either finishes. Tests also cover a quick disposition
using the opening write's version, a concurrent review conflict retaining the
other reviewer's decision, and a late opening response leaving the new run intact.
Loading replaces old findings and actions rather than rendering them invisibly; the evidence panel clears and Trace stays unavailable until the selected capture is loaded.

A local Windows benchmark over 300 synthetic read-model captures made from the
bundled Langfuse toy export (102,434 input bytes each) measured warm uncached
summary rebuilds at 1.431 seconds, versus 1.421 seconds with excerpts disabled
(best of three). The first cold scan was 40.360 seconds while browser verification
also ran: full-input reads/redaction still happen once per capture per process.
The cache retains only redacted excerpts of at most 240 characters, with a minimum
entry budget of 4,096 per concrete project Store. The entry budget grows to twice
that Store's largest observed run count, retaining a complete current scan plus
room for historical/pinned captures; weak ownership releases the cache with the
Store. New captures get new keys, pinned old captures
reuse their own entry, and mutable workflow writes or listing TTL expiry do not
invalidate immutable excerpts. These machine-specific timings establish repeated
work avoidance, not a cold-start latency guarantee or a real-trace usability study.


Follow-up cache review: full summary builds serialize per concrete project Store;
shared locks protect only map access. Full source reads and redaction run outside
global locks. A project exceeding the minimum cache budget retains its complete
current scan instead of churning a fixed-size LRU on every rebuild. Pattern-based
redaction masks email-shaped user IDs, while opaque IDs and UUIDs remain visible
in captured metadata; user IDs are not categorically anonymized.
