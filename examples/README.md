# Examples — six runs, six things a review can tell you

The fastest way to understand Agent Game Review is to watch it review a run.
This directory is a guided tour of the six trajectories that ship with the
package (in [`agr/demo_fixtures/`](../agr/demo_fixtures/)). Each one is a
hand-built [ATIF](../docs/atif-schema.json) capture chosen to exercise a
different part of the review — a clean pass, a recovered failure, a run that
passed the verifier but still misbehaved, a failure the agent ignored, a
partial-credit miss, and a task whose contract fights itself.

Together they answer the question "what does a review actually surface?"
better than any prose about the machinery does.

## Run the whole gallery

Build a store from all six and open the evidence browser:

```bash
pip install .[api]

# Ingest all six shipped scenarios into a scratch store
for f in agr/demo_fixtures/*.atif.json; do
  agr --store .agr-examples ingest "$f"
done

agr --store .agr-examples runs      # list them with outcomes
agr --store .agr-examples serve     # browse at http://127.0.0.1:8000
```

Or read a single deterministic review in the terminal, no server:

```bash
agr --store .agr-examples ingest agr/demo_fixtures/ignored_failure.atif.json
agr --store .agr-examples show build_task__ignored_failure
```

> Every review below is **deterministic** — no model calls, no credentials.
> Runs ingested from a bare verifier are marked `PROVISIONAL` until a human
> confirms the task contract (`agr confirm`); that watermark is part of the
> point, not a bug. To see a model-enriched review, run `agr review` with a
> `model-*` extra and an API key (see the [main README](../README.md)).

---

## 1. Clean pass — the baseline

**File:** `clean_pass.atif.json` · **Run:** `greeting_file__clean_pass` ·
**Outcome:** `PASSED · 4/4`

The task is "write `hello world` to `/out.txt`" and the agent does exactly
that. The contract is human-confirmed (v2), all four atomic checks pass, and
**no detector fires**. Every behaviour the run actually exercised shows
positive evidence in the Task Ability Signature; behaviours it never triggered
(recovering from a tool failure, since none occurred) are marked *not
measured* rather than counted for or against it.

> **What it teaches:** a passing run reads as a clean pass. AGR does not invent
> concerns to look busy — when there is nothing wrong, the review says so, and
> the reviewer's attention goes elsewhere.

## 2. Recovered failure — a review names strengths, not just faults

**File:** `tool_failure_recovery.atif.json` · **Run:** `solve_task__recovered` ·
**Outcome:** `PASSED · 2/2`

A tool call fails partway through; the agent **changes strategy** and
succeeds. The `successful_recovery_via_strategy_change` detector fires
(`recovery @ evt_004, evt_007`), a `good_recovery` episode is recorded, and
the reviewed moment is a **`[strength]`**, not a concern.

> **What it teaches:** the review is not a bug list. Behaving well under a tool
> failure is a first-class, evidence-linked observation — the same machinery
> that flags mistakes also credits good recovery.

## 3. Passed the verifier, still misbehaved — pass ≠ clean

**File:** `stuck_retry.atif.json` · **Run:** `fetch_task__unchanged_retry` ·
**Outcome:** `PASSED · 1/1`

The verifier is green, but the agent **repeated an identical action with no
new information** in between. The `repeated_action_no_new_info` detector fires
and the evidence is a **`distributed`** slice (`evt_002, evt_004`) — no single
step is decisive; the concern lives in the interval. The recovery episode is
`retry_succeeded_without_strategy_change`: it worked, but by chance, not by
adaptation.

> **What it teaches:** a green verifier does not mean a clean run. AGR surfaces
> behavioural concerns on **passing** runs — exactly the ones a pass-rate
> number hides.

## 4. Ignored failure — one run, several converging signals

**File:** `ignored_failure.atif.json` · **Run:** `build_task__ignored_failure` ·
**Outcome:** `FAILED · 0/1`

The build tool errors, the agent presses on without addressing it, and submits
with the required binary absent. **Three detectors** fire together —
`ignored_tool_failure`, `unresolved_requirement_at_submission`, and
`required_artifact_absent` — and the recovery episode is `unrecovered_failure`.

The run produces three different *kinds* of evidence slice, carrying two
different certainty **ceilings**:

- an **`omission`** slice (ceiling `dependency_linked`) — the required
  behaviour is absent across a window the run could have used; observability
  supports the absence claim;
- an **`external`** slice (ceiling `hypothesized`) — a *tool* event on the
  path may explain the failure better than an agent decision, offered as
  explanation, **not** established cause;
- a **`standard`** slice (ceiling `hypothesized`) tying events to the failed
  check.

> **What it teaches:** slice *kind* (where the evidence lives — an omission, an
> external cause, a standard link) is separate from the certainty *ceiling*
> (how strong the claim is). `dependency_linked` and `hypothesized` are
> different claims, and the review never dresses a hypothesis up as a proven
> cause.

## 5. Partial credit — the strongest evidence grade, and honest abstention

**File:** `chess_best_move.atif.json` · **Run:** `chess_best_move__seed42` ·
**Outcome:** `FAILED · 5/6`

Five of six checks pass. The miss (C3, "include *every* winning move") is
backed by a **`dependency_linked`** evidence slice — the strongest grade the
deterministic core assigns: a required value *appeared in a tool result* but is
absent from the written artifact, and a state path links the two to the failed
check. The `unresolved_requirement_at_submission` detector fires on that one
check.

Note the capability gate: `compaction_requirement_loss` reports **`not
evaluated (missing: pre_post_compaction_context)`** rather than guessing. The
Task Ability Signature breaks the run down behaviour by behaviour, so one
failed check does not tar the five real strengths.

> **What it teaches:** evidence has grades, detectors **abstain** when the
> capture cannot support them, and a partial failure is scored per behaviour
> instead of collapsing to a single pass/fail.

## 6. Contract mismatch — auditing the task, not just the agent

**File:** `contract_mismatch.atif.json` · **Run:** `greeting_report__seed7` ·
**Outcome:** `FAILED · 2/3`

Here the interesting findings are about the **task**, not the agent. The
contract builder emits a stack of warnings from the same run:

- `prompt_only_unverified_requirement` — the prompt asks for a *polite* and
  *terse* greeting, but no verifier check covers either;
- `uncovered_artifact_requirement` — a declared `/summary.txt` no check looks
  at;
- `reference_only_assumption` — an expectation present only in the reference
  solution;
- `verifier_only_requirement` — a check enforcing something the author never
  stated;
- **`contradiction`** — `/out.txt` is both required and forbidden by different
  contract items.

> **What it teaches:** a lot of "agent failures" are really under-specified or
> self-contradictory tasks. AGR reconciles prompt, verifier, and reference into
> one contract and shows exactly where they disagree — before you blame the
> model.

---

## Where to go next

- **Review your own run** — point AGR at a real Harbor / Terminal-Bench sweep
  or a pi session. See ["Review your own run"](../README.md#review-your-own-run)
  in the main README.
- **Adapters** — the source formats AGR ingests, and how to add one:
  [`docs/adapters.md`](../docs/adapters.md).
- **Terminal-Bench end to end** — run a small model through Harbor and review
  the result: [`docs/terminal-bench.md`](../docs/terminal-bench.md).
- **What maps to what** — every surface above, traced to spec section and
  module: the "What maps to what" table in the [main README](../README.md).
