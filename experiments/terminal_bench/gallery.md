# Agent Game Review — Pilot Gallery and Failure Audit

Five evidence-backed game reviews of real Terminal-Bench coding-agent runs.
Same model (`stealth/ox-alpha`) throughout; two agent configurations
(mini-swe-agent, Terminus-2). Every annotation links to a specific source
event. Where the automated detectors abstained (no evidence), the review says
so rather than inventing a story.

**Benchmark:** Terminal-Bench (`terminal-bench/terminal-bench-2`).
**Harness:** Harbor (ATIF-v1.7). **Agent model:** `stealth/ox-alpha` (free
tier, $0/run). **Review model:** Kimi-K3 via Fireworks (`agr review`,
$0/review). **Task wall:** `max_agent_timeout_sec = 900 s` (15 min) unless
noted.

**Precision disclosure (post-audit):** a manual post-hoc audit found that
three of the four auto-moments in earlier generated versions of this gallery
were false positives — anchored on harness-synthesised "submission" events for
runs that were killed by timeout or crashed before acting. The ingest adapter
was fixed so synthetic submissions are only created for normally-completed runs
with real agent activity; affected captures were re-ingested and re-reviewed
(all abstain). The gallery below reflects the post-fix state and discloses each
withdrawal.

**Disclosure convention (per brief §5, revised post-audit):** each review
reports separated counters — `manual_moments` (moment entries a human added),
`manual_narrative_sections` (human-written or Harbor-generated prose integrated
by a human; AGR emits structured moments, not narratives), `manual_edits`
(corrections to auto moments), `removed` (auto moments deleted as unsupported),
plus `deterministic_auto` / `semantic_auto` for detector- vs model-proposed
moments and `accepted` for what survived audit. Audit/rewrite time is recorded
per review. Read the per-review disclosures with this asymmetry in mind:
across the five featured reviews, **four of five depend on manual narrative
prose** (one sourced from Harbor's `harbor analyze`); AGR itself generated two
surviving annotations — one deterministic (Review 2) and one semantic (`sem_1`,
Review 4) — though the semantic moment does not yet match Harbor's depth.

---

## Review 1 — Format-loop crash contrasted with same-task successful runs

| field | value |
|---|---|
| Task | `terminal-bench/nginx-request-logging` |
| Benchmark | Terminal-Bench |
| Agent | mini-swe-agent @ 2.4.6 |
| Model | `openai/stealth/ox-alpha` (OpenRouter) |
| Outcome | **FAILED** (attempt 2, temperature 0.3) · reward 0 · agent crashed before acting |
| Sibling (same task/agent/model) | attempt 1 (temp 0.0) **PASS** · attempt 3 (temp 0.7) **PASS** |
| Run | `harbor__terminal-bench/nginx-request-logging__nginx-reques` (capture rev2) |
| Trajectory | `eval-runs/swpB-nginx-request-logging-a2/…/agent/trajectory.json` |

**Annotated timeline (manual, from raw agent log):** attempt 2 never started.
mini-swe-agent received three consecutive model responses containing no tool
call (a reasoning-model format mismatch), emitted its format-correction prompt
three times, then exited with `RepeatedFormatError`. Zero tool calls executed;
nothing was installed or configured; the verifier scored reward 0 against an
untouched environment. Two sibling attempts on the *same* task with the *same*
agent and model — one at temperature 0.0 and one at 0.7 — both passed.

**Locus note:** three consecutive invalid model responses followed by
`RepeatedFormatError` place this failure at the model↔agent protocol/interface
boundary rather than in straightforward agent behavior. (Round 4 update: AGR's
semantic reviewer, re-run on this capture, now generates exactly this moment
itself — verdict "task_never_attempted", grounded on the zero-tool-call
timeline and terminal event.)

**Moments:** *none automatically generated.* All detectors returned "no
candidate" — correctly so: the run has no agent-authored submission to anchor
on. (Earlier generated versions carried an
`unresolved_requirement_at_submission` moment here; the manual post-hoc audit showed it
was anchored on a harness-synthesised submission event for a run that crashed
before acting. The ingest adapter was fixed, the capture was re-ingested, and
this review was rewritten from the raw log.)

**Successful-run divergence (the brief's §5.1):** The two passing runs
used the same task, agent, model, and verifier; only the attempt
(temperature) differed. The failed attempt's distinguishing behavior — a
format-loop crash before any work — is exactly what the passing attempts did
not do. We do **not** claim the lower temperature *caused* the pass; we report
that the failed attempt produced no work at all and the siblings completed.
The PASS@0.0/FAIL@0.3/PASS@0.7 set is a same-task comparison, **not** a
controlled matched-run experiment (temperature varied); it illustrates
behavioral variance, it cannot isolate causes.

**Disclosure:** deterministic_auto=0, semantic_auto=0, accepted=0,
removed=1 (the false-positive moment described above), manual_moments=0,
manual_narrative_sections=1 (timeline prose), manual_edits=0. Human editing
time: ~15 min.

---

## Review 2 — Redundant completion call in a successful run

| field | value |
|---|---|
| Task | `terminal-bench/polyglot-c-py` |
| Benchmark | Terminal-Bench |
| Agent | Terminus-2 @ 2.0.0 |
| Model | `stealth/ox-alpha` (OpenRouter) |
| Outcome | **PASSED** (attempt 2, temp 0.3) · reward 1 |
| Run | `harbor__terminal-bench/polyglot-c-py__f03be3af-66c` |
| Trajectory | `eval-runs/swpC3-polyglot-c-py-a2/…/agent/trajectory.json` |

**Annotated timeline (manual):** After producing a working solution, the
agent called its task-completion tool twice (`evt_014`, `evt_018`) with no
new information in between, then proceeded to final submission. The task
passed; the repeated call was wasted work, not outcome-relevant.

**Moments:**
1. `redundant completion signal` / `repeated_action_no_new_info` (behaviour,
   negative) — **auto, accepted.** Anchors: `evt_014`, `evt_018`. Ceiling:
   `dependency_linked` (the repeat is linked to the first call, not an
   independent failure). Better action (Kimi-K3): *"Call mark_task_complete
   exactly once, then proceed directly to final submission; if uncertain
   whether the first call registered, inspect the tool result of evt_014
   instead of blindly re-invoking."* (Verified against the source trajectory:
   the first call's result — a confirmation prompt — was visible to the agent,
   so this recommendation references observable state.)

**Disclosure:** deterministic_auto=1, semantic_auto=0, accepted=1,
removed=0, manual_moments=0, manual_narrative_sections=1 (timeline prose),
manual_edits=0. Human editing time: ~4 min.

---

## Review 3 — Meaningful recovery … honestly not detected

| field | value |
|---|---|
| Task | `terminal-bench/crack-7z-hash` |
| Benchmark | Terminal-Bench |
| Agent | mini-swe-agent @ 2.4.6 |
| Model | `openai/stealth/ox-alpha` (OpenRouter) |
| Outcome | **PASSED** (attempt 1, temp 0.0) · reward 1 · **41 steps** |
| Run | `harbor__terminal-bench/crack-7z-hash__crack-7z-has` |
| Trajectory | `eval-runs/batch1r-crack-7z-hash/…/agent/trajectory.json` |

**Annotated timeline (manual):** The agent took 41 steps and ~954K input
tokens to crack a 7z archive and write the secret to `solution.txt` — far
more than the 5-minute human-expert estimate, but it succeeded within the
900 s wall. A human reading the trajectory can see exploratory dead ends
(trying `Crypto`, then `7z`, then hash tools) before reaching the answer.

**Moments:** *none automatically generated.* The
`successful_recovery_via_strategy_change` detector returned **no candidate**,
as did `ignored_tool_failure`, `repeated_action_no_new_info`, and
`required_artifact_absent`.

**The honest review:** AGR does **not** claim a "meaningful recovery" here
because the detectors found no mechanically-grounded strategy-change-after-
failure pattern in the evidence. A human can *see* exploratory pivots in the
raw trajectory, but the review abstains rather than asserting a recovery the
evidence does not support. This is the brief's §3 `Insufficient evidence`
moment type, applied honestly. The single most useful annotation a human
could add (manual) is: "the agent recovered from the `Crypto` dead end by
switching to the `7z` CLI" — but that would be a human reading, not an
auto-grounded finding, and is disclosed as such.

**Disclosure:** deterministic_auto=0, semantic_auto=0, accepted=0,
removed=0, manual_moments=0, manual_narrative_sections=1 (annotated timeline
is a human reading of the raw trajectory), manual_edits=0. Human editing
time: ~3 min (reviewed the abstention and chose not to override).

---

## Review 4 — Prolonged strategy drift before timeout — outcome caught, drift missed

> **Update (post semantic-discovery build, round 4):** AGR's reviewer now
> proposes model-generated moments validated against the evidence. On this run
> it independently produced a strategy-drift finding: verdict "prolonged
> investigation without producing the deliverable", anchored on the pivot
> away from fitting (evt_052) through continued window-scanning (evt_062/064)
> to the `run_timed_out` terminal event (evt_065), with an observational
> connection to the missing deliverable (ceiling=hypothesized — no causation
> claim). Repeated on make-mips and on the nginx crash negative control (see
> Gate B1 results). Harbor's prose remains more specific on concrete
> mechanisms; the broad-pattern gap narrowed, specificity parity not claimed.

| field | value |
|---|---|
| Task | `terminal-bench/raman-fitting` |
| Benchmark | Terminal-Bench |
| Agent | mini-swe-agent @ 2.4.6 |
| Model | `openai/stealth/ox-alpha` (OpenRouter) |
| Outcome | **FAILED** (attempt 2, temp 0.3) · reward 0 · timed out @ 900 s |
| Run | `harbor__terminal-bench/raman-fitting__raman-fittin` (capture rev2) |
| Trajectory | `eval-runs/raman-r2-raman-fitting/…/agent/trajectory.json` |

**Annotated timeline (narrative from Harbor's `harbor analyze`, manual
integration):** The task was to fit the G and 2D peaks of a Raman spectrum
and write the fit parameters to `/app/results.json`. The agent correctly
located and parsed the data (handling decimal commas), installed numpy/scipy,
plotted the spectrum, and detected candidate peaks — then spent roughly the
last two-thirds of its run (steps ~16–35) reverse-engineering a non-linear
spacing of the first column instead of actually fitting Lorentzians and
writing the output file. It never called `curve_fit`, never created
`/app/results.json`, and the trial ended with `AgentTimeoutError` at 900 s.
All three verifier tests failed because the output file did not exist.

**Moments:** AGR's deterministic detectors abstain natively on this capture
(after `harbor-adapter-0.4`, timeout terminations are never submissions). The
semantic-discovery stage independently proposed one grounded moment (`sem_1`,
verdict "timeout_without_submission"): it quoted the agent's still-running
window-scan work (evt_063/064), anchored on the `run_timed_out` terminal event
(evt_065), and advised writing best-fit parameters before further refinement.
It is accepted as evidence-grounded. Earlier generated versions also carried an
`unresolved_requirement_at_submission` moment; that was withdrawn after the
manual post-hoc audit: the anchor was the harness's synthetic terminal marker
— the trial ended in `AgentTimeoutError` at 900 s, so **the agent never
authored a submission**. The "rerun the reward check" criticism also stands:
the failing check was the post-run verifier, not something the agent could
observe.

**Cross-check vs Harbor's AI summary:** Harbor's narrative adds the
*strategy-pivot* story (the step-16 turn into axis-spacing forensics) that
`sem_1` does not capture. `sem_1` correctly recognizes the
timeout-without-submission outcome but does not identify the steps-16–35
strategy drift — Harbor's narrative remains the deeper content layer for this
run.

**Disclosure:** deterministic_auto=0, semantic_auto=1, accepted=1,
removed=1 (false-positive submission moment), manual_moments=0,
manual_narrative_sections=1 (timeline prose, sourced from `harbor analyze`),
manual_edits=0. Human editing time: ~12 min.

---

## Review 5 — Potential evaluation-budget confound; causal locus unresolved

| field | value |
|---|---|
| Task | `terminal-bench/make-mips-interpreter` |
| Benchmark | Terminal-Bench |
| Agent | Terminus-2 @ 2.0.0 (also ran with mini-swe-agent — same outcome) |
| Model | `stealth/ox-alpha` (OpenRouter) |
| Outcome | **FAILED** · reward 0 · timed out @ 1800 s (30 min) |
| Run | `harbor__terminal-bench/make-mips-interpreter__5426bb92-ddf` |
| Trajectory | `eval-runs/pilot-terminus2/…/agent/trajectory.json` |

**Annotated timeline (manual):** The task asks the agent to implement a MIPS
interpreter (`vm.js`) that boots Doom and renders frames — a task whose own
metadata declares `expert_time_estimate_min = 480` (8 hours of expert work).
The agent wall for this task is 1800 s (30 min). Both agents (mini-swe-agent
and Terminus-2) ran the full budget, produced substantial trajectories
(Terminus-2: 26 steps; mini-swe-agent: ~138 steps / 2.7M input tokens), and
were killed by `AgentTimeoutError`. The verifier then ran and reported reward
0 because `/tmp/frame.bmp` was never produced.

**Attribution discipline:** the 30-minute agent wall is a *potential*
evaluation-budget confound relative to the task's 480-minute expert estimate.
Both tested agent configurations exhausted the wall, but two failures do not
prove impossibility, and the causal locus remains unresolved: an expert-time
estimate is not a lower bound, and an oracle/solution pass under the same wall
would establish task feasibility — not whether the 30-minute exploration
budget is fair. Stronger evidence would be a controlled budget experiment:
same task/agent/model/configuration, a 30-minute arm and an extended-budget
arm, multiple attempts per arm. We label this run **potential evaluation-budget
confound; causal locus unresolved** — not confirmed infrastructure failure.

**Moments:** *none automatically generated.* The
`unresolved_requirement_at_submission` moment in earlier generated versions was
anchored on the timeout-forced terminal event; the adapter fix
(`harbor-adapter-0.4`) now records timeout terminations as run_timed_out — never
a submission, so the detectors abstain natively. The budget-confound discussion
above is a human reading of the evidence, recorded as manual narrative.

**The separation (brief §3 `Infrastructure failure`):** the timeout itself is
harness-side (`run_timed_out`, provenance `synthetic`), and no moment blames
the agent for it — but the *causal* attribution to the budget is explicitly
left open pending the evidence listed above. This is a weaker, more honest
claim than the "infrastructure failure" framing in earlier generated versions.

**Disclosure:** deterministic_auto=0, semantic_auto=0, accepted=0,
removed=1 (timeout-anchored moment), manual_moments=0,
manual_narrative_sections=2 (timeline prose + budget-confound discussion),
manual_edits=0. Human editing time: ~10 min.

---

## Aggregate disclosure (brief §6)

### Pilot corpus denominator (full, not just the 5 featured reviews)

> The planned 36-run sweep has **not** been completed: the sweep stopped early
> (path-2 stop per the brief's rejection criteria) rather than running the
> remaining low-variance easy-task repeats. Everything below counts what was
> actually run, and every row reconciles arithmetically.

| measure | value |
|---|---|
| Planned evaluated runs | 36 (6 tasks × 2 agents × 3 attempts) |
| Executed attempts (captures on disk) | 19 — spread over 16 distinct task × agent cells |
| Not executed (path-2 early stop) | 17 low-variance easy-task repeats |
| Completed trajectories | 19 (all executed attempts captured a trajectory) |
| Retries / duplicate captures folded into canonical runs | 4 |
| Canonical logical runs | 15 (= 19 attempts − 4 retry/duplicate captures) |
| Successfully ingested runs | 15 of 15 canonical logical runs |
| Reviewed runs | 15 of 15 (re-reviewed post-fix; retry/duplicate captures remain inspectable on disk but reviews attach to canonical runs) |
| Unique tasks | 7 selected from our 30-task pilot subset of the 89-task Terminal-Bench 2.0 dataset |
| Agents × model | mini-swe-agent 2.4.6 and Terminus-2 2.0.0, both on the same anonymous preview model (`openai/stealth/ox-alpha`, OpenRouter free tier) |
| Verifier passes (reward ≥ 1) | 11 |
| Verifier failures | 8 — 6 timeouts (`run_timed_out`), 1 format-loop crash before any action (`RepeatedFormatError`, zero agent steps → recorded as `run_failed` with `termination_reason: agent_protocol_failure`, never `run_completed`), 1 genuine failed attempt |
| Infrastructure-invalid runs | 0 confirmed (1 potential budget confound under human review: make-mips — causal locus unresolved) |
| Unreviewable runs | 0 of 15 canonical logical runs unreviewable (15/15 ingested and reviewed) |
| Runs with ≥ 1 valid auto moment | 2 of 5 featured (Review 2 deterministic; Review 4 semantic); corpus-wide, only repeated-action detections fired deterministically |
| Recorded token usage | ≈ 5.86M input / 261K output |
| Model cost per run / per review | $0 (free tier for both agent and reviewer models) |

### Moment-level disclosure

| measure | value |
|---|---|
| Automatically generated moments surviving audit | 2 — 1 deterministic (Review 2) + 1 semantic (`sem_1`, Review 4) |
| Moments removed as unsupported | 3 — all three `unresolved_requirement_at_submission` moments (Reviews 1/4/5) were false positives anchored on harness-synthesised submission events for runs that timed out or crashed before acting; found by a manual post-hoc audit, fixed at the adapter level (`harbor-adapter-0.4`: submissions are recorded only when directly observed, with provenance tags), captures re-ingested, detectors now abstain natively |
| Moments added manually | 0 (abstentions kept) |
| Human editing time per review | 4–15 min (incl. audit + rewrite session) |

## How examples were selected

Reviews 1–5 map to the brief's suggested mix. Where the strongest real case
differed from the taxonomy, we kept the real case and disclosed the
substitution (Review 3 is an honest *abstention* rather than a manufactured
recovery; Review 5's failure carries an unresolved evaluation-budget confound,
not a confirmed infra or agent verdict). No example was fabricated; every
moment links to a source event in an ingested trajectory.

## Underlying trajectories

All trajectories are in `eval-runs/<job>/…/agent/trajectory.json` (ATIF
format) and are browsable via `harbor view eval-runs` and via the AGR UI
(`agr serve`). Run IDs above resolve in `agr runs` / `agr show <run_id>`.

**Reproducibility status (honest):** these paths are local to the experiment
machine — **not yet publicly downloadable**. A public launch requires:
frozen trajectory + verifier outputs published as an artifact, exact dataset
task revisions (checksums below), configuration manifests, and working links.
The agent model is an anonymous third-party preview (`stealth/ox-alpha` via
OpenRouter; invoked as `openai/stealth/ox-alpha` by mini-swe-agent and
`openrouter/stealth/ox-alpha` by Terminus-2 — same model, different provider
prefixes). Frozen outputs are inspectable, but **exact semantic reproduction
is weakly reproducible** while the model is unnamed and unpinned. Any final
head-to-head claim should use a named, pinned model.

**Task versions (Terminal-Bench 2.0 vs 2.1):** all runs used
`terminal-bench/terminal-bench-2` at ref `latest` (2.0 era). Featured-task
checksums as recorded by Harbor in each trial's `result.json`:

| task | task_checksum (first 16 hex) |
|---|---|
| crack-7z-hash | `22789e2dc5cc9b54` |
| fix-git | `d3220d70bc668ec6` |
| make-mips-interpreter | `f7e68fab72321eea` |
| nginx-request-logging | `913305d8f286ff12` |
| polyglot-c-py | `d1e52e6139c57528` |
| prove-plus-comm | `77604ed7016a1021` |
| raman-fitting | `73157911d2f6196d` |

Terminal-Bench 2.1 fixed 26–28 tasks, and its published change list explicitly
includes **two** of our seven featured tasks: `fix-git` and `polyglot-c-py`
([official Terminal-Bench 2.1 changes](https://github.com/harbor-framework/terminal-bench-2/pull/53)).
`polyglot-c-py` is Review 2 — the only surviving deterministic AGR moment. Its
behavioral annotation may remain valid, but that run must be rerun or
hash-compared against the 2.1 revision before any launch claim. Unchanged 2.0
trajectories for the other five tasks are not invalidated, and new runs should
pin 2.1.

## The claim

> Agent trajectories contain useful behavioral information that aggregate
> benchmark scores discard. Evidence-grounded game reviews make that
> information easier to inspect.

The claim is **not** that every highlighted action caused the final outcome.
Review 2 (the surviving deterministic moment) uses `ceiling=dependency_linked`
and avoids counterfactual causation; Review 4's semantic moment (`sem_1`) is
evidence-grounded but recognizes only the timeout-without-submission outcome,
not the deeper strategy drift. Reviews 1, 3, and 5 abstain entirely — after
the audit fix, the honest answer for those runs is "no supported moment", plus
manual, source-linked prose. This is the discipline the brief requires.

**Status: pilot gallery and failure audit — not a launch gallery.** The
document itself records why: trajectories are local (not publicly
downloadable), exact reproduction is weak while the model is unnamed and
unpinned, Gate B is on HOLD, four of five reviews rely on manual narrative,
and only two AGR-generated moments survived audit (one deterministic, one
semantic). "Launch Gallery" is restored only when public artifacts exist and
several useful AGR-generated semantic moments reproduce across runs.
