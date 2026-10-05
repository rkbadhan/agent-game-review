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
| Langfuse | Trace JSON with observations/scores; explicit trace fetch | Live fetch targets Langfuse v4 observations v2 and scores v3 with a bounded UTC time range. |
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
The UI requests a trace ID and time window, retrieves one immutable preview,
and imports that snapshot without refetching on confirmation. Time-scoped
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
Mocked Langfuse tests verify paginated observations, score mapping, and rejection
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
  account credentials. Network tests here use mocked responses.
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
