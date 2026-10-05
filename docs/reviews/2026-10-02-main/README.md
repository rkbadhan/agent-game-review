# Main branch review — October 2, 2026

Agent Game Review has a substantial working MVP and a strong automated test suite. The next stage is correctness hardening and independent evaluation. The repository does not yet establish that its reviewer reliably finds the most useful moments on real workloads, or that it improves on existing viewers.

## Reviewed version and validation

- Fetched remote main: `d62efb856393ea236d97edf7bb4ffa12e871653a`, last commit September 26, 2026, merge of PR #99 (EV evaluation workflows).
- Local main: `a875f0500704f79adc5af48accd0a85f7ca76ea4`, **116 commits behind** remote main. The existing checkout was clean before this review and was left at the same commit.
- Reviewed a separate exported snapshot in `C:/Users/Rahul Badhan/AppData/Local/Temp/eval-trace-main-d62efb8-review`.
- Repository scope: 59 Python modules in `agr/` (26,443 lines), 24 browser JavaScript modules, 68 test modules, packaging, deployment configuration, CI, product specifications, and evaluation documentation.
- Local full suite: **1,256 passed, 4 skipped, 0 failures**, including **36 browser tests**, in approximately 184 seconds, using the existing Python 3.12 environment. [Local test results](test-results.xml).
- Skips: missing optional `python-dotenv`; opt-in Anthropic live-model test without credentials; two deliberately skipped non-string path-safety cases already covered by direct validation tests.
- Remote main CI: Linux, Windows, installed-package smoke test, and browser job all passed. [CI run](https://github.com/rkbadhan/agent-game-review/actions/runs/36257366703).
- All 24 browser scripts parsed successfully. An additional Node VM probe reproduced initialization failure with blocked browser storage.
- `agr eval` on the six synthetic reference runs: Precision@3 = 1.0, Recall@3 = 1.0, evidence-span precision = 0.8571, evidence-span recall = 0.3529, affected-check overlap = 0.5. [Evaluation artifact](synthetic-eval.json). These establish fixture behavior, not real-world reviewer quality.

This was a repository-wide architecture and status review with deeper inspection and executable probes of the trust, storage, evaluation, and workflow boundaries. It is not an exhaustive security audit or a claim that every source line is defect-free. No paid model requests, live customer integrations, deployments, or production load tests were performed. Source code was not changed; this directory contains review artifacts only.

## Confirmed findings

The seven Python probes below are recorded in [probe-results.json](probe-results.json); the eighth finding was reproduced by [browser-probe.js](browser-probe.js). All token examples are fabricated. Priority P1 means resolve before relying on the affected operation; P2 means a concrete correctness issue that should be fixed in the hardening pass.

### 1. [P1] Hyphenated API tokens survive model-facing redaction

[agr/redaction.py:81](https://github.com/rkbadhan/agent-game-review/blob/d62efb856393ea236d97edf7bb4ffa12e871653a/agr/redaction.py#L81)

The generic API-key regex only matches `sk-` followed by at least 20 alphanumeric characters. Fabricated tokens beginning `sk-proj-`, `sk-svcacct-`, and `sk-or-v1-` survive unchanged. The probe verified survival through `build_packet()` and the additional `redact_value()` traversal, not just the standalone regex.

Consequently, a trace or task instruction containing such a token can send it to the configured reviewer provider. This does not imply that real credentials were found in the committed corpus; the reproduced defect concerns the redaction boundary.

Fix: recognize the supported token prefixes and their complete payloads, and add model-packet regression cases covering instructions, structured input, dictionary keys, and expansion/revision requests. Retain typed redaction markers.

### 2. [P1] A deterministic benchmark can score a previously stored model review

[agr/cli.py:1224](https://github.com/rkbadhan/agent-game-review/blob/d62efb856393ea236d97edf7bb4ffa12e871653a/agr/cli.py#L1224), [agr/benchmark.py:549](https://github.com/rkbadhan/agent-game-review/blob/d62efb856393ea236d97edf7bb4ffa12e871653a/agr/benchmark.py#L549)

When `agr benchmark` runs without a provider, it passes `reviewer_key=None`. On a reused explicit `--store`, deterministic analysis refreshes its own slot but leaves model slots intact. The default read selector prefers a healthy model slot, so the scorer can consume an older model review while the frozen manifest declares `reviewer.kind = deterministic`.

Reproduction: seed the Who&When fixture with a scripted model review, then run the benchmark with no reviewer factory. The manifest says deterministic; the actual selected slot is `model:scripted`, and the scored status is `no_decisive_moment` instead of the deterministic slot's `not_configured`. A fresh default scratch store avoids this particular trigger.

The older `agr eval --store ...` baseline path also uses default review selection and deserves the same repair.

Fix: explicitly score `deterministic` for baseline runs and the exact configured reviewer key for model runs. Record the actually served key and reject fallback/substitution in evaluation paths. Add fresh-store versus reused-store equivalence checks.

### 3. [P2] Concurrent workflow edits bypass optimistic version protection

[agr/workflow.py:208](https://github.com/rkbadhan/agent-game-review/blob/d62efb856393ea236d97edf7bb4ffa12e871653a/agr/workflow.py#L208), [agr/workflow.py:255](https://github.com/rkbadhan/agent-game-review/blob/d62efb856393ea236d97edf7bb4ffa12e871653a/agr/workflow.py#L255)

Reading the workflow, checking `base_version`, and writing the new record are separate operations. Two requests can both read version 0, both pass the check, and both return success at version 1. The last write overwrites the other edit and its revision history.

The two-thread probe synchronized the initial reads and serialized only the physical writes. Both edits succeeded, but the stored workflow had one revision and only one editor's note. This isolates the lost-update problem from concurrent JSON-file truncation.

Fix: hold a per-capture lock over the complete read/check/write transaction, with cross-process protection if multiple processes can write. Replace JSON files atomically. Apply the same transaction design to append-style feedback, lessons, and capture index updates; those neighboring paths were inspected but not separately race-tested here.

### 4. [P2] Adapter-version changes create self-referential capture lineage

[agr/ingest.py:118](https://github.com/rkbadhan/agent-game-review/blob/d62efb856393ea236d97edf7bb4ffa12e871653a/agr/ingest.py#L118), [agr/store.py:141](https://github.com/rkbadhan/agent-game-review/blob/d62efb856393ea236d97edf7bb4ffa12e871653a/agr/store.py#L141)

Capture registration distinguishes `(source_hash, adapter_version)`, but the capture directory identifier depends only on `source_hash`. Ingesting identical source bytes through the supported explicit adapter-version override registers revision 2 in the same directory as revision 1. Revision 2 then says it supersedes itself, and its `run_source.json` overwrites the earlier adapter provenance.

Reproduction: `ingest(doc, store, adapter_version='adapter-v1')`, then the same document with `adapter-v2`. Both capture IDs were `capture_b1eb5f9cacff`; the second predecessor was that same ID; reading the first capture's persisted provenance returned `adapter-v2`. Source bytes themselves remained unchanged.

Fix: align registration identity and persisted revision identity. Either version derived analyses separately under one immutable source capture or include the adapter version in a distinct processing/capture identifier. Preserve prior provenance and prohibit self-supersession.

### 5. [P2] Human-audit sampling loses the actual verifier outcome

[agr/evaluation.py:83](https://github.com/rkbadhan/agent-game-review/blob/d62efb856393ea236d97edf7bb4ffa12e871653a/agr/evaluation.py#L83)

`create_audit_pack()` stratifies on `source.verifier.status`. Normal source documents store statuses on `verifier.checks[]`; the reconciled run outcome is a derived field. The top-level source status is normally absent.

The probe sampled one genuinely PASSED and one FAILED run. Both were recorded in strata prefixed `unverified`. This collapses a promised sampling dimension and makes the audit manifest misdescribe its population, even though proportional allocation remains implemented.

Fix: use the normalized outcome from the run summary or served review and the shared outcome-bucket function. Test pass, fail, undetermined, and genuinely unverified inputs from actual adapter output shapes.

### 6. [P2] A command containing “check” earns false verification credit

[agr/signature.py:138](https://github.com/rkbadhan/agent-game-review/blob/d62efb856393ea236d97edf7bb4ffa12e871653a/agr/signature.py#L138)

The verification signature uses the substring `check` anywhere in a tool call or result as proof of verification. Inserting only `git checkout -b feature` between artifact observation and submission changed the row to `Successful` / `Positive evidence`, despite no artifact inspection or test execution occurring.

Fix: recognize real verification actions from paired calls/results and relevant structured checks or explicit artifact inspection. Cite the verification action itself. A substring match on unrelated command/output text should not establish the behavior.

### 7. [P2] Ability signatures ignore reconciled check status

[agr/signature.py:56](https://github.com/rkbadhan/agent-game-review/blob/d62efb856393ea236d97edf7bb4ffa12e871653a/agr/signature.py#L56)

Requirement signature rows read raw `c.status`, while outcome reconciliation uses `c.effective_status`. A failed test superseded by a later same-scope pass still produces a current negative signature row. The same implementation can positively report a pass that reconciliation has marked stale after a relevant mutation.

The pipeline probe produced an old check with `effective_status=None` and a new check with `effective_status='passed'`, yet the signature emitted both `Failed` and `Successful` for the same test suite without a historical/superseded label.

Fix: use effective status for current ability assessment. Preserve historical observations in an explicitly labeled history view, and represent stale/unknown evidence without awarding success.

### 8. [P2] Blocked browser storage aborts application state initialization

[agr/static/js/state.js:54](https://github.com/rkbadhan/agent-game-review/blob/d62efb856393ea236d97edf7bb4ffa12e871653a/agr/static/js/state.js#L54)

The top-level `state` initializer calls `localStorage.getItem()` without protection. A `SecurityError` leaves the shared state uninitialized and breaks dependent scripts. Panel width reads and several writes have the same unguarded pattern, although theme/sidebar storage operations already use try/catch.

The Node VM probe reproduced failure immediately when storage access threw. This was a simulated restricted-storage environment; the 36 normal browser tests passed.

Fix: centralize storage access behind safe read/write helpers with in-memory/default fallbacks. Add a browser initialization test with storage disabled.

## Where the project stands

| Area | Assessment from main |
| --- | --- |
| Deterministic analysis | Implemented: ingestion, source capture, events, atomic checks, contracts, opportunities, recovery, evidence slicing, detectors, task/verifier audit, and ability signatures. Confirmed trust/lineage defects above need repair. |
| Integrations | Six registered source adapters: Harbor, pi, Claude Code, OTel, Langfuse, LangSmith. Recorded-fixture paths are tested; live customer exports and authenticated services were not tested in this review. |
| Model reviewer | Implemented: Anthropic and OpenAI-compatible providers, phase chunking, bounded packets, expansion, structured-fact validation, reference/quote gates, attribution limits, selection/deduplication, explicit failure/incomplete/abstention states, and budget telemetry. Provider behavior was not validated with live paid calls. |
| Product UI | Working Runs, guided review, timeline/evidence/full trace, Patterns, comparisons, corrections, dispositions, lessons, and keyboard workflows. Real precomputed demo reviews ship with no required model call. |
| Execution quality | Implemented separately from outcome, including context usage, latency, repeated work, fleet incidence, evidence drilldown, and missing-telemetry reporting. |
| Evaluation tooling | Who&When and local TRAIL adapters/scorers, evidence-integrity audit, seeded human-audit packets, frozen run manifests, and scoped report aggregation are implemented. Tooling is ahead of completed independent validation. |
| Real evaluation evidence | `gold/real` contains no gold annotation files. The committed corpus manifest records 22 eligible logical runs, but it is a September 8 historical artifact. Gate B1 remains a historical, provisional HOLD; subsequent changes require a fresh registered comparison. |
| Improvement loop | Lessons and experiment proposals exist. Controlled replay, private-holdout experiment execution, feedback-to-consumer lineage, skill aggregation, and exportable training signals remain incomplete/deferred. |
| Packaging/operations | Version 0.2.0 is unreleased. Wheel assets, Docker demo, read-only serving, Render configuration, and CI exist. Shared writable use needs storage transactions; production load and recovery behavior remain unmeasured here. |

The strongest part of the design is its explicit distinction between recorded evidence, model interpretation, missing observability, verifier outcome, and execution quality. The integrity audit also correctly avoids treating valid references and authentic quotes as proof that narrative explanations are true. Maintain those boundaries while repairing the signature and evaluation paths.

Several status documents are stale: the coverage checklist marks milestone work closed while real independent labeling remains pending, and marks performance/cost budgets absent despite reviewer budget targets now existing. `DEPLOY.md` still describes an entirely synthetic demo, while the Docker build now includes real runs and baked reviews. The landing page leaves `DEMO_URL` empty, so its deployment hookup is unfinished in this source snapshot. These are documentation/launch follow-ups, not claims about whether an external deployment currently exists.

One open PR was found: [#95 — Add FQ-1 fleet-scale semantic query implementation](https://github.com/rkbadhan/agent-game-review/pull/95). That proposed capability is not on the reviewed main branch.

## Recommended order of work

1. Repair redaction and evaluation reviewer selection before sending sensitive traces or generating baseline claims.
2. Repair storage transactions/capture lineage and the two signature errors, with regression checks for the reproduced triggers.
3. Correct audit strata before drawing the independent human sample. Freeze configuration, data partitions, thresholds, and reviewer identity.
4. Run the external benchmarks and blind/revealed human audit, with independent adjudication and clear denominators. Publish scoped results, including failed targets and unchecked narrative coverage.
5. Test usefulness on users' own traces: time to understand a failure, useful evidence-supported findings, repeat use, and resulting improvement decisions. Then prioritize fleet search or the experiment loop from those observations.

Treat main as a feature-complete MVP under hardening and validation. A demo or limited pilot is reasonable; strong reviewer-quality, comparative-superiority, and shared-production-readiness claims are not yet supported by the repository evidence.

## Reproducing this review

[probes.py](probes.py) contains offline reproductions for findings 1–7; [browser-probe.js](browser-probe.js) parses the browser scripts and reproduces finding 8. Run them with the latest reviewed source directory as the current working directory, using Python with the repository's dev dependencies and Node. They use temporary synthetic stores and do not call real providers or execute the recorded agent commands.

```powershell
# From the repository root, with the dev dependencies installed:
python docs/reviews/2026-10-02-main/probes.py
node docs/reviews/2026-10-02-main/browser-probe.js
```

The review ran in an exported snapshot of the pinned main commit. This PR adds only the review report, offline reproductions, and recorded validation artifacts. Application code and the local main branch remain unchanged.
