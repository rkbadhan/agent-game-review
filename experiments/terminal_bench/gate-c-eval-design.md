# Gate C — Sweep workflow (REGISTERED, not yet run)

> Registered 2026-08-25 BEFORE execution. This gate tests the product the spec
> was actually written for (§2.1 primary job, §4 triage queue → review →
> disposition → next experiment). No official claim about "the Agent Game
> Review product" may be made from Gate B1/B2 alone.

## Question

Given a realistic sweep (~20–50 runs), can a reviewer using AGR do the full
job faster and more reliably than with existing tools (harbor view + raw JSON
+ ad-hoc reading)?

## Setup

- Sweep: backfilled corpus (target ≥20 reviewable runs incl. passes, failures,
  infra-failures, sibling sets) + `agr serve` UI vs harbor view side by side.
- Reviewers: ≥1 human harness-engineer-profile reviewer (the AI assistant may
  shadow but not substitute); time-separated sessions, order counterbalanced.
- Blind: reviewers score their own workflow without seeing the other tool's
  session notes until after.

## Tasks for each reviewer

1. Select the five runs worth inspecting.
2. Find repeated behavioral patterns across runs.
3. Separate agent failures from environment/harness failures.
4. Compare matched attempts where siblings exist.
5. Assign dispositions.
6. Recommend the next agent change or experiment.

## Measures (recorded per session)

```text
time_to_useful_diagnosis
critical_failures_missed        (vs answer key drafted beforehand)
incorrect_attribution_count
evidence_inspection_time
quality_of_next_experiment      (rubric 0–3)
reviewer_confidence             (self-report 1–5 post-session)
```

## Decision rule (fixed now)

- **C PASS:** AGR sessions show ≥30% faster time-to-useful-diagnosis AND zero
  additional critical failures missed vs the control session, on the majority
  of measured dimensions.
- **C FAIL:** no material difference or control wins.
- Workflow features that are implemented-but-untested elsewhere (signatures,
  lessons pipeline) get scored here as part of task 6 quality.
