# Real-corpus annotation protocol (AGR-01)

This is the labeling protocol for human annotation of the published
`eval-runs/` corpus. It complements, and does not replace, the annotation
schema in `agr/gold.py` (spec §15.2) — this document says *what an annotator
does*; `agr.gold` says *what the resulting record must look like*.

Run `python experiments/terminal_bench/build_corpus_manifest.py` first. The
manifest it writes (`experiments/terminal_bench/corpus_manifest.json`) is the
authoritative list of logical runs eligible for annotation, each with its
run id, task id, and source checksums — annotate from that list, not from
browsing `eval-runs/` by hand, so every label ties back to a specific,
checksummed capture.

## The one rule everything else follows

**Annotate from source evidence, never from AGR's own output.** Open the
trial's `trajectory.json` / `result.json` (or browse the ingested run through
`agr serve` using only the *evidence* views — timeline, checks, contract —
never the review/detector/recovery pages) and form a judgement before looking
at anything AGR generated for that run. A label produced by looking at what
the detectors already claimed is not independent evidence for whether those
detectors are right; it is circular. If you break this rule for a run (e.g.
you glanced at a review page before labeling), say so in that trajectory's
annotation notes rather than silently keeping the label.

## What to label

For each logical run in the manifest, produce one `agr.gold.GoldAnnotation`
(§15.2 schema) covering:

### 1. Episodes (recovery)

An *episode* is a failed or concerning result followed by the agent's
response to it. Label:

- Whether a strategy change actually occurred (a materially different
  action, not a retry of the same command).
- Whether that change's own execution succeeded.
- Whether the change is plausibly *relevant* to the original failure (acts on
  the same object/scope the failure concerned).
- Whether the original objective was subsequently verified — not merely
  "something later passed," but the same check/requirement the failure was
  about.

Only when all four hold does an episode count as a confirmed recovery.
Partial evidence (e.g. a relevant successful change with no subsequent
verification) is `plausibly_resolved`, not `good_recovery` — do not round up.
Use `anchor_type: "recovery"`.

### 2. Terminal summaries

A run's terminal state (its final outcome and failing checks) is a *fact*,
not a *cause*. When labeling a moment anchored on the terminal state, do not
imply the last agent action caused the outcome unless the evidence actually
supports that causal link (see attribution ceilings below). If a run has
several failing checks, they describe one terminal state — label it as one
moment, not one per failing check.

### 3. Supported behavioral findings

A behavioral finding (`anchor_type: "decision"` or `"omission"`) needs:

- A specific anchor step (or steps) where the behavior is observable.
- At least one `behaviour_tags` entry from the controlled vocabulary
  (`agr.taxonomy.NEGATIVE_BEHAVIOUR_TAGS` / `POSITIVE_BEHAVIOUR_TAGS`).
- An `attribution_ceiling` no stronger than the evidence licenses:
  - `hypothesized` — plausible but not directly evidenced.
  - `dependency_linked` — the behavior is on the causal path but other
    factors could also explain the outcome.
  - `direct` — the evidence directly shows the behavior producing the effect.
  - `counterfactually_supported` — a comparable passing run (same task,
    compatible configuration) took the alternative action and succeeded.

  Default to the *weaker* level when in doubt. Overclaiming attribution is a
  labeling error, not a stylistic choice.

### 4. Ordinary submission

A run that ends with a normal submission control sequence (e.g.
`echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` and the harness's matching
acknowledgement) is not, by itself, a finding of any kind — it is the
harness's own submission protocol. Do not label it as a format loop, a
missed instruction, or an unexecuted action. If a genuinely different failure
happens to co-occur with an ordinary submission, label the failure on its own
anchor, not on the submission event.

### 5. Uncertain resolution

When the evidence for or against recovery/resolution is genuinely mixed —
e.g. a relevant change was made but you cannot tell from the capture whether
it was verified — label `plausibly_resolved`, record what specific evidence
is missing in `task_verifier_concerns` or the moment's rationale, and do not
force a `good_recovery` or `unresolved` call to make the case count.

### 6. Incomplete captures

If the trial's capture is partial (timeout, truncated trajectory, missing
verifier output — check `capture_completeness` in the manifest/source), state
this explicitly in the annotation rather than labeling around the gap. A
missing later event is evidence of nothing; do not infer a moment (positive
or negative) from an event that could have existed but was not captured. Set
`no_decisive_moment: true` with a rationale naming the gap if the capture is
too thin to support any defensible moment.

### 7. No decisive moment

Some runs are clean passes with nothing behaviorally interesting to report.
Label these `no_decisive_moment: true` with a one-line rationale. These are a
required part of the sample (§15.1 calibration requirement) — do not skip a
run just because it looks unremarkable; an annotator who only labels
interesting runs biases the gold set toward findings.

## Annotator provenance and batching

- Set `GoldAnnotation.annotator` to a stable identifier for you (a name or
  handle), not "annotator" or a role.
- Group the trajectories you complete in one sitting/pass into a
  `label_batch` (a short id, e.g. `2026-09-anna-batch1`) on the
  `GoldTrajectory`. A batch is the unit that gets frozen.
- A model-generated draft label (used only to speed up your first pass, never
  submitted instead of one) is stored with `label_source: "model_draft"`. It
  is never treated as your annotation — if you start from a draft, you must
  still independently confirm or correct every field before the record can
  be marked `label_source: "human"`.

## Freezing

Do **not** set `frozen: true` on a trajectory until:

1. Its annotation is complete per this protocol.
2. If it was double-labelled, the two annotations have been adjudicated
   (`GoldTrajectory.adjudicated` is set) and the disagreement report
   (`agr.gold.disagreement_report`) has been reviewed.

Only `frozen` trajectories are eligible for `GoldSet.independent_human_gold()`
— the subset AGR-15 scores against. An unfrozen trajectory still loads and
validates (so partial progress is visible), it just does not count as gold
yet. Do not tune detector, recovery, or reviewer behavior against any label
before it is frozen — freezing first is what keeps the labels independent of
the implementation being measured.

## Development corpus vs. holdout

Every run in the current `eval-runs/` corpus (22 logical runs at the time of
writing) is **development/regression data**: it may be looked at while fixing
detectors, and its scored performance describes fit to this corpus, not
generalization. Label it fully — it is still the basis for regression tests
— but do not describe metrics on it as evidence the reviewer generalizes.
A generalization claim requires a **fresh holdout**: new trials from tasks
(or task families) not already represented in `eval-runs/`, collected and
labeled *after* detector/reviewer changes are frozen, with no trial from the
same task appearing on both sides of the split.
