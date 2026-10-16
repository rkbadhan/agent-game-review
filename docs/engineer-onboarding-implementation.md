# Engineer onboarding: implementation and verification

This implements the local engineer workflow and repeat-use surfaces from
[the product spec](engineer-onboarding-and-connectors-spec.md). Hosted accounts
and collaboration remain a separate product phase. The local display name
provides attribution; it is not authenticated identity.

## Engineer journey

Start `agr serve` and open the printed loopback URL. An empty workspace offers
project creation and a bundled example. Returning users land on Home, with
their project, recent imports, saved actions, and a link to continue reviewing.
Existing CLI stores appear as **Existing runs** without migrating their captures.

**Add runs** selects a source and files. An engineer can inspect and deselect
files before transfer, then preview logical runs, available evidence, missing
evaluation results, warnings, duplicates, and invalid inputs before importing.
Advanced fields supply task/configuration identity, instructions, or one
explicit evaluation sidecar. A single sidecar cannot be applied across several
logical runs. Harbor discovers its own per-trial sidecars.

Import progress survives page refresh. Mixed-success imports let the engineer
open completed runs and retry unfinished work. Import history includes links
to original files. A batch can open a filtered Runs view whose run navigation
stays within that batch. Patterns and Compare versions use the selected project
and preserve existing matching and evidence rules.

An engineer can save a next action from a run and reopen it from Investigations.
Saved actions pin the capture and optional moment, retain revision history, and
open evidence read-only. Changing status does not move the evidence reference
to a newer capture. Open latest capture resumes editable review.

## Supported sources

| Source | Browser input | Important boundary |
|---|---|---|
| Harbor / Terminal-Bench | Trial/job folder, trajectory JSON, ZIP | Trial discovery associates verifier/result files; rollups are auxiliary inputs. |
| Claude Code | Saved session JSONL, stream JSONL, final-result JSON | Final-only output is limited; session completion does not establish task success. |
| pi | Session JSONL or deliberately selected session folder | Only selected uploads are read; missing evaluation evidence stays unverified. |
| OpenTelemetry | OTLP JSON containing `resourceSpans` | Partition by trace ID before conversion; this does not start a collector. |
| Langfuse | Trace JSON with observations/scores; browse and batch fetch from a connection | Live access reads the server's version from the unauthenticated `/api/public/health`: Langfuse 4 and later use observations v2 and scores v3 with a bounded UTC time range, and Langfuse 3.x uses the v1 trace endpoints (`/api/public/traces` to list, `/api/public/traces/{id}` to fetch a whole trace). Browsing lists root observations (v4) or traces (3.x); up to 500 traces are listed and 100 previewed at once. |
| LangSmith | Run-record list, or `{runs: [...], feedback: [...]}` | Partition by trace ancestry/identity; unrelated unscoped feedback is excluded with a warning. No live API connection. |
| Custom ATIF | AGR ATIF-shaped document | Validate the existing contract; arbitrary JSON is not accepted. |

Source selection is explicit. The browser does not crawl home directories.
Folder pickers transfer only checked files and preserve relative paths; ordinary
file selection and ZIP provide fallbacks. Existing adapters and CLI commands
remain the source format authority. See [adapters](adapters.md) and
[Terminal-Bench guidance](terminal-bench.md).

Langfuse credentials remain in server memory. Environment keys are available
only when the connection endpoint matches `LANGFUSE_HOST` (scheme, host, port,
and base path), or the CLI's default `http://localhost:3000` when `LANGFUSE_HOST`
is unset or empty; this restriction also applies after a server restart. Both keys
must be supplied explicitly for other hosts. Keys are never serialized in workspace metadata. Disconnect removes
connection metadata and session keys while retaining imported evidence.
The UI connects, reads the server's Langfuse version and tests access, then lists traces in a UTC time range
(optionally narrowed by trace name, tag, user, session or environment). Langfuse
v4 has no trace-list endpoint, so candidates are the root observations returned
by observations v2, grouped by trace ID; each filter is re-checked locally on
the fields returned (a filtered field the response omits excludes the trace). The engineer selects up to 100 traces (already-imported
ones start unchecked), and AGR retrieves them into one immutable preview, four
at a time. A trace that fails to fetch is reported beside the preview and does
not block the others; authentication and redirect failures stop the batch, as
does an unreachable host until something has been fetched. After that, three
unreachable traces or a rate limit stop the batch but keep the traces already
fetched. A trace that is too large, or whose ID cannot be fetched, is skipped on
its own, and a batch has a four-minute limit with traces not reached reported. The limit is checked before each fetch and between pages, and each request is also capped by what remains of it (score retrieval may run at most 30 seconds past it), so a batch ends shortly after four minutes. At most two batch fetches run at once per server; another returns a busy error, and previews abandoned while the server runs are expired after 24 hours. Time bounds are sent as UTC with milliseconds and a Z suffix, the only form Langfuse documents. Langfuse documents that data from older SDKs and exporters can take up to 15 minutes to appear, so a trace from the last 15 minutes carries a capture note saying it may be incomplete. A search that uses filters also sends the time range and root-observation flag inside the filter and re-checks the time range locally, because Langfuse documents that the filter overrides the fixed parameters. A trace that Langfuse returns in a shape AGR cannot read fails on its own.
Traces are fetched
for the search window extended by one day (never past now) so a run that
outlived the window is not cut off. The listing also carries the fields Langfuse's own trace table shows (release,
version, latency, cost, input and output previews, metadata) wherever the server
returns them, and can be filtered by trace ID (matched anywhere in the ID, over
the last 400 days rather than the form's range) and by one metadata key and
value. On Langfuse 4 each listed row is the trace's root observation, so Latency, the metadata shown and the
metadata filter describe that observation, not the whole trace (Langfuse 3.x returns trace-level values).
Cost is summed over every observation of the listed traces through `GET /api/public/v2/metrics` (view
`observations`, dimension `traceId` filtered to 50 IDs per request and ordered descending by the summed
cost as the server's top-N rule requires, measure `totalCost` summed, result key `sum_totalCost`; shapes
recorded in `server_contract.json` from the server source; the requests of one listing share a 30 second budget). If that
endpoint is unavailable the root observation's cost is shown and the Cost header says so. The imported trace
always carries every observation. The
connection records the Langfuse project the key pair belongs to,
from `/api/public/projects`, when the server reports it. A single trace ID can
still be fetched directly. The import imports that snapshot without refetching on confirmation.
After an import, **Review imported runs with AI** opens AI review with that
batch preselected; no model call happens until the engineer starts it. Time-scoped
retrieval is explicitly partial; observation completion never fabricates a
task outcome. Score retrieval failures produce an evidence limitation.
Scores are fetched by trace ID without restricting their creation timestamps
to the observation window, so later evaluator or human scores are retained.
Langfuse documents that timestamp filters refer to the score itself in its
[Scores API](https://langfuse.com/docs/api-and-data-platform/features/scores-api).

Current endpoint contracts are documented by Langfuse's
[Public API](https://langfuse.com/docs/api-and-data-platform/features/public-api)
and [API migration guide](https://langfuse.com/faq/all/deprecated-api-migration).
The existing legacy CLI fetcher remains available for older deployments.

## Storage and execution

The configured default store contains `.workspace/workspace.json`, project
stores, preview records, connection metadata, saved investigations, persistent
job records, and staging/provenance files. Project IDs are opaque generated
values. Settings shows the actual store path for project-targeted CLI commands.

Workspace JSON writes use temporary files, flush/fsync, and atomic replacement.
A workspace lock serializes mutations across threads and processes. A single
background worker processes import items. Per-run index locks are shared with
CLI ingestion, and browser publication re-reads the live index after analysis
so concurrent CLI captures keep their revisions. An item is analyzed in an isolated
store; completed capture files publish before the run index. An incomplete
analysis is never listed as a completed run. Failed items retain original files
and can retry analysis without another upload or remote fetch.

Import confirmation binds a selected-item list to an immutable manifest hash
and an idempotency key. Identical source hash plus adapter version is a no-op.
A Langfuse capture's source records the host, project ID and trace ID but not the
time range it was searched with or the project's name, so re-importing an
unchanged trace is a duplicate however it was found; only a change in the trace
itself (late observations or scores) creates a new capture.
New captures retain the prior revision chain. Conflicting task/source identity
is checked both at preview and publication, including competing previews.
Cancellation takes effect between committed items. A dead job owner's active
job is marked interrupted on startup and can retry unfinished items. Ownership
includes a server-incarnation token and process birth identity so recycled PIDs,
including a restarted container's PID 1, cannot keep an abandoned job active.
An alive foreign process is retained when either recorded or current birth
identity is unavailable; missing identity is not proof that the worker died.
Analysis releases the global workspace lock. Cancel and project settings respond
during analysis; the current item may finish, and cancellation skips remaining items.

Unsubmitted previews expire after 24 hours and their staging files are cleaned
on server startup. Submitted previews and original uploaded files remain local
as import provenance and retry inputs, including uploaded auxiliary files.
No retention-management UI or automated deletion policy is included yet.

Request headers scope existing review routes to a project, and saved-evidence
reads to a specific capture. Cache keys use the concrete selected store.
Existing writable servers bound to other interfaces and reverse proxies remain
supported; the default CLI bind still uses loopback. Cross-origin mutations are
rejected, while a browser's same-origin request metadata accommodates proxy
Host/scheme rewriting. Sample/archived projects and pinned
evidence snapshots reject review writes. Public read-only sample servers retain
their read-only behavior. This boundary does not provide hosted authentication.
The existing-store project and legacy API routes share one Store instance, so
project writes invalidate cached legacy reads immediately.

Saved evidence headers pin only the route's target run. Sibling discovery and
queue navigation continue reading other runs at their latest captures. New
investigations require the displayed capture ID and validate any selected moment
against that capture; an import arriving before Save cannot move the evidence.
Disconnect checks are performed again after remote fetches so an in-flight
request cannot recreate a deleted connection.

## Remote access and server credentials

Writable servers accept loopback Host names by default. `--allowed-host` adds
an exact browser or reverse-proxy host name; wildcards are rejected. A specific
`--host` bind address is also allowed. Unknown or malformed Host headers are
rejected before routes execute, including matching-Origin DNS-rebinding requests.
Host validation follows the boundary described by
[Starlette's TrustedHost middleware](https://starlette.dev/middleware/#trustedhostmiddleware).

Non-loopback binds, remote clients, and allowed non-loopback Host names require
a bearer session token for API reads and writes, even when Origin is absent.
`agr serve` generates a token and prints a launch link. Set `AGR_SESSION_TOKEN`
to supply a stable private token when needed. For example:

```sh
agr serve --host 0.0.0.0 --allowed-host review.example
```

Open the printed `#agr-session=...` link, using your HTTPS proxy URL when behind
a proxy. The browser removes the fragment and retains the token only in that
tab's session storage. It sends Authorization for API calls, uploads, fleet
pagination, and original downloads. Sharing a review URL does not share the token;
another reviewer must first open their server session link. CLI clients use
`Authorization: Bearer <token>`. Opening a run in a new tab or following a shared
link may require opening the session launch link in that tab first. Generated
tokens change on every server restart; reopen the newly printed link in each
affected tab. `AGR_SESSION_TOKEN` keeps the configured token stable across
restarts. Treat session links as credentials and use HTTPS for remote connections.
Reverse proxies must forward Authorization. Configuring any non-loopback
allowed host also requires the token on a rewritten loopback upstream. Set
`require_session=True` when constructing an app directly behind a proxy without
an explicit remote host configuration.

Origin checks, snapshot/sample/archive write protection, and public read-only
demo behavior remain in place. Session tokens grant access to the whole local
workspace; they do not implement hosted accounts or per-user permissions.
If workspace settings fail to load, existing-store shared run links fall back to
legacy review routes with project controls disabled. Authentication errors and
links to other projects never use this fallback.

Regression tests cover no-Origin remote access, matching-Origin rebinding,
proxy token enforcement, environment endpoint binding at connection creation
and after restart, malformed snapshot IDs, cancellation/project switching during
analysis, cache reuse with one pinned run, and missing foreign process identity.
Real Chromium tests cover corrupt settings and authenticated upload/import,
original download, reload, and launch links entered in an already loaded tab.

## Limits

| Resource | Limit |
|---|---|
| Individual input file | 100 MiB |
| ZIP upload | 500 MiB |
| Staged batch / expanded archive | 2 GiB |
| Uploaded entries / archive entries | 10,000 |
| Logical runs per preview | 1,000 |
| Spans per logical trace | 100,000 |
| Langfuse combined response | 100 MiB |

Archive extraction rejects traversal, absolute paths, links, special files,
duplicate filenames, and unsafe Windows names. Limit violations reject the
input rather than truncating evidence. Live requests have network timeouts,
pagination-cycle checks, trace-identity checks, and a two-minute budget checked
between pages. Credentials are not forwarded through redirects.

## Verification and remaining release gates

At `bbf506b`, after both PR correctness reviews, the full Python suite passed with **1,293 tests
and four skips**, and all **71 workspace tests** passed. Workspace tests exercise all seven
source paths through import/review, project and cache isolation, sample behavior,
sidecars, mixed traces, manifest/idempotency, duplicates, capture revisions,
analysis failure/retry, cancellation/restart, original-file retention, remote
and proxy request compatibility, reviewer attribution, and saved evidence pinning.
Mocked Langfuse tests verify paginated observations, score preservation, and rejection
of unrelated traces.

Regression coverage for the PR review:

| Finding | Fix and verification |
|---|---|
| 1. Snapshot routing | Bind the capture to one full run ID, including namespaced IDs; verify sibling discovery and next-run navigation. |
| 2. Remote access | Support configured remote/proxy hosts with session tokens while retaining cross-origin/read-only rejection. |
| 3. Harbor ZIP discovery | Descend excluded wrapper directories that contain trajectories; verify ordinary and deeply wrapped job ZIPs with sidecars and rollups. |
| 4. Reused PID | Track server incarnation and process birth identity; verify interruption and retry with an old owner sharing the current PID. |
| 5. Lost error specificity | Preserve redirect, size-limit, and multi-trace sidecar errors with their codes and statuses. |
| 6. Disconnect race | Re-check connection existence after both successful and failed fetches; verify neither can recreate a disconnected record. |
| 7. Lost CLI capture | Share per-run writer locks, re-read the live index at publication, and reconcile revisions; verify a CLI write during analysis and actual two-process locking. |
| 8. Wrong investigation capture | Send the displayed capture ID and validate it server-side; verify saving after another import retains the original evidence. |
| 9. Late Langfuse scores | Fetch trace scores outside the observation timestamp window; verify a later evaluation remains in the snapshot. |

Additional regressions cover immediate existing-store cache invalidation and
bounded Windows file-sharing retries while reading/replacing JSON records.
The Python browser CI suite passed **40 tests** locally. Legacy forensic tests
enter review explicitly; separate cases verify the default Home landing and a
fresh Welcome flow that creates a project and shows all seven sources. Chromium
fallback discovery supports modern Linux and Windows browser directory names.

The default-host follow-up passed **128 workspace/API/Langfuse tests**, including
unset and empty `LANGFUSE_HOST`, local connection creation and retrieval after
restart, and rejection of other credential endpoints.

`scripts/verify-onboarding.cjs` is an optional real Chromium gate against a
running disposable local server. It checks first use, project creation, file
preview/import, review, saved actions after a newer capture arrives, snapshot
header scope, pinned evidence, local identity, project
switching, reload, source catalog, runtime errors, and a narrow viewport. It
accepts `AGR_CHECK_URL`, `PLAYWRIGHT_MODULE`, and `CHROMIUM_PATH`; install
Playwright separately or point at an available installation. Run it against a
test store because it creates projects and imports a fixture.

The following gates remain before declaring the spec's release validated:

- **AC-22:** Test live Langfuse access on representative deployments with real
  account credentials. Network tests here use mocked responses and a local fake
  Langfuse server; the filter syntax used for tag and trace-name narrowing follows
  Langfuse's migration guide and is unverified against a live account.
- **Initial usability gate:** Observe five unfamiliar engineers completing
  own-data imports, distinguishing unverified outcomes, and identifying useful
  next actions. Browser automation does not establish usability or usefulness.
- **AC-38:** Keyboard-accessible controls, heading focus, progress labels, and
  alert messages are implemented. Complete a manual keyboard/screen-reader
  audit across source-specific edge cases.
- **AC-34:** Project-scoped comparisons are available through the existing
  Compare versions surface. A dedicated batch-to-comparison preset is deferred.
- Benchmark representative large captures and refine limits and streaming.
- Define artifact retention controls and richer source-specific export recipes.
- Follow up on streaming uploads, shorter parsing lock scope, fewer job-file
  scans, continuous preview cleanup, and consolidation of proxy-cache and
  Langfuse HTTP helpers. These performance/refactoring items remain separate.

Hosted login, memberships, authorization, and local-to-hosted transfer
(AC-36/37), automatic source sync, OS keychain persistence, and external issue
notifications are outside this local implementation.

## Langfuse API compatibility notes

The connector's requests were checked against the open-source Langfuse server's own source code
([langfuse/langfuse](https://github.com/langfuse/langfuse), commit `c900b11`, web package version 4.59.0), not only
its documentation. The checks that were run:

- Every filter the connector can send, for both APIs and every filter combination, was parsed with the server's own
  validation schemas (its `eventsTableSingleFilterList` and `singleFilterList`, run with zod 4). All were accepted.
- Each filter column was matched against the server's column mappings for the traces table (v1) and the events table
  (v2), and the field groups against the server's `OBSERVATION_FIELD_GROUPS_PUBLIC_API` and `TRACE_FIELD_GROUPS`.
- The server's handlers were read for status codes, limits, timestamp parsing and the health response.

What the server's source established, and what is still open:

| Point | Result |
|---|---|
| `GET /api/public/health` returns `{status, version}` (for example `4.59.0`) and needs no credentials | Confirmed |
| v1 list parameters (`page`, `limit` up to 100, `fromTimestamp`, `toTimestamp`, `orderBy`, `tags`, `name`, `userId`, `sessionId`, `environment`, `fields`, `filter`) and the `core`, `io`, `scores`, `observations`, `metrics` field groups | Confirmed |
| v1 filter columns `id`, `name`, `timestamp`, `userId`, `sessionId`, `environment`, `tags`, `metadata` (with a key), and the type and operator pairs the connector sends | Confirmed: accepted by the server's schemas and resolved by its column mapping |
| v1 trace fields: `latency` in seconds, `totalCost` in USD, `metadata`, `release`, `version`, `tags`, `input`, `output` | Confirmed |
| v2 parameters including `isRootObservation` (`true` or `false`), `fields`, `limit` up to 1000, `cursor`; unknown field groups are ignored | Confirmed. The earlier disagreement between documentation pages is settled: it is a parameter and a filter column |
| v2 field groups: cost is in `usage`, latency in `metrics` (seconds), trace name, tags and release in `trace_context`, user and session in `basic` | Confirmed |
| v2 filter columns `traceId`, `traceName`, `tags`, `userId`, `sessionId`, `metadata` (with a key), `startTime`, `isRootObservation`, `environment` | Confirmed |
| `GET /api/public/v2/metrics` with view `observations`, dimension `traceId`, measure `totalCost` summed, `row_limit` up to 1000, result key `sum_totalCost` | Request shape confirmed against the server source and its query validator: a trace-ID dimension is high cardinality, so the server accepts it only as a top-N, with an explicit `config.row_limit` and an `orderBy` descending on a measure of the query (here `sum_totalCost`); otherwise it answers 400. The fake server in the tests enforces the same rule. Not run against a live server |
| `isRootObservation` is true for observations the SDK marked as app roots, which keep a parent ID | Confirmed. The listing trusts the flag and only falls back to "no parent" when it is absent |
| Timestamps: `...Z` and `+00:00` are accepted; a naive time, a space separator and `+0200` are rejected | Confirmed |
| On a **self-hosted** server, observations v2 answers 404 unless `LANGFUSE_MIGRATION_V4_ALLOW_PREVIEW_OPT_IN=true` | Confirmed. The v1 endpoints are used instead, whatever version the server reports |
| In `events_only` write mode the v1 trace endpoints answer 404 | Confirmed. The v2 endpoint is used instead |
| `latency` and `totalCost` are `-1` when not computed | Confirmed. Shown as unknown |
| Cloud plans clamp `fromTimestamp` to the plan's data access window | Confirmed. A trace ID search over 400 days is clamped on a plan with a shorter window |
| The server itself running against the connector | **Not run.** It needs Postgres, ClickHouse and Redis. `scripts/verify-langfuse-live.py` runs the same checks against any real server |

The recorded contract is `tests/fixtures/langfuse/server_contract.json`; a test checks every request the connector
makes against it, so a change to what is sent cannot drift from what the server validates.


## Langfuse onboarding recovery and evaluation semantics

Saved connections expose current credential availability. After the AGR server
restarts, connections without matching environment keys show `needs_keys`.
Reconnect replaces credentials on the existing connection, verifies access and
rejects a key pair for another known upstream project. Keys remain memory-only.

New imports preserve the original trace name as `display_name`, separately from
stable task/run IDs. Source host, upstream project and trace identity support an
Open in Langfuse link. Bounded observation retrieval is shown separately from
telemetry capability coverage and score availability. Preview summarizes batch
coverage and common capture notes; individual rows expand for exceptions.

Adapter 0.4 retains Langfuse scores as typed metrics in immutable `source_scores`,
including their subjects and evaluator provenance. It no longer guesses task
pass/fail from boolean, numeric or categorical score values. Supply an explicit
verifier results sidecar via file import to define task checks. Existing captures
remain immutable; re-import to create a capture with these semantics. No model
review or automatic source sync is started by importing.


Validation commands and current results are recorded below. Tests use local
simulated services; live customer Langfuse credentials were not used.


## Onboarding navigation and interaction behavior

Source selection, capture browsing and each server-side preview have distinct
browser history destinations. Preview URLs carry project, source, connection
and preview IDs so reload can retrieve the existing preview. Session storage
keeps selected preview item IDs and return scroll positions; credentials and
trace bodies are excluded. Expired or missing previews offer a new search or
file selection. Reloading the browse step restores the saved connection, while
trace-list contents must be fetched again; large trace payloads are not cached
in browser storage.

Back to traces restores the in-memory list, selection, filters and both page
and table positions, returning focus to the preview action at that position.
The source heading, connected search form, search results
and preview summary receive focus on transition. Late preview responses cannot
replace a newer navigation destination. Search initially exposes time, name and
environment; the other filters and result limit are under More filters.
Connection facts collapse after setup, and the next search action is brought
into view. The duplicate numbered connection/search/selection steps are removed.

Requests show a specific progress label beside the initiating control and mark
the working surface busy. Sidebar destinations remain available, and finishing
a request does not pull the engineer away from a newer destination. Project
switching remains disabled during a workspace request. File transfer progress
only repaints its originating page.

At mobile widths the menu moves focus inside, makes the background inert, traps
Tab, closes on every navigation destination, and restores the opener on Escape,
the close button or the shade. Escape also works from editable controls.
Review tabs expose tab/tabpanel semantics, selected state, a single tab stop,
and Arrow Left/Right, Home and End navigation. Source view is a separate browser
history destination, so Back returns to the review.


The browser regressions cover preview reload selection, Back/Forward, return
position, expired previews, visible request feedback, navigation during a
delayed request, late preview responses, mobile menu focus and dismissal,
and review-tab keyboard behavior. Desktop and mobile layouts were inspected
at 1440x900 and 390x844. Real upstream latency remains untested.

The CI browser gate includes Langfuse browsing and AI-review journeys alongside
the review and demo suites.
The eight demo regressions also passed, including the mobile story browser.


## Stable refresh and recovery contracts

A failed project lookup keeps the previously verified Langfuse project binding.
Reconnect requires both a known saved project ID and a matching upstream ID;
unknown bindings cannot be reassigned by replacement keys. Fetched trace or
observation project IDs take precedence over connection metadata for capture
provenance, including Langfuse 3 trace responses.

Scores are sorted canonically before they enter the immutable source document.
Score ordering, search-window endpoints and project display names do not change
capture identity. Actual changed scores or captured observations do. A test fake
that enforces the observation window covers genuinely narrower captures, in
addition to the v3/v4 unchanged-refresh regressions.

A score-fetch failure on an existing run leaves its preview item unchecked and
explains why. Engineers can retry fetching, or explicitly select the degraded
capture. This avoids replacing a good capture by default; it does not rewrite
existing revision history or change content-addressed duplicate semantics.

Workspace requests are isolated by project and view. An action on Sources can
run while Add runs is fetching; a late error is announced for its originating
view rather than appearing on a different page. Help and Glossary focus their
selected tab, contain keyboard focus and restore the opener on dismissal.

Submitted previews reopen as a notice linking to the existing import. Equivalent
submissions with a fresh request key reuse the original job; changed selections
require a new preview. Preview session state is removed on submission or expiry,
ages out after a day, and is capped at 50 entries.

Langfuse v2 capture fetches omit unused observation metadata and user IDs; trace browsing
retains fields needed for its visible filters and details. Imported source
context accepts only host/project identifiers, excludes arbitrary labels, and
trace links display their actual hostname. Self-hosted destinations remain
supported; an uploaded host is not an independently verified connection.


Reproducible validation for the refresh/recovery changes (with API/test/browser
dependencies and Chromium installed):

```sh
python -m pytest tests/test_ingest_langfuse.py tests/test_langfuse_browse.py tests/test_workspace.py tests/test_langfuse_api.py tests/test_span_tree.py tests/test_adapter.py
python -m pytest tests/test_ui.py tests/test_langfuse_browse_ui.py tests/test_ai_review_ui.py
python -m pytest tests/test_no_internal_spec_refs.py tests/test_outcome_status_coverage.py tests/test_review_status.py
```

Results: 357 backend tests, 70 browser tests, and 39 static safeguards passed.
All 26 JavaScript modules parse and the diff passes whitespace checks. The
backend fakes cover Langfuse 3/4, shuffled scores, failed project lookup,
unknown-project reconnect, degraded refresh, submitted-preview reuse, and
observation windows that actually omit spans. The services and model reviewer
are simulated; real accounts, live latency and screen-reader behavior were
not exercised.
