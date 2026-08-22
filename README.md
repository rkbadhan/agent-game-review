# Agent Game Review

**Five-minute path** — clone to browsing the demo sweep:

```bash
pip install .[api]
agr demo
# Open http://127.0.0.1:8000 in your browser
```

That installs the package (with the optional HTTP transport), builds a
synthetic two-configuration demo store with 12 runs across 5 tasks, and
starts the evidence-browser SPA. All contracts are auto-confirmed; no
watermarks, no credentials, no model calls. Press `Ctrl+C` to stop.

The explicit command chain (no single-command shortcut):

```bash
pip install .[api]
agr demo-store                  # build the demo data into .agr-store
agr serve                       # start the server at http://127.0.0.1:8000
```

> **Verifying the five-minute path** — two ways:
> - `python -m pytest tests/test_demo_path.py` (in-process, 22 tests, runs in CI)
> - `bash scripts/verify-five-minute-path.sh` (cold-start simulation: fresh venv,
>   pip install, actual CLI commands, HTTP endpoint checks)

## Review your own run

The demo is synthetic. To review a **real** run, ingest it and open the same
browser — no model calls or credentials needed for the deterministic review.

**A Harbor / Terminal-Bench 2.0 run.** Point `ingest-harbor` at one trial
directory, a `trajectory.json`, or a whole `harbor run` job directory (batch):

```bash
pip install .[api]
agr ingest-harbor ./my-harbor-run/     # trial dir, trajectory.json, or a job dir
agr runs                               # see it listed with its outcome
agr serve                              # browse at http://127.0.0.1:8000
```

**A pi session.** A pi `.jsonl` transcript ingests the same way:

```bash
agr ingest-pi ./session.jsonl
agr serve
```

Task identity, the verifier outcome, and capabilities come from what the
harness itself recorded; where the adapter has to infer something it says so
with a contract warning, and a run ingested from a bare verifier stays
`PROVISIONAL` until you confirm its contract with `agr confirm`. A full
walkthrough — running a small model through Harbor and reviewing the result —
is in [`docs/terminal-bench.md`](./docs/terminal-bench.md); the source formats
and how to add your own adapter are in [`docs/adapters.md`](./docs/adapters.md).

---

**See *why* your agent eval runs passed or failed.** Point Agent Game Review
at a Terminal-Bench / Harbor sweep (or any agent trajectory) and get a
per-run, evidence-linked behavioural review: the decisive moments, what the
agent actually did, and what to fix — including the failures a green pass-rate
hides.

Every review is grounded in a **deterministic core**: it ingests an agent
trajectory, stores it immutably, derives an event timeline, decomposes the
verifier outcome into atomic checks, builds evidence slices, and runs
deterministic detectors — no model calls anywhere in this package. A model
reviewer (Stage F) can enrich the narrative on top, but every structured fact
it rests on is recomputed here, so the interpretation stays bounded by
evidence rather than free to hallucinate.

**New here?** [`examples/`](./examples/README.md) is a six-run guided tour of
exactly what a review surfaces — a clean pass, a recovered failure, a run that
passed the verifier but still misbehaved, an ignored failure, a partial-credit
miss, and a task whose contract contradicts itself.

## Why start here

The spec ships its own build order (§20). Milestone 0 is human annotation;
the first *code* is Milestone 1, and Milestones 1–3 are almost entirely
deterministic — which makes them cheap to build and rigorously testable
against fixtures before any spend on the model reviewer (M4) or a UI. This
package is that deterministic slice.

> Note on "Harbor ATIF": the spec names Harbor's trajectory format as the MVP
> source. There is no public library for it, so this adapter defines a
> concrete, minimal ATIF-shaped JSON contract (see `archive/synthetic/fixtures/`) and imports it
> deterministically, preserving the exact `atif_version` on every capture.

## Layout

```
agr/
  schema.py        Typed records (spec §6): RunSource, CapabilityProfile,
                   DerivedEvent, VerifierCheck, EvidenceSlice, Opportunity,
                   RecoveryEpisode, Candidate.
  store.py         Immutable, content-addressed capture store (§5.1, §7.3).
  ingest.py        ATIF ingestion + capability profiling (§7, Stage A).
  events.py        Derived event timeline (§6.3, Stage A).
  checks.py        Atomic verifier checks + run outcome (§6.6).
  opportunities.py Opportunity windows (§3.2, §6.7).
  recovery.py      Recovery state machine (§8.6).
  predicates.py    Safe absence/presence predicates (§8.4, §16.1).
  evidence.py      Evidence slicing — standard/omission/distributed/external (§8.4).
  detectors.py     Five MVP detectors + capability gating (§8.5, §6.2).
  reviewer.py      Reviewer envelope + seam — fact validation (§8.8), attribution
                   gate (§8.9), moment selection/dedup (§8.10), Reviewer protocol.
  taxonomy.py      Controlled behavioural vocabulary (§9.1) shared by gold + reviewer.
  redaction.py     Redaction + untrusted-content isolation before any model call (§7.4, §16.1).
  model_packet.py  Typed reviewer input packet from derived records only (§8.7).
  model_reviewer.py Stage F model reviewer — Scripted/Anthropic/OpenAI adapters (§8.7).
  contract.py      Contract builder + human-confirmation flow (§8.2, §6.4, §6.5).
  signature.py     Task Ability Signature rows (§4.3).
  pipeline.py      Orchestration (Stages A, B, D, E + recovery/opportunities/signature).
  phases.py        Deterministic phase segmentation (Stage C1, §8).
  read.py          Read layer over the store — summaries, review, forensic + guided views.
  api.py           Optional FastAPI transport over `read.py` + serves the SPA (`.[api]` extra).
  static/          Self-contained evidence-browser SPA (vanilla JS, no external hosts).
  gold.py          Reviewer gold-set schema, loader, validator + disagreement report (§15.2).
  reviewer_eval.py Reviewer evaluation harness — Precision@3/Recall@3 etc. (§15.3).
   ingest_pi.py     Pi session adapter — pi session JSONL → ATIF-shaped doc (§5.3).
   ingest_harbor.py Harbor adapter — Terminal-Bench 2.0 trial or job dir → ATIF-shaped doc,
                    with the reward → verifier synthesis (§5.3; see docs/terminal-bench.md).
   cli.py           `python -m agr ingest-harbor|ingest|ingest-pi|show|confirm|runs|eval|review|serve`.
archive/synthetic/fixtures/  Synthetic ATIF runs (chess, recovery, retry, ignored-failure,
                   contract-mismatch, clean-pass).
archive/synthetic/gold/  Synthetic reviewer gold set: annotation guide + per-run gold labels (§15.1).
tests/             Milestone 1 + Milestone 2 + Milestone 3 acceptance tests
                   (read-model tests are stdlib; HTTP tests skip without FastAPI).
```

## Real runs vs archived synthetic data

The **primary real data path is an eval framework**: [Harbor](https://www.harborframework.com),
the official harness for **Terminal-Bench 2.0**. A `harbor run` job directory
(one subdirectory per trial, each with its ATIF-v1.7 `trajectory.json` and
`result.json`) ingests in one command — task identity comes from what Harbor
recorded on the trial, and the verifier is derived from its reward:

```bash
python3 -m agr ingest-harbor <trial_dir>     # one trial
python3 -m agr ingest-harbor <job_dir>       # every trial in a harbor run
python3 -m agr runs
```

The other real path is **pi sessions**: `agr ingest-pi` converts a pi session
file (`~/.pi/agent/sessions/**/*.jsonl`) into the ATIF-shaped contract and the
normal pipeline takes over. Nothing is invented — task identity, instruction,
and the verifier result are explicit inputs or honestly absent (a run with no
verifier sidecar ingests as **UNVERIFIED**, never a vacuous pass):

```bash
python3 -m agr ingest-pi ~/.pi/agent/sessions/<dir>/<session>.jsonl \
    --task-id my-task --verifier verifier-sidecar.json
python3 -m agr runs
```

Other harnesses (opencode next, then otel) plug into the same generic path,
`agr ingest-from --adapter <name>`; see
[`docs/adapters.md`](./docs/adapters.md) for the adapter roadmap.

The original **synthetic** fixtures and gold labels — hand-authored to build
the core before any real source existed — are parked under `archive/synthetic/`
(see its README). The test suite and `agr eval` still use them (the yardstick
doesn't change), but they no longer mix with real runs: the default
`.agr-store` holds real ingested sessions only, and `agr eval` scores in an
isolated scratch store unless `--store` is passed explicitly.

## Use

```bash
python3 -m pytest                                          # 135 tests (API/UI + live-model tests skip without their extra/key)
python3 -m agr ingest archive/synthetic/fixtures/chess_best_move.atif.json   # into .agr-store/
python3 -m agr show    chess_best_move__seed42

# Task contract + human confirmation (Milestone 2)
python3 -m agr ingest archive/synthetic/fixtures/contract_mismatch.atif.json
python3 -m agr show    greeting_report__seed7              # watermarked, 6 warnings (5 types)
python3 -m agr confirm greeting_report__seed7 --all --by me  # clears the watermark
```

### Reviewer gold set + evaluation harness (Milestone 0, ahead of M4)

The spec builds the reviewer's **gold dataset and evaluation harness before the
model reviewer itself** (§15, §20 Milestone 0, principle #11): you cannot accept
the reviewer ("Precision@3 and missed-critical targets met on held-out labelled
traces", §20 M4) without a yardstick that already exists and already runs. This
increment is that yardstick.

- `agr/gold.py` — the annotation schema (§15.2): decisive moments anchored on
  **source step ids**, anchor type, evidence spans, behaviour tags (§9.1),
  affected checks/requirements, attribution ceiling, opportunity window, ranked
  root-cause candidates (§11 loci), acceptable better actions, and the
  no-decisive-moment case. `validate()` rejects anything outside the controlled
  vocabularies or referencing a step the source never captured; multi-annotator
  trajectories carry an adjudication and a `disagreement_report`.
- `archive/synthetic/gold/` — the synthetic gold labels for the fixtures, covering every required sampling
  category (§15.1): omission, external tool failure, recovery, a low-value
  concern, a task/verifier concern, and a clean-pass no-moment case. See
  [`archive/synthetic/gold/README.md`](./archive/synthetic/gold/README.md) for the annotation guide — the labels are
  honestly marked as *synthetic reference annotations* for the synthetic
  fixtures.
- `agr/reviewer_eval.py` — the **reviewer-agnostic** harness (§15.3). It scores a
  list of `PredictedMoment`s against the adjudicated gold, in source-step
  coordinates, computing Precision@3, Recall@3, redundant-card rate,
  missed-critical rate, no-moment calibration, evidence-span precision/recall,
  and attribution-overclaim rate. The only reviewer that exists yet is the
  deterministic detector layer, so its projected moments are the **baseline** the
  harness scores today; the M4 model reviewer will feed the same functions and
  must beat it.

```bash
python3 -m agr eval        # ingest fixtures, validate gold, score the baseline
```

The raw detector layer scored `Recall@3 = 1.0` (it recovers every decisive gold
moment) but `Precision@3 = 0.625` with a `redundant_card_rate` of `0.375` —
several detector cards fired on one moment. The **deterministic reviewer
envelope** (Stages G/H/I, see below) now de-duplicates those cards before the
harness scores them, so `agr eval` reports `Precision@3 = 1.0` and
`redundant_card_rate = 0.0` **with `Recall@3` still `1.0`** — the moment-selection
win M4 promised (§8.10), reached with no model call. It still calibrates the clean
pass to *no card* and never overclaims attribution (`0.0`), since the core caps at
`dependency_linked`. That improved envelope is the honest baseline the model
reviewer (Stage F) must itself beat.

### Model reviewer (Milestone 4, Stage F)

The Stage F reviewer is the one model-assisted stage. It receives a **typed,
redacted packet** (contract + atomic checks + deterministic candidates + compact
evidence + phase summaries — never the raw trace; `agr/model_packet.py`,
`agr/redaction.py`) and returns taxonomy verdicts, behaviour tags, ranked root
causes, and the smallest better action. It plugs into the `Reviewer` seam
**behind** the deterministic envelope, so its facts are recomputed (Stage G), its
attribution language capped (Stage H), and its cards selected (Stage I). It is
given no tools, and trace content reaches it only as data — a reviewer fooled by
injected trace text still cannot fabricate a card, because a false fact fails
recomputation and is dropped.

Two provider adapters sit behind the same seam (`agr/model_reviewer.py`); each is
an optional extra and is lazily imported, so the core installs and tests without
either. See [`.env.example`](./.env.example) for the (optional) credentials — none
are needed for the deterministic `ingest`/`show`/`eval`/`serve` commands.

```bash
pip install '.[model-anthropic]'   # or '.[model-openai]'
export ANTHROPIC_API_KEY=...        # or put it in ./.env (auto-loaded); OPENAI_API_KEY for --provider openai
python3 -m agr ingest archive/synthetic/fixtures/chess_best_move.atif.json
python3 -m agr review chess_best_move__seed42 --provider anthropic --model claude-opus-4-8
```

The CLI auto-loads a `./.env` file (via python-dotenv, bundled with the model
extras; override with `--env-file`), so credentials and the model id can live in
`.env` — see [`.env.example`](./.env.example). An exported env var still wins.

**Use any model.** The OpenAI adapter speaks the standard chat-completions format,
so `--base-url` points it at any OpenAI-compatible endpoint — OpenRouter, Together,
Groq, or a local vLLM / Ollama / LM Studio server:

```bash
pip install '.[model-openai]'
python3 -m agr review chess_best_move__seed42 --provider openai \
    --model meta-llama/llama-3.1-70b-instruct \
    --base-url https://openrouter.ai/api/v1        # or http://localhost:11434/v1, etc.
```

`--base-url` falls back to `OPENAI_BASE_URL`; the endpoint's API key comes from
`OPENAI_API_KEY` (set any non-empty value for a local server that ignores it). The
`AnthropicReviewer` accepts a `base_url` too, for an Anthropic-compatible gateway.
The model id resolves `--model` → `AGR_REVIEW_MODEL` → the provider default, so you
can pin the model in `.env` instead of passing it every time.

The guided view then shows the real taxonomy verdict in place of the neutral
`concern`/`strength` tag, with the model-generated better action labelled as such;
runs reviewed deterministically keep the neutral tag and a `deterministic_only`
badge. The live `agr eval` comparison against the gold baseline needs a provider
key and is therefore opt-in (skipped in CI).

**Score a model against the baseline.** `agr eval --provider <p>` runs the model
reviewer over the gold runs and prints a baseline→model comparison plus the M4
acceptance verdict — the model must not regress on precision/recall/missed-critical
and must not overclaim attribution:

```bash
python3 -m agr eval --provider openai   # (add --model / --base-url as for `review`)
```
Because the deterministic envelope de-duplicates whatever the model surfaces, a
chatty model does not cost precision — the selector, not the model, guards it.

### Read API (Milestone 1 evidence browser)

The read layer projects the immutable source and its persisted derived records
into read-only views — run summaries, the full deterministic review, and the
synchronized *forensic view* (spec §4.5): every source step routed onto the
timeline / agent-message / tool-I/O / environment / artifact / verifier panels,
with a capability badge marking which evidence was captured completely,
partially, or not at all. It recomputes nothing and never writes, so a GET is
side-effect free.

```bash
python3 -m agr runs                                        # summary of every run
pip install '.[api]'                                       # optional HTTP transport
python3 -m agr serve --store .agr-store                    # open http://127.0.0.1:8000
```

`serve` hosts both the JSON API and the **evidence browser** — open the root URL
in a browser to click through it.

| Endpoint | Returns |
|---|---|
| `GET /` | the evidence-browser SPA (triage inbox, guided review, forensic, source) |
| `GET /sweep` | implicit-sweep summary — outcome/review-mode counts, handling progress (§4.3.1) |
| `GET /queue` | a grouped/filtered/sorted triage queue with a stable `queue_view_id` (§4.3.2) |
| `GET /runs` | one summary row per logical run (outcome, review mode, counts, workflow) |
| `GET /runs/{run_id}` | full deterministic review + human workflow and feedback |
| `GET /runs/{run_id}/forensic` | synchronized panels + capability badge (§4.5) |
| `GET /runs/{run_id}/source` | raw immutable source with a re-verified hash |
| `GET /runs/{run_id}/reviews` | reviewer keys that have scored this capture |
| `GET /runs/{run_id}/compare` | diff of two reviews of one run (reviewer vs reviewer) |
| `GET /configurations` | pinned configurations a comparison side can select (§4.16.1) |
| `GET /comparisons/preview` | match report + result for an unsaved definition (§4.16) |
| `GET /comparisons/{id}` | a saved comparison, recomputed from its frozen definition |
| `POST /comparisons` | freeze a comparison definition (§4.16.1) |
| `GET /runs/{run_id}/next` | the next unhandled run in a frozen queue order (§4.2) |
| `POST /runs/{run_id}/workflow` | set disposition / assignment / progress — optimistic on `base_version` (§4.3.4) |
| `POST /runs/{run_id}/feedback` | Tier-1/2/3 moment feedback and corrections — idempotent on `mutation_id` (§4.13) |
| `POST /events` | review-workflow analytics events, allowlisted (§4.21) |
| `GET /metrics` | derived product measures over that log (§4.21) |

The two `POST` routes are the only writes; they record human review as **new
derived records beside the immutable source** (exactly like `agr confirm`),
never mutating the source or a generated moment. Writes are idempotent or
optimistic-concurrency checked (§4.17); a stale write returns `409` with the
current state so no unsaved edit is lost.

The browser front-end is a single self-contained `static/index.html` (vanilla
JS, inline CSS, no external hosts, light **and** dark) served by the same app.
It is a three-column workspace: a **runs triage inbox** (§4.3) on the left
(sweep summary, `Failed / Needs attention / Recovered / Verifier concern /
Unreviewed` chips, a sort control defaulting to *Triage priority*, and run
cards), the **guided review** in the centre, and a persistent
**evidence panel** (§4.9) on the right with trust cards (evidence grade +
attribution) and an interpretation-boundary note. Both side columns are
**resizable** — drag the divider, nudge it with the arrow keys, double-click to
reset — and the widths are remembered per browser. A keyboard workflow (§4.20:
`[`/`]` chapters, `J`/`K` moments, `E` evidence, `D` disposition, `N` next
unhandled, `T` trace, `C` compare, `?` help, `Esc`), a disposition menu, and
toast confirmations round it out.
The centre column is driven by the **review outline rail** (§4.2/§4.4) — six
chapters (Outcome, Opportunities, Key moments, Strongest behaviour, Ability
signature, Eval lesson), each marked completed / current / corrected /
unavailable, with an unavailable chapter naming *why* rather than going blank —
plus two non-chapter views (**Compare**, **Source**) and an **Open full trace**
slide-over:

- **Outcome** (§4.5) — the opening chapter: overall outcome, the atomic-check
  table with each check expandable to its verifier evidence and mapped contract
  item, requirement warnings, and a **final environment/artifact summary** — the
  closing observed state, where a declared artifact the capture never observed
  stays a visible row and the filesystem/process-state capability levels that
  bound the section are printed beside it — then the review mode and what limits
  the review.
- **Key moments** (default) — the §4.7 Key-Moment Review workspace: a unified
  **Review Timeline** (§4.8) with one event axis and three aligned lanes (phase /
  contract-progress / moment markers — discrete markers only, no fabricated
  success-probability curve; markers that share an event cluster side by side
  rather than overlap), a **five-block moment card** (§4.7.1: Situation / Agent
  action / Observed consequence / Likely impact / Better action) — each block
  labelling its own source (validated fact / source event / interpretation /
  hypothesis) — with moment-centric *Moment X of Y* navigation. On a deterministic
  review the three fact blocks are restated from validated facts while *Likely
  impact* and *Better action* read "Not generated" rather than being fabricated
  (§4.15). Tier-1/Tier-2 actions — *Agree*, *Not decisive*, *Flag task/verifier*,
  *Correct* — sit on the card; evidence for the selected moment fills the right panel.
  *Correct* stays a one-click relabel and its confirmation offers **Add detail**,
  which escalates to the §4.13 Tier-3 editor: the nine expert fields, each shown
  beside the value it replaces, validated against the active taxonomy before the
  write lands. Correcting something *factual* (consequence, opportunity window,
  linked checks) requires evidence; interpretation is the reviewer's to give.
  `counterfactually_supported` cannot be set by hand — replay evidence is not
  something a human can grant themselves. Saving creates a new annotation
  revision: the card shows **Human corrected** and the accepted values, the
  generated version stays one click away, and the only lineage claimed is the one
  that exists ("Applied to this review").
- **Compare** — a side-by-side view of two reviews of the *same* capture (the
  deterministic baseline and a Stage F pass, or two model passes), aligned by
  anchor overlap into matched / added / removed / redundant pairs with
  field-level deltas in the gutter. This holds the run fixed and varies the
  reviewer, so it is a reviewer-quality tool in the §15.3 family — **not** the
  §4.16 comparison surface, which is the version comparison below. Enabled only
  when the capture carries two or more reviews; otherwise the control stays
  disabled with an honest tooltip.
- **Source** — the immutable source with its re-verified hash.
- **Open full trace** (§4.5/§4.12) — a slide-over with the source-step timeline
  whose selection lights up the panel its evidence belongs to (agent messages /
  tool I/O / environment / artifacts / verifier) and a capability badge marking
  each evidence type complete, partial, or unavailable.

**Compare versions** (§4.16) is a surface of its own, reached from the app bar
(or `V`), because it is about two *configurations* rather than one run:

```bash
python3 -m agr demo-store --store .agr-demo             # a slice to compare (see below)
python3 -m agr configurations --store .agr-demo         # what can be compared
python3 -m agr compare-versions --store .agr-demo \
    --baseline sweep_id=sweep_141 --candidate sweep_id=sweep_142 \
    --axis evaluation_harness [--save]                  # prints the result as JSON
```

The shipped fixtures are **one** configuration (one harness, no sweep), so a store
built from them shows the comparison surface's honest empty state. `demo-store`
(`agr/demo.py`) builds the second configuration from the first: same tasks and
match keys, a different harness version and sweep id, and a verifier outcome
flipped to passing on most tasks — plus one unmatched run per side so the match
report has real exclusions to show. That flip is the only fabricated data in the
package; every run it writes carries `source_type="synthetic_demo"`. The
version-comparison tests and the browser test build their slices with the same
generator, so the demo and the tests cannot drift apart.

*Construct* picks the two sides, declares the intended changed axis, and shows
the **match report before any number**: exact matched tasks, matched run pairs,
repeated-run strata, and every excluded run grouped by the key that excluded it
(`missing_counterpart`, `task_version_mismatch`, `environment_mismatch`, …).
*Inspect* then renders pass rate, opportunity-normalized behaviour, failure
modes, and resources — each row with its numerator, eligible denominator, task
and run-pair counts, a 95% paired interval, and a change label
(`Improved` / `Regressed` / `Within uncertainty` / `Insufficient evidence` /
`Observed only on one side`). Every row opens onto the run pairs that fed *it*,
and each pair opens that run's review. The definition is shareable and
recomputed on load, so a saved comparison over a grown store reports the new
slice rather than a stale number.

What the surface refuses to do is the point:

- an **unresolved version field** is never "equal by absence" — it blocks the
  `Matched` label outright and says which field did it;
- **more than the declared axis changed** downgrades the result to a
  *configuration comparison* that states it cannot isolate a component;
- estimates are **task-clustered**, so repeated runs of one task cannot outvote a
  task with one run (the Simpson's-paradox guard §12.3 asks for);
- a slice too thin is `Insufficient evidence`, not a confident direction — no
  variance below two tasks, and no directional claim below three (a declared
  default, per §12.2, not a proof threshold); and
- failure-mode rates are counted from **detector output, not the moment cards a
  reviewer was shown** — card selection (§8.10) is a presentation ranking and
  must not become a measured version difference.

The generated synthesis sits in an **Interpretation** block, stays
correlational, and takes its direction from the sign of the measured difference.

**The review workflow is instrumented** (§4.21) — the twenty named events, and
nothing else:

```bash
python3 -m agr metrics --store .agr-store
```

The events describe how a *reviewer* moved (opened a run, viewed a moment, opened
evidence, saved a correction), never what a run contained. `agr/instrumentation.py`
is an allowlist rather than a logger: an unknown event name, an undeclared
property key, or a value shaped like prose — whitespace-bearing or over 96
characters — is refused, and a batch containing one is rejected whole. That is
what keeps a prompt, a tool result, or a reviewer's free-text note out of the
analytics log by construction rather than by care. Derived measures (median time
to first disposition, share of moment views that open evidence, quick-relabel vs
full-correction rates, Full Trace fallback rate) report **not observed** rather
than zero for a workflow nobody exercised, and the one measure that still needs a
surface this package does not have yet — §14.2 adjudication — is listed by name
instead of quietly missing. (The §4.11 Eval Lesson funnel and the §4.3.5 fast path
are now reported measures, not named gaps.)

**Every review position is a link** (§4.1). Run, chapter, selected moment,
selected trace step, the compare pair, and the queue's filter/sort context are
encoded in the query string and restored on load, so a reviewer can share the
exact spot they are standing on:

```text
?run=chess_best_move__seed42&chapter=moments&moment=mom_003&trace=1&evidence=s7
```

A link naming a run, moment, or review the store no longer holds says so and
falls back — to the queue, the first moment, or the default compare pair — rather
than stranding the reviewer on an empty workspace.

**Honest by construction.** The guided view deliberately shows *structural*
stand-ins — a neutral `concern` / `strength` tag and a restatement of the
detector's structured facts — where a finished product shows a taxonomy verdict
("Mistake") and a narrative. Those labels, the prose, the alternatives, and the
lessons are the **model reviewer's** output (Stage F / Milestone 4), which the
spec sequences last so every displayed fact is recomputed rather than authored.
The deterministic core never fabricates them; M4 will swap the neutral tags for
real verdicts behind the same view.

FastAPI is an *optional* dependency; the read model itself is pure stdlib, so
the deterministic core stays dependency-free and its tests need no install. The
browser interaction test (`tests/test_ui.py`) skips unless Playwright and a
browser are present, so it doesn't burden the default CI matrix.

`show` reproduces the spec's chess example: `FAILED · 5/6`, C3 fails on the
missing winning move, a `dependency_linked` evidence slice, an
unresolved-requirement candidate at submission, and the compaction detector
reported as *not evaluated* because pre/post-compaction visibility is only
partial. Chess ships a clean, human-confirmed contract; the
`contract_mismatch` run seeds every contract/verifier mismatch the builder
surfaces (verifier-only, prompt-only, reference-only, uncovered-artifact, and
contradiction) and stays watermarked until confirmed.

## What maps to what (spec → code)

| Spec | Status |
|---|---|
| §6 data model (source, capability, event, check, slice) | implemented (subset) |
| §7 ingestion, idempotency, capture revisions (M1) | implemented |
| §8.1 Stage A deterministic extraction | implemented |
| §8 Stage C1 phase segmentation (deterministic) | implemented (structural; semantic naming is M4) |
| §6.7 opportunities | implemented |
| §8.6 recovery state machine | implemented (good_recovery vs unchanged-retry) |
| §8.4 Stage D evidence slicing — standard/omission/distributed/external | implemented |
| §8.5 Stage E detectors | 5 of 5 MVP detectors + 1 capability-gated demo |
| §4.3 Task Ability Signature (deterministic rows) | implemented |
| §8.2 Stage B contract builder + cross-check warnings (M2) | implemented (structured sources) |
| §6.4 contract versioning + human-confirmation flow + watermarking (M2) | implemented |
| §6.5 per-run contract observations (M2) | implemented (from mapped checks) |
| §15.1 gold dataset + §15.2 annotation schema/protocol (M0) | implemented (schema + validator + synthetic reference labels) |
| §15.3 reviewer evaluation harness — moment-selection + attribution metrics (M0) | implemented (Precision@3/Recall@3, redundancy, calibration, overclaim) |
| §8.8 fact validator + §8.9 attribution gate + §8.10 moment selection (M4) | implemented (deterministic envelope, no model call; validates facts, caps attribution, dedupes/ranks cards) |
| §8.7 global model reviewer (M4) | implemented (Anthropic + OpenAI adapters behind the `Reviewer` seam; taxonomy verdicts/tags/root causes/better action, facts still recomputed) |
| §7.4 redaction + §16.1 untrusted-content isolation (M4) | implemented (secret/PII redaction + map, strict instruction/data boundary before any model call) |
| §8.3 Stage C2 local phase analyst (M4) | out of this package (deferred); deterministic phase summaries feed the packet today |
| §4.5 forensic view + §4.4 guided review | implemented (read API + browser; phase strip + key moments; model verdicts render when Stage F ran, neutral tags + `deterministic_only` badge otherwise) |
| §4.3 runs triage inbox (sweep summary, filter chips, sorts, triage-priority order) | implemented (implicit single sweep; `agr/queue.py`, `GET /sweep` + `GET /queue`) |
| §4.13 Tier-3 full structured correction | implemented (`agr/workflow.py`; nine fields validated against the active taxonomy, evidence required for a corrected factual claim, `counterfactually_supported` unreachable by hand, accepted view overlaid at read time with the generated version kept inspectable) |
| §4.21 product instrumentation | implemented (`agr/instrumentation.py`; the 20 named events behind an allowlist that refuses undeclared keys and free-text-shaped values, plus the derived product measures and `agr metrics`) |
| §4.3.4 review workflow (dispositions + progress) + §4.13 Tier-1/2 feedback | implemented (`agr/workflow.py`; written beside the immutable source like `agr confirm`; optimistic `base_version`, idempotent `mutation_id`; `POST /runs/{id}/workflow` + `/feedback`; `agr disposition`) |
| §4.17 frontend data/action contract | implemented (read: sweep/queue/review+workflow/next; write: workflow + feedback, idempotent + optimistic-concurrency); §4.19 vocabulary layer in the browser |
| §4.7 Key-Moment Review workspace + §4.7.1 five-block card + §4.8 unified timeline + §4.9 evidence drawer | implemented (browser; `evidence_grade` derived deterministically in `read.py`; interpretation blocks shown "not generated" on deterministic reviews, never faked) |
| §4.4 guided review chapters + §4.2 outline rail | implemented (six chapters with completed/current/corrected/unavailable states; an unavailable chapter names why) |
| §4.5 Outcome chapter (checks → contract mapping, warnings, final environment/artifact summary, review limits) | implemented (`final_state` shaped in `read.py`; declared-but-never-observed artifacts stay visible, capability levels shown beside the section) |
| reviewer-vs-reviewer diff (two reviews of one run, §15.3 family) | implemented (`agr/compare.py`; anchor-overlap alignment, field-level diffs, `GET /runs/{id}/compare`) |
| §4.16 comparison surface + §6.12 matched comparison + §12.1/§12.3 statistics | implemented (`agr/versions.py`; exact match keys with per-key exclusion reasons, unresolved-version block, declared-axis check, task-clustered paired intervals, opportunity-normalized rows, saved definitions, `agr compare-versions`) |
| §4.1 shareable review location | implemented (run / chapter / moment / trace step / compare pair / queue filter + sort encoded in the URL and restored on load) |

## Design commitments carried from the spec

- **Source is immutable.** Derived records are written alongside, never over,
  the source. Reprocessing is versioned (`agr/version.py`).
- **Relevance is not causality.** The deterministic core never emits an
  attribution above `dependency_linked`; `counterfactually_supported` requires
  replay evidence this package does not generate.
- **Missing observability is explicit.** A detector without its required
  capabilities reports *not evaluated*, never "no problem found".
- **Disagreement is preserved, not resolved.** The contract builder surfaces
  every contract/verifier mismatch as a warning; confirmation records a human
  judgement over those warnings rather than deleting them.
- **Unconfirmed contracts are watermarked.** A review built on a `draft` or
  `provisional` contract is marked provisional; only human confirmation of
  every required item clears the watermark.

### On "model-assisted" contract building

The spec marks the contract builder as model-assisted (§table). The
model-assisted part is natural-language extraction from the free-text
instruction and reference solution. This deterministic core deliberately does
*not* parse free text: it consumes only structured declarations
(`task.requirements`, `task.artifacts`, `task.environment.preconditions`,
`task.reference_solution.assumptions`) plus the verifier's structured checks.
Everything it does — mapping every check to an item, and the five cross-checks
— is the mechanical part of §8.2 and needs no model. A later increment can
prepend model extraction that emits the same structured declarations.

## Roadmap (next increments)

1. ~~Remaining four MVP detectors + omission-branch slicing (M3).~~ **done.**
2. ~~Distributed/external slicing branches + Task Ability Signature rows.~~ **done.**
3. ~~Contract builder draft + human-confirmation flow and watermarking (M2).~~ **done.**
4. ~~FastAPI read API + synchronized evidence browser (M1 UI).~~ **done.**
5. ~~Deterministic guided review: C1 phase segmentation + key-moments rail +
   step navigation.~~ **done** (structural stand-ins where M4 adds verdicts/prose).
6. ~~Reviewer gold set + evaluation harness (M0), built ahead of the reviewer so
   every later model-reviewer change is scored against a fixed yardstick.~~
   **done** (`agr/gold.py`, `agr/reviewer_eval.py`, `gold/`; `python -m agr eval`).
7. ~~Deterministic reviewer envelope (M4 Stages G/H/I): fact validation,
   attribution gate, and moment selection/dedup — the safety machinery the model
   reviewer must pass through, built and tested with no model call.~~ **done**
   (`agr/reviewer.py`; seeded false facts rejected, causal language capped at the
   evidence ceiling, and the harness improves to `Precision@3 = 1.0` /
   `redundant_card_rate = 0.0` with `Recall@3` still `1.0`).
8. ~~Model reviewer itself (M4 Stage F) — the actual LLM call, plugged into the
   `Reviewer` seam **behind** the deterministic envelope so every displayed fact
   is still recomputed. Swaps the guided view's neutral tags for real taxonomy
   verdicts and a model-generated better action behind the same UI.~~ **done**
   (`agr/model_reviewer.py`: Anthropic + OpenAI adapters; `agr/redaction.py` +
   `agr/model_packet.py` build the redacted, typed, no-raw-trace packet; the
   §8.8 return-once fact loop; a fooled reviewer still cannot fabricate a card,
   because its facts are recomputed and it is given no tools. `python -m agr
   review <run> --provider anthropic|openai`). The live "beats the baseline"
   `agr eval` run is opt-in — it needs a provider key and is gated out of CI.
9. ~~Runs triage inbox (§4.3) + review write path (§4.3.4 dispositions, §4.13
   Tier-1/2 feedback) behind the §4.17 read/write contract, with the §4.19
   vocabulary layer in the browser.~~ **done** (`agr/workflow.py`, `agr/queue.py`;
   human review is recorded beside the immutable source — never mutating it —
   with optimistic concurrency and idempotent writes; `agr disposition`).
10. ~~Guided-review restructure (§4.7.1 five-block moment card, §4.8 unified
    Review Timeline, §4.9 evidence drawer).~~ **done** (moment-centric workspace;
    deterministic evidence grade; interpretation blocks shown "not generated"
    rather than fabricated on a deterministic review).
11. ~~Guided review chapters (§4.4) behind the §4.2 outline rail, the §4.5 Outcome
    chapter, the reviewer-vs-reviewer diff, and the §4.1 shareable review
    location.~~ **done** (these close out the §4 review surface).
12. ~~Matched version comparison (§4.16 / §6.12 / §12.1 / §12.3): construction
    checks, exclusion reasons, opportunity-normalized rows, and task-clustered
    paired uncertainty.~~ **done** (`agr/versions.py`, `agr compare-versions`;
    Milestone 5's comparison half — a seeded regression on a matched slice is
    detected, and the surface refuses the `Matched` label when the slice cannot
    support it).
13. ~~Tier-3 full structured correction (§4.13) and product instrumentation
    (§4.21).~~ **done** (`agr/workflow.py`, `agr/instrumentation.py`; corrections
    are taxonomy-validated annotation revisions, and the analytics allowlist keeps
    trace content out of the event log by construction).
14. ~~The Eval Lesson lifecycle (§4.11 — propose / approve / reject / promote to a
    regression-eval proposal) with its §13.2 experiment proposal, the remaining
    half of Milestone 5.~~ **done** (`agr/lessons.py`; an accepted, model-enriched
    moment becomes a versioned Eval Lesson, and one approved lesson produces a
    human-approved experiment proposal — the M5 accept criterion — with no fabricated
    success thresholds). **Milestone 5 is closed.**
15. ~~The §4.2 shell details (real sweep identity now that ingest carries
    `sweep_id`, harness version, disposition, `not_reviewable` routing) and the
    §4.3.5 fast/deep entry preference.~~ **done** (the run-review shell header shows
    sweep identity + queue position, model/harness versions, duration/cost, and a
    review-mode/workflow/capture-completeness strip; a `not_reviewable` capture
    routes to Outcome and names the missing evidence rather than presenting empty
    chapters. The §4.3.5 entry preference is remembered per browser and drives the
    landing chapter, with a `fast_path_open_rate` product measure.)
16. ~~The §4.19 glossary + first-session guidance (tooltips already ship).~~
    **done** (a compact glossary of the controlled vocabulary sits in the app bar,
    reachable from every Review / Full Trace / Compare surface and built from the
    same `LABELS` map the badges use, so it cannot drift from the tooltips; the
    first session gets a short, dismissible overlay explaining review mode,
    evidence grade, attribution, and the fact/interpretation split, shown once per
    browser.)
17. Next: §14.2 correction-to-consumer lineage and the §11 task/verifier audit.
    Stage C2 local phase analyst remains an optional pre-pass.
