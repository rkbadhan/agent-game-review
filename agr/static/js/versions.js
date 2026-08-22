"use strict";

// --- §4.16 matched version comparison ---------------------------------------
//   A surface, not a chapter: it compares two *configurations* across a matched
//   task slice, where the per-run compare above holds the run fixed and varies
//   the reviewer. Construct mode picks the two sides, declares the changed axis,
//   and shows the match report *before* any number; inspect mode renders the
//   result. Nothing here computes: every count, denominator, and interval comes
//   from the backend so the surface cannot quietly disagree with `agr
//   compare-versions`.
const AXES = [["evaluation_harness", "Evaluation harness"], ["model", "Model"],
  ["agent_scaffold", "Agent scaffold"], ["environment", "Environment"],
  ["complete_configuration", "Complete configuration"]];
const MATCH_KEYS = ["task_id", "task_version", "verifier_version",
  "environment_image_digest", "task_parameters", "seed"];
const CHANGE_LABELS = {
  improved: ["Improved", "The paired interval excludes zero in the better direction"],
  regressed: ["Regressed", "The paired interval excludes zero in the worse direction"],
  within_uncertainty: ["Within uncertainty", "The paired interval covers zero — no direction is supported"],
  insufficient_evidence: ["Insufficient evidence", "Too few matched tasks to estimate variance"],
  observed_only_on_one_side: ["Observed on one side", "One side had no eligible opportunity"],
  observed_only_in_candidate: ["Observed only in candidate", "The baseline never surfaced this, but its detector did run"],
  observed_only_in_baseline: ["Observed only in baseline", "The candidate never surfaced this, but its detector did run"],
  new_failure_mode: ["New failure mode", "Absent on the baseline where the detector was eligible"],
  not_captured: ["Not captured", "This capture carries no such measurement"],
};

function renderVersionsSurface(main) {
  const nav = el("div", "review-nav");
  const head = el("div", "outline");
  head.append(el("span", "eyebrow", "Compare versions · §4.16"));
  nav.append(head);
  const util = el("div", "review-util");
  const back = el("button", "seg", state.runId ? "‹ Back to review" : "‹ Back to runs");
  back.addEventListener("click", () => { state.view = "review"; render(); });
  util.append(back);
  nav.append(util);
  main.append(nav);
  renderVersions(main).catch(e => main.append(el("div", "empty", "Failed: " + e.message)));
  renderEvidencePanel();
}

async function renderVersions(main) {
  const v = state.versions;
  const wrap = el("div", "section");
  main.append(wrap);
  if (!v.configurations) {
    wrap.append(el("div", "subline", "Loading configurations…"));
    try {
      const body = await api("/configurations");
      v.configurations = body.configurations || [];
      v.defaultPair = body.default_pair;
    }
    catch (e) { wrap.textContent = ""; wrap.append(el("div", "empty", "Failed: " + e.message)); return; }
    wrap.textContent = "";
  }
  if (v.configurations.length < 2) {
    wrap.append(el("div", "empty", "A version comparison needs two configurations in the store. "
      + "Run `agr demo-store` for a synthetic slice, or ingest runs that declare a "
      + "sweep_id or harness/model version."));
    return;
  }
  if (!v.defaultPair) {
    wrap.append(el("div", "empty", "This store holds " + v.configurations.length
      + " configurations, but no two of them ran the same task — there is no matched "
      + "slice to compare. Pick a pair below to see the match report explain why."));
  }
  // Open on the pair that actually shares a task slice. A store can hold three
  // configurations (the shipped fixtures plus a demo slice, say), and picking
  // the first two would compare unrelated runs and label it a configuration
  // comparison — technically true, and useless.
  if (!v.baseline || !v.candidate) {
    if (v.defaultPair) [v.baseline, v.candidate] = v.defaultPair;
    else { v.baseline = v.configurations[0].label; v.candidate = v.configurations[1].label; }
  }
  wrap.textContent = "";
  wrap.append(renderVersionConstruct(v));

  if (!v.result && !v.pending) return;
  if (v.pending) { wrap.append(el("div", "subline", "Building the matched slice…")); return; }
  renderVersionResult(main, v.result);
}

function selectorOf(v, label) {
  const cfg = (v.configurations || []).find(c => c.label === label);
  return cfg ? cfg.selector : null;
}
function selectorParam(selector) {
  const [field] = Object.keys(selector || {});
  return field ? field + "=" + selector[field] : "";
}

function renderVersionConstruct(v) {
  const card = el("div", "card card-pad");
  card.append(el("p", "eyebrow", "Construct comparison"));
  card.append(el("p", "chapter-lede",
    "Pick the two configurations, declare what you intended to change, and review the match "
    + "report before any result. Anything the keys cannot match is excluded with its reason."));
  const row = el("div", "vs-construct");
  const pick = (label, side) => {
    const f = el("label", "vs-field"); f.append(el("span", null, label));
    const s = el("select");
    for (const c of v.configurations) {
      const o = el("option", null, c.label + " · " + c.run_count + " runs");
      o.value = c.label; if (c.label === v[side]) o.selected = true; s.append(o);
    }
    s.addEventListener("change", () => { v[side] = s.value; v.result = null; render(); });
    f.append(s); return f;
  };
  row.append(pick("Baseline", "baseline"), pick("Candidate", "candidate"));
  const af = el("label", "vs-field"); af.append(el("span", null, "Declared change axis"));
  const axis = el("select");
  for (const [val, label] of AXES) {
    const o = el("option", null, label); o.value = val; if (val === v.axis) o.selected = true; axis.append(o);
  }
  axis.addEventListener("change", () => { v.axis = axis.value; v.result = null; render(); });
  af.append(axis); row.append(af);
  const go = el("button", "button primary", "Preview match");
  go.addEventListener("click", () => previewComparison());
  row.append(go);
  const save = el("button", "button", "Save comparison");
  save.disabled = !v.result || v.result.match_status !== "valid";
  save.title = save.disabled ? "Preview a valid matched slice first." : "Freeze this definition and share its link.";
  save.addEventListener("click", () => saveComparison());
  row.append(save);
  card.append(row);

  const keys = el("div", "vs-keys");
  keys.append(el("span", "vs-counts", "Match keys:"));
  for (const key of MATCH_KEYS) {
    const on = v.keys.includes(key);
    const chip = el("button", "fchip" + (on ? " on" : ""), key);
    chip.setAttribute("aria-pressed", on ? "true" : "false");
    chip.addEventListener("click", () => {
      v.keys = on ? v.keys.filter(k => k !== key) : MATCH_KEYS.filter(k => v.keys.includes(k) || k === key);
      v.result = null; render();
    });
    keys.append(chip);
  }
  card.append(keys);
  return card;
}

async function previewComparison() {
  const v = state.versions;
  const baseline = selectorOf(v, v.baseline), candidate = selectorOf(v, v.candidate);
  if (!baseline || !candidate) return;
  const params = new URLSearchParams({ baseline: selectorParam(baseline),
    candidate: selectorParam(candidate), axis: v.axis });
  for (const k of v.keys) params.append("key", k);
  v.pending = true; v.savedId = null; render();
  try { v.result = await api("/comparisons/preview?" + params.toString()); }
  catch (e) { v.result = null; toast("Comparison failed: " + e.message); }
  finally { v.pending = false; render(); }
}

async function saveComparison() {
  const v = state.versions;
  try {
    const saved = await apiPost("/comparisons", {
      baseline: selectorOf(v, v.baseline), candidate: selectorOf(v, v.candidate),
      axis: v.axis, match_keys: v.keys,
      name: v.baseline + " → " + v.candidate,
    });
    v.savedId = saved.comparison_id;
    track("comparison_definition_saved", { comparison_id: saved.comparison_id });
    toast("Comparison saved — the link now reopens this definition");
    render();
  } catch (e) { toast("Save failed"); }
}

async function openSavedComparison(cid) {
  const v = state.versions;
  v.pending = true; render();
  try {
    v.result = await api("/comparisons/" + encodeURIComponent(cid));
    v.savedId = cid;
    v.axis = v.result.declared_change_axis;
    v.keys = v.result.match_keys || MATCH_KEYS.slice();
    if (v.configurations) {
      const label = s => (v.configurations.find(c => selectorParam(c.selector) === selectorParam(s)) || {}).label;
      v.baseline = label(v.result.baseline.selector) || v.baseline;
      v.candidate = label(v.result.candidate.selector) || v.candidate;
    }
  } catch (e) { toast("That saved comparison is not in this store."); v.savedId = null; }
  finally { v.pending = false; render(); }
}

function renderVersionResult(main, r) {
  const head = el("div", "vs-head");
  const title = el("div", "vs-title");
  title.append(el("h2", null, r.baseline.label + " vs " + r.candidate.label));
  const label = el("span", "vs-label " + r.label, r.label.replace(/_/g, " "));
  label.title = r.label === "matched"
    ? "Exact slice, and only the declared axis changed."
    : r.label === "configuration_comparison"
      ? "More than the declared axis changed — no single component is isolated."
      : "This slice cannot support a matched claim.";
  title.append(label);
  head.append(title);
  const pr = r.pass_rate;
  head.append(el("div", "vs-sub",
    "Declared axis " + r.declared_change_axis.replace(/_/g, " ")
    + " · observed " + (r.observed_change_axes.join(", ") || "none")
    + " · " + r.report.exact_matched_tasks + " tasks · " + r.report.matched_run_pairs + " run pairs"
    + " · pass rate " + pctOf(pr.baseline) + " → " + pctOf(pr.candidate)
    + " · " + r.statistics_version));
  main.append(head);

  if (r.blocked_reasons.length) {
    const b = el("div", "card card-pad");
    b.append(el("div", "vs-blocked", "Not a matched comparison — "
      + r.blocked_reasons.map(x => x.replace(/_/g, " ").replace(":", ": ")).join("; ")
      + ". The match report below shows what was and was not comparable."));
    main.append(b);
  }

  // Match report + exclusions, before any result (§4.16.1).
  const rep = el("div", "card card-pad");
  rep.append(el("p", "eyebrow", "Match report"));
  const cells = el("div", "vs-report");
  const cell = (n, lbl) => { const d = el("div", "vs-cell");
    d.append(el("strong", null, String(n)), el("span", null, lbl)); return d; };
  cells.append(
    cell(r.baseline.run_count, "baseline runs"), cell(r.candidate.run_count, "candidate runs"),
    cell(r.report.exact_matched_tasks, "exact matched tasks"),
    cell(r.report.matched_run_pairs, "matched run pairs"),
    cell(r.report.repeated_run_strata, "repeated-run strata"),
    cell(r.report.excluded_baseline_runs, "excluded baseline"),
    cell(r.report.excluded_candidate_runs, "excluded candidate"));
  rep.append(cells);
  rep.append(el("div", "vs-counts", "Match keys used: "
    + (r.match_keys_used.join(", ") || "none") + " · " + r.match_key_version));
  if (r.unresolved_keys.length)
    rep.append(el("div", "vs-caveat", "Unresolved keys: " + r.unresolved_keys.join(", ")));
  for (const caveat of r.caveats) rep.append(el("div", "vs-caveat", caveat));
  for (const group of r.report.exclusions_by_reason) {
    const d = el("details", "vs-drill");
    d.append(el("summary", null, group.side + " · " + group.reason.replace(/_/g, " ")
      + " · " + group.count + " run(s)"));
    d.addEventListener("toggle", () => { if (d.open)
      track("comparison_exclusion_opened", { comparison_id: r.comparison_id,
        exclusion_reason: group.reason, count: group.count }); });
    const list = el("div", "vs-pairs");
    for (const id of group.run_ids) list.append(runLink(id));
    d.append(list); rep.append(d);
  }
  main.append(rep);

  if (!r.pairs.length) return;

  main.append(metricCard("Outcome", [r.pass_rate], r));
  if (r.behaviours.length)
    main.append(metricCard("Opportunity-normalized behaviour", r.behaviours, r,
      "Denominators are eligible opportunities, not runs — an ability the task never "
      + "created a chance to show is absent, not zero."));
  if (r.failure_modes.length)
    main.append(metricCard("Failure modes", r.failure_modes, r,
      "Counted from detector output, not from the moment cards a reviewer was shown."));
  main.append(metricCard("Resources", r.resources, r));

  const cov = r.review_coverage;
  const covCard = el("div", "card card-pad");
  covCard.append(el("p", "eyebrow", "Review coverage"));
  covCard.append(el("p", "chapter-lede",
    "Baseline " + cov.baseline.model_enriched + " of " + cov.baseline.runs
    + " model-enriched · candidate " + cov.candidate.model_enriched + " of " + cov.candidate.runs
    + ". Deterministic-only runs still carry validated facts; interpretation rows are absent, not empty."));
  main.append(covCard);

  const interp = el("div", "vs-interp");
  interp.append(el("p", "eyebrow", "Interpretation · generated"));
  interp.append(el("p", null, r.interpretation.summary));
  interp.append(el("p", null, r.interpretation.limit));
  main.append(interp);
}

function pctOf(side) {
  if (!side || !side.denominator) return "—";
  return Math.round((side.numerator / side.denominator) * 100) + "%";
}
function rateCell(side) {
  const td = el("td");
  if (!side || side.denominator == null) { td.append(el("span", "vs-rate", "—")); return td; }
  td.append(el("span", "vs-rate", side.numerator + "/" + side.denominator));
  td.append(el("span", "vs-counts", " " + pctOf(side)));
  if (side.eligible_not_evaluated)
    td.append(el("div", "vs-counts", side.eligible_not_evaluated + " eligible, not evaluated"));
  return td;
}
function metricCard(title, rows, result, lede) {
  const card = el("div", "card card-pad");
  card.append(el("p", "eyebrow", title));
  if (lede) card.append(el("p", "chapter-lede", lede));
  const t = el("table", "vs-table");
  t.append(rowEls("tr", ["Measure", "Baseline", "Candidate", "Change", "Evidence"], "th"));
  for (const row of rows) {
    const tr = el("tr");
    tr.append(td(row.label));
    if (row.kind === "resource") {
      tr.append(td(row.captured ? row.baseline.mean.toFixed(1) : "—", "vs-rate"));
      tr.append(td(row.captured ? row.candidate.mean.toFixed(1) : "—", "vs-rate"));
    } else {
      tr.append(rateCell(row.baseline), rateCell(row.candidate));
    }
    const cTd = el("td");
    const v = CHANGE_LABELS[row.change] || [row.change, ""];
    const chip = el("span", "chg " + row.change, v[0]); chip.title = v[1]; cTd.append(chip);
    tr.append(cTd);
    tr.append(evidenceForMetric(row, result));
    t.append(tr);
  }
  card.append(t);
  return card;
}
function evidenceForMetric(row, result) {
  const cell = el("td");
  const stats = row.statistics || {};
  const bits = [];
  if (row.task_count != null) bits.push(row.task_count + " tasks");
  if (row.run_pairs != null) bits.push(row.run_pairs + " run pairs");
  cell.append(el("div", "vs-counts", bits.join(" · ")));
  cell.append(el("div", "vs-counts", stats.interval
    ? "95% CI [" + stats.interval.map(x => (x >= 0 ? "+" : "") + x.toFixed(2)).join(", ") + "]"
    : (stats.method ? "interval not estimable" : "")));
  // §4.16.2: every aggregate opens onto the run pairs behind *it* — the pairs
  // that actually fed this row, which for a gated row is fewer than the slice.
  const contributing = new Set(row.contributing_pair_ids || []);
  const feeding = result.pairs.filter(p => contributing.has(p.pair_id));
  if (feeding.length) {
    const d = el("details", "vs-drill");
    d.append(el("summary", null, feeding.length + " matched run pair(s) behind this row"));
    const list = el("div", "vs-pairs");
    for (const p of feeding) {
      const line = el("div", "vs-pair");
      line.append(el("span", null, p.task_id));
      line.append(runLink(p.baseline_run_id));
      line.append(el("span", "vs-arrow", "→"));
      line.append(runLink(p.candidate_run_id));
      list.append(line);
    }
    d.append(list); cell.append(d);
  }
  return cell;
}
function runLink(runId) {
  const wrap = el("div", "vs-pair");
  const b = el("button", null, runId);
  b.title = "Open this run's review";
  b.addEventListener("click", () => { state.view = "review"; selectRun(runId); });
  wrap.append(b);
  return wrap;
}

