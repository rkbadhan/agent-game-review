"use strict";

// --- item 30/32: fleet view over recovery episodes across every run --------
//   A surface of its own, like Compare versions: it groups every run's
//   already-persisted recovery episodes (agr/fleet.py) by tool and/or error
//   signature, so a repeated failure across many runs is one row instead of
//   scattered across per-run reviews. Nothing here computes anything new —
//   every number comes straight from GET /fleet/episodes.
const FLEET_GROUP_OPTIONS = [
  ["tool,error_signature", "Tool + error"],
  ["tool", "Tool only"],
  ["error_signature", "Error only"],
];

function renderFleetSurface(main) {
  const nav = el("div", "review-nav");
  const head = el("div", "outline");
  head.append(el("span", "eyebrow", "Patterns · tool failures and recovery episodes across every run"));
  nav.append(head);
  const util = el("div", "review-util");
  const back = el("button", "seg", state.runId ? "‹ Back to review" : "‹ Back to runs");
  back.addEventListener("click", () => { state.view = state.runId ? "review" : "runs"; render(); });
  util.append(back);
  nav.append(util);
  main.append(nav);
  renderFleet(main).catch(e => main.append(el("div", "empty", "Failed: " + e.message)));
  renderEvidencePanel();
}

async function loadFleetEpisodes() {
  const fl = state.fleet;
  // U3: capture the token AND the groupBy this fetch is actually for. Two
  // grouping changes in quick succession both land here; only the token
  // bumped by the SECOND call is still current when its response arrives, so
  // the first one's `await` resolving later must not overwrite fl.episodes
  // with data for a grouping the reader has already switched away from.
  const token = ++fl.loadToken;
  const groupBy = fl.groupBy;
  fl.pending = true; fl.error = null;
  try {
    const episodes = await api("/fleet/episodes?group_by=" + encodeURIComponent(groupBy));
    if (token !== fl.loadToken) return;  // superseded by a newer grouping change
    fl.episodes = episodes;
  } catch (e) {
    if (token === fl.loadToken) fl.error = e.message || "failed to load";
  } finally {
    if (token === fl.loadToken) fl.pending = false;
  }
}

// U3: item 31's argument-shape distribution, always keyed by (tool,
// error_signature) regardless of the fleet table's own --group-by — it is
// its own read model, not reshaped per grouping. Loaded once and matched
// against each fleet group's key below; a group whose key does not carry
// both dimensions (a "Tool only"/"Error only" grouping) has nothing to match
// against and shows the column as not applicable rather than a false zero.
async function loadFleetArgumentShapes() {
  const fl = state.fleet;
  if (fl.argumentShapes || fl.argumentShapesPending) return;
  fl.argumentShapesPending = true;
  try { fl.argumentShapes = await api("/fleet/argument-shapes"); }
  catch (e) { fl.argumentShapes = []; }
  finally { fl.argumentShapesPending = false; }
}
function argumentShapesFor(g) {
  const fl = state.fleet;
  if (!fl.argumentShapes || fl.groupBy !== "tool,error_signature") return null;
  const key = JSON.stringify(g.key);
  return fl.argumentShapes.find(s => JSON.stringify(s.key) === key) || null;
}

// Patterns overview (item 4): affected runs, episode count, and recorded
// usage are the three numbers a reader needs before reading any group row —
// shown as prominent stat tiles (the same .sweep-stats treatment Runs already
// uses for its own top-line counts) instead of buried in a sentence. AGR-06:
// this is the union across EVERY episode in the store, never a sum of the
// per-group "Recorded usage" column below (which would double-count an event
// two different groups' episodes both cover).
//
// "Display one coverage qualification, with counting methodology expandable"
// (item 4): the measured/partial/unavailable qualifier is ONE line; the full
// union/overlap/undercount methodology this store already computes moves
// behind a disclosure instead of repeating as several always-visible
// warning paragraphs.
function coverageQualification(summary) {
  const avail = summary.usage_availability;
  if (avail === "unavailable")
    return { cls: "warn", text: "Recorded usage is unavailable — no episode in the store was ever instrumented." };
  if (avail === "partial") {
    const bits = [];
    if (summary.usage_unavailable_episode_count)
      bits.push(summary.usage_unavailable_episode_count + " never instrumented");
    if (summary.usage_partial_episode_count)
      bits.push(summary.usage_partial_episode_count + " only partly instrumented");
    return { cls: "warn", text: "Recorded usage is partial — " + bits.join(", ") + " of " + summary.episode_count + " episode(s); the totals below undercount." };
  }
  return { cls: "pos", text: "Recorded usage covers every episode in the store." };
}
function renderFleetUsageSummaryCard(summary) {
  const card = el("div", "card card-pad");
  card.append(el("p", "eyebrow", "Patterns overview"));
  const stats = el("div", "sweep-stats fleet-stats");
  const stat = (n, lbl) => { const d = el("div", "sweep-stat"); d.append(el("strong", null, String(n)), el("span", null, lbl)); return d; };
  stats.append(
    stat(summary.affected_runs, "affected runs"),
    stat(summary.episode_count, "episodes"),
    stat(fmtCompact(summary.total_tokens), "recorded usage (tokens)"));
  card.append(stats);
  const cov = coverageQualification(summary);
  const covLine = el("div", "coverage-line " + cov.cls);
  covLine.append(el("span", null, cov.text + " "));
  const d = el("details", "coverage-detail");
  d.append(el("summary", null, "How this is counted"));
  d.append(el("p", "subline", summary.usage_note));
  covLine.append(d);
  card.append(covLine);
  return card;
}

// A dimension's cell is its incidence over the runs that carried the telemetry
// it needs. Runs that could not be evaluated are never folded into the score.
// The one fact an outcome cell must convey is the score itself, so an
// unevaluated bucket reads as a plain em dash instead of the old "Not
// evaluated" repeated under every outcome — the row's Coverage column now
// carries that story once, at the dimension level, with the missing capability
// named. "no runs" still marks a bucket that never existed (commonly OTHER on
// a pass/fail-only store).
function eqCell(metric) {
  const cell = el("td");
  if (metric.eligible_runs) {
    const score = el("span", null, metric.affected_percent + "% ("
      + metric.affected_runs + "/" + metric.eligible_runs + ")");
    // Runs measure incidence; tasks measure reach. A tooltip states both so a
    // percentage over many runs of one task cannot read as many tasks.
    if (metric.eligible_tasks !== undefined) {
      score.title = metric.affected_runs + " of " + metric.eligible_runs
        + " run(s) across " + metric.eligible_tasks + " task(s); "
        + (metric.affected_tasks || 0) + " task(s) affected";
    }
    cell.append(score);
  } else if (metric.unevaluated_runs) {
    const dash = el("span", "vs-counts eq-not-evaluated", "—");
    dash.title = "Not evaluated — " + metric.unevaluated_runs
      + " run(s) here lacked the telemetry this dimension needs";
    cell.append(dash);
  } else {
    cell.append(el("span", "vs-counts", "no runs"));
  }
  return cell;
}

// Coverage, read across every outcome bucket: of the runs that belong to this
// dimension's row, how many were actually scored? A reader triaging a
// dimension needs that denominator next to the scores, not buried in each
// outcome cell — a 0% over 8 runs means something entirely different from
// "nobody could be measured".
function eqCoverage(byOutcome, key) {
  let eligible = 0, unevaluated = 0;
  for (const [outcome] of EQ_OUTCOMES) {
    const metric = (((byOutcome[outcome] || {}).dimensions || {})[key]) || {};
    eligible += metric.eligible_runs || 0;
    unevaluated += metric.unevaluated_runs || 0;
  }
  return { eligible, unevaluated, total: eligible + unevaluated };
}

// Group unevaluated runs by the EXACT set of capabilities they are missing, so
// a dimension nothing could score states its one reason once ("missing
// generation_usage · 12 runs") instead of repeating "not evaluated — missing
// generation_usage" on a dozen rows. Grouping — rather than merging every run
// under a union of reasons — keeps each capture's precise unmet capabilities
// intact, so two differently-incomplete runs never inherit each other's gaps.
function eqReasonGroups(unevaluated) {
  const groups = new Map();
  for (const run of (unevaluated || [])) {
    const reasons = (run.reasons || []).slice();
    const k = reasons.join(", ");
    if (!groups.has(k)) groups.set(k, { reasons, runIds: [] });
    groups.get(k).runIds.push(run.run_id);
  }
  return [...groups.values()].sort((a, b) => b.runIds.length - a.runIds.length);
}
// A missing-capability set reads as one unit ("generation_usage + generation_
// timestamps"); the " + " keeps it from blurring into the " · " that separates
// one reason-set from the next in the coverage note.
function eqReasonText(reasons) {
  return (reasons || []).join(" + ") || "required telemetry";
}
function eqMetricRuns(metric) {
  // Compatibility with pre-migration payloads is intentionally read-only:
  // new payloads always carry reasons per run in `unevaluated`.
  return metric.unevaluated || (metric.unevaluated_run_ids || []).map(runId => ({
    run_id: runId, reasons: metric.unevaluated_reasons || [],
  }));
}

function eqCoverageCell(matrix, key) {
  const byOutcome = matrix.by_outcome || {};
  const cell = el("td", "eq-coverage");
  const { eligible, unevaluated, total } = eqCoverage(byOutcome, key);
  if (!total) {
    // A bucket with no runs at all (commonly OTHER on a pass/fail-only store)
    // has nothing to evaluate — never imply a missing capability on runs that
    // do not exist.
    cell.append(el("span", "vs-counts", "no runs"));
    return cell;
  }
  const state = eligible === 0 ? "warn" : unevaluated ? "partial" : "ok";
  cell.append(el("span", "eq-coverage-count " + state,
    eligible + " of " + total + " evaluated"));
  // The same coverage stated in distinct tasks: "8 of 12 runs" over 8 tasks
  // and "8 of 12 runs" over 2 tasks are different stories, and only the second
  // suggests a systemic gap. Repeated runs of one task count once.
  const totals = (matrix.by_dimension || {})[key] || {};
  if (totals.tasks) {
    const t = el("div", "vs-counts eq-coverage-tasks",
      (totals.eligible_tasks || 0) + " of " + totals.tasks + " task"
      + (totals.tasks === 1 ? "" : "s") + " evaluated");
    t.title = "Distinct tasks behind this row; repeated runs of one task count once";
    cell.append(t);
  }
  if (unevaluated) {
    const bar = el("div", "eq-coverage-bar");
    const fill = el("i");
    fill.style.width = (100 * eligible / total) + "%";
    bar.append(fill);
    bar.title = eligible + " of " + total + " runs carried the telemetry this dimension needs";
    cell.append(bar);
    const reasons = [];
    for (const g of eqReasonGroups(eqAllUnevaluated(byOutcome, key))) {
      reasons.push("missing " + eqReasonText(g.reasons)
        + (g.runIds.length > 1 ? " (" + g.runIds.length + ")" : ""));
    }
    cell.append(el("div", "vs-counts eq-coverage-note", reasons.join(" · ")));
  }
  return cell;
}

// Every unevaluated run for one dimension, across all outcome buckets.
function eqAllUnevaluated(byOutcome, key) {
  const out = [];
  for (const [outcome] of EQ_OUTCOMES) {
    const metric = (((byOutcome[outcome] || {}).dimensions || {})[key]) || {};
    out.push(...eqMetricRuns(metric));
  }
  return out;
}

// The runs behind one dimension, split by outcome. Every run is a link that
// opens its full trace: an affected run lands on the exact violating event,
// while a healthy or unevaluated run lands on its first model generation. A
// dimension with no findings still lets a reader open the runs it looked at —
// previously only affected runs were links, so "Context bloat: not evaluated"
// left nothing to click.
// The same four buckets as the Runs chips and the pattern outcome splits
// (queue.OUTCOME_BUCKETS) — pass / fail / undetermined / unverified — so a run
// can never sit in one bucket here and a different one in the inbox.
const EQ_OUTCOMES = [
  ["pass", "Passing runs"],
  ["fail", "Failing runs"],
  ["undetermined", "Undetermined runs (verifier ran, no clean verdict)"],
  ["unverified", "Unverified runs (no verifier evidence)"],
];

function eqRunRow(runId, eventIds, note) {
  const row = el("div", "vs-pair eq-example-row");
  const { task } = runIdParts(runId);
  const first = (eventIds || [])[0] || null;
  const b = el("button", "eq-example-link", task);
  const target = "Open " + runId + (first ? " at " + first : " and its full trace");
  b.title = target; b.setAttribute("aria-label", target);
  b.addEventListener("click", () => openRunAtEvent(runId, first));
  row.append(b);
  const short = shortRunId(runId);
  if (short) row.append(el("span", "mono eq-example-id", short));
  // A run with several violating events keeps each one addressable.
  for (const eventId of (eventIds || []).slice(1)) {
    const ev = el("button", "eq-example-link mono", eventId);
    ev.title = "Open " + runId + " at " + eventId;
    ev.setAttribute("aria-label", ev.title);
    ev.addEventListener("click", () => openRunAtEvent(runId, eventId));
    row.append(ev);
  }
  if (note) row.append(el("span", "vs-counts", note));
  return row;
}

// The list of unevaluated runs, collapsed under one heading per missing-
// capability set. The heading carries the "why" once; each run is then just
// its task name, short id, and an opening link.
function eqUnevaluatedList(unevaluated) {
  const list = el("div", "vs-pairs eq-examples");
  for (const g of eqReasonGroups(unevaluated)) {
    const head = el("div", "eq-reason");
    head.append(el("span", "eq-reason-label", "Not evaluated — missing "
      + eqReasonText(g.reasons)));
    head.append(el("span", "vs-counts", g.runIds.length
      + (g.runIds.length === 1 ? " run" : " runs")));
    list.append(head);
    for (const runId of g.runIds) list.append(eqRunRow(runId, null, null));
  }
  return list;
}

function eqDetail(byOutcome, key) {
  const wrap = el("div", "eq-detail-body");
  // When nothing in this dimension could be evaluated, splitting the runs by
  // outcome would imply an outcome-by-outcome measurement that never happened
  // (the old drill-down read "Passing runs / Failing runs" for a dimension that
  // measured neither). State the one true finding and group the runs by what
  // they were missing instead.
  if (!eqCoverage(byOutcome, key).eligible) {
    const block = el("div", "kv-block");
    block.append(el("span", "kv-k", "Not evaluated"));
    const v = el("span", "kv-v");
    v.append(el("p", "eq-detail-lede",
      "No run carried the telemetry this dimension needs, so there is no score "
      + "to report. The runs and their missing capabilities are listed below."));
    v.append(eqUnevaluatedList(eqAllUnevaluated(byOutcome, key)));
    block.append(v);
    wrap.append(block);
    return wrap;
  }
  for (const [outcome, label] of EQ_OUTCOMES) {
    const metric = (((byOutcome[outcome] || {}).dimensions || {})[key]) || {};
    const block = el("div", "kv-block");
    block.append(el("span", "kv-k", label));
    const v = el("span", "kv-v");
    const affected = metric.affected || [];
    const healthyIds = metric.healthy_run_ids || [];
    const unevaluated = eqMetricRuns(metric);
    if (!affected.length && !healthyIds.length && !unevaluated.length) {
      v.append(el("span", "vs-counts", "no runs"));
    } else {
      if (affected.length || healthyIds.length) {
        const list = el("div", "vs-pairs eq-examples");
        for (const a of affected)
          list.append(eqRunRow(a.run_id, a.event_ids, a.violations + " violation(s)"));
        for (const runId of healthyIds)
          list.append(eqRunRow(runId, null, "evaluated, no violation"));
        v.append(list);
      }
      if (unevaluated.length) v.append(eqUnevaluatedList(unevaluated));
    }
    block.append(v);
    wrap.append(block);
  }
  return wrap;
}

function renderFleetExecutionQuality(matrix) {
  const card = el("div", "card card-pad");
  card.append(el("p", "eyebrow", "Execution quality"));
  card.append(el("p", "chapter-lede",
    "Per-dimension incidence by outcome. A dimension is scored only on the runs "
    + "that carried the telemetry it needs; Coverage tells you how many that was "
    + "and names what the rest were missing. Expand a row to see the runs behind it."));
  const t = el("table", "vs-table eq-table");
  t.append(rowEls("tr", ["", "Dimension", "Coverage", "PASS", "FAIL",
                          "UNDETERMINED", "UNVERIFIED"], "th"));
  const labels = {
    context_bloat: "Context bloat", latency: "Slow generation", redundant_work: "Redundant work",
  };
  const byOutcome = matrix.by_outcome || {};
  for (const [key, label] of Object.entries(labels)) {
    const detail = el("details", "eq-detail");
    const tr = el("tr", "eq-row");
    const detailRow = el("tr", "eq-detail-row");
    const toggleCell = el("td", "eq-toggle");
    const toggleBtn = el("button", "eq-toggle-btn", "▸");
    toggleBtn.setAttribute("aria-label", "Show the runs behind " + label);
    toggleBtn.setAttribute("aria-expanded", "false");
    toggleBtn.addEventListener("click", () => { detail.open = !detail.open; });
    detail.addEventListener("toggle", () => {
      toggleBtn.textContent = detail.open ? "▾" : "▸";
      toggleBtn.setAttribute("aria-expanded", detail.open ? "true" : "false");
      detailRow.classList.toggle("open", detail.open);
    });
    toggleCell.append(toggleBtn);
    tr.append(toggleCell, td(label));
    tr.append(eqCoverageCell(matrix, key));
    for (const [outcome] of EQ_OUTCOMES) {
      const metric = (((byOutcome[outcome] || {}).dimensions || {})[key]) || {};
      tr.append(eqCell(metric));
    }
    t.append(tr);
    const detailCell = el("td"); detailCell.colSpan = 8;
    detail.append(el("summary", null, "Runs behind " + label));
    detail.append(eqDetail(byOutcome, key));
    detailCell.append(detail);
    detailRow.append(detailCell);
    t.append(detailRow);
  }
  card.append(t); return card;
}

function renderFleetGroupByCard(fl) {
  const card = el("div", "card card-pad");
  card.append(el("p", "eyebrow", "Group by"));
  const keys = el("div", "vs-keys");
  for (const [val, label] of FLEET_GROUP_OPTIONS) {
    const on = fl.groupBy === val;
    const chip = el("button", "fchip" + (on ? " on" : ""), label);
    chip.setAttribute("aria-pressed", on ? "true" : "false");
    chip.addEventListener("click", () => {
      if (fl.groupBy === val) return;
      fl.groupBy = val; fl.episodes = null; fl.error = null;
      // U3: start the fetch for the NEW grouping right here rather than
      // leaving renderFleet to notice fl.episodes is null — if an earlier
      // grouping's fetch is still in flight, waiting for "the pending fetch"
      // to finish would resolve with THAT grouping's data, not this one's.
      // loadFleetEpisodes()'s token bump supersedes any earlier in-flight
      // call, whichever settles first or last.
      loadFleetEpisodes().then(render);
      render();
    });
    keys.append(chip);
  }
  card.append(keys);
  return card;
}

async function renderFleet(main) {
  const fl = state.fleet;
  const wrap = el("div", "section");
  main.append(wrap);
  wrap.append(renderFleetGroupByCard(fl));

  // Guarded the same way loadFleetArgumentShapes() guards its own fetch: two
  // renderFleet() calls overlapping in time (e.g. this await still pending
  // when something elsewhere triggers a fresh top-level render()) must not
  // both see `!fl.executionQuality` and each fire their own request to the
  // same endpoint.
  if (!fl.executionQuality && !fl.executionQualityPending) {
    fl.executionQualityPending = true;
    try { fl.executionQuality = await api("/fleet/execution-quality"); }
    catch (e) { fl.executionQuality = null; }
    finally { fl.executionQualityPending = false; }
  }
  if (fl.executionQuality) wrap.append(renderFleetExecutionQuality(fl.executionQuality));

  if (fl.pending) { wrap.append(el("div", "subline", "Loading fleet episodes…")); return; }
  if (fl.error) { wrap.append(el("div", "empty", "Failed: " + fl.error)); return; }
  if (!fl.episodes) {
    wrap.append(el("div", "subline", "Loading fleet episodes…"));
    await loadFleetEpisodes();
    // U3: re-render from the top instead of continuing to build into `wrap`.
    // A grouping change while this was in flight may have started ANOTHER
    // fetch that finished first (or since started one still pending) — by
    // the time this await resolves, `wrap` can already be detached from
    // #main (a later render() call replaces it), so painting into it would
    // update a screen nobody sees while the visible one stays stuck on
    // "Loading…". A fresh render() reads fl.pending/fl.episodes/fl.error as
    // they stand right now and always reconciles the visible screen with it.
    render();
    return;
  }
  if (!fl.episodes.length) {
    wrap.append(el("div", "empty",
      "No recovery episodes in this store yet — a run needs at least one qualifying "
      + "tool failure for Patterns to have anything to group."));
    return;
  }
  if (!fl.usageSummary && !fl.usageSummaryPending) {
    fl.usageSummaryPending = true;
    try { fl.usageSummary = await api("/fleet/usage-summary"); }
    catch (e) { fl.usageSummary = null; }
    finally { fl.usageSummaryPending = false; }
  }
  if (fl.usageSummary) wrap.append(renderFleetUsageSummaryCard(fl.usageSummary));
  if (!fl.argumentShapes && !fl.argumentShapesPending) await loadFleetArgumentShapes();
  wrap.append(renderFleetTable(fl.episodes));
}

// Item 5/6: a pattern's title is derived from its diagnostic error text
// (the same traceback/diagnostic-marker extraction error_signature.py already
// does), never a raw opaque fallback line presented as if it identified a
// common cause. A group where EVERY episode selected its signature via the
// opaque fallback tier (agr.fleet.EpisodeGroup.all_fallback_basis — no
// traceback, no recognised diagnostic marker anywhere, e.g. bare "---") is
// labelled "Unclassified tool failures" instead; its raw fallback text is
// preserved and shown, just not presented as an established common cause.
function patternTitle(g) {
  const toolIdx = g.group_by.indexOf("tool");
  const sigIdx = g.group_by.indexOf("error_signature");
  const tool = toolIdx >= 0 ? g.key[toolIdx] : null;
  const sig = sigIdx >= 0 ? g.key[sigIdx] : null;
  if (g.all_fallback_basis && sig)
    return { title: "Unclassified tool failures" + (tool ? " · " + tool : ""), raw: sig, unclassified: true };
  const parts = g.key.filter(Boolean);
  return { title: parts.join(" — ") || "(none)", raw: null, unclassified: false };
}

// Item 6: compact rows — Pattern · Affected runs · Episodes · Recovery ·
// Recorded usage is everything a triage read needs; repeat rate, avg turns,
// wall time, argument shapes, and representative episodes move into a
// per-row expandable detail area (a <details> in a full-width second row) so
// opening examples for one pattern never reflows or crowds the other rows.
function renderFleetTable(groups) {
  const card = el("div", "card card-pad");
  card.append(el("p", "eyebrow", groups.length + " pattern(s)"));
  card.append(el("p", "chapter-lede",
    "Ranked to lead with recurring behaviours: recurring before one-off, then "
    + "reach (distinct tasks, then runs), evidence strength, and measured tokens. "
    + "The order is explainable from each row — there is no single blended score. "
    + "Every number is a re-read of persisted recovery episodes; nothing is recomputed."));
  const t = el("table", "vs-table fleet-table");
  t.append(rowEls("tr", ["", "Pattern", "Affected runs", "Episodes", "Recovery", "Recorded usage"], "th"));
  for (const g of groups) {
    const title = patternTitle(g);
    const detail = el("details", "fleet-detail");
    const tr = el("tr", "fleet-row" + (title.unclassified ? " unclassified" : ""));

    const detailRow = el("tr", "fleet-detail-row");
    const toggleCell = el("td", "fleet-toggle");
    const toggleBtn = el("button", "fleet-toggle-btn", "▸");
    toggleBtn.setAttribute("aria-label", "Show details for this pattern");
    toggleBtn.setAttribute("aria-expanded", "false");
    toggleBtn.addEventListener("click", () => { detail.open = !detail.open; });
    // A closed <details> hides its own children, but the wrapping table row
    // still has ITS OWN padding/border — mirror the open state onto the row
    // so a closed pattern's detail row fully collapses instead of leaving a
    // thin empty divider between every compact row.
    detail.addEventListener("toggle", () => {
      toggleBtn.textContent = detail.open ? "▾" : "▸";
      toggleBtn.setAttribute("aria-expanded", detail.open ? "true" : "false");
      detailRow.classList.toggle("open", detail.open);
    });
    toggleCell.append(toggleBtn);
    tr.append(toggleCell);

    const patternCell = el("td", "fleet-pattern");
    patternCell.append(el("div", "fleet-pattern-title" + (title.unclassified ? " unclassified" : ""), title.title));
    if (title.raw) {
      const raw = el("div", "fleet-pattern-raw");
      raw.append(el("span", null, "raw: "), el("span", "mono", title.raw));
      patternCell.append(raw);
    }
    // The two ranking inputs a reader cannot read off the numeric columns:
    // whether this is a pattern at all, and how well its signature is supported.
    const meta = [g.recurring ? "recurring" : "one-off"];
    if (g.evidence_strength && g.evidence_strength !== "observed")
      meta.push(g.evidence_strength + " signature");
    patternCell.append(el("div", "fleet-pattern-meta", meta.join(" · ")));
    tr.append(patternCell);

    // §12.1: a run count without its task count cannot show whether a pattern
    // spans many tasks or repeats within a few.
    const runsCell = td(String(g.runs));
    if (g.tasks != null) runsCell.append(el("div", "vs-counts", g.tasks + (g.tasks === 1 ? " task" : " tasks")));
    tr.append(runsCell);
    tr.append(td(String(g.count)));
    tr.append(resolutionBreakdownCell(g));

    // AGR-05/06 (review 82cc113): a group whose episodes NEVER had usage
    // instrumented renders as "unavailable", never a bare "0" indistinguishable
    // from a group that genuinely cost nothing. A group with SOME but not all
    // episodes measured keeps the number but flags it as a partial count.
    let tokensText = fmtCompact(g.total_tokens);
    if (g.usage_availability === "unavailable") tokensText = "unavailable";
    else if (g.usage_availability === "partial") tokensText += " (partial)";
    const tokensCell = td(tokensText);
    if (g.overlapping_usage_events || g.usage_unavailable_count) tokensCell.title = g.usage_note;
    // The blended total_tokens figure includes an unrecovered episode's
    // window all the way to the end of its run — attempt_tokens_total is
    // the honest "cost of the failed attempt itself" number, shown right
    // next to it rather than replacing it.
    if (g.attempt_tokens_total)
      tokensCell.append(el("div", "vs-counts", fmtCompact(g.attempt_tokens_total) + " wasted on the failed attempt(s)"));
    tr.append(tokensCell);
    t.append(tr);

    const detailCell = el("td"); detailCell.colSpan = 6;
    detail.append(el("summary", null, "Details — task outcome, avg turns, wall time, argument shapes, examples"));
    const body = el("div", "fleet-detail-body");
    const ocBlock = el("div", "kv-block");
    ocBlock.append(el("span", "kv-k", "Task outcome"));
    const ocV = el("span", "kv-v");
    ocV.append(outcomeSplitBlock(g));
    ocBlock.append(ocV);
    body.append(ocBlock);
    // AGR-06: repeat_rate is affected_runs_with_>1_episode / affected_runs;
    // an empty denominator is `null` (unavailable), never a misleading 0.00.
    const rate = kvBlock("Repeat rate", g.repeat_rate != null ? Math.round(g.repeat_rate * 100) + "%" : "unavailable");
    rate.title = "Share of this group's affected runs where the failure recurred more than once within that same run.";
    body.append(rate);
    body.append(kvBlock("Avg turns to resolve", g.avg_turns_to_resolve != null ? g.avg_turns_to_resolve.toFixed(1) : "—"));
    body.append(kvBlock("Wall time", fmtWallMs(g.total_wall_ms)));
    // The trustworthy "cost of this bug" number — the failed attempts
    // themselves, not the blended total_tokens which for an unrecovered
    // episode also absorbs every later, unrelated turn in that run.
    const wasted = kvBlock("Wasted on failed attempts", fmtCompact(g.attempt_tokens_total));
    wasted.title = g.usage_note;
    body.append(wasted);
    const argBlock = el("div", "kv-block");
    argBlock.append(el("span", "kv-k", "Argument shapes"));
    const argV = el("span", "kv-v"); argV.append(argumentShapesBlock(g)); argBlock.append(argV);
    body.append(argBlock);
    const exBlock = el("div", "kv-block");
    exBlock.append(el("span", "kv-k", "Representative episodes"));
    const exV = el("span", "kv-v"); exV.append(representativeEpisodesBlock(g)); exBlock.append(exV);
    body.append(exBlock);
    detail.append(body);
    detailCell.append(detail);
    detailRow.append(detailCell);
    t.append(detailRow);
  }
  const scroll = el("div", "table-scroll");
  scroll.append(t);
  card.append(scroll);
  return card;
}

// P3-B: how the group's AFFECTED runs ended, in the shared outcome vocabulary
// so Patterns cannot disagree with the Runs chips. A passing run alongside
// failing ones is shown, but never as the answer: the caveat states it proves
// the behaviour survivable, not that its approach is correct.
function outcomeSplitBlock(g) {
  const wrap = el("div", "outcome-split");
  const split = g.outcome_split || {};
  const order = [["fail", "failed"], ["pass", "passed"],
                 ["undetermined", "undetermined"], ["unverified", "unverified"]];
  const present = order.filter(([k]) => split[k]);
  if (!present.length) {
    wrap.append(el("span", "vs-counts", "none recorded"));
    return wrap;
  }
  const line = el("div", "vs-counts outcome-split-line");
  line.append(document.createTextNode("Affected runs: "));
  present.forEach(([k, label], i) => {
    if (i) line.append(document.createTextNode(" · "));
    line.append(el("span", "outcome-" + k, split[k] + " " + label));
  });
  wrap.append(line);
  if (g.outcome_mixed) {
    wrap.append(el("p", "outcome-caveat",
      "A passing run with the same behaviour shows the behaviour is survivable — "
      + "it does not establish that its approach is the correct alternative. "
      + "This compares task outcomes, not the traces behind them."));
  }
  return wrap;
}

// U3: a drilldown onto this group's sample episodes (up to _MAX_ANCHORS,
// item 30) — which run, how that one episode resolved, and which tier
// selected its error signature. Session links show a readable task name plus
// a short, copyable id fragment (never the full "namespace/task__uuid"
// string, which wraps into unreadable stacked fragments in a table cell).
function representativeEpisodesBlock(g) {
  const wrap = el("div");
  const anchors = g.example_anchors || [];
  if (!anchors.length) { wrap.append(el("span", "vs-counts", "none captured")); return wrap; }
  const list = el("div", "vs-pairs fleet-examples");
  for (const a of anchors) {
    const row = el("div", "vs-pair fleet-example-row");
    const { task } = runIdParts(a.run_id);
    const short = shortRunId(a.run_id);
    const target = "Open " + a.run_id + " at this episode's failure (" + a.classification + ")";
    const b = el("button", "fleet-example-link", task);
    b.title = target; b.setAttribute("aria-label", target);
    b.addEventListener("click", () => openRunAtEvent(a.run_id, a.failure_event_id));
    row.append(b);
    if (short) {
      row.append(el("span", "mono fleet-example-id", short));
      row.append(copyButton(a.run_id, "Copy full run id"));
    }
    row.append(el("span", "vs-counts", (a.classification || "?").replace(/_/g, " ")
      + (a.error_signature_basis === "fallback_last_nonempty" ? " · fallback signature" : "")));
    list.append(row);
    // P2 (§B1): what THIS episode does not establish, stated beside it — the
    // classification is never shown without its evidence limits.
    if (a.limits && a.limits.length) {
      const limits = el("div", "fleet-example-limits");
      limits.append(el("span", "fleet-example-limits-label", "Limits"));
      const ul = el("ul");
      for (const limit of a.limits) ul.append(el("li", null, limit));
      limits.append(ul);
      list.append(limits);
    }
  }
  wrap.append(list);
  return wrap;
}

// U3: item 31's argument-shape distribution for this exact (tool,
// error_signature) group — the key SET and value TYPE at each key among the
// group's failing calls, never the retained values (argument_shapes.py never
// computes or stores those). Only meaningful when the table is grouped by
// both dimensions together; a coarser grouping ("Tool only"/"Error only")
// spans multiple (tool, error_signature) pairs and has no single distribution
// to show, so the block says so rather than picking one arbitrarily.
function argumentShapesBlock(g) {
  const wrap = el("div");
  if (state.fleet.groupBy !== "tool,error_signature") {
    wrap.append(el("span", "vs-counts", "n/a for this grouping"));
    return wrap;
  }
  const shapes = argumentShapesFor(g);
  if (!shapes || !shapes.shapes.length) {
    wrap.append(el("span", "vs-counts", "no retained tool_input"));
    return wrap;
  }
  wrap.append(el("p", "vs-counts", shapes.shapes.length + " shape(s) among "
    + shapes.total_failing_calls + " failing call(s)"));
  const list = el("div", "vs-pairs");
  for (const shape of shapes.shapes) {
    const row = el("div", "vs-pair");
    const keys = shape.keys.map(([k, t]) => k + ":" + t).join(", ") || "(no keys)";
    row.append(el("span", "mono", keys));
    row.append(el("span", "vs-counts", Math.round(shape.share * 100) + "% · " + shape.count + " call(s)"));
    list.append(row);
  }
  wrap.append(list);
  return wrap;
}

// AGR-04 (review 82cc113): confirmed/plausible/unrecovered as three distinct
// counts sharing one cell, not one collapsed "unrecovered %" — a group whose
// unrecovered share reads 0% can still be entirely plausible (weaker-tier,
// never promoted to confirmed) resolutions, which the single number hid.
function resolutionBreakdownCell(g) {
  const cell = el("td", "vs-resolution");
  const parts = [
    ["confirmed", g.confirmed_count, g.confirmed_share],
    ["plausible", g.plausible_count, g.plausible_share],
    ["unrecovered", g.unrecovered_count, g.unrecovered_share],
  ];
  for (const [label, count, share] of parts) {
    if (!count) continue;
    const detail = count + " of " + g.count + " episode(s) " + label + ".";
    const span = el("span", "vs-resolution-part " + label,
      Math.round(share * 100) + "% " + label);
    span.title = detail;
    // §4.18/§4.19 (AGR-14): the visible "NN% label" already carries the
    // headline meaning, but the exact count is otherwise hover-only and
    // unreachable by keyboard or tap — give it a focus stop and matching
    // accessible name.
    span.setAttribute("tabindex", "0");
    span.setAttribute("aria-label", Math.round(share * 100) + "% " + label + ": " + detail);
    cell.append(span);
  }
  return cell;
}

function fmtWallMs(ms) {
  if (!ms) return "—";
  const s = ms / 1000;
  if (s < 60) return s.toFixed(1) + "s";
  const m = s / 60;
  if (m < 60) return m.toFixed(1) + "m";
  return (m / 60).toFixed(1) + "h";
}

// Opens the run's own review and jumps the full-trace drawer straight to the
// step that produced this episode's failure event — a fleet example is not
// useful unless it lands exactly where the failure happened, not just
// somewhere in the right run.
async function openRunAtEvent(runId, eventId) {
  state.view = "review";
  try { await selectRun(runId); }
  catch (e) { toast("That run is not in this store."); return; }
  const steps = (state.forensic || {}).steps || [];
  let step = null;
  if (eventId) {
    step = steps.find(s => (s.event_ids || []).includes(eventId));
    if (!step) { toast("Could not locate that event in the run's trace."); return; }
  } else {
    // No specific event (a healthy or unevaluated execution-quality run): still
    // open the trace, landing on the first model generation — the step these
    // dimensions are measured over.
    step = steps.find(s => s.kind === "model_output" || s.event_type === "model_output")
      || steps.find(s => s.kind === "tool_call" || s.event_type === "tool_call")
      || steps[0];
  }
  if (step) openTrace(step.step_id);
}
