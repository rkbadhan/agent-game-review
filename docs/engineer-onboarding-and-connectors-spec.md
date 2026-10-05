# Engineer onboarding, projects, and connector experience

Status: proposed product specification; no implementation authorized by this document.

Version: 0.1. Date: 2026-10-02.

Product: Agent Game Review (AGR).

## 1. Product decision and intended outcome

AGR should help an engineer bring in agent runs, understand the behaviors worth investigating, record a next action, and return with new runs to check whether a change helped.

The first release defined here is a **local engineer workspace** with a public, read-only sample experience. A hosted workspace with login and team access is a later release. This is a proposed sequencing decision, based on the existing local engine and the uncertainty about the eventual deployment model; it is not a claim that hosted demand has been validated.

The first milestone is:

> A new engineer can import their own supported run and identify a useful next action without reading the README or entering a CLI command.

The repeat-use milestone is:

> The engineer can return to the same project, import a subsequent run or batch, reopen their investigation, and inspect an eligible comparison without rebuilding their workspace.

Login is an ownership and collaboration capability. It should accompany private hosted projects, real reviewer identity, and access control. Local analysis should not require an account.

This specification covers the surrounding product journey and its integration with the existing investigation screens. It does not replace the behavioral analysis specification or relax its evidence rules.

## 2. Evidence and current implementation

The baseline below was checked against repository code on 2026-10-02. External platform compatibility must be verified again during implementation; a registered adapter is not a promise of compatibility with every export version.

Primary implementation references:

- [Adapter registry](../agr/adapter.py): six registered source adapters.
- [CLI](../agr/cli.py): import commands, verifier sidecars, Langfuse fetch, and the existing run-sweep workflow.
- [Langfuse API fetcher](../agr/langfuse_api.py): fetches one trace by ID using host and public/secret keys.
- [API transport](../agr/api.py): existing read and review-write routes, read-only demo mode, and one store per app instance.
- [Browser shell](../agr/static/index.html) and [boot flow](../agr/static/js/boot.js): Runs, Patterns, Compare versions, and run investigation.
- [Browser state](../agr/static/js/state.js): hard-coded reviewer identity today.
- [Store](../agr/store.py): content-addressed normalized captures and versioned derived records.
- [Span flattener](../agr/span_tree.py): shared transformation for span-tree sources.
- [Product analysis framing](verdict-vs-analysis.md): task outcome and execution quality are independent dimensions.
- [Existing product specification](../agent-game-review-spec.md): analysis semantics and review workflows.

The adapter and import verification performed before writing this document passed 201 existing tests. Network tests for Langfuse use mocked responses; a live account was not exercised.

### 2.1 Source inventory

| Source | Implemented input | Current user entry | Current limitations |
|---|---|---|---|
| Harbor / Terminal-Bench | Trial directory, trajectory file, or job directory containing trials | `agr ingest-harbor <path>`; named adapter also exists | Batch discovery is implemented in the dedicated Harbor CLI path; UI ingestion must reuse its discovery behavior |
| Claude Code | Saved JSONL transcript, stream-json output, or final-result-only JSON | `agr ingest-from --adapter claude <file>` | Final-result-only input has no full trajectory; ordinary transcripts do not inherently supply a task verifier |
| pi | Session JSONL | `agr ingest-pi <file>` or named adapter | Task evaluation evidence can be supplied separately; session completion is not a task pass |
| OpenTelemetry | Supported OTLP JSON containing GenAI spans | `agr ingest-from --adapter otel <file>` | No collector receiver; missing content limits behavioral review; mixed-trace exports need explicit partitioning before conversion |
| Langfuse | Supported trace/observations/scores JSON; flat trace-details response | File adapter or `agr ingest-langfuse-api --trace-id <id>` | Live fetch is one trace by ID; no project browser, recurring sync, or managed connection UI |
| LangSmith | JSON list of run records, or an object containing `runs` and optional `feedback` | `agr ingest-from --adapter langsmith <file>` | No live API fetch; nested chains are not reliable subagent identity; mixed-trace exports need partitioning |
| Custom ATIF | Document matching AGR's documented ATIF-shaped contract | `agr ingest <file>` | Direct ingestion format, not an additional registered adapter; arbitrary JSON is not supported |

OpenCode is not registered or implemented. Model-reviewer providers are a separate capability and must not appear as trace-import connectors.

Some older adapter documentation describes an earlier inventory. This specification uses the registry and implementation as the baseline.

### 2.2 Existing product capabilities to preserve

AGR already has run triage, evidence-linked investigation, full trace inspection, pattern analysis, version comparison, feedback/corrections, dispositions, and lesson/experiment primitives. The new experience should make these capabilities discoverable and connect them into a journey.

Source imports currently start outside the browser. There is no browser upload endpoint, source connection management, folder picker flow, persistent project model, authenticated user model, or automatic source synchronization. Public demo read-only behavior exists and should remain explicit.

## 3. Users, jobs, and scenarios

### 3.1 Primary user

An engineer developing or evaluating an agent. They may work with a harness, an interactive coding agent, or an observability platform. They understand their task and toolchain but should not need to understand AGR's storage schema, adapter classes, taxonomy, or capability names.

Their main jobs are:

1. Bring in one problematic run or a completed batch.
2. Find which behavior deserves attention and inspect its supporting evidence.
3. Distinguish agent behavior from missing capture, environment failure, and task-evaluation uncertainty.
4. Record a concrete follow-up.
5. Import new runs and investigate whether the change improved the relevant behavior.

### 3.2 Secondary users

- A teammate reviewing a finding or comparing configurations: later hosted collaboration.
- A curious visitor exploring a published example: public sample mode.
- An engineer automating repeated imports from a script or CI: later automated ingestion, with existing local CLI available throughout.

### 3.3 Representative scenarios

| Scenario | Entry | Successful result |
|---|---|---|
| Failed Harbor batch | Select a job folder | Trial count is previewed, each trial is accounted for, and the engineer reaches a filtered run inbox |
| Claude session investigation | Select one saved session | Captured tool activity is reviewable; task outcome remains unverified when no checks exist |
| Langfuse incident | Configure a connection and enter one trace ID | Authentication and retrieval errors are understandable, and the fetched trace opens as a project run |
| Existing LangSmith user | Import a supported export | The engineer sees which trace groups and feedback were included without being promised a live connection |
| OTel export without content | Import an OTLP JSON file | Names, timing, and usage are available where captured; unavailable behavioral analysis is explained |
| Second investigation session | Open AGR again | Last project and investigation are restored, with a visible Add runs action |
| Unsupported source | Try another tool's export | AGR explains the unsupported format and offers supported-source instructions or custom ATIF guidance |

## 4. Scope and release boundaries

### 4.1 Release A: local first-use workflow

Required:

- Welcome screen with Analyze my runs and Explore an example.
- Persistent local projects and clear local identity.
- Add runs entry from every project-level surface.
- Source selection and supported-format help.
- Browser file import and deliberate local folder selection.
- Claude/pi session discovery within engineer-approved directories.
- Harbor trial/job discovery and batch accounting.
- Preview, validation, selection, import job progress, result summary, retry, and cancellation.
- Langfuse connection testing and one-trace fetch using a trace ID.
- Project-scoped runs, import history, and restored investigation location.
- Useful evidence/verification messaging, with deterministic analysis as the default.

### 4.2 Release B: repeat use and action workflow

Required:

- Saved investigations with evidence-linked next actions.
- Eligible baseline/candidate comparison from imported batches.
- Copyable source-specific CLI instructions for importing into the same local project.
- Basic project organization and connection maintenance.
- Sample/real-data separation and clear unavailable actions.

### 4.3 Later extensions

- Langfuse project trace browsing and recurring sync.
- LangSmith live API import.
- Folder watching, unattended ingestion, and CI delivery to a hosted destination.
- OTel collector receiver.
- Hosted authentication, private workspaces, collaboration, and membership.
- Issue-tracker integrations, notifications, automatic regression execution, and replay.

The UI must not display a usable Connect or Sync action for a source whose live integration has not shipped. Optional roadmap information should be separate from the available source catalog.

### 4.4 Out of scope

Building new agent harnesses, automatically repairing agent code, inferring task correctness from session completion, replacing the existing analysis engine, redesigning every investigation component, arbitrary log conversion, billing, and enterprise identity features are out of scope for Releases A and B.

## 5. Product principles

1. **Begin with an engineer's task.** Explain what a source can help investigate before exposing technical metadata.
2. **Offer a useful next action.** Empty, limited, failed, and completed states each have a clear action.
3. **Preserve evidence semantics.** Unknown is not healthy; missing checks are not success; a plausible explanation is not proven causality.
4. **Make repetition easy.** Projects, source choices, and investigation state persist.
5. **Ask for missing information only when needed.** Source-derived identity is shown for confirmation; optional advanced metadata is collapsed.
6. **Show actual capabilities.** File import, API fetch, and continuous ingestion have distinct availability.
7. **Keep sample data recognizable.** Engineers can explore examples without confusing them with their own runs.
8. **Keep credentials and deployment details out of ordinary review work.** Connection setup contains configuration; investigation contains findings and evidence.

## 6. Information architecture and navigation

The local shell contains a project switcher, primary navigation, and a persistent Add runs action.

```text
Project switcher: Payments agent

Home
Runs
Patterns
Compare versions
Investigations              [Release B]
Sources
Settings

Primary action: Add runs
Footer: Local workspace · reviewer name · Help
```

Home is a lightweight orientation and continuation surface. It does not duplicate the complete Runs table or imply unsupported aggregate conclusions.

| Surface | Purpose | Primary action |
|---|---|---|
| Welcome | Start with own data or a sample | Analyze my runs |
| Home | Continue work and understand project activity | Add runs / Continue investigation |
| Runs | Triage and filter project runs | Open run |
| Patterns | Investigate recurring observed behavior | Open contributing runs |
| Compare versions | Inspect an eligible comparison | Select baseline and candidate |
| Investigations | Revisit recorded findings and actions | Open investigation |
| Sources | View supported imports, configured connections, and import history | Add runs / Configure Langfuse |
| Settings | Project details, local storage, identity, connection options | Save explicit changes |

Run-level navigation retains the current review chapters, trace view, and evidence panel. The selected project and batch context remain visible. Advanced analysis stays available without dominating initial orientation.

### 6.1 Landing and restoration rules

- A valid deep link opens its exact project/run/review position.
- A new installation opens Welcome.
- A returning engineer opens their last project Home with Continue investigation visible.
- A deleted or unavailable investigation produces a reason and a usable fallback.
- Explicit browser Back/Forward navigation restores destination, filters, and selected project.
- Opening a project never silently switches to a run from another project.

## 7. Projects and local reviewer identity

### 7.1 Project creation

On Analyze my runs, request only a project name. Description and repository reference are optional. Choose a sensible writable local storage location and expose it under Settings; a storage error must be recoverable before upload or conversion starts.

An engineer may rename or archive a project. Archiving hides it from the default switcher and preserves its data. Permanent deletion requires a separate, concrete data-deletion flow; it is not part of ordinary navigation.

A source may feed multiple projects only through explicit selection. Project context must be present before preview and import.

### 7.2 Identity

The local reviewer name is optional and editable. An absent name renders as Local reviewer. Local names are display attribution, not authenticated identity. Existing hard-coded initials must be removed when this feature is implemented.

Review mutations derive local attribution from workspace settings rather than unrelated initials in browser state. Hosted mutations will instead derive identity from the authenticated server session.

### 7.3 Existing store compatibility

An existing single store is represented as an Existing runs project on first project-enabled startup. Opening it does not copy, rewrite, or reidentify its captures. Registration is idempotent and preserves current run identifiers, annotations, comparisons, and deep-link fallbacks.

Project association and new display labels live outside immutable source documents. CLI imports without explicit project selection continue to use the legacy/default store, and the browser can register that store as a project. A future project selector must not silently redirect existing commands to another destination.

## 8. Welcome and sample experience

Suggested welcome copy:

> Understand what your agent did—and what to investigate next.
>
> Import a run from your tools, or explore a reviewed example.

Actions:

- Analyze my runs: creates/selects a project and opens Add runs.
- Explore an example: opens a distinct sample context.

Sample mode has a persistent Sample data label and an Analyze my runs action. Public sample mode stays read-only; local exploration should also avoid writing changes into the bundled fixtures. Trying an unavailable write explains the mode and points to a personal project.

Onboarding is contextual. Do not block the first run behind a terminology tutorial. A short welcome callout may explain the three steps Import, Inspect, and Record a next action. Capability explanations appear where the relevant limitation occurs.

## 9. Add runs: common interaction contract

### 9.1 Entry and steps

Add runs is available from Home, Runs, Sources, and the empty-project state.

```text
Add runs to Payments agent

1. Choose source
2. Select files, sessions, or a trace
3. Preview and select
4. Import
5. Open results
```

The step label adapts to the source. Do not make a Langfuse user pass through an upload screen. Project context remains visible throughout, and Back retains valid selections.

### 9.2 Source catalog

Cards show source name, supported entry type, and concise input guidance. Available actions are:

- Harbor: Select trial or job folder; import supported archive when folder transfer is needed.
- Claude Code: Select sessions or import a capture file.
- pi: Select sessions or import JSONL.
- OTel: Import OTLP JSON.
- Langfuse: Fetch a trace or import JSON.
- LangSmith: Import JSON export.
- Custom ATIF: Import ATIF JSON.

Choosing a source is supported even when automatic detection fails. Extension alone is insufficient for detection; inspect content structure. When multiple source interpretations remain plausible, request a choice instead of applying the first adapter.

### 9.3 Selection and transfer

Requirements:

- Drag-and-drop and keyboard-accessible file selection are equivalent entry paths.
- Selecting multiple single-run files is supported.
- Folder/relative-path transfer preserves source structure for Harbor sidecars.
- A source-specific export guide is available inline, without leaving the wizard.
- Unsupported and auxiliary files are accounted for separately from candidate runs.
- A file dialog dismissal returns to an unchanged selection screen.
- Browser compatibility fallback is explicit: when folder selection is unavailable, offer a supported archive upload or documented local CLI path.
- Files selected for preview are staged; they do not become visible project runs until Import is confirmed.
- Neither file selection nor preview invokes a model reviewer.

### 9.4 Preview

Show one row per candidate logical run, not one row per span, sidecar, or message. Each row includes:

| Field | Behavior |
|---|---|
| Selection | Checkbox; invalid candidates are not selectable |
| Display title | Task/session title where captured; neutral identifier fallback |
| Source | Source type and file/session/trace reference |
| Captured time | Only when available; unknown is explicit |
| Model/configuration | Captured or unresolved; editable display labels do not fabricate versions |
| Evidence available | Messages, tools, results, timing/usage, and checks at a readable level |
| Import status | New run, duplicate, new capture of an existing run, limited, or invalid |
| Warnings | Short summary, expandable detail, and an actionable explanation where possible |

A summary reports candidate run count, selected count, invalid count, duplicates, and auxiliary/unsupported file counts. Clearly distinguish files from runs.

Optional fields are grouped under Advanced: task identity overrides, batch label, configuration identity, and verifier attachment. Overrides must preserve their provenance and use the existing typed input contracts. A display label cannot change an evidence-backed identity.

### 9.5 Import and completion

Import operates on the confirmed preview manifest. A changed input or changed selection requires a new manifest/revision, rather than importing a silently different set.

Completion copy uses accounted outcomes:

> 18 new runs, 2 new captures, 4 already imported, 1 failed.

Actions: Open runs, Open run when exactly one is available, View failed items, and Import more. A partial failure remains a useful result; successful runs are immediately accessible.

The destination inbox is filtered to the successful selected runs/import batch. It must not lose context by opening an unrelated high-priority run.

## 10. Source-specific requirements

### 10.1 Harbor / Terminal-Bench

- Accept a trial directory, trajectory file, or job directory with multiple trials.
- Reuse dedicated Harbor trial discovery, including its handling of rollup results and incomplete directories.
- Preserve relative paths and associate `trajectory.json`, `result.json`, and supported verifier artifacts with the correct trial.
- Report each discovered trial as importable, limited, invalid, duplicate, or excluded with a reason.
- Never count a job-level summary as an additional run.
- Missing reward/check evidence results in the appropriate unverified/unknown state, not an assumed failure or success.
- Batch label defaults from the source only when known. A user-defined batch label is separate from source sweep identity.
- Offer batch archive upload as a transfer mechanism; archive handling is new infrastructure, not an existing adapter feature.

### 10.2 Claude Code

- Accept the three currently supported capture shapes and name the detected one.
- Saved-session discovery is restricted to an engineer-approved directory, with a directory picker and explanatory scope.
- Conventional session locations may be suggested; automatic scanning of unrelated directories is prohibited.
- Discovery lists file/session title, time where known, and size before selection. It must not imply a full task verifier exists.
- Snapshot selected session bytes for preview. If a selected file changes before staging completes, resnapshot or report the change; never pair a preview with different bytes.
- An active session can be imported as a snapshot, explicitly labeled possibly incomplete. Later snapshots may become new captures of the same logical run.
- Final-result-only JSON says No execution trajectory captured and offers a limited outcome/output view.
- Existing subagent/capture limitations remain visible. The wizard must not upgrade unsupported coverage.

### 10.3 pi

- Accept supported session JSONL and offer scoped local session discovery.
- Preserve source session identity and adapter warnings.
- Provide optional task and verifier inputs without requiring them for behavioral analysis.
- Apply the same active-session snapshot and missing-evaluation rules as Claude Code.

### 10.4 OpenTelemetry

- Accept supported OTLP JSON; do not accept arbitrary telemetry CSV, protobuf, or an exporter URL as though supported.
- Detect distinct trace IDs before flattening. Partition by reliable trace identity and parent relationships before invoking the adapter for each trace.
- Repeated export fragments for one trace may be combined only when span identity can be reconciled without contradictory records; contradictory duplicates are rejected with a reason.
- Ambiguous roots, cycles, disconnected fragments, and missing parents must be reported. Never silently merge unrelated traces into one review.
- If trace partitioning is unavailable in an implementation increment, reject mixed-trace input with a clear restriction rather than shipping misleading batch support.
- Metadata-only captures remain importable. Show which views remain useful and which analysis needs captured messages/tool payloads.
- A receiver or collector connection must not be advertised in Release A.

### 10.5 Langfuse files and live fetch

File imports accept the supported wrapped export and flat trace-details shapes. An export from a different platform version is validated by structure and not by its filename.

Live setup fields:

| Field | Required | Behavior |
|---|---|---|
| Connection name | Yes | Friendly project-local label |
| Host/base URL | Yes | Explicit address; environment-provided value may prefill it |
| Public key | Yes | Used with the secret key; never part of analytics |
| Secret key | Yes | Masked field; never shown after save |
| Remember credentials | No | Available only with an approved OS credential storage integration |

Environment-provided credentials may be used without exposing their values to the browser. If secure persistence is unavailable, credentials are kept in memory for the connection session; the next server start asks for them again. Do not fall back to plaintext project files.

Test connection requires a trace ID in Release A. It exercises authentication and retrieval through the supported fetch path. Do not assume a separate account/project-list API exists. Before the request, say that testing retrieves the selected trace locally for preview. It neither creates a run nor invokes a model.

After a successful test, show Trace accessible and preview the staged response. Successful authentication does not establish that the complete trace or evaluation checks were captured.

Errors distinguish missing configuration, unreachable host, authentication rejection, trace not found, rate limit, unsupported response, and retrieval failure. The UI uses actionable copy without exposing keys or raw authorization headers.

No automatic re-fetch occurs on page load, account setup, or connection edit. An engineer explicitly chooses Fetch trace. The same staged snapshot is used for preview and import.

The current fetcher documents an endpoint compatibility/deprecation concern. Implementers must validate the supported endpoint against current official Langfuse documentation and representative deployed versions before declaring live import ready. This spec does not treat the repository comment's timeline as externally verified. Add a versioned fetch layer or migration as required and expose supported versions in source help.

### 10.6 LangSmith

- Accept a JSON list of run records or an object with `runs` and optional `feedback`.
- Help copy names the accepted shapes and provides a validated export recipe during implementation; it must not promise a universal native UI export button.
- Group distinct traces before conversion. Scope feedback to its referenced run/trace; unscoped ambiguous feedback is shown as unresolved and never copied onto every trace.
- Preserve the adapter's explicit limitation on identifying subagents from nested chain records.
- Numeric feedback on an arbitrary scale is not thresholded into pass/fail by the import UI.
- No API key field or Connect button appears until a live fetcher exists.

### 10.7 Custom ATIF

- Validate against the documented AGR input contract and relevant semantic checks.
- Show validation errors using file and field locations where feasible.
- Do not label any JSON document as compatible merely because it contains a `steps` field.
- Link input-format documentation and a valid example from source help.
- Future schema migrations are explicit, versioned transformations preserving the original input.

## 11. Evidence, outcome, and interpretation presentation

Import success, source session termination, task outcome, evidence coverage, contract confirmation, and review enrichment are independent statuses. Do not collapse them into one Ready/Failed badge.

| Situation | User-facing meaning | Required action/behavior |
|---|---|---|
| No verifier evidence | Task outcome unverified | Allow available behavioral review; offer Attach evaluation results |
| Some checks unresolved | Task outcome undetermined | Show the unresolved checks and explanation |
| No tool payloads | Tool content was not captured | Preserve timing/status views where supported; limit dependent findings |
| Final result only | No execution trajectory captured | Open the available output/outcome view |
| Contract unconfirmed | Requirements need confirmation | Retain watermark; link the confirmation flow |
| Deterministic review | Analysis from captured evidence | Do not fabricate an AI summary or suggested fix |
| AI-enriched review | Includes AI interpretation | Preserve fact/interpretation distinction and provenance |
| Derived-analysis failure | Run imported; analysis incomplete | Keep source accessible and offer analysis retry |

Do not invent percentage completeness by averaging incomparable capability dimensions. Show a readable coverage summary with detailed source declarations available on demand.

Task-verifier attachment is optional. Pair sidecars by explicit selection or reliable declared identity, never guessed filename adjacency in a mixed batch. Verifier attachment validates shape and identity and produces a new version/capture as appropriate; it does not rewrite existing evidence or bypass required human confirmation.

Model enrichment is a separate explicit action after deterministic analysis. It displays configured provider/model and known cost information or an honest unavailable estimate, and follows existing redaction and evidence-validation behavior. Credentials for a trace source are never reused as model credentials.

## 12. Project Home, triage, and investigation

### 12.1 Empty Home

Copy: No runs yet. Add a run from your tools to start investigating.

Actions: Add runs and Explore an example. Do not show empty comparison widgets or zero-valued quality claims.

### 12.2 Populated Home

Show recent imports with counts/status, Continue investigation, and a link to runs needing attention. Source health is based on actual last test/fetch results, with a timestamp; an untested connection says Not tested.

Headline counts must name their population and exclude sample data. Outcome unverified/undetermined counts stay visible rather than disappearing from pass/fail totals.

### 12.3 Runs and investigations

- Retain source, batch, captured time, task outcome, review status, and captured model where available.
- Support filters for imported batch, source, evaluation availability, and review status.
- Keep current evidence-based triage logic; new ingestion UX does not create an unsupported priority score.
- Present a concise finding headline where one exists and a neutral explanation where analysis cannot produce one.
- Opening a finding reaches relevant evidence without requiring engineers to locate internal event IDs.

### 12.4 Saved next actions: Release B

A saved investigation contains title, source run/capture references, selected evidence/moment references, engineer note, next action, and status Open, In progress, or Resolved. A single engineer can own it locally; team assignment is later hosted scope.

Recording an action does not approve a lesson, execute an experiment, or declare a proven root cause. Promotion into existing lesson/experiment workflows is a separate explicit operation with their existing validation.

Resolved means the engineer closed the investigation. A system-generated claim that a fix worked requires an eligible comparison and should still reflect the comparison's uncertainty and attribution limits.

## 13. Repeat imports and comparison

- A subsequent import defaults to the active project and remembered source, but still asks for files or an explicit trace fetch.
- Re-importing identical source content is a no-op for capture storage and does not duplicate annotations.
- New bytes for the same logical run become a new capture when supported by the source identity; the previous capture remains inspectable.
- Changing adapter or analysis versions is reprocessing provenance, not a new source run.
- Identity collisions with conflicting task/source identity require review and must not overwrite either source.
- Project separation is explicit: importing the same source into two projects does not give either project access to the other's annotations.
- A user batch label groups imports but is not proof of a matched evaluation sweep.
- Compare imported batches links to the existing matching flow, showing unresolved identity/version fields and exclusions.
- Friendly baseline/candidate labels cannot replace task/environment/version match requirements or remove uncertainty gates.
- A single before/after session may be inspectable side by side without being called a statistically supported improvement.

## 14. Import job lifecycle and reliability

### 14.1 State machine

```text
Source selected -> Staging -> Previewing -> Awaiting confirmation
Awaiting confirmation -> Queued -> Importing -> terminal result

Terminal results: Completed | Completed with errors | Failed | Cancelled
Active job with cancel request: Cancelling -> Cancelled
```

Stage and preview errors are recoverable before a job is submitted. Inside Importing, item-level progress distinguishes Validating, Saving capture, and Analyzing. One failed item does not erase successful items.

Each job has a durable ID, project scope, manifest revision/hash, adapter versions, selection, status, counts, timestamps, and per-item outcomes. Cancellation and retries are idempotent.

### 14.2 Progress and accounting

- Use bytes for transfer progress and processed/total selected items for import progress.
- A percentage is shown only when its denominator is known. Discovery uses an indeterminate state until counted.
- Report import failures separately from agent task failures.
- Account for every discovered candidate and every selected candidate, including excluded/duplicate items.
- Existing captures are marked Already imported; new captures and new runs have distinct counters.
- Persisted source with failed derived analysis counts as an imported item with analysis failure, rather than a failed transfer.
- Retry failed imports and Retry analysis are different actions. Neither repeats successful writes or model work automatically.

### 14.3 Refresh, cancellation, and restart

- Refresh reattaches to the existing job; it does not resubmit the manifest.
- Cancellation stops new item scheduling and requests interruption at supported safe boundaries.
- An atomic source commit that already began may finish. Completed items remain available, and the final summary reports them.
- An unfinished staging item is not visible as a valid project run.
- A restarted server reconciles its job journal with durable captures. It marks interrupted work and offers a resume/retry action instead of declaring completion.
- Per-run capture publication and derived-record visibility use atomic boundaries. Failures do not expose truncated JSON as a completed record.
- Parallel imports to the same logical run serialize/conflict safely; request retries cannot bypass idempotency.

## 15. Proposed data model

These are logical records, not a database technology decision. The local filesystem store remains viable for the first release if atomicity, isolation, and indexing requirements are met.

| Record | Required fields / responsibilities |
|---|---|
| Local workspace | ID, project registry, reviewer display name, last project, local settings version |
| Project | ID, name, optional description/repository reference, store association, created/updated time, archive state |
| Source connection | ID, project ID, source type, label, host, credential reference, supported fetch version, last test/fetch status and time |
| Import manifest | ID/revision, project ID, candidate records, selected records, staged artifact references, content hashes, grouping/validation decisions |
| Import job | ID, project ID, manifest reference, request idempotency key, status, counters, item results, timestamps, sanitized errors |
| Input artifact | Original byte hash, scoped staging/storage reference, original relative filename, byte count, retention state |
| Imported run link | Project/import ID, source identity, logical run/capture ID, original-artifact provenance, adapter version |
| Saved investigation | ID, project ID, title, evidence/run/capture references, note, action, status, local actor, revisions |

Connection secrets are not fields in the serialized connection record. References to OS credential storage or an in-memory session are opaque.

Original byte hashes and normalized capture hashes are different concepts. AGR's existing source hash is over the normalized document; it must not be relabeled as a byte-for-byte original-file hash. Archive provenance records the archive hash and selected entry hashes separately.

Input grouping is determined before adapter conversion. The manifest records trace/trial membership, ignored auxiliary entries, malformed candidates, and any required engineer choices.

## 16. Backend and API contract requirements

The following routes describe the proposed contract. They are not existing endpoints and exact transport names may change during implementation if behavior remains equivalent.

| Proposed operation | Purpose |
|---|---|
| `GET /projects` | List local registered projects |
| `POST /projects` | Create a project with explicit storage association |
| `GET /projects/{project_id}` | Project metadata and summary |
| `PATCH /projects/{project_id}` | Rename/archive/update mutable project metadata |
| `GET /sources/catalog` | Source capabilities and accepted input shapes for this installed version |
| `POST /projects/{project_id}/import-previews` | Stage selected uploads/artifacts and build a preview manifest |
| `GET /projects/{project_id}/import-previews/{preview_id}` | Preview status, candidates, coverage, and errors |
| `POST /projects/{project_id}/imports` | Submit selected manifest revision with idempotency key |
| `GET /projects/{project_id}/imports` | Paginated import history |
| `GET /projects/{project_id}/imports/{import_id}` | Status, counters, and paginated item results |
| `POST /projects/{project_id}/imports/{import_id}/cancel` | Request idempotent cancellation |
| `POST /projects/{project_id}/imports/{import_id}/retry` | Retry explicitly selected failed work |
| `GET/POST /projects/{project_id}/connections` | List/create connection metadata and secret references |
| `POST /projects/{project_id}/connections/{connection_id}/trace-previews` | Test/fetch a supplied trace ID into a staged preview |
| `PATCH/DELETE /projects/{project_id}/connections/{connection_id}` | Update/disconnect; preserve imported captures |
| `GET/POST/PATCH /projects/{project_id}/investigations...` | Release B saved-investigation operations |

Local directory selection/session discovery is mediated by a local picker or an explicitly granted directory reference. Do not add an endpoint accepting arbitrary server paths from any browser caller. Ordinary uploads work without privileged filesystem selection.

All reads and writes resolve project scope before run/capture lookup. Legacy single-store routes retain their current default-store behavior during transition; adding a project model must not cause ambiguous lookup across all registered stores.

Error envelopes include a stable code, actionable message, retryability, and optional item/field reference. They exclude secrets, full trace contents, and raw provider headers. Examples: `unsupported_format`, `ambiguous_source`, `mixed_trace_input`, `invalid_verifier`, `source_changed`, `source_auth_failed`, `source_not_found`, `storage_unavailable`, and `preview_expired`.

Previews are expiring server-side resources. The client sends preview IDs and selection, not arbitrary filesystem paths. An expired preview returns an explicit action to reselect or fetch again.

## 17. Local storage, credentials, and ingestion boundaries

These requirements apply to importing user-selected files and making explicit source requests; they should not become a checklist that obstructs ordinary analysis.

- The local server binds to loopback by default. A browser origin check and protection for state-changing requests are required before exposing import, credential, or filesystem-selection endpoints.
- Release A must not expose local filesystem selection or credential management through an unauthenticated non-loopback deployment. Remote exposure is restricted to read-only sample mode until the hosted access model is implemented.
- Local picker grants are scoped, short-lived references to selected directories. Do not follow symlinks/reparse points outside granted roots or import device/special files.
- Uploads and archive extraction stay in application-managed staging roots. Reject absolute/traversal paths, escaping links, and archive entries that violate limits.
- Validate expansion limits before and during extraction; do not trust compressed byte size.
- Source JSON/JSONL is parsed as data and never executed.
- Original selected artifacts are retained with imported local captures by default for provenance; staging-only and excluded files expire. Unrelated files in selected directories are not copied.
- Settings show storage usage and explicit retention options. Cleanup of original artifacts leaves immutable normalized captures intact unless project data deletion is explicitly requested.
- Credentials are stored only in memory, environment variables, or an approved OS credential store for local mode. They are never serialized into imports, URLs, projects, reviews, or analytics.
- Fetches have connection/read timeouts, bounded responses, and controlled retries. Authorization failures are not automatically retried.
- Local self-hosted hosts may be reachable through explicit configuration. Hosted fetchers later require server-side destination policy and private-network restrictions; the hosted server must not inherit unrestricted local URL behavior.
- Raw traces are not transmitted to an external model as part of import. Explicit later model review uses the existing redaction boundary.
- Imported records and stored investigations are project scoped, including error reports and original-artifact access.

### 17.1 Proposed initial limits

These are implementation starting values to validate with representative engineer datasets, not measured capacities or permanent product promises. Limits are configurable and returned in the source/import contract.

| Limit | Proposed local default |
|---|---|
| Single uploaded capture file | 100 MiB |
| Compressed archive | 500 MiB |
| Expanded artifacts per import | 2 GiB |
| Archive/discovery file entries | 10,000 |
| Selected logical runs per import | 1,000 |
| Span records per candidate trace | 100,000 |
| Langfuse fetched response | 100 MiB |
| Langfuse connect/read timeout | 10 seconds / 60 seconds |
| Completed/abandoned preview retention | At most 24 hours; remove sooner after successful import |

Check available disk space before staging and committing large imports, and stop with a recoverable error if storage becomes unavailable. A source larger than the configured limit offers a smaller-batch path or local CLI guidance; it is never silently truncated.

## 18. Hosted login and collaboration extension

Hosted mode is a separately gated release. It cannot be enabled by merely placing a login screen in front of the local UI.

### 18.1 First hosted journey

Public sample -> Sign in to analyze my runs -> Create/join workspace -> Create project -> Import -> Inspect -> Save/share within authorized workspace.

The public sample remains accessible without authentication. Private projects, original artifacts, previews, connections, imports, and annotations require authorization on every server operation.

### 18.2 Minimum hosted requirements

- Managed authentication using a selected provider; exact provider and sign-in methods remain open decisions.
- Durable session, sign-out, expired-session recovery, and consistent account display.
- Workspace membership and project visibility.
- Server-derived actor identity for mutations; client-provided actor strings cannot impersonate another reviewer.
- Roles: Owner manages membership/settings; Editor imports and annotates; Viewer reads authorized records.
- A permission matrix covering upload, connection creation/use, review mutation, membership, and deletion.
- Workspace/project-scoped connection secrets encrypted in server-managed secret storage.
- Scoped, revocable automation credentials for later CI imports; source-platform keys are not AGR authentication tokens.
- Authorized share links by default. Public publishing is a separate explicit disclosure action, not ordinary sharing.
- Session expiration during upload pauses/requires reauthentication and resumes from server-owned state where supported; it must not make staged files public.
- Tenant isolation and permission tests for metadata, raw artifacts, jobs, saved comparisons, and errors.

Cross-device access, invitation flows, recovery policy, retention, and hosted storage quotas must be specified before that release. Local projects are not silently uploaded when an engineer signs in; migration requires explicit source/project selection and transfer confirmation.

## 19. Accessibility and interaction quality

- Source cards, upload controls, preview rows, dialogs, and progress actions are keyboard accessible.
- Focus moves to the new step heading and returns to the initiating control when a modal closes.
- Validation identifies the affected field/item in text and does not rely on color.
- Progress announcements are throttled and useful to screen readers; completed/error states are announced.
- Drag-and-drop has a visible browse alternative.
- The wizard works at narrow widths; long filenames and IDs can be expanded/copied without breaking layout.
- Empty states remain useful with no sources connected and no captures available.
- Loading, empty, partial, failed, and permission-denied states are distinct.
- First-use screens use plain terms: Evaluation results, Captured tool output, Import history. Internal names remain available in technical detail.
- Destructive actions and external model work are separate from ordinary navigation and import confirmation.

## 20. Instrumentation and success criteria

Existing instrumentation accepts a finite event/property allowlist. New funnel events require explicit schema updates during implementation; do not pass arbitrary names into the existing recorder or log free text.

Local product events stay local by default. Any remote collection is a separate opt-in decision; this spec does not authorize sending telemetry.

Proposed events, each containing only bounded enumerations/counts and local opaque identifiers:

| Event | Purpose |
|---|---|
| `onboarding_started` | Entry into own-data or sample journey |
| `source_selected` | Source/method choice |
| `import_preview_completed` | Candidate, invalid, duplicate, and limited counts |
| `import_submitted` | Confirmed selected count |
| `import_completed` | Successful/failed/duplicate and analysis-failure counts |
| `connection_test_completed` | Source and bounded status code |
| `first_imported_run_opened` | Own-data review activation |
| `investigation_action_saved` | Recording a concrete follow-up |
| `repeat_import_completed` | Repeat-use cohort observation |

No filenames, directories, trace IDs, hostnames, repository names, prompts, tool content, credentials, or investigation notes enter these events. The full property schema must be enumerated before implementation.

Measures:

- Time from onboarding start to opening an own-data run, separating transfer time from analysis time.
- Preview-to-import completion rate and failure reasons by supported source.
- Time to a first engineer-reported useful finding/next action; opening evidence alone is not proof of usefulness.
- Share of activated engineers who import a second distinct run/batch in a later session.
- Import discovery/accounting correctness and duplicate behavior.
- Comparison attempts blocked by missing metadata, to inform future source capture guidance.

### 20.1 Initial usability gate

Test with five engineers unfamiliar with AGR, using at least Harbor, a session source, and a span-tree source across the sample. Provisional release gate: at least four complete a supported own-data import and open relevant evidence without README/CLI assistance. Participants must correctly distinguish an unverified task from a passed task.

For small representative captures, target an own-data review within five minutes of starting the flow, excluding installation, external credential retrieval, and external platform outages. Record exclusions explicitly. This is a design target to test, not an established performance claim.

A participant identifying no useful finding is recorded honestly; import success does not manufacture product usefulness. Repeat-use observation is a subsequent validation gate and must not be inferred from sample browsing.

## 21. Acceptance criteria

### 21.1 First use and project scope

- **AC-01:** A fresh installation shows Analyze my runs and Explore an example, with no account requirement for local use.
- **AC-02:** An engineer creates/selects a project and reaches source selection without entering technical identifiers.
- **AC-03:** A returning engineer reaches their last project and can resume the last valid investigation.
- **AC-04:** Sample and own-data runs are visibly distinct; public sample writes are unavailable with explanatory copy.
- **AC-05:** Existing stores register without changing captures, identifiers, or review history.
- **AC-06:** A project switch cannot expose another project's runs, artifacts, connections, or mutation context.

### 21.2 Sources and preview

- **AC-07:** All six registered adapters and direct ATIF input appear with accurate methods and accepted shapes.
- **AC-08:** Unsupported sources have no functioning Connect/Import claim; model-review providers are absent from the source catalog.
- **AC-09:** Ambiguous JSON detection asks for source selection rather than converting silently.
- **AC-10:** Harbor job import accounts for each trial and correctly associates sidecars; rollup files are not extra runs.
- **AC-11:** Claude saved transcript, stream output, and final-only output produce the appropriate distinct preview states.
- **AC-12:** Scoped Claude/pi discovery never reads outside approved directory roots.
- **AC-13:** OTel and LangSmith multi-trace inputs are reliably partitioned or explicitly rejected; unrelated traces are not combined.
- **AC-14:** Missing messages, tool payloads, and verifier evidence remain visible and limit only dependent analysis.
- **AC-15:** Invalid candidates cannot be selected; the preview accounts for excluded and auxiliary files.
- **AC-16:** A selection change/source change cannot be imported using an earlier manifest without renewed preview validation.

### 21.3 Langfuse and credentials

- **AC-17:** An engineer tests access using a supplied trace ID and receives clear authentication/not-found/network errors.
- **AC-18:** Preview uses the explicitly retrieved trace snapshot; confirmation does not silently refetch different data.
- **AC-19:** Secrets never appear in browser-visible saved metadata, logs, job errors, URLs, or instrumentation.
- **AC-20:** Credential persistence is secure or explicitly session-only; unavailable secure storage does not produce plaintext fallback.
- **AC-21:** Disconnecting a source preserves imported captures and their provenance.
- **AC-22:** Supported live endpoint behavior is verified with representative platform deployments before release.

### 21.4 Reliability and evidence semantics

- **AC-23:** Identical re-imports do not create duplicate captures or annotations.
- **AC-24:** Fuller captures of the same logical run remain versioned and do not overwrite previous evidence.
- **AC-25:** A mixed-success batch gives accurate counts and lets the engineer open successes and retry only failed work.
- **AC-26:** Refresh reconnects to an existing job; double submission with the same idempotency key does not repeat work.
- **AC-27:** Cancellation and restart preserve already committed captures and do not expose incomplete records as completed runs.
- **AC-28:** Analysis failure keeps the imported source accessible and offers an analysis-only retry.
- **AC-29:** Missing task checks never render a task pass; unconfirmed contracts retain their confirmation requirements.
- **AC-30:** Import and preview make no model calls; enrichment is a separate explicit action.
- **AC-31:** Exceeding limits or running out of disk produces a recoverable error without silent truncation.
- **AC-32:** Archive/path attacks cannot escape staging or approved roots.

### 21.5 Repeat use and later hosted gate

- **AC-33:** An engineer records and reopens a next action with stable evidence references in Release B.
- **AC-34:** Imported batches can enter comparison without bypassing existing matching/uncertainty checks.
- **AC-35:** Local reviewer attribution is editable and never presented as authenticated identity.
- **AC-36:** A hosted release authorizes every project/artifact/job operation and derives actors from server sessions.
- **AC-37:** Signing into hosted mode does not silently upload local projects.
- **AC-38:** The main first-use flow works with keyboard navigation and understandable error announcements.

## 22. Implementation workstreams and sequencing

No code changes are part of this document's delivery. When implementation is authorized, sequence work as follows.

| Stage | Work | Exit gate |
|---|---|---|
| 0. Product walkthrough | Review wireframes, accepted source shapes, and representative datasets | Agree on first-use journey and local release scope |
| 1. Project and import foundation | Project registry, staging, manifest, jobs, local boundaries, legacy-store compatibility | One ATIF import with reliable preview/progress and isolation |
| 2. File-source experience | Harbor, Claude, pi, OTel, LangSmith; source help and trace partitioning | Source-specific acceptance criteria and useful limited-capture states pass |
| 3. Langfuse connection | Credential handling, endpoint compatibility, test/fetch/preview flow | Mocked contract tests plus an opt-in real-account check; no secrets exposed |
| 4. First-use polish | Welcome, Home, empty states, restoration, accessible interactions | Initial unfamiliar-engineer usability gate passes |
| 5. Repeat-use workflow | Saved actions, import history, batch comparison, project-targeted CLI guidance | Engineer completes an import-investigate-change-reimport loop |
| 6. Hosted decision | Validate demand for shared/cross-device work; resolve open hosted choices | Separate hosted spec/authorization before implementation |

Release A requires stages 1 through 4. A source may ship later if its acceptance criteria cannot be met, but its card must clearly reflect actual availability and the release must not claim the full source catalog works through the UI.

### 22.1 Verification plan

- Reuse existing adapter fixture tests; add boundary cases only where new grouping/discovery changes behavior.
- Test every supported source through preview -> import -> project run, not only adapter conversion.
- Exercise duplicates, mixed-trace exports, sidecar mismatches, changing active sessions, parser errors, missing content, and partial batches.
- Exercise network timeout/auth/rate-limit/unsupported-response cases with bounded mocked requests.
- Validate live Langfuse compatibility separately with explicit test credentials; never require a live account for default CI.
- Exercise job refresh/cancel/restart and storage failures with actual durable-state checks.
- Add browser flows for first use, restored state, file/folder fallback, narrow layout, and keyboard use.
- Hosted authorization tests are a prerequisite to hosted exposure and include direct API/artifact access attempts.
- Source import release notes state tested format versions and known limitations.

## 23. Decisions proposed here and remaining questions

### 23.1 Proposed defaults

- Local engineer workflow first; public sample remains read-only; hosted collaboration later.
- One prominent Add runs flow, with source-specific entry rather than a generic credentials form.
- Deterministic analysis by default; optional model enrichment after import.
- File imports for all supported sources; Langfuse single-trace fetch is the first managed connection.
- Projects organize real work; sample data stays separate.
- Scoped session discovery requires deliberate directory selection.
- No silent trace merging, source truncation, outcome inference, or capture overwrite.
- Local reviewer names provide display attribution only.

### 23.2 Questions to resolve during design/implementation

| Question | Current proposal / dependency |
|---|---|
| Browser/local picker mechanism across platforms | Choose during technical design; preserve ordinary upload and archive fallback |
| Exact session discovery defaults | Validate installed Claude/pi layouts and fixtures rather than assume universal paths |
| Project storage location and registry migration | Prototype safe defaults and existing-store registration before UI rollout |
| OS credential storage integration | Choose supported runtime/platform integration; session-only is the fallback |
| Langfuse endpoint/version matrix | Verify current official API contracts and representative deployments |
| Export instructions for LangSmith/OTel | Produce tested recipes matching accepted shapes before shipping source help |
| Large-trace parsing and concurrency limits | Benchmark representative data; adjust draft limits without changing semantics |
| Original-artifact retention controls | Validate storage cost and investigation needs; make retention explicit |
| Hosted auth provider, roles, membership, and quotas | Resolve only when hosted work is selected |
| External issue/notification integrations | Defer until saved next actions and repeat-use needs are validated |

The product can move forward with the local first-use scope while these implementation choices are resolved. No unresolved question permits advertising an unimplemented connector or weakening evidence and access boundaries.
