"use strict";

// --- U2: full-width Runs table ------------------------------------------------
//   The Runs workspace (U1): reached from the app bar, it is a destination of
//   its own — not a sidebar glued to whatever run happens to be open. It reads
//   the same `/queue` result the investigation sidebar already fetches
//   (state.queue, loaded by loadInbox()), so filters/sort/search can never
//   disagree between the two surfaces. Search is client-side over that
//   already-loaded page, since the full run list is already in memory and a
//   round trip per keystroke would only add latency.
const RUNS_PAGE = 50;
function runsMatchesSearch(r, q) {
  const needle = (q || "").trim().toLowerCase();
  if (!needle) return true;
  return (r.task_id || "").toLowerCase().includes(needle) || (r.run_id || "").toLowerCase().includes(needle);
}
// R3 (coordinator review 2, item 2): a boilerplate main_finding must not
// fall back to a verdict-shaped sentence here — the Outcome column already
// shows the status badge and its own "X/Y checks" line right next to this
// one, so "Failed — 0/1 checks." simply repeated it. chapters.js/moments.js
// can fall back to a moment-specific "<Kind> during <Phase>" headline because
// they hold the full moment (kind, phase_id, anchor step); this table's row
// is the lightweight queue summary (agr/read.py's list_runs) — outcome
// status/passed/total only, no moment kind or phase — and adding either
// would mean changing that backend summary, out of scope here. The honest
// fallback is a plain "no finding text" statement, styled the same as the
// "no finding at all" case below, rather than fabricating check names this
// row does not carry.
function runRowFindingText(r) {
  const finding = r.main_finding;
  return groundedBoilerplateIds(finding) ? null : finding;
}

// The page used to go straight from the eyebrow to the
// controls with no orientation — a reader couldn't tell how big the corpus
// was, or how much of it still needed review, without scrolling to the
// table's own meta line below the fold. sweepIdentityText (inbox.js) is the
// same computation the sidebar's own summary uses, so the sweep/finished-time
// wording can never read differently on the two surfaces. `null` (not an
// empty element) before state.sweep has loaded, so the caller can skip it
// cleanly on that first render rather than showing an empty bar.
function renderRunsStatLine() {
  const s = state.sweep;
  if (!s) return null;
  const wrap = el("div", "runs-stat-line");
  const total = s.total_runs ?? 0;
  wrap.append(el("span", "runs-stat-count", total.toLocaleString() + (total === 1 ? " run" : " runs")));
  const counts = state.queue && state.queue.filter_counts;
  const unreviewed = counts ? counts.unreviewed : null;
  if (unreviewed != null) {
    wrap.append(el("span", "runs-stat-sep", "·"));
    wrap.append(el("span", "runs-stat-unreviewed", unreviewed.toLocaleString() + " unreviewed"));
  }
  const { detail } = sweepIdentityText(s);
  if (detail) {
    wrap.append(el("span", "runs-stat-sep", "·"));
    wrap.append(el("span", "runs-stat-detail", detail));
  }
  return wrap;
}

function renderRunsSurface(main) {
  // Coordinator review 2, item 7: one page title ("Runs"), with the stat
  // line ("27 runs · 27 unreviewed · …") as its subtitle right below —
  // "Sweep triage queue" read as a second, competing title sitting directly
  // above the stat line's own big "27 runs" count.
  const nav = el("div", "review-nav runs-page-nav");
  const head = el("div", "outline");
  head.append(el("span", "eyebrow", "Runs"));
  nav.append(head);
  const util = el("div", "review-util");
  if (state.runId) {
    const back = el("button", "seg"); back.append(icon("chevron-left"), document.createTextNode(" Back to review"));
    back.addEventListener("click", () => { state.view = "review"; render(); });
    util.append(back);
  }
  nav.append(util);
  main.append(nav);

  const stat = renderRunsStatLine();
  if (stat) main.append(stat);

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
  let clear;
  const searchWrap = el("div", "runs-search-wrap");
  const searchIcon = el("span", "runs-search-icon"); searchIcon.append(icon("search"));
  searchIcon.setAttribute("aria-hidden", "true");
  searchWrap.append(searchIcon);
  const search = el("input", "runs-search");
  search.type = "search";
  search.placeholder = "Search task name or run ID…";
  search.value = state.runsSearch || "";
  search.setAttribute("aria-label", "Search runs by task name or run ID");
  search.addEventListener("input", () => {
    state.runsSearch = search.value;
    if (clear) clear.hidden = !(state.filters.size || state.runsSearch);
    renderRunsTable($("#runs-table-host"));
    syncUrl();
  });
  searchWrap.append(search);
  host.append(searchWrap);
  const refresh = () => loadInbox().then(render).catch(showInboxError);
  host.append(buildFilterChipsRow(refresh));
  const actions = el("div", "runs-toolbar-actions");
  actions.append(buildSortRow(refresh));
  clear = el("button", "runs-clear", "Clear all");
  clear.type = "button";
  clear.hidden = !(state.filters.size || state.runsSearch);
  clear.addEventListener("click", () => {
    state.filters.clear();
    state.runsSearch = "";
    refresh();
  });
  actions.append(clear);
  host.append(actions);
}

function runsOutcomeBadgeClass(status) {
  const sc = statusClass(status);
  return sc === "undetermined" ? "warn" : sc === "warning" ? "warn" : sc === "passed" ? "pass" : "fail";
}

// Runs §item "readable session identity": the captured task is the title
// (already r.task_id, the closest thing to a human-given name this source
// carries); below it, ONE shortened, copyable session-id fragment — never the
// full "namespace/task__uuid" string repeated in mono, which is what forced a
// reader to read UUIDs to tell two runs apart — plus repository (the
// harness's working directory, when the source captured one — currently only
// Claude Code sessions do) and captured time, when available.
function runsIdentityCell(r) {
  const cell = el("td", "runs-id-cell");
  cell.append(el("div", "runs-task", r.task_id || r.run_id));
  const meta = el("div", "runs-id-meta");
  const short = shortRunId(r.run_id);
  if (short) {
    meta.append(el("span", "mono runs-run-id", short));
    meta.append(copyButton(r.run_id, "Copy full session id"));
  }
  if (r.cwd) meta.append(el("span", "runs-id-repo", r.cwd));
  if (r.model) meta.append(el("span", "runs-id-model", r.model));
  const when = relTime(r.finished_at || r.started_at);
  if (when) meta.append(el("span", "runs-id-time", when));
  if (meta.childNodes.length) cell.append(meta);
  return cell;
}

// Runs §item "simpler controls": Cost and Duration are shown only when at
// least one run in the CURRENT (filtered) set actually captured a value —
// a column of permanent "—" cells is noise, and a missing value must never
// be hidden by collapsing it into a false 0 either.
function renderRunsTable(host) {
  host.textContent = "";
  const all = (state.queue && state.queue.runs) || [];
  const runs = all.filter(r => runsMatchesSearch(r, state.runsSearch));
  const card = el("div", "card card-pad runs-table-card");
  // Coordinator review 2, item 7: this used to always repeat "N runs ·
  // Sorted by …" — a duplicate of the page's own stat line above whenever
  // nothing was actually narrowing the table (the overwhelmingly common
  // case), and "Sorted by" duplicates what the Sort control's own selected
  // option already says. It's worth a line only when a search or a filter
  // has made this table's count differ from the sweep's total.
  if (state.runsSearch || state.filters.size) {
    const tableMeta = el("div", "runs-table-meta");
    // `all` is already filter-narrowed (state.queue.runs, loaded with the
    // active filters applied server-side) — only the client-side search
    // narrows further, so "N of M" only makes sense against the search.
    const label = state.runsSearch
      ? runs.length + " of " + all.length + (all.length === 1 ? " run" : " runs") + " match your search"
      : runs.length + (runs.length === 1 ? " run" : " runs") + " match the selected filters";
    tableMeta.append(el("strong", null, label));
    card.append(tableMeta);
  }
  if (!runs.length) {
    const empty = el("div", "runs-empty");
    empty.append(icon("search", "runs-empty-icon"));
    empty.append(el("strong", null, state.runsSearch
      ? "No runs match that search"
      : state.filters.size ? "No runs match the selected filters" : "No runs in this sweep"));
    empty.append(el("p", null, state.runsSearch
      ? "Try a task name, run ID, or clear the search."
      : state.filters.size
        ? "Clear the filters to return to the full triage queue."
        : "Ingest a run to begin reviewing this sweep."));
    const emptyActions = el("div", "runs-empty-actions");
    if (state.runsSearch) {
      const clearSearch = el("button", "button subtle", "Clear search");
      clearSearch.type = "button";
      clearSearch.addEventListener("click", () => {
        state.runsSearch = "";
        renderRunsControls($(".runs-controls-row"));
        renderRunsTable($("#runs-table-host"));
        syncUrl();
      });
      emptyActions.append(clearSearch);
    }
    if (state.filters.size) {
      const clearFilters = el("button", "button", "Show all runs");
      clearFilters.type = "button";
      clearFilters.addEventListener("click", () => {
        state.filters.clear();
        loadInbox().then(render).catch(showInboxError);
      });
      emptyActions.append(clearFilters);
    }
    if (emptyActions.childNodes.length) empty.append(emptyActions);
    card.append(empty);
    host.append(card);
    return;
  }
  const showDuration = runs.some(r => r.duration_s != null);
  const showCost = runs.some(r => r.cost != null);
  const scroll = el("div", "table-scroll");
  const t = el("table", "vs-table runs-table"
    + (showDuration ? " has-duration" : "")
    + (showCost ? " has-cost" : ""));
  // table-layout:auto with a shrink-to-fit hint on the short
  // columns (.col-compact, width:1% in app.css) replaces the old ~20 lines
  // of has-duration/has-cost nth-child width percentages — Session and Main
  // finding (no compact class) simply take whatever space is left over.
  const headerDefs = [["Session", null], ["Outcome", "col-compact"],
    ["Main finding", null], ["Review status", "col-compact"]];
  if (showDuration) headerDefs.push(["Duration", "col-compact num"]);
  if (showCost) headerDefs.push(["Cost", "col-compact num"]);
  const headRow = el("tr");
  for (const [label, cls] of headerDefs) headRow.append(el("th", cls, label));
  t.append(headRow);

  // Declared before runsRow so its `open()` (below) closes over the LIVE
  // value — read fresh at click time, not the 0 this had when runsRow was
  // defined — and can save how many rows were actually on screen.
  let shown = 0;

  function runsRow(r) {
    const tr = el("tr", "runs-row" + (r.run_id === state.runId ? " active" : ""));
    tr.tabIndex = 0;
    tr.setAttribute("role", "link");
    tr.setAttribute("aria-label", "Open " + (r.task_id || r.run_id));
    tr.dataset.runId = r.run_id;

    tr.append(runsIdentityCell(r));

    const o = r.outcome || {};
    const outTd = el("td");
    outTd.append(el("span", "badge " + runsOutcomeBadgeClass(o.status), (o.status || "?").toUpperCase()));
    outTd.append(el("div", "vs-counts", (o.passed ?? "?") + "/" + (o.total ?? "?") + " checks"));
    // P1: no verifier evidence means no task verdict is possible — the badge
    // above is then a coverage statement, not a judgement. Say so where the
    // verdict would otherwise read as one.
    if (!(r.verification || {}).has_verifier)
      outTd.append(el("div", "vs-counts runs-no-verifier", "no verifier · analysis only"));
    tr.append(outTd);

    const mfTd = el("td", "runs-finding");
    const text = runRowFindingText(r);
    if (text) {
      const finding = el("span",
        "runs-finding-text " + (r.main_finding_polarity === "positive" ? "runs-finding-pos" : "runs-finding-neg"),
        leadFinding(text, 112));
      finding.title = r.main_finding;
      mfTd.append(finding);
    } else if (r.main_finding) {
      // The generated "Grounded in quoted run evidence (evt_…)" summary: say
      // what it is rather than repeat a list of event ids in the table.
      const n = groundedBoilerplateIds(r.main_finding).length;
      const finding = el("span", "runs-finding-text runs-finding-neg",
        "Evidence-grounded finding · " + n + (n === 1 ? " quoted event" : " quoted events"));
      finding.title = r.main_finding;
      mfTd.append(finding);
    } else {
      mfTd.append(el("span", "runs-finding-text runs-finding-none", "No decisive finding"));
    }
    tr.append(mfTd);

    // A pill only when review status is something other than the
    // default (U3 — every row showing the same bordered "Unreviewed" pill is
    // noise, not signal). Unreviewed instead shows a small empty-circle icon;
    // its label stays in the DOM as .sr-only text, not dropped, so a test's
    // .inner_text() or a screen reader still reads "Unreviewed".
    const wf = r.workflow || {};
    const handled = wf.review_progress === "handled";
    const inProgress = wf.review_progress === "in_progress";
    const rsCls = handled ? "handled" : inProgress ? "progress" : "unreviewed";
    const rsTd = el("td");
    const label = handled && wf.disposition ? vocab("disposition", wf.disposition).label : vocab("progress", wf.review_progress || "unreviewed").label;
    if (handled || inProgress) {
      rsTd.append(el("span", "review-state " + rsCls, label));
    } else {
      const rs = el("span", "review-state unreviewed-icon");
      rs.append(icon("circle"), el("span", "sr-only", label));
      rs.title = label;
      rsTd.append(rs);
    }
    tr.append(rsTd);

    if (showDuration) tr.append(td(fmtDuration(r.duration_s) || "—", "vs-rate num"));
    if (showCost) tr.append(td(fmtCost(r.cost) || "—", "vs-rate num"));

    // U2 follow-up: `shown` is saved alongside the scroll offset so a return
    // to this table (below) can restore ENOUGH rows for that offset to mean
    // anything, not just the scroll number itself.
    const open = () => { state.runsScroll = $("#main").scrollTop; state.runsShown = shown; selectRun(r.run_id); };
    tr.addEventListener("click", open);
    tr.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    });
    return tr;
  }

  // UX audit finding #1: this table had no pagination anywhere — every run in
  // the (filtered) set rendered as a DOM row unconditionally, so an unfiltered
  // 1,628-run store meant 1,628 <tr>s on first paint. Same client-side pager
  // as the execution-quality drilldown's eqRunList() (fleet.js) — `runs` is
  // already fully in memory (client-side search is the only filtering that
  // happens here; outcome/status filtering is server-side via /queue), so
  // revealing more needs no extra request, just the next slice.
  const moreRow = el("tr", "runs-load-more-row");
  const moreCell = el("td"); moreCell.colSpan = headerDefs.length;
  const moreBtn = el("button", "seg", "");
  moreCell.append(moreBtn);
  moreRow.append(moreCell);
  t.append(moreRow);
  function showMore(count) {
    const take = count || RUNS_PAGE;
    for (const r of runs.slice(shown, shown + take)) t.insertBefore(runsRow(r), moreRow);
    shown = Math.min(shown + take, runs.length);
    if (shown >= runs.length) moreRow.remove();
    else moreBtn.textContent = "Show more (" + (runs.length - shown) + " of " + runs.length + " remaining)";
  }
  // The click listener must not forward its MouseEvent as `count` — showMore
  // treats any truthy argument as a row count, and an Event object there
  // would corrupt the slice.
  moreBtn.addEventListener("click", () => showMore());
  // U2 follow-up (review of this PR): returning to this table — via Back,
  // which restores state.runId without going through the "Runs" nav
  // button's reset (goToRuns(), keyboard.js) — restores state.runsScroll
  // AND the previously-open run's `.active` highlight. Neither means
  // anything if that run's row is still behind an unrevealed page: with a
  // flat RUNS_PAGE-row first reveal, opening run #800 out of 1,628 and
  // coming back would clamp the saved scroll to the bottom of a
  // much-shorter page-1 table, and #800's row (not among the first 50)
  // would never show its highlight either. Reveal at least as far as that
  // run's own position, and at least as many rows as were on screen when it
  // was opened (state.runsShown) — both fall back to 0/-1 for an ordinary
  // fresh visit, so this never over-reveals when nothing needs restoring.
  const activeIdx = state.runId ? runs.findIndex(r => r.run_id === state.runId) : -1;
  const restoreFloor = activeIdx >= 0 ? Math.max(activeIdx + 1, state.runsShown || 0) : 0;
  showMore(Math.max(RUNS_PAGE, restoreFloor));

  scroll.append(t);
  card.append(scroll);
  host.append(card);
}
