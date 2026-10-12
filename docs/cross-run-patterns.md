# Cross-run failure patterns

Patterns has two views: **Tool failures during runs** and **Test failures at the end**.
The latter groups failed, native, atomic post-run checks by task, full test name and
normalized diagnostic. The aggregate `terminal_bench_reward` check is excluded.

Harbor ingestion preserves explicit exit codes and boolean error flags, id-matched
step metadata, Judy exit-code tags, and mini-swe return codes, in that order.
`status_source` records the source. Boolean flags do not invent an exit code.
Terminus screen-text heuristics remain separate from explicit tool status.
Exact `[error] tool reported failure` wrapper lines never become diagnostic keys;
wrapper-only responses normalize to `<empty>`. Signature version `error-sig-0.4`
also skips a standalone leading Claude Code `Exit code N` banner and unwraps
complete Judy stdout/stderr envelopes before diagnostic selection. Exit codes,
execution-time metadata and empty channels do not become diagnostic keys. Raw
failure text remains available; status extraction is unchanged. Tool recoveries
are stamped `recovery-0.13`; re-analyze existing source captures to refresh their
stored signatures. Re-convert Harbor bundles to refresh stored test diagnostics.

CTRF failures preserve the first pytest `E ` assertion (otherwise the deterministic
diagnostic selector), its normalized signature and signature version, a redacted
2,000-character excerpt, a truncation flag and a source pointer. Checks and the
“What resulted” evidence panel show the diagnostic. Output excerpts remain
explicitly post-run evidence.

`GET /fleet/test-failures?group_by=task,test,signature` returns ranked groups.
`group_by=task` and `group_by=signature` are also supported. `limit` and `offset`
page the result; `X-Total-Count` reports all groups. Each group includes every
matching `run_id` for drill-down and at most three check/capture examples.

Counts use each logical run's latest imported capture. Task and verifier versions
partition task cohorts. Unknown, unverified and operational outcomes remain in
`attempts_total` and `unknown_attempts`; only PASSED and FAILED count as verdicts.
`always_fails_task` requires every imported attempt to have outcome FAILED.
`no_observed_pass` is separate. Signature-only denominators are the union of the
contributing cohorts; cross-configuration claims are disabled across cohorts or
with unresolved versions/configurations. Signature revisions are kept separate.

“This failure appears in every tested configuration” means at least one matching
failure per (agent, model) configuration. It does not mean the signature occurred
in every attempt or establish a task/verifier defect. The optional investigation
button opens an example run's disposition menu and never writes a disposition.

Harbor task versions use `checksum:<task_checksum>`, falling back to the trial
config's `git:<git_commit_id>`. Bundled verifier versions use the same value;
an explicitly supplied external verifier stays unresolved. The historical
[Harbor Task.checksum implementation](https://github.com/harbor-framework/harbor/blob/e3ffbc2bef970c7c3bd33871220a47a681f33b3f/src/harbor/models/task/task.py)
hashes the entire task directory including `tests/`. Git URL, commit, task path,
version sources and checksum are retained in run metadata. Contract warnings
state the version sources; matching versions leave environment, seed and
parameter caveats intact.

When trial `config.task.source` is `terminal-bench`, default task IDs get the
`terminal-bench/` prefix. Explicit caller task IDs win. Execution identity is
resolved before namespacing so re-imports do not create duplicate logical runs.

Comparison reads only the persisted metric evidence rather than enriching every
full review with fleet alternatives. Fleet-list caches start their lifetime after
a slow scan completes while retaining write-sequence invalidation. Patterns
read only run identity, versions, outcomes and their own persisted evidence;
they do not build full review/inbox summaries. Test records are shared across
grouping and pagination requests. A direct Patterns link defers the inbox until
run navigation, and supporting cards load independently of the tool table.
The test table fits wider content areas; narrower areas show labelled rows with
all counts visible.

## Upgrade existing stores

Re-import original Harbor trial/job bundles with `agr ingest-harbor`. The adapter
is now `harbor-adapter-0.14`; event mapping, check extraction, contract building
and signature derivation are versioned. Re-importing creates a new immutable
capture under the existing logical run ID, retaining older captures. A second
import of the same converted document is idempotent. Re-reading old checks
cannot restore CTRF output that was discarded at ingestion.

The existing polyglot demo fixture now attaches its captured C=6 assertion to
its sole failing atomic test. Its saved review carries a capture-refresh note
and is revalidated on demo import without another model call.

## Leaderboard verification

The local acceptance corpus is pinned to Hugging Face revision
`572b2614be2c0cb2527e14f5b1e4026f1072e6c1` of
`harborframework/terminal-bench-2-leaderboard`, Terminal-Bench 2.0:
Terminus2 GLM-4.7, GLM-5, DeepSeek-V3.2, Kimi-k2.5, Minimax-m2.5 and GPT-5.3-Codex.

Expected persisted-store results: 2,657 imported runs (13 missing trajectories
excluded), 440 failure groups, 103 spanning at least four configurations, and
12 task cohorts where every imported attempt explicitly failed. This differs
from 15 tasks with no observed pass and from 10 strictly failing tasks across
all 2,670 downloaded trials. Excluded source trials are outside the store's
denominator.

GLM-4.7 to GLM-5, axis=model: valid/matched, 435 pairs across 89 tasks,
147/435 versus 226/435 passing. The ten surplus candidate attempts and unresolved
environment/seed/parameter caveats remain visible. Terminus tool-failure groups
stay at zero. The unspecified original 30-run Claude sample is not an acceptance
fixture; an available pinned Claude polyglot sample has 19 explicit errors.

To serve the isolated acceptance store in this checkout:

```powershell
.venv/Scripts/python.exe -m agr --store .benchmarks/tb2-feature-store serve --read-only --port 8772
```

Open `http://127.0.0.1:8772/?view=fleet&pattern_type=tests`.
