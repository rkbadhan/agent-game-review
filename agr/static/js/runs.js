"use strict";

// --- U2: full-width Runs table ------------------------------------------------
//   The Runs workspace (U1): reached from the app bar, it is a destination of
//   its own — not a sidebar glued to whatever run happens to be open. It reads
//   the same `/queue` result the investigation sidebar already fetches
//   (state.queue, loaded by loadInbox()), so filters/sort/search can never
//   disagree between the two surfaces. Search is client-side over that
//   already-loaded page, since the full run list is already in memory and a
//   round trip per keystroke would only add latency.
function runsMatchesSearch(r, q) {
  const needle = (q || "").trim().toLowerCase();
  if (!needle) return true;
  return (r.task_id || "").toLowerCase().includes(needle) || (r.run_id || "").toLowerCase().includes(needle);
}

function renderRunsSurface(main) {
  const nav = el("div", "review-nav");
  const head = el("div", "outline");
  head.append(el("span", "eyebrow", "Runs · sweep triage queue"));
  nav.append(head);
  const util = el("div", "review-util");
  if (state.runId) {
    const back = el("button", "seg", "‹ Back to review");
    back.addEventListener("click", () => { state.view = "review"; render(); });
    util.append(back);
  }
  nav.append(util);
  main.append(nav);

  const controls = el("div", "runs-controls-row");
  renderRunsControls(controls);
  main.append(controls);

  const tableHost = el("div"); tableHost.id = "runs-table-host";
  main.append(tableHost);
  renderRunsTable(tableHost);

  // U2: returning from a run restores the scroll position the reader left,
  // rather than dropping them back at the top of a freshly rendered table.
  // render() just wiped #main's content, which resets scrollTop to 0, so the
  // restore has to happen on the next frame, after layout.
  requestAnimationFrame(() => { main.scrollTop = state.runsScroll || 0; });
}

function renderRunsControls(host) {
  host.textContent = "";
  const search = el("input", "runs-search");
  search.type = "search";
  search.placeholder = "Search task name or run ID…";
  search.value = state.runsSearch || "";
  search.setAttribute("aria-label", "Search runs by task name or run ID");
  search.addEventListener("input", () => {
    state.runsSearch = search.value;
    renderRunsTable($("#runs-table-host"));
    syncUrl();
  });
  host.append(search);
  const refresh = () => loadInbox().then(render).catch(showInboxError);
  host.append(buildFilterChipsRow(refresh));
  host.append(buildSortRow(refresh));
}

function runsOutcomeBadgeClass(status) {
  const sc = statusClass(status);
  return sc === "undetermined" ? "warn" : sc === "warning" ? "warn" : sc === "passed" ? "pass" : "fail";
}

function renderRunsTable(host) {
  host.textContent = "";
  const all = (state.queue && state.queue.runs) || [];
  const runs = all.filter(r => runsMatchesSearch(r, state.runsSearch));
  const card = el("div", "card card-pad runs-table-card");
  card.append(el("p", "eyebrow", state.runsSearch
    ? runs.length + " of " + all.length + " run(s) match “" + state.runsSearch + "”"
    : runs.length + " run(s)"));
  if (!runs.length) {
    card.append(el("div", "empty", all.length
      ? "No run matches this search."
      : "No runs match this queue."));
    host.append(card);
    return;
  }
  const scroll = el("div", "table-scroll");
  const t = el("table", "vs-table runs-table");
  t.append(rowEls("tr", ["Run", "Outcome", "Main finding", "Review status", "Duration", "Cost"], "th"));
  for (const r of runs) {
    const tr = el("tr", "runs-row" + (r.run_id === state.runId ? " active" : ""));
    tr.tabIndex = 0;
    tr.dataset.runId = r.run_id;

    const idTd = el("td", "runs-id-cell");
    idTd.append(el("div", "runs-task", r.task_id || r.run_id));
    if (r.task_id && r.task_id !== r.run_id) idTd.append(el("div", "runs-run-id mono", r.run_id));
    tr.append(idTd);

    const o = r.outcome || {};
    const outTd = el("td");
    outTd.append(el("span", "badge " + runsOutcomeBadgeClass(o.status), (o.status || "?").toUpperCase()));
    outTd.append(el("div", "vs-counts", (o.passed ?? "?") + "/" + (o.total ?? "?") + " checks"));
    tr.append(outTd);

    const mfTd = el("td", "runs-finding");
    if (r.main_finding) {
      const text = r.main_finding.length > 140 ? r.main_finding.slice(0, 138) + "…" : r.main_finding;
      mfTd.append(el("span", r.main_finding_polarity === "positive" ? "runs-finding-pos" : "runs-finding-neg", text));
    } else {
      mfTd.append(el("span", "panel-dim", "No decisive finding"));
    }
    tr.append(mfTd);

    const wf = r.workflow || {};
    const handled = wf.review_progress === "handled";
    const rsTd = el("td");
    const rsCls = handled ? "handled" : wf.review_progress === "in_progress" ? "progress" : "";
    rsTd.append(el("span", "review-state " + rsCls,
      handled && wf.disposition ? vocab("disposition", wf.disposition).label : vocab("progress", wf.review_progress || "unreviewed").label));
    tr.append(rsTd);

    tr.append(td(fmtDuration(r.duration_s) || "—", "vs-rate"));
    tr.append(td(fmtCost(r.cost) || "—", "vs-rate"));

    const open = () => { state.runsScroll = $("#main").scrollTop; selectRun(r.run_id); };
    tr.addEventListener("click", open);
    tr.addEventListener("keydown", e => { if (e.key === "Enter") open(); });
    t.append(tr);
  }
  scroll.append(t);
  card.append(scroll);
  host.append(card);
}
