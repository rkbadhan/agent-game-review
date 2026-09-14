# Problem framing — Verdict vs Analysis in Agent Game Review

Status: working framing, 2026-09-13. Not a spec change.
Purpose: capture the whole problem so the design can be decided in one sitting, later.

---

## 1. What AGR is (per spec v1.3)

- A **behavioural review and triage layer** over sweeps, tasks, trajectories,
  environment state, and verifier evidence (`agent-game-review-spec.md` §1).
- Primary user: the **agent harness engineer**; primary job: triage a completed
  sweep, find the few behaviours that mattered, decide the next change (§2.1).
- Non-negotiable principle #1: **"Contract-and-outcome-first, not trace-first."**
  The spec was written for verifier-bearing Harbor sweeps.
- Core unit is the **task**; the working session starts at a **completed sweep**.
- Four surfaces: Runs, Review, Full trace, Compare (§4.1). Shipped nav (T1):
  `Runs · Patterns · Compare versions`.

## 2. What changed since the spec (the drift)

The code has outgrown the spec's verifier-first assumption:

| Added after v1.3 | What it brought |
|---|---|
| OTel / Langfuse / LangSmith / claude / pi adapters | **Trace sources with no verifier** — production/observability data |
| Patterns (formerly Fleet) | Cross-run **recovery-episode clustering** (`/fleet/episodes`) |
| Execution Quality | `context_bloat` / `latency` / `redundant_work`, explicitly **independent of outcome** |
| Outcome states `UNDETERMINED` / `UNVERIFIED` | Honest verdicts when checks are partial or absent (commit `9ff2819`) |

Net effect: **the store now holds two epistemically different kinds of capture,
but the model and the UI still treat them as one population.**

## 3. The core tension

A capture may or may not carry **verifier evidence** (ground truth):

- **With verifier evidence** → a *verdict* is possible, including *which check failed*.
- **Without it** → only *behaviour* is observable. No right/wrong can be claimed.

The code improvises around this instead of naming it:

- `agr/fleet.py::fleet_execution_quality` buckets runs into `pass` / `fail` /
  **`other`**, where `other` is *everything else*: `WARNING`, `ERROR`,
  `UNDETERMINED`, and `UNVERIFIED`. On a verifier-less store the outcome matrix
  collapses to one live column.
- `agr/queue.py` fuses `UNDETERMINED` (GT present, no clean verdict) with
  `UNVERIFIED` (no verifier at all) under one `undetermined` chip, while
  `WARNING`/`ERROR` are `needs_attention`, not `failed`.
- The spec's own mechanism for this — the **adapter capability profile** (§6.2,
  `verifier_results` / `verifier_code`) — is **not exposed** in `read.list_runs`,
  so nothing in the list/queue can filter or group by it.

## 4. The key realization: two orthogonal axes

Verdict and execution are **not** hierarchical (analysis is not "below"
evaluation). They are independent:

| Axis | Requires | Answers |
|---|---|---|
| **Verdict** | ground truth (verifier) | *Was it right, and which check failed?* |
| **Execution** | telemetry (any capture) | *How did it go, and what did it cost?* |

```
                 Efficient            Wasteful
Passed      the ideal            OPTIMIZATION HEADROOM ← the missed case
Failed      correctness bug      worst; waste may be the cause
```

Consequences:

1. **"Correct but expensive" is a first-class finding** that correctness-only
   evaluation discards. It is a *candidate*, not a verdict — the task may
   genuinely require the cost (spec §1.2 #7, opportunity precedes skill inference).
2. **GT and analysis compose.** GT anchors the failure (which check); analysis
   explains it (which events/mechanism) and prices it. Neither alone is complete.
3. **Do not segregate the app into GT vs non-GT.** That would discard the most
   valuable comparison available: the *behaviour of passing runs versus failing
   runs*. The verification flag is needed as an **overlay**, not a partition.

## 5. What is incoherent today (concrete)

- Execution Quality repeats "Not evaluated" under every outcome column, then
  repeats "not evaluated — missing <cap>" on every run in the drill-down. The
  information is present; the story is not. (Addressed on branch
  `improve/execution-quality-coverage`.)
- Patterns leads with a contextless statistical grid *before* the actual
  patterns — the page's own story ("tool failures and recovery across every
  run") has a false start.
- No surface can say **"these runs passed but are the most expensive."**
- The word "eval" is used for captures that cannot be evaluated.

## 6. Design space

- **Option A — Segregate** (`GT` vs `non-GT` as two populations/tabs).
  Rejected: destroys the pass-vs-fail behaviour comparison; treats analysis as
  second-class when it is valid everywhere.
- **Option B — Overlay (recommended).** One analysis surface over all captures;
  the verdict is an overlay/filter where it exists. The 2×2 (verdict × execution)
  is the organising idea.

## 7. Open questions (blocking)

1. **Is cost-optimization on *passing* agents a real product goal?** If yes,
   "correct but expensive" leads. If not, efficiency stays secondary and much of
   this collapses to "verdict + why it failed."
2. **Is production in scope?** The spec's user triages sweeps (eval). Trace
   adapters imply a second product (observability). Is AGR one product or two?
3. **GT granularity:** aggregate reward ("0/1 checks") vs CTRF atomic tests
   ("3 of 5 tests failed") vs moment-level attribution. What does "we know where
   it failed" mean — a check, or the events behind it?
4. **Taxonomy:** split `UNDETERMINED` (GT, no verdict) from `UNVERIFIED` (no GT)
   everywhere?
5. **Project:** is a store = a project, or do we need multi-project?
6. **Opportunity:** for an efficiency finding, what constitutes "this task
   provided an opportunity to be efficient"? (Needed to avoid flagging genuinely
   expensive-but-necessary runs.)

## 8. Decided / built so far

- Two independent bug-fix branches (base `main`, not merged):
  - `fix/runs-filter-toolbar-overlap` — toolbar chip overlap + regression test.
  - `improve/execution-quality-coverage` — Coverage column + grouped missing
    capabilities + test. (Wireframes are untracked scratch.)
- **Ground truth is the capability profile**, not a new field:
  Harbor `result.json` / `ctrf.json` → `verifier_results: complete`,
  `verifier_code: partial`; trace sources → `unavailable`.
- Next slice agreed in principle: expose `verification` on the run card so the
  verdict becomes an **overlay** (not a partition).

## 9. Recommendation

1. Land the two bug-fix branches.
2. Expose `verification` (`has_verifier`, `results`, `code`, `checks`, `atomic`)
   on the run card + a Ground-truth filter — to enable the overlay.
3. Organise the analysis surface around **verdict × execution**, leading with
   the quadrant that matters once Q1 is answered.
4. Only then design the Patterns story (mechanisms on GT captures; reliability +
   efficiency on verifier-less ones).
