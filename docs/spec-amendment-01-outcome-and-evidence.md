# Spec Amendment 01 — Outcome & Evidence Model, Findings, and Surfaces

Status: proposed amendment to `agent-game-review-spec.md` v1.3
Date: 2026-09-14
Basis: `AGR-project-direction-report.md` (2026-09-14), `docs/verdict-vs-analysis.md`
(2026-09-13), and a repository inspection performed creating this amendment.
This document proposes changes; it does not by itself modify the authoritative spec.

---

## A. Outcome & evidence model (normative)

**A1.** Every run communicates three things **separately**:

| Information | Question | Values |
|---|---|---|
| **Task outcome** | What do the available checks establish? | passed / failed (named check) / unverified / inconclusive |
| **Execution findings** | What happened that deserves attention? | e.g. repeated failed reads before recovery |
| **Evidence coverage** | What can AGR inspect, and what is missing? | e.g. tool events available; token usage available; verifier results unavailable |

**A2.** Verifier availability is **not** complete ground truth. Capability metadata
(§6.2) describes *available evidence*; **check scope** determines what conclusions
it supports. A reward of 1.0 does not license claims about unverified requirements.

**A3.** No verifier does **not** mean no useful analysis. Local findings (recovery
episodes, redundant work, context growth, latency) are supported without an overall
task verdict. Overall task success stays **unverified**.

**A4.** A missing signal is **not** a clean result. "Not evaluated" (no opportunity
/ no capability) must never render as "evaluated, no issue found", and vice versa.
This is the spec's existing capability rule (§6.2) applied to every surface.

**A5.** Unknown outcomes, missing measurements, and uncertain opportunities must
remain visible. The verdict × execution matrix is an **explanation aid**, not a
forced classification: no run is coerced into passed/failed or efficient/wasteful.

**A6.** High cost does not establish waste; association does not establish
causality; episode cost is not validated savings. See §B.

## B. Finding requirements (normative)

**B1.** Every surfaced finding carries, explicitly:
- the **observation** (what happened);
- **evidence pointers** (events / artifacts / checks);
- the **interpretation** (dependency-linked, not asserted as cause);
- available **resource measurements** (and their coverage);
- material **limits** (what the evidence does not establish).

**B2.** A suggested change is presented as an **experiment** until validated by a
comparable before/after measurement (§D4).

**B3.** Related findings must not double-count overlapping episodes or resource
measurements. Aggregate impact is a **union**, not a sum (already required for
fleet usage; extend to every aggregate).

**B4.** Benign behaviour — legitimate retries, expensive-but-necessary work — must
not be auto-flagged. Findings that exceed their evidence are corrected, narrowed,
or suppressed; each priority finding type carries a documented audit with
examples, denominators, and unsupported cases.

## C. Affected surfaces (normative)

**C1. Consistent exposure.** Verification state and relevant capability information
are exposed consistently across **Runs, Review, Patterns, Compare** — from one
source (the §6.2 capability profile + `outcome`), not re-derived per surface.

**C2. Overlay, not partition.** Verification is a **filter and annotation within a
shared workflow**. The app must not split into disjoint GT / non-GT populations:
comparing passing and failing runs must remain possible when they are meaningfully
comparable.

**C3. Runs.** The run card exposes a `verification` summary (has verifier, results
and code capability, check count, atomicity). Task-verification state is kept
separate from execution/import errors.

**C4. Review.** The three model parts (A1) are separately legible. Capability gaps
render as coverage, never as a verdict.

**C5. Patterns.** Leads with **recurring behaviours**; each pattern shows what
happened, **distinct runs affected**, representative evidence, and measured
resource impact where available. Ranking uses recurrence, evidence strength, and
impact — **no single opaque score**.

**C6. Compare.** Preserves comparison limitations; distinguishes an observed
difference from a demonstrated improvement.

## D. Completion criteria

- **D1 (P1) Reconcile spec and UI.** A passing run with an execution issue, a
  failing run with check evidence, and an unverified trace with a supported
  recovery episode each receive a coherent review. Missing measurements never
  appear as zero or as proof of efficiency.
- **D2 (P2) Finding quality.** Each priority finding type has a documented audit
  (examples, denominators, unsupported cases, corrections); claims are traceable
  to raw evidence.
- **D3 (P3) Patterns useful for choosing work.** An engineer can select a
  recurring issue, inspect its episodes, understand its limits, and state a
  concrete experiment; aggregate impact does not double-count.
- **D4 (P4) Improvement loop.** At least one documented case connects a finding to
  a change and a defensible before/after result, with comparison limits stated.
  Pilot on Claude traces alongside Harbor runs.

## E. Requirement → repository map

Legend: **I** implemented · **C** needs correction · **M** missing.
(Repository inspected 2026-09-14.)

| # | Requirement | Status | Evidence / gap |
|---|---|---|---|
| A1 | Three things communicated separately | C | Review shows outcome + execution quality + capabilities (`chapters.js:236-240,337-358`); **not** consistent across list/queue/Patterns/Compare |
| A2 | Verifier availability ≠ complete GT | C | Capability declared at ingest (`ingest_harbor.py:926-931`), but **check scope** is not surfaced and the list treats a check as a verdict |
| A3 | No verifier ⇒ still analysis | I | `fleet/episodes`, execution quality run on verifier-less captures; `claude` ingest declares `verifier_code: unavailable` (`ingest_claude.py:620`) |
| A4 | Missing ≠ clean; not-evaluated ≠ no-issue | C | Review distinguishes "Evaluated; no violation" from "not evaluated" (`chapters.js:340-358`); the fleet outcome matrix still folds everything non-PASS/FAIL into `other` (`fleet.py::fleet_execution_quality`) |
| A5 | Unknown/missing stay visible; matrix not forced | C | `queue.py` fuses `UNDETERMINED`+`UNVERIFIED` under one `undetermined` chip; the EQ matrix forces pass/fail/other |
| A6/B1 | Finding carries observation, evidence, interpretation, usage, limits | C | Moments carry evidence; recovery episodes carry anchors. No uniform "limits" field on every surfaced finding |
| B2 | Suggested change = experiment | C | `agr/lessons.py` models lessons/experiments; not yet linked to a measurement |
| B3 | No double-count of overlapping measurements | I | Fleet usage is a union, not a sum (`f9baa25`); extend discipline to new aggregates |
| B4 | Benign behaviour not auto-flagged; per-type audit | M | Reported audit (9 Sep) found weak unresolved-failure precision; no per-type audit artifact in-repo |
| C1 | Verification + capability exposed consistently | M | `read.list_runs` does **not** read `capabilities.json`; `/queue` returns those cards verbatim; frontend has no `verification` concept |
| C2 | Overlay, not partition | M | Current app blends verified and verifier-less runs with no filter; no partition exists yet, but no overlay either |
| C3 | `verification` on the run card | M | See C1 — the smallest enabling change |
| C4 | Review: three parts legible | C | See A1 |
| C5 | Patterns lead with recurring behaviours + evidence + impact, no opaque score | C | `renderFleetTable` groups by size, carries resolution breakdown and usage; ranking is size-only, evidence strength not represented |
| C6 | Compare: observation vs demonstrated improvement | I | `agr/versions.py` / compare surface enforce matched slices, exclusions, uncertainty, interpretation labels |
| D4 | Improvement loop (finding → change → before/after) | M | Lessons exist; no baseline/subsequent-run measurement record |

### Taxonomy inconsistencies (one root cause)

`agr/queue.py::FILTER_CHIPS` and `agr/fleet.py::fleet_execution_quality` disagree
about the same statuses:

| Status | Queue | Fleet EQ | Correct? |
|---|---|---|---|
| `FAILED`, `ERROR` | `failed` / `needs_attention` | `fail` (ERROR→other) | inconsistent |
| `WARNING` | `needs_attention` | `other` | inconsistent |
| `UNDETERMINED` (GT, no verdict) | `undetermined` | `other` | should be its own state |
| `UNVERIFIED` (no verifier) | `undetermined` | `other` | must not fuse with UNDETERMINED |

This is the single highest-leverage correction for A4/A5/C1–C3.

## F. Branch status (confirmed 2026-09-14)

| Branch | Head | Merged into `main`? |
|---|---|---|
| `fix/runs-filter-toolbar-overlap` | `c1ed1c5` | **No** (1 ahead) |
| `improve/execution-quality-coverage` | `a51e1c3` | **No** (1 ahead) |

Neither is merged. Both are bounded, tested fixes consistent with this amendment
(they reduce A4's "not evaluated" confusion); land them through normal checks.

## G. Recommended bounded sequence

1. Land the two branches above.
2. **P1:** expose `verification` on `read.list_runs`; fix the status taxonomy
   (`UNDETERMINED` ≠ `UNVERIFIED`; `WARNING`/`ERROR` consistent); add the
   verification filter/annotation to Runs and Patterns. This satisfies C1–C3 and
   unblocks A4/A5.
3. **P2:** per-finding-type audit + `limits` on surfaced findings.
4. **P3:** Patterns ranking by recurrence × evidence strength × impact.
5. **P4:** the improvement-loop record + Claude-trace pilot.

Defer (per report §6): continuous monitoring, broad detector catalog, automatic
remediation, multi-project, broad visual redesign.
