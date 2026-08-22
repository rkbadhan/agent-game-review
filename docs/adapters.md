# Writing an adapter

An **adapter** converts one harness's session log into the ATIF-shaped document
that `agr` ingests. This guide is what you target so you don't have to read
`agr` source. The formal shape is `docs/atif-schema.json`; this page explains
the parts that need judgement.

There is no universal converter. Every harness names things differently, so
each harness needs its own small adapter. What you're mapping *onto* is stable
and documented here; the mapping itself is one file per harness (see
`agr/ingest_pi.py` for a worked example).

## What an adapter does

Read the source log and emit a single ATIF document (`docs/atif-schema.json`),
returning it alongside a list of **warnings**. A warning is how you stay honest:
every place the mapping had to make a conservative choice — an unmapped entry
skipped, a task instruction guessed, a missing verifier — is recorded rather
than silently resolved. See `agr/ingest_pi.py:convert`.

Then ingest it:

```
# primary path: a Harbor (Terminal-Bench 2.0) trial or job directory
agr ingest-harbor trials/my-task__agent__attempt-1
agr ingest-harbor jobs/my-eval            # every trial in the job, batch

# generic path: pick any adapter by name (registry order is support priority)
agr ingest-from --adapter harbor <trial-dir>
agr ingest-from --adapter pi session.jsonl --task-id my-task --verifier checks.json
# ingest-pi is a thin alias of the above for the pi adapter
agr ingest-pi session.jsonl --task-id my-task --verifier checks.json
# or, for a raw ATIF document you produced yourself:
agr ingest my-run.atif.json
```

## The interface (in code)

Adapters share one shape, defined in `agr/adapter.py`, so a new harness is one
file and one registry entry — never a new CLI verb:

- **`AdapterResult`** — what `convert` returns: `doc` (the ATIF document),
  `warnings` (the honesty log above), and `meta` (adapter-specific extras the
  pipeline ignores, e.g. an entry count).
- **`Adapter`** protocol — a `name`, a `version` (your provenance stamp, which
  you also write into the doc top-level as `"adapter_version"`), plus
  `convert(source, *, task_id, instruction, run_id, verifier, sweep_id,
  configuration_id) -> AdapterResult`. `source` is whatever your harness log is
  (a path, an export); the keyword arguments are the identity/evidence the core
  never invents (see below).
- **Registry** — add your adapter to `_BUILTIN_ADAPTERS` in `agr/adapter.py`
  (`name -> (module, attribute)`); `agr ingest-from --adapter <name>` then
  dispatches to it. Registry order is support priority: eval-framework sources
  first (Harbor), then interactive-session sources (pi). See
  `agr/ingest_harbor.py:HARBOR_ADAPTER` for the worked example.

Two adapters ship today, in support priority order: **`harbor`** (a
Terminal-Bench 2.0 / Harbor trial directory or job directory —
`agr/ingest_harbor.py`) and **`pi`** (a pi session `.jsonl` —
`agr/ingest_pi.py`). The harbor adapter is the primary path and also shows the
two mappings most adapters need: fanning a coarse-grained turn into kinded
steps, and synthesising the verifier from the harness's own pass/fail (Harbor's
`result.json` reward). For the end-to-end Terminal-Bench recipe see
[`terminal-bench.md`](./terminal-bench.md).
- **`apply_capability_defaults`** — the one definition of "missing capability →
  unavailable"; ingest runs every doc through it, so omitting a capability
  yields the honest default for free.

## Adapter roadmap

Support priority, newest last:

1. **`harbor`** — shipped. The eval-framework path; a `harbor run` job directory
   ingests batch with one command (`agr ingest-harbor <job-dir>`).
2. **`pi`** — shipped. Interactive pi sessions.
3. **`opencode`** — scaffolded, not yet implemented. To add it:
   - Locate opencode's on-disk session storage for your install (its docs
     describe the storage layout; verify against your version before mapping —
     do not copy a layout blindly).
   - Write `agr/ingest_opencode.py` following this guide: map user/assistant
     messages to `task_received` / `model_output`, tool invocations to
     `tool_call` / `tool_result`, and declare only capabilities the log truly
     carries. Stamp `"adapter_version"` top-level.
   - Register as `("opencode", "agr.ingest_opencode", "OPENCODE_ADAPTER")`
     after `pi`, add an `OPENCODE_ADAPTER_VERSION`, and pin the mapping with
     tests over a captured sample session.
4. **`otel`** — planned (spec §5.3 Increment 3).

## The step-kind vocabulary

Every step's `kind` must be one of these. It's a small controlled set the event
timeline maps directly (`agr/events.py:KIND_TO_EVENT`); an unknown kind is
rejected at ingest, not silently dropped. Map to the closest kind and, if the
fit is loose, emit a warning.

| kind | use it for |
|------|-----------|
| `task_received` | the task/instruction arriving at the agent |
| `model_output` | anything the model says or thinks (tag thinking in the content) |
| `plan_declared` | an explicit plan the agent commits to |
| `tool_call` | the agent invoking a tool (include `tool`, and `path`/`data` when relevant) |
| `tool_result` | the tool's response (include `tool`, `exit_code`, `content`) |
| `environment_observation` | state the environment reports back |
| `artifact_observation` | an artifact's observed contents (include `artifact_path`) |
| `process_started` / `process_observed` | a subprocess starting / being observed |
| `error_observed` | an error surfaced to the agent |
| `retry` | an explicit retry of a prior action |
| `strategy_change` | the agent changing approach |
| `context_compaction` | history compaction (include a `summary`) |
| `final_submission` | the agent submitting its answer |
| `run_finished` | the session closing |

Actor (`main_agent`, `tool`, `harness`, `environment`, …) is free text, not a
controlled vocabulary; it defaults to `unknown` if omitted.

## Capabilities: missing means unavailable

`capabilities` declares, per observability dimension, how much the source log
actually captures. **Anything you don't declare defaults to `unavailable`.**
That default is the whole point: a detector that needs `filesystem` state will
report *"not evaluated"* on a log that lacks it, instead of guessing. So declare
a capability as `complete`/`partial`/etc. **only when the log genuinely carries
it**, and leave the rest off.

Levels, weakest to strongest: `unavailable`, `final_only`, `checkpoint_only`,
`partial`, `complete`.

Dimensions: `messages`, `tool_calls`, `tool_results`, `filesystem`,
`process_state`, `network_state`, `compaction_boundary`,
`pre_post_compaction_context`, `sidecar_state`, `verifier_code`.

## What must be supplied from outside the log

The core never invents identity or results. These come from adapter arguments
or an explicit sidecar, or they are honestly absent:

- **`run.task_id`** — the benchmark/task identity. Required. Pass it in; don't
  fabricate it from the log.
- **`run.logical_run_id`** — stable run identity (derivable from the session id).
- **`task.instruction`** — if you take it from the log (e.g. the first user
  message), warn that the contract should be confirmed.
- **`verifier`** — the pass/fail evidence. With no verifier, the run ingests
  with no verifier evidence and is watermarked accordingly; warn when it's absent.

### If you target the version-comparison / sweep surface

These `run` fields are optional for a single run but power the cross-run
comparison and sweep surfaces (spec §4.16, §4.3.1, §12.3). Supply them when the
source knows them — an absent field is reported as an *unresolved* comparison
key, never treated as "equal by absence", so leaving one out is honest but
weakens a comparison rather than faking a match.

- **`run.sweep_id`** — the sweep this run belongs to. Names a comparison side.
- **`run.configuration_id`** — pinned configuration identity. Names a comparison side.
- **`run.environment_image_digest`** — e.g. `sha256:…`; an exact-match key.
- **`run.task_parameters`** — structured task parameters; an exact-match key.

## Checklist

- [ ] Every step has a unique `step_id` and a `kind` from the table above.
- [ ] Unmappable source entries are skipped **with a warning**, never dropped silently.
- [ ] `capabilities` declares only what the log truly captures; everything else is left to default to `unavailable`.
- [ ] `run.task_id` (and ideally `logical_run_id`) is supplied, not invented.
- [ ] A missing verifier is warned about, not faked.
