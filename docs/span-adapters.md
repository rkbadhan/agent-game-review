# Span-tree adapters: OTel GenAI, Langfuse, LangSmith

Status: **shipped** (`agr/span_tree.py`, `agr/ingest_otel.py`,
`agr/ingest_langfuse.py`, `agr/ingest_langsmith.py`). This document was the
plan for the three source adapters and the shared machinery they need, and
remains the design reference for how the mapping works; deviations from the
original plan are called out inline below. It targets the same contract every
adapter targets — [`adapters.md`](./adapters.md) and `docs/atif-schema.json`
— and assumes you have read the first before this one.

## Thesis: three sources, one problem

Everything `agr` ingests today is already a linear trajectory: an ordered list
of kinded steps (`agr/events.py:KIND_TO_EVENT`). The Harbor and pi adapters map
a per-turn or per-message log onto that sequence.

OTel GenAI spans, a Langfuse export, and a LangSmith run tree are not per-turn
logs. They are **span trees**: nested, timestamped records with parent ids and
overlapping durations. That is the one genuinely new shape. Once a span tree is
flattened into an ordered step sequence, each of the three sources is a
*field mapping* into the flattener — the same kind of small, per-harness file
`adapters.md` already describes, not three independent adapters.

So the work is one **flattener** plus three thin **field maps**, not three
adapters. Build the flattener first; it is where the difficulty and the reusable
value both live.

## The shared flattener: span tree → trajectory

A `span_tree.py` module (name provisional) that takes a normalized span tree and
returns the ordered `steps` list and the honest `capabilities`/warnings the ATIF
document needs. Each source adapter normalizes its raw export into the
flattener's input shape, then delegates. Responsibilities:

- **Order and role assignment.** Order by span start time. Identify the agent's
  main loop as the `main_agent` actor; treat nested agent spans as subagents.
  This is the span-tree analogue of the rules the existing adapters already use
  for sidechains (`isSidechain` in the Claude adapter, `parent_tool_use_id`
  pairing) — a nested `invoke_agent` under another `invoke_agent` is a subagent,
  not a new main loop.
- **Tool spans → `tool_call` / `tool_result` pair.** Each tool span fans out
  into a `tool_call` step (carrying `tool`, and `path`/`content` when the
  arguments were captured) and a paired `tool_result` step. The span's error
  state drives the result's `status`/`exit_code`. This mirrors Harbor's fan-out
  (`ingest_harbor.py:convert`) — the flattener re-shapes what the span already
  contains, it does not invent.
- **Model spans → `model_output`.** Carry usage (tokens/cost) onto the step's
  `cost` field, and, when the assistant's tool-call blocks were captured, the
  call order they imply (see *parallel tool spans* below).
- **Root span → task + terminal event.** The root span's input is the task
  instruction (`task.instruction`, warned as needing contract confirmation, per
  `adapters.md`); its output is the final output; its error state selects the
  terminal event kind (`run_completed` / `run_failed` / `run_timed_out`),
  exactly as Harbor's termination block does — never a synthesised
  `final_submission` unless a submission is directly observed.
- **Framework-level retries.** Some frameworks emit a retry as a *sibling span*
  (a repeated `chat` or `execute_tool` under the same parent) rather than a
  step the agent authored. The flattener recognises that shape and maps it to a
  `retry` step, so it is not double-counted as a fresh action.

Applying yesterday's failure-visibility rule directly: **the flattener declares
what it could not see rather than guessing.** Where content was not captured, it
sets the corresponding capability to `unavailable` and emits a warning, so
downstream detectors report *"not evaluated"* instead of inventing a pass.

Sizing: **three to four days including fixtures.** This is the new part.

## Per-source mapping

### OTel GenAI spans (build first)

The cleanest of the three and the highest-leverage: Langfuse ingests OTLP
natively and LangSmith accepts it, so anything emitting spec-compliant GenAI
spans is covered once this exists.

| ATIF target | OTel GenAI source |
|---|---|
| step kind | `gen_ai.operation.name` — `chat`/`invoke_agent` → `model_output`, `execute_tool` → `tool_call`/`tool_result` |
| `tool` | `gen_ai.tool.name` (on an `execute_tool {tool.name}` span) |
| subagent grouping | `execute_tool` under an `invoke_agent {gen_ai.agent.name}` parent |
| usage/cost | `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens` |
| error/status | span status + `error.type` |
| session grouping | `gen_ai.conversation.id` |

**The content catch.** OTel instrumentations *SHOULD NOT* capture inputs or
outputs by default, but *SHOULD* offer an opt-in. Without opt-in you get tool
names, statuses, timings and token counts — enough for the fleet table's error
rate, turns-to-recover, and cost — but **no arguments or results**. In that case
the adapter must declare:

- `tool_calls: partial` (names/timings, no arguments)
- `tool_results: unavailable` (no captured payloads)
- `messages: unavailable` (or `final_only` if only the root output is present)

Detectors depending on `tool_results` then say *"not evaluated"* for error
signatures, argument shapes, and action identity — the honest outcome, not a
silent gap. When opt-in content *is* present, raise those declarations
accordingly.

Input for v1 is **OTLP JSON files**; a collector-side receiver comes later.

Sizing: **two days on top of the flattener.**

### Langfuse export

Langfuse's 10 observation types map almost one-to-one onto the flattener's
categories:

| Langfuse observation | flattener category |
|---|---|
| `event`, `span`, `chain` | generic step / grouping |
| `generation` | model span → `model_output` |
| `agent` | agent span (main loop or subagent) |
| `tool`, `retriever` | tool span → `tool_call`/`tool_result` |
| `evaluator`, `guardrail` | check-like signal (see scores below) |
| `embedding` | model span (usage only) |

Two things to get right:

1. **Errors are the `level` attribute** (`DEBUG`, `DEFAULT`, `WARNING`,
   `ERROR`) plus `statusMessage` — but **many integrations never set `level`**.
   A tool that returned an error payload with a `DEFAULT` level looks clean at
   the span layer. So, in addition to `level`, the adapter must inspect the
   observation's **output** for the same JSON-error shapes the Harbor adapter
   already decodes (`ingest_harbor.py:_parse_returncode` /
   `_is_submission_non_execution_response` — `{"returncode": …}` and friends).
   Reuse that decoder; do not re-derive it.
2. **Langfuse `scores`** attached to a trace are a natural source for atomic
   checks — the first *external verifier signal* any adapter would carry, mapping
   into the `verifier.checks[]` shape (`source: instrumented_assertion`) the way
   `verifier_synth.synthesize_verifier` builds in-session checks. This is
   stronger evidence than the reward-only Harbor path. **A `CATEGORICAL`
   score's `value` is a numeric category-index, not the label** — the
   human-readable label ("pass"/"fail"/…) is in `stringValue`. Match the
   closed pass/fail vocabulary against `stringValue` (falling back to
   `value` only when it happens to already be a string); matching `value`
   itself means every categorical score silently reports `unknown` forever.
   `BOOLEAN` scores are the other way round: `value` (0/1) is the primary
   encoding, `stringValue` ("True"/"False") a fallback.

**Two source shapes are accepted**, both handled by `agr/ingest_langfuse.py`:
the original blob-storage/UI export (`{"trace": {...}, "observations": [...],
"scores": [...]}`, three top-level siblings), and the REAL shape
`GET /api/public/traces/{id}` actually returns — a FLAT
`TraceWithFullDetails` object where the trace's own fields sit at the top
level and `observations`/`scores` are nested inside as siblings of `id`,
with no `"trace"` wrapper key at all. The adapter shipped only recognising
the first shape; a raw API response silently parsed as zero observations
until that was fixed (`langfuse-adapter-0.2`) — a cautionary example for any
future span-tree adapter of validating field mappings against the
*authoritative* schema, not just a hand-written fixture.

**Live pull**: `agr/langfuse_api.py`'s `fetch_trace()` calls that same
by-id endpoint over HTTP Basic auth (stdlib `urllib` only — no runtime
dependency added) and hands the result straight to `convert()`, wired up as
`agr ingest-langfuse-api --trace-id <id>`. Note Langfuse has deprecated the
by-id trace endpoint on Langfuse Cloud (removal 2026-11-16) in favour of a
v2 time-range observations query in v4; self-hosted instances are
unaffected until they upgrade. The by-id fetcher was still worth building —
it works today on both self-hosted and cloud, and a single trace id is the
natural unit to fetch and review one run — but a future v2-based fetcher may
be needed once the old endpoint is actually gone.

Sizing: **one to two days.**

### LangSmith (build last)

Structurally the closest to Langfuse. Runs carry `run_type` (`llm`, `tool`,
`chain`, …), `inputs`, `outputs`, an `error` string, `parent_run_id`,
`trace_id`, and per-run token counts — a direct fit for the flattener. Feedback
records map to `verifier.checks[]` the same way Langfuse scores do.

Do it last: a large share of LangSmith users run LangChain and can emit OTel
instead, so the OTel adapter already covers many of them. Source is the SDK's
`list_runs` output or a bulk export.

**Deviation (implementation note):** unlike OTel's `invoke_agent` operation
and Langfuse's dedicated `AGENT` observation type, LangSmith's `run_type`
enum has no explicit agent/subagent marker — a nested `chain` run is its
generic grouping for any sub-workflow, not specifically a subagent
invocation. Rather than guess subagent boundaries from a chain's name or
tags (exactly the kind of invented identity `adapters.md` rules out), the
shipped adapter maps every LangSmith run_type onto the flattener's
non-agent kinds (`chain` for chain/prompt/parser) and attributes every
tool/model run to `main_agent`, with a warning whenever nested chain runs
are present. A source that wants real subagent grouping from LangSmith
should emit OTel spans instead (which the `otel` adapter already handles).

Sizing: **one to two days.**

## Gotchas that are the same everywhere

- **No source carries a verifier.** Every run ingests `UNVERIFIED` unless
  scores/feedback exist, or `verifier_synth` finds recognised test invocations
  in tool outputs. Say this plainly in each adapter's docs — it is the
  difference between "AGR found failures" and "AGR found process problems."
- **"No error" is not "success."** Unlike a clean Messages API turn, a tool
  span with no error *and no captured output* is `unknown`, not `passed`. The
  capability declaration must reflect that (`tool_results: unavailable`), and
  the terminal event must not be promoted to a success.
- **Session grouping differs** per source: `gen_ai.conversation.id`,
  Langfuse `sessionId`, LangSmith `trace_id`/thread. The fleet table and sibling
  sets need a stable `logical_run_id` and `configuration_id`; when nothing
  better exists, derive the configuration from **model + tool set** (never
  invent identity — see `adapters.md`, "What must be supplied from outside the
  log").
- **Parallel tool spans overlap in time.** Prefer the call order from the
  model's captured output; fall back to span start-time order only when the
  output was not captured.

## Fixtures: the real bottleneck

You need real exports with known ground truth. The honest way: **instrument a
small agent yourself.** A toy LangGraph agent with a couple of deliberately
flaky tools, traced with the Langfuse SDK, yields a Langfuse export, OTel spans,
and LangSmith runs *from the same run* in one afternoon — with labelled
failures. Same trick as the `claude -p` sweeps: control the task, own the
ground truth. Pin every mapping with tests over these captured samples, as the
Harbor and pi adapters do.

## Sizing and order

| Phase | Deliverable | Estimate |
|---|---|---|
| 1 | Flattener + toy-agent fixtures | ~3–4 days |
| 2 | OTel GenAI adapter | +2 days |
| 3 | Langfuse export adapter | +1–2 days |
| 4 | LangSmith adapter | +1–2 days |

**~2 weeks for all three** with fixtures and capability declarations, assuming
the freeze on other work holds.

Order: **flattener (with the toy-agent fixtures) → OTel → Langfuse → LangSmith.**
If only one ships, ship the flattener + OTel, and write the Langfuse adapter as
the first thing the day someone with Langfuse data asks for it.

## Registry and contract notes

Each adapter is one file plus one `_BUILTIN_ADAPTERS` entry in
`agr/adapter.py`, registered after `claude` in support-priority order, with its
own `*_ADAPTER_VERSION` provenance stamp written top-level as `adapter_version`.
No new CLI verb — `agr ingest-from --adapter <name>` dispatches through the
registry. Every conservative choice is a warning; every capability the log does
not genuinely carry is left to default to `unavailable`
(`apply_capability_defaults`). This design changes none of that contract; it
only adds the shared flattener the three span sources share.

See spec §5.3 (Interoperability strategy) for the versioning rule: OTel
conventions evolve, so every mapping is versioned and no field is assumed stable
without a pinned convention version.
