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
  head.append(el("span", "eyebrow", "Fleet · recovery episodes across every run"));
  nav.append(head);
  const util = el("div", "review-util");
  const back = el("button", "seg", state.runId ? "‹ Back to review" : "‹ Back to runs");
  back.addEventListener("click", () => { state.view = "review"; render(); });
  util.append(back);
  nav.append(util);
  main.append(nav);
  renderFleet(main).catch(e => main.append(el("div", "empty", "Failed: " + e.message)));
  renderEvidencePanel();
}

async function loadFleetEpisodes() {
  const fl = state.fleet;
  fl.pending = true;
  try { fl.episodes = await api("/fleet/episodes?group_by=" + encodeURIComponent(fl.groupBy)); }
  finally { fl.pending = false; }
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
      fl.groupBy = val; fl.episodes = null; render();
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

  if (!fl.episodes && !fl.pending) {
    const loading = el("div", "subline", "Loading fleet episodes…");
    wrap.append(loading);
    try { await loadFleetEpisodes(); }
    catch (e) { loading.textContent = "Failed: " + e.message; loading.className = "empty"; return; }
    loading.remove();
  }
  if (fl.pending) { wrap.append(el("div", "subline", "Loading fleet episodes…")); return; }
  if (!fl.episodes || !fl.episodes.length) {
    wrap.append(el("div", "empty",
      "No recovery episodes in this store yet — a run needs at least one qualifying "
      + "tool failure for the fleet view to have anything to group."));
    return;
  }
  wrap.append(renderFleetTable(fl.episodes));
}

function renderFleetTable(groups) {
  const card = el("div", "card card-pad");
  card.append(el("p", "eyebrow", groups.length + " group(s)"));
  card.append(el("p", "chapter-lede",
    "Sorted by group size — the most-repeated failure first. Every number is a "
    + "re-read of persisted recovery episodes; nothing here is recomputed."));
  const t = el("table", "vs-table");
  t.append(rowEls("tr", ["Group", "Count", "Runs", "Repeat rate", "Unrecovered",
    "Avg turns", "Tokens", "Wall time", "Examples"], "th"));
  for (const g of groups) {
    const tr = el("tr");
    tr.append(td(g.key.filter(Boolean).join(" / ") || "(none)"));
    tr.append(td(String(g.count), "vs-rate"));
    tr.append(td(String(g.runs)));
    tr.append(td(g.repeat_rate.toFixed(2)));
    tr.append(td(Math.round(g.unrecovered_share * 100) + "%"));
    tr.append(td(g.avg_turns_to_resolve != null ? g.avg_turns_to_resolve.toFixed(1) : "—"));
    tr.append(td(String(g.total_tokens)));
    tr.append(td(fmtWallMs(g.total_wall_ms)));
    const exCell = el("td");
    for (const a of (g.example_anchors || [])) {
      const label = a.run_id.length > 22 ? a.run_id.slice(0, 20) + "…" : a.run_id;
      const b = el("button", "seg", label);
      b.title = "Open " + a.run_id + " at this episode's failure (" + a.classification + ")";
      b.addEventListener("click", () => openRunAtEvent(a.run_id, a.failure_event_id));
      exCell.append(b);
    }
    tr.append(exCell);
    t.append(tr);
  }
  const scroll = el("div", "table-scroll");
  scroll.append(t);
  card.append(scroll);
  return card;
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
