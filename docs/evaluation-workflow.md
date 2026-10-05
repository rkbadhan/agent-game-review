# EV-1, EV-3, and EV-4 workflow

## EV-1 benchmark runs

`agr benchmark who-and-when --data DATA --manifest run.json --out results.json`
continues to use the Who&When dataset. TRAIL uses an exported local JSON or
JSONL directory:

```text
{"id":"case-1","split":"swe_bench","trace":"{...OTLP JSON...}",
 "labels":"[{\"span_id\":\"...\",\"category\":\"...\",\"impact\":\"High\"}]"}
```

`trace` and `labels` may be JSON values or JSON-encoded strings. `trace` must
contain OTLP `resourceSpans`; each label must identify an OTel span using
`span_id` (also accepts `spanId` or the documented `location` field). Unmapped
labels are reported as warnings. The scorer submits AGR's top five negative
moments as a set and reports span precision/recall/F1 plus recall on the
predeclared `impact=High` subset. That subset is a criticality proxy; TRAIL does
not itself call those spans decisive. The gated records are not downloaded or
bundled by AGR.

An optional `--protocol protocol.json` is frozen into the run manifest before
scoring. Put the configuration identity, threshold choices, and partition
policy there. For model-reviewed runs, include non-empty `training_cutoff` and
`partition_policy` fields before the report can issue a publishable benchmark
claim. If the cutoff or partition policy is unknown/unavailable, the report
marks the benchmark exploratory and excludes it from publishable claims. The
manifest also contains software and
reviewer versions, the system prompt hash, selected-record hashes, and reviewer
endpoint origin (without credentials). A frozen manifest path cannot be reused.

## EV-3 human audit

```text
agr audit-pack --store .agr-store --out audit-2026-09 --sample-size 30 \
  --double-review-fraction 0.25 --seed 42
```

Share `blind/cases.json` and `blind/annotations-template.json` first. The blind
form captures each reviewer's independently identified moments and outcome
assessment using the source-step IDs. Reviewers must complete and seal it
before anyone shares `revealed/cases.json` and
`revealed/annotations-template.json`, which assess AGR's explanations,
alternatives, evidence support, and moment precision. `key/case-map.json` maps anonymous IDs to run IDs and
should remain with the evaluation coordinator. The seeded sample uses
proportional allocation across strata of verifier outcome, review status,
moment/no-moment, and observed recovery/no-recovery. Hard-case analyses should be labeled
exploratory and kept separate from this sampled estimate. The revealed packet
identifies the planned double-review cases; assign those to independent
reviewers, then adjudicate disagreements separately.

The template records 1–5 rubric scores, counts of unsupported factual claims
and interpretations, assertion/moment denominators, and a rationale. It is an
annotation form, not an annotation result. Human reviewers must supply and
adjudicate the judgments.

## EV-4 scoped report

Save completed annotation forms as JSON objects with an `annotations` array.
Then combine any number of result files:

```text
agr publish-evaluation --benchmark-report trail-results.json \
  --audit annotations.json --integrity integrity-run-1.json \
  --integrity integrity-run-2.json --out evaluation-report.json
```

The report keeps the submitted denominators, review-failure counts, human
disagreement, and integrity coverage. Benchmark uncertainty uses a cluster
bootstrap over `task_id`, and human-audit rates resample by `case_id`; with
fewer than two independent clusters, the interval is unavailable. Default EV-4 thresholds are moment precision
≥0.8, zero unsupported factual assertions, and unsupported interpretations
≤10% of assessed interpretations. Override them only with a predeclared
threshold JSON and preserve it with the published report. No report can
establish that a target was met until actual benchmark scores and human
annotations are supplied.
