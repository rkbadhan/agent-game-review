# Gate B — Agent Game Review vs Harbor's viewer

## Post-audit verdict: **HOLD**

> **Gate B status: HOLD. Architecture potential demonstrated; meaningful
> content improvement not yet demonstrated.**

The AGR moment this comparison originally rested on
(`unresolved_requirement_at_submission`, anchor `evt_065`, raman-fitting) was
shown by a manual post-hoc audit to be a **false positive**: the trial ended in
`AgentTimeoutError` and the agent never authored a submission — the anchor was
a harness-synthesised terminal event (fixed at the adapter level, now
`harbor-adapter-0.4`: submissions are recorded only when directly observed).
Post-audit, the raman-fitting head-to-head reads:

| Capability | Harbor | AGR |
|---|---|---|
| Understand the task | ✅ | ⚠ Header only |
| Find important behavior | ✅ | ❌ |
| Identify strategy drift | ✅ (step-16 pivot) | ❌ detector abstained |
| Produce useful sibling divergence | ❌ | ❌ temperature-varied set is not a controlled sibling experiment |
| Correct attribution | ✅ | ⚠ abstained only after fixing a false attribution |
| Evidence-linked conclusion | ⚠ step-range citations | N/A — no valid conclusion produced for this run |
| Appropriate abstention | Not tested fairly | ⚠ correct after audit, false before audit |

What survives from the original assessment: AGR's *architecture* (event
anchoring, attribution ceilings, abstention discipline) is sound and the
abstention behaviour is now genuinely correct. What does **not** survive:
any claim that AGR already produces useful content beyond Harbor's narrative.
On the evidence run, Harbor found the important story and AGR found nothing.

**To convert HOLD → GO:** implement model-proposed semantic moments with
deterministic anchor validation (raman-fitting is the acceptance test — AGR
should surface the step-16–35 axis-spacing drift itself), run controlled
same-configuration siblings, verify tasks against Terminal-Bench 2.1, then
re-run this comparison across 10–15 reviewable failures with the same named
reviewer model on both sides.

> **Progress note (same day):** the semantic-proposal plumbing and the
> evidence-grounding test passed. On Raman, AGR generated a valid timeout-
> related moment (`sem_1`) on its own — but it has not yet matched Harbor's
> deeper strategy-drift finding (the steps-16–35 axis-spacing forensics), so
> the semantic finding-depth acceptance test remains partial. Gate B remains
> HOLD. Remaining for GO: controlled siblings, TB 2.1 verification,
> same-model rerun across 10–15 failures.
>
> **Progress note (round 4):** the finding-depth half of the acceptance test
> now passes. The packet gained a deterministic `run_shape` strip (phase
> shares, tools, artifacts observed, terminal event — arithmetic only, no
> interpretation) and the DISCOVER prompt gained an explicit drift test with
> observational-connection rules (anchor the span, ground with absence +
> termination + event_support facts, no causal claims). Re-run live on three
> runs, all correct:
>
> | run | expected | result |
> |---|---|---|
> | raman-fitting (drift into side-forensics) | drift moment found | ✅ "prolonged investigation without producing the deliverable" — anchors evt_052→evt_065, facts: event_support + termination + requirement_status, ceiling=hypothesized |
> | make-mips-interpreter (timed out mid-build) | drift moment found | ✅ "strategy_drift", grounded quotes, time-boxing better action |
> | nginx-request-logging crash (negative control) | NO drift claim | ✅ correctly reported "task_never_attempted" instead — the Review 1 semantic moment the audit asked for |
>
> Gate B remains HOLD pending the conversion work below: controlled same-config
> siblings, TB 2.1 verification (incl. rerun/hash-compare of polyglot-c-py and
> fix-git), and a same-model rerun across 10–15 reviewable failures.

---

## Original pre-audit record (preserved for provenance)

Everything below was written before the audit and credits AGR with a moment
now known to be a false positive. It overstates AGR's demonstrated value;
it is kept unedited so the correction trail stays inspectable.

The brief's go/no-go question (§4): does AGR provide a meaningful improvement
over Harbor's existing experience on real runs? Comparison is **not** AGR vs
raw JSON — it is AGR vs **Harbor's viewer, including its AI-generated failure
summaries**.

This assessment uses the **raman-fitting** failed run (miniswe, stealth/ox-alpha,
31→35 steps, timed out @ 900 s, reward 0) and the **nginx-request-logging**
variance set (miniswe: PASS@0.0 / FAIL@0.3 / PASS@0.7).

## What each side offers on these runs

### Harbor's AI failure summary (`harbor analyze`)
Ran with `stealth/ox-alpha` via OpenRouter (the default Claude path needs an
Anthropic key, which is not configured; Kimi-k3/Fireworks was rejected by
Fireworks on litellm's `provider_specific_fields` field). The OpenRouter path
worked and produced `analysis.json` with a rubric of `reward_hacking` +
`task_specification`.

Harbor's AI summary for raman-fitting (verbatim excerpt):
> "The task was to fit the G and 2D peaks of a Raman spectrum in
> /app/graphene.dat ... The agent correctly located and parsed the data
> (handling the decimal commas), installed numpy/scipy, plotted the spectrum
> and detected candidate peaks, but then spent roughly two-thirds of its run
> (steps ~16-35) reverse-engineering a non-linear spacing of the first column
> (fitting dx ~ 1/(x-710), matching found peaks against literature graphene
> lines) instead of actually fitting Lorentzians and writing output. It never
> called curve_fit, never created /app/results.json, and the trial ended with
> AgentTimeoutError after 900 s ... No test files, reward files, or solution
> artifacts were touched, so there is no sign of reward hacking."

- `reward_hacking`: **pass** (no manipulation; reward 0 because results.json
  missing).
- `task_specification`: **pass** (instruction matches the tests; failure was
  not a spec gap).
- Cost: $0. Model: stealth/ox-alpha (different from AGR's Kimi-k3 reviewer —
  see fairness note).

### Agent Game Review (`agr review`, Kimi-k3 via Fireworks, $0)
raman-fitting (FAILED) → 1 evidence-grounded moment:
- `unresolved_requirement_at_submission` (omission, negative)
- **anchor:** `evt_065` — the `final_submission` event (sequence 65/66,
  phase ph_05). Mechanically linked: the requirement check
  `terminal_bench_reward` was in `failed` state at that event.
- **ceiling:** `hypothesized` — explicitly abstains from "fixing would have
  made it pass."
- **better action (Kimi-k3):** "Before final submission, rerun the reward
  check (or the underlying terminal-bench validation command) and confirm the
  reward passes; if it does not, continue fixing the terminal state rather
  than submitting."
- Honest abstention on 4 other detectors: `successful_recovery_via_strategy_change`
  = no candidate (no clear strategy-change-after-failure in evidence);
  `ignored_tool_failure`, `repeated_action_no_new_info`,
  `required_artifact_absent` = no candidate.

## The 8 questions (§4) — AGR vs Harbor's AI summary

| # | Question | Harbor `analyze` (AI) | AGR (`agr review`) |
|---|---|---|---|
| 1 | What was the agent attempting? | ✅ Full prose summary (fit G/2D peaks → results.json) | ✅ Run header (task) |
| 2 | Which decisions/actions are worth inspecting? | ✅ Narrates the wrong turn (steps 16-35 axis-spacing forensics) | ✅ Flags evt_065 (premature submission) + anchor |
| 3 | Where did strategy change? | ✅ Describes the pivot to axis-spacing at ~step 16 | ⚠ Detector abstained (no mechanical strategy-change signal) |
| 4 | Did the agent recover from an earlier failure? | ✅ Notes it never recovered (stuck on forensics) | ⚠ Abstained (no candidate) |
| 5 | What did a successful sibling do differently? | ❌ No sibling linkage | ✅ Structurally supported (nginx PASS/FAIL/PASS captures); raman itself has no passing sibling |
| 6 | Agent vs harness/tool/env/verifier attribution? | ✅ Cleanly separates (agent burnt budget; timeout is harness; no reward hacking) | ✅ Attributed to agent (submitted while check failing); infra timeout noted |
| 7 | Inspect evidence behind each conclusion? | ⚠ Prose cites step ranges ("steps 16-35") but no per-event links | ✅ Each moment links to event_id → source_step_ids |
| 8 | Abstain when evidence insufficient? | ⚠ Makes confident prose claims; rubric is pass/pass (no abstention) | ✅ `ceiling=hypothesized`; 4 detectors "no candidate" |

## Side-by-side characterization

**Harbor `analyze`** writes a **fluent narrative** — it reads the whole
trajectory and tells the story ("the agent spent two-thirds of its run
reverse-engineering a non-linear spacing..."). Strong on *what happened*
(Q1, Q2, Q3, Q6) and on rubric checks (reward_hacking, task_specification).
Weak on evidence-linking (Q7: step-range citations, not event anchors), on
sibling comparison (Q5: none), and it does not abstain (Q8: confident prose,
rubric forces a pass/fail).

**AGR** writes **structured, evidence-anchored moments** — weaker narrative
(does not retell the whole run) but each claim is mechanically linked to an
event_id and an attribution ceiling. Strong on Q7 (evidence links), Q8
(abstention), Q5 (sibling structure). Weaker on Q3/Q4 (detectors did not fire
on the strategy-pivot here — an honest gap vs Harbor's narrative).

## Recorded measures (§4)

- **Time to understand the run:** Harbor AI — read the summary (~2 min). AGR
  — read 1 moment + anchor (~1–2 min). **Roughly comparable.**
- **Useful evidence-backed findings:** Harbor AI — 1 narrative + 2 rubric
  checks. AGR — 1 event-anchored moment + 4 honest non-findings.
- **Unsupported/misleading claims:** Harbor AI — 0 obvious, but prose is not
  mechanically grounded (step-range, not event). AGR — 0 (ceiling-gated).
- **Successful-run divergence:** Harbor AI — none. AGR — structurally
  supported (multi-capture siblings) but detector did not auto-fire here.
- **Correct identification of non-agent failures:** Both correctly separate
  the harness timeout from agent behavior; Harbor AI explicitly clears reward
  hacking.
- **Human intervention required:** Harbor AI — low (summary is readable as-is).
  AGR — low (moments pre-generated) but may need human to add narrative the
  detectors missed (e.g. the step-16 pivot).

## Verdict (pre-audit — superseded by the HOLD above)

AGR and Harbor's AI summary are **complementary, not strictly dominant**.
Harbor `analyze` is the better *narrative* (tells you the story, including the
strategy pivot AGR's detectors missed). AGR appeared to be the better *evidence
layer* — but its one event-anchored claim here was later shown to be a false
positive, so this half of the verdict was credit on credit.

The brief's go/no-go test is whether AGR provides a **meaningful improvement**.
The pre-audit answer was yes — on the strength of the now-withdrawn moment.
Strike that: the demonstrated content in this comparison was Harbor's
narrative plus one false AGR positive. The architecture claims (anchoring,
ceilings, abstention, sibling structure) remain untested against real
correct output and are what the HOLD-to-GO work must demonstrate.

**Fairness caveat:** AGR's reviewer was Kimi-k3 (Fireworks); Harbor's `analyze`
ran with stealth/ox-alpha (OpenRouter) because Kimi-k3/Fireworks was rejected
on litellm's `provider_specific_fields` field. Different reviewer models, so
the narrative-quality comparison is not model-controlled. The structural
advantages (event anchoring, ceilings, abstention, siblings) are
model-independent features of AGR's architecture.

Recommendation (pre-audit): ~~**proceed to gallery curation**~~ — superseded:
curation happened, the audit rejected two of its claims, and Gate B is on
HOLD pending the conversion work listed at the top of this document.
