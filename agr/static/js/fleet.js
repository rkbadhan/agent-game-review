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

// AGR-06: the whole-fleet usage headline — the union across EVERY episode,
// never a sum of the per-group "Tokens" column above (which would
// double-count an event two different groups' episodes both cover).
function renderFleetUsageSummaryCard(summary) {
  const card = el("div", "card card-pad");
  card.append(el("p", "eyebrow", "Patterns usage"));
  const line = el("p", "chapter-lede",
    summary.total_tokens + " token(s) across " + summary.episode_count + " episode(s) in "
    + summary.affected_runs + " affected run(s).");
  card.append(line);
  // AGR-05/06 (review 82cc113): an episode with no usage instrumentation at
  // all contributes nothing to total_tokens — without this line the headline
  // reads as a real, complete measurement even when it isn't.
  if (summary.usage_unavailable_episode_count) {
    card.append(el("p", "subline warn",
      summary.usage_unavailable_episode_count + " of " + summary.episode_count
      + " episode(s) never had usage instrumented — total_tokens undercounts the fleet's real cost."));
  }
  // Follow-up (review of commit 5782f1b): a partially-instrumented episode
  // (some but not all of its window measured) previously had no fleet-wide
  // equivalent to this line — its qualifier was silently dropped even
  // though the per-episode data already distinguished it.
  if (summary.usage_partial_episode_count) {
    card.append(el("p", "subline warn",
      summary.usage_partial_episode_count + " of " + summary.episode_count
      + " episode(s) had only part of their window instrumented — total_tokens may undercount "
      + "even where it is nonzero."));
  }
  const note = el("p", "subline", summary.usage_note);
  card.append(note);
  return card;
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
  if (!fl.usageSummary) {
    try { fl.usageSummary = await api("/fleet/usage-summary"); }
    catch (e) { fl.usageSummary = null; }
  }
  if (fl.usageSummary) wrap.append(renderFleetUsageSummaryCard(fl.usageSummary));
  if (!fl.argumentShapes && !fl.argumentShapesPending) await loadFleetArgumentShapes();
  wrap.append(renderFleetTable(fl.episodes));
}

function renderFleetTable(groups) {
  const card = el("div", "card card-pad");
  card.append(el("p", "eyebrow", groups.length + " group(s)"));
  card.append(el("p", "chapter-lede",
    "Sorted by group size — the most-repeated failure first. Every number is a "
    + "re-read of persisted recovery episodes; nothing here is recomputed."));
  const t = el("table", "vs-table");
  t.append(rowEls("tr", ["Group", "Count", "Runs", "Repeat rate", "Resolved",
    "Avg turns", "Tokens", "Wall time", "Episodes", "Argument shapes"], "th"));
  for (const g of groups) {
    const tr = el("tr");
    const groupCell = td(g.key.filter(Boolean).join(" / ") || "(none)");
    // AGR-07 (review 82cc113): a group keyed by an opaque fallback signature
    // (e.g. bare "---"/"}"/"===" — no traceback, no recognised diagnostic
    // marker anywhere) is not a meaningful exception group. Flag it instead
    // of presenting it identically to a confident traceback-derived one.
    if (g.all_fallback_basis) {
      // §4.18/§4.19 (AGR-14): a controlled-vocabulary flag needs a definition
      // reachable by keyboard and by tap, not only mouse hover — a bare <span>
      // with only a `title` is invisible to both. tabindex + aria-label give
      // it a focus stop and an accessible name that carries the same meaning.
      const flagText = "Every episode in this group selected its error signature via the "
        + "opaque fallback tier — no traceback or recognised diagnostic marker was found; "
        + "the signature is just whatever line happened to be last in the failure text.";
      const flag = el("span", "chip", "fallback");
      flag.title = flagText;
      flag.setAttribute("tabindex", "0");
      flag.setAttribute("aria-label", "Fallback signature group: " + flagText);
      groupCell.append(" ");
      groupCell.append(flag);
    }
    tr.append(groupCell);
    tr.append(td(String(g.count), "vs-rate"));
    tr.append(td(String(g.runs)));
    // AGR-06: repeat_rate is affected_runs_with_>1_episode / affected_runs,
    // rendered as a percentage; an empty denominator is `null` (unavailable),
    // never a misleading 0.00.
    const rateCell = td(g.repeat_rate != null ? Math.round(g.repeat_rate * 100) + "%" : "unavailable");
    rateCell.title = "Share of this group's affected runs where the failure recurred "
      + "more than once within that same run.";
    tr.append(rateCell);
    // AGR-04 (review 82cc113): a group can be 0% unrecovered and still
    // contain only plausible (weaker-tier) resolutions — showing only the
    // unrecovered share hid that distinction entirely. Render the full
    // confirmed/plausible/unrecovered breakdown instead of one number.
    tr.append(resolutionBreakdownCell(g));
    tr.append(td(g.avg_turns_to_resolve != null ? g.avg_turns_to_resolve.toFixed(1) : "—"));
    // AGR-05/06 (review 82cc113): a group whose episodes NEVER had usage
    // instrumented renders as "unavailable", never a bare "0" indistinguishable
    // from a group that genuinely cost nothing. A group with SOME but not all
    // episodes measured keeps the number but flags it as a partial count.
    // §4.18 "Redacted content is labelled `Redacted`; missing capture is
    // labelled `Not captured`" (AGR-14): a partial count gets the same kind
    // of visible, readable label as the "unavailable" case — never a bare
    // trailing "*" whose meaning only exists in a hover-only title.
    let tokensText = String(g.total_tokens);
    if (g.usage_availability === "unavailable") tokensText = "unavailable";
    else if (g.usage_availability === "partial") tokensText += " (partial)";
    const tokensCell = td(tokensText);
    if (g.overlapping_usage_events || g.usage_unavailable_count) tokensCell.title = g.usage_note;
    tr.append(tokensCell);
    tr.append(td(fmtWallMs(g.total_wall_ms)));
    tr.append(representativeEpisodesCell(g));
    tr.append(argumentShapesCell(g));
    t.append(tr);
  }
  const scroll = el("div", "table-scroll");
  scroll.append(t);
  card.append(scroll);
  return card;
}

// U3: a drilldown onto this group's sample episodes (up to _MAX_ANCHORS,
// item 30) — which run, how that one episode resolved, and which tier
// selected its error signature — not just a bare "open" link with the detail
// hidden behind a hover title. Matches the §4.16 .vs-drill convention used
// for a comparison's contributing run pairs.
function representativeEpisodesCell(g) {
  const cell = el("td");
  const anchors = g.example_anchors || [];
  if (!anchors.length) { cell.append(el("span", "vs-counts", "none captured")); return cell; }
  const d = el("details", "vs-drill");
  d.append(el("summary", null, anchors.length + " representative episode(s)"));
  const list = el("div", "vs-pairs");
  for (const a of anchors) {
    const row = el("div", "vs-pair");
    const target = "Open " + a.run_id + " at this episode's failure (" + a.classification + ")";
    const b = el("button", null, a.run_id);
    b.title = target; b.setAttribute("aria-label", target);
    b.addEventListener("click", () => openRunAtEvent(a.run_id, a.failure_event_id));
    row.append(b);
    row.append(el("span", "vs-counts", (a.classification || "?").replace(/_/g, " ")
      + (a.error_signature_basis === "fallback_last_nonempty" ? " · fallback signature" : "")));
    list.append(row);
  }
  d.append(list);
  cell.append(d);
  return cell;
}

// U3: item 31's argument-shape distribution for this exact (tool,
// error_signature) group — the key SET and value TYPE at each key among the
// group's failing calls, never the retained values (argument_shapes.py never
// computes or stores those). Only meaningful when the table is grouped by
// both dimensions together; a coarser grouping ("Tool only"/"Error only")
// spans multiple (tool, error_signature) pairs and has no single distribution
// to show, so the column says so rather than picking one arbitrarily.
function argumentShapesCell(g) {
  const cell = el("td");
  if (state.fleet.groupBy !== "tool,error_signature") {
    cell.append(el("span", "vs-counts", "n/a for this grouping"));
    return cell;
  }
  const shapes = argumentShapesFor(g);
  if (!shapes || !shapes.shapes.length) {
    cell.append(el("span", "vs-counts", "no retained tool_input"));
    return cell;
  }
  const d = el("details", "vs-drill");
  d.append(el("summary", null, shapes.shapes.length + " shape(s) among "
    + shapes.total_failing_calls + " failing call(s)"));
  const list = el("div", "vs-pairs");
  for (const shape of shapes.shapes) {
    const row = el("div", "vs-pair");
    const keys = shape.keys.map(([k, t]) => k + ":" + t).join(", ") || "(no keys)";
    row.append(el("span", "mono", keys));
    row.append(el("span", "vs-counts", Math.round(shape.share * 100) + "% · " + shape.count + " call(s)"));
    list.append(row);
  }
  d.append(list);
  cell.append(d);
  return cell;
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
  if (!eventId) return;
  const f = state.forensic || {};
  const step = (f.steps || []).find(s => (s.event_ids || []).includes(eventId));
  if (step) openTrace(step.step_id);
  else toast("Could not locate that event in the run's trace.");
}
