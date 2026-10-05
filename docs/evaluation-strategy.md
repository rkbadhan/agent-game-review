# Evaluation Strategy

**Status:** Adopted 26 Sep 2026 (revises EV-1–EV-3 of the Remaining Dev Work spec).
**Basis:** the benchmark-first / evidence-integrity / bounded-human-audit amendment.

## Recommendation

Keep AGR's product direction: a guided review that identifies the important
moments in an agent's trajectory, explains them with evidence, and suggests
alternatives with clearly stated limits. Change the *evaluation* to reuse existing
labelled attribution benchmarks first, automate evidence-integrity checks, and use
a small human audit for the parts neither covers. Create new labels only for a
demonstrated coverage gap.

Human labelling is an evaluation activity, not a requirement for every review a
user runs. AGR operates automatically; people establish how reliably it works.

## Three separate questions

| Question | Evidence needed |
| --- | --- |
| Did the agent complete its task? | Task tests, verifier results or outcome labels |
| Did AGR find the important error? | Error-location / failure-attribution labels |
| Is AGR's explanation faithful and useful? | Claim-level review against a written rubric |

An outcome label does not identify the decisive moment. Some attribution
benchmarks do.

## EV-1 — Data and coverage

The 22 published demo runs stay as **development** data (they shaped the
reviewer). Candidate benchmarks:

| Benchmark | What it holds | Fit | Caveat |
| --- | --- | --- | --- |
| [TRAIL](https://arxiv.org/abs/2505.08638) | 148 traces (117 GAIA, 31 SWE-Bench), 841 span-level errors with category/evidence/impact; OTel/OpenInference; expert annotators; MIT | Closest fit — AGR has an OTel adapter; multiple errors per trace suits up-to-five moments | Every trace contains errors (no abstention coverage); no verifier results; labels mark errors, not "decisive" moments, so a High-impact proxy must be declared up front |
| [Who&When](https://github.com/ag2ai/Agents_Failure_Attribution) | 184 failure tasks (CaptainAgent/Magentic-One, GAIA/AssistantBench); responsible agent, decisive step, explanation; MIT | Direct decisive-step labels | Multi-agent web tasks; needs a single-prediction rule; failures only |
| [Who&When Pro](https://arxiv.org/abs/2607.09996) | 12,326 failed trajectories, 26 benchmarks (Jul 2026) | Scale | Failures injected after a successful prefix (synthetic); format/licence unverified |

Plan:

1. Score locally supplied TRAIL JSON/JSONL exports and Who&When through their
   respective adapters. Assess Who&When Pro after that.
2. Record each benchmark's version, label provenance, coverage and limits. Freeze
   AGR's configuration and case selection **before** scoring; keep related task
   attempts in one partition where the data allows.
3. Preserve original event IDs on import; keep reference labels out of reviewer input.
4. Record the review model's training cutoff against each dataset release date (contamination).
5. Add a small supplementary set for what benchmarks lack: successful runs,
   recoveries, no-decisive-moment cases, and a few Terminal-Bench failures with
   verifier checks. Label only what that evaluation needs.

Missing annotations are never evidence that no valid moment exists. A failure-only
dataset cannot show correct abstention on clean runs.

## EV-2 — Three separate result groups

| Group | Measures |
| --- | --- |
| Attribution performance | The benchmark's own applicable metrics under its scoring protocol |
| Evidence integrity | Invalid references, quote mismatches, recomputation failures, rejected proposals, **checking coverage** |
| Full-review audit | Unsupported facts and interpretations, consequence support, alternative quality, abstention quality |

Rules:

- Use each benchmark's prediction format. Where it expects one decisive step,
  declare before evaluation how AGR picks one (e.g. its top-ranked negative
  moment). Never compare AGR's best of five against a baseline allowed one.
- Keep `harbor analyze` on compatible Terminal-Bench runs with the same model,
  comparable input, and disclosed budgets/cost/latency. On other benchmarks,
  compare against their published baselines.
- Keep provider failures, incomplete reviews and rejected proposals separate from
  genuine abstention.

### The automated layer is an **evidence-integrity audit**

Implemented by `agr.integrity` (`agr audit-integrity <run_id>`). It reports
exactly what it checked:

| Check | Establishes | Does not establish |
| --- | --- | --- |
| Event-reference validation | The referenced event exists | The event supports the interpretation |
| Exact-quote matching | The quote occurs in that event | The surrounding claim is true |
| Structured-fact recomputation | A supported field/calculation matches the data | Every statement is grounded |
| Schema and status checks | Output follows structure and status rules | The chosen moment is important |
| Benchmark label comparison | The prediction matches the reference | The whole coaching narrative is correct |

It also reports **checking coverage** — the share of assertion-like units the
automated checks can test. *Zero mechanical failures does not mean zero fabricated
facts.* Problems are classified as **contradicted facts**, **unsupported factual
assertions**, or **unsupported interpretations**. Suggested alternatives are judged
as proposals under the GR-2 information cutoff, not rejected because the agent
never took them.

## EV-3 — Human audit

Human review covers what labels and checks cannot:

- Does the evidence support AGR's explanation of cause or consequence?
- Does a coach summary add claims absent from the moment cards?
- Is a suggested alternative reasonable given what was known before the decision?
- Are positive moments and abstentions justified?
- Does the review help someone understand the run?

Audit complete reviews (cards, summaries, alternatives) from a predefined
representative sample with a written rubric. Examine hard cases separately; they
do not feed reported rates. Double-review a subset and report disagreement. Where
practical, reviewers assess the trace before seeing AGR's conclusions.

An LLM judge may draft annotations and flag claims. Calibrate it against
independently adjudicated examples kept apart from final cases, prefer a different
model family from the reviewer, and report its judgments separately from human
findings.

## EV-4 — Publish scoped conclusions

- Publish the dataset manifest, scoring rules, configuration, denominators, failure
  counts and audit method, with uncertainty that accounts for repeated runs of one task.
- Acceptable wording: *"AGR matched the reference decisive step in X of N evaluated
  cases"*; *"All displayed evidence references passed the implemented reference and
  quote checks"*; *"Human reviewers observed zero unsupported factual assertions
  across N audited reviews containing M assessed assertions."* None extends to
  unaudited reviews.
- Set thresholds before the final evaluation. Carried over where the measure is
  unchanged: moment precision ≥ 0.8, zero observed unsupported factual assertions,
  unsupported interpretations ≤ 10% (all on the audited sample). Attribution
  thresholds are set from each benchmark's published baselines before AGR is run.
- Report missed targets. Tune on development data; use fresh evaluation data once
  earlier results have guided changes.

## Implementation order

1. Correctness blockers + model-led review with explicit statuses. **(done)**
2. Evidence-integrity audit and its coverage report. **(`agr.integrity`, done)**
3. Attribution-benchmark adapters + scorers. Who&When and TRAIL are implemented
   (`agr/benchmark.py`, `agr/benchmark_whowhen.py`, `agr/benchmark_trail.py`).
   TRAIL takes a user-supplied local JSON/JSONL export; gated records are not
   fetched or redistributed. Its High-impact subset is a declared criticality
   proxy. Run the comparison once approved access and exports are available.
4. Freeze and run the comparison against a compatible baseline.
5. Targeted full-review audit; inspect coverage gaps.
6. New gold labels / a labelling assistant only where those gaps justify them.
7. Publish the results alongside the guided demo, using claims the measurements support.

## Implementation status

| Item | State |
| --- | --- |
| EV-1 data + attribution adapters + scorers | Who&When and local TRAIL adapters/scorers implemented; actual TRAIL run needs gated data; supplementary sets and model cutoff evidence pending |
| EV-2 evidence-integrity audit + coverage | done (`agr.integrity`) |
| EV-3 human audit | `agr audit-pack` creates a seeded, stratified blind/revealed packet and rubric; independent human annotation and adjudication remain pending |
| EV-4 publish scoped conclusions | `agr publish-evaluation` combines supplied benchmark, integrity, and human audit reports with denominators and cluster bootstrap intervals; final evidence and publication remain pending |
