"use strict";

// Leaving a pending selection restores the last fully loaded run. A late
// response must never reopen review or pair old findings with the pending ID.
function cancelPendingRunLoad(destination) {
  if (!state.pendingSelection) return;
  const previous = state.pendingSelection.previous;
  state.loadToken++;
  Object.assign(state, previous);
  state.pendingSelection = null; state.loading = false; state.view = destination;
}

// Worklist, inspector and focused review share one definition of the visible order.
function worklistGroups(rows) {
  if (state.runsGrouping === "none") return [{ key: "all", label: "All runs", runs: rows }];
  const groups = new Map();
  for (const run of rows) {
    const context = run.source_context || {};
    const generatedTask = run.harness_version === "langfuse-export" && !context.task_id_explicit
      && (run.task_id || "").startsWith("langfuse-trace-");
    const label = state.runsGrouping === "session" ? (typeof context.sessionId === "string" && context.sessionId || "No captured session")
      : generatedTask ? runDisplayName(run) : run.task_id || runDisplayName(run);
    const key = JSON.stringify([context.host || "", context.project_id || "", label]);
    if (!groups.has(key)) groups.set(key, { key, label, runs: [] });
    groups.get(key).runs.push(run);
  }
  return [...groups.values()].sort((a, b) => a.label.localeCompare(b.label) || a.key.localeCompare(b.key));
}
function visibleWorklistRuns() {
  const rows = ((state.queue && state.queue.runs) || []).filter(r =>
    (!state.patternRunIds || state.patternRunIds.has(r.run_id)) && (!state.importRunIds || state.importRunIds.has(r.run_id))
    && runsMatchesSearch(r, state.runsSearch) && aiFilterMatches(r));
  return worklistGroups(rows).flatMap(group => group.runs);
}
function groupStorageKey() { return "agr-run-groups-" + (state.projectId || "existing"); }
function groupIsClosed(key) {
  try { return JSON.parse(localStorage.getItem(groupStorageKey()) || "[]").includes(key); } catch (_) { return false; }
}
function saveGroupState(key, closed) {
  try {
    const saved = new Set(JSON.parse(localStorage.getItem(groupStorageKey()) || "[]"));
    if (closed) saved.add(key); else saved.delete(key);
    localStorage.setItem(groupStorageKey(), JSON.stringify([...saved].slice(-1000)));
  } catch (_) {}
}
function frozenStorageKey(project, id) { return "agr-review-queue-" + project + "-" + id; }
function readFrozenQueue(id, project) {
  if (!id || !/^[a-zA-Z0-9_-]{1,80}$/.test(id)) return null;
  try {
    const saved = JSON.parse(localStorage.getItem(frozenStorageKey(project, id)) || "null");
    if (!saved || saved.id !== id || saved.project !== project || !Array.isArray(saved.run_ids)
        || saved.run_ids.length > 10000 || !saved.run_ids.every(r => typeof r === "string" && r.length <= 512)
        || new Set(saved.run_ids).size !== saved.run_ids.length) return null;
    if (saved.groups != null) {
      if (!Array.isArray(saved.groups) || !saved.groups.every(group => group && typeof group.key === "string" && typeof group.label === "string" && Array.isArray(group.run_ids))) return null;
      const grouped = saved.groups.flatMap(group => group.run_ids);
      if (grouped.length !== saved.run_ids.length || grouped.some((id, index) => id !== saved.run_ids[index])) return null;
    }
    return saved;
  } catch (_) { return null; }
}
async function startFocusedReview(runId, evidenceStep, fromHome = false) {
  if (state.reviewStarting) return;
  state.reviewStarting = true;
  const project = state.projectId, navigation = state.navigationVersion;
  try {
    if (state.inboxPromise) await state.inboxPromise;
    if (state.queueError) { toast("Reload the runs before starting a new review queue."); return; }
    if (project !== state.projectId || navigation !== state.navigationVersion) return;
    let rows = visibleWorklistRuns();
    if (!runId && !fromHome && rows.some(r => state.runsSelected.has(r.run_id))) rows = rows.filter(r => state.runsSelected.has(r.run_id));
    if (!rows.length && !runId) { toast("No runs match. Change the filters to start review."); return; }
    const id = uid();
    state.focusedQueue = { id, created_at: Date.now(), project: project || "existing", run_ids: rows.map(r => r.run_id),
      definition: { filters: [...state.filters], sort: state.sort, search: state.runsSearch, ai: state.ai.filter, grouping: state.runsGrouping,
        pattern_ids: state.patternRunIds ? [...state.patternRunIds] : null, import_ids: state.importRunIds ? [...state.importRunIds] : null },
      groups: state.runsGrouping !== "none" ? worklistGroups(rows).map(group => ({ key: group.key, label: group.label, run_ids: group.runs.map(run => run.run_id) })) : null,
      return_to: { inspector: state.inspector.runId, anchor: state.runsAnchorId, shown: state.runsShown, scroll: state.view === "runs" ? $("#main").scrollTop : 0 },
      runs: rows };
    try {
      const saved = Object.assign({}, state.focusedQueue); delete saved.runs;
      localStorage.setItem(frozenStorageKey(saved.project, id), JSON.stringify(saved));
      const prefix = "agr-review-queue-" + saved.project + "-";
      const createdAt = key => { try { return JSON.parse(localStorage.getItem(key)).created_at || 0; } catch (_) { return 0; } };
      const keys = Object.keys(localStorage).filter(key => key.startsWith(prefix) && key !== frozenStorageKey(saved.project, id))
        .sort((a, b) => createdAt(a) - createdAt(b));
      for (const key of keys.slice(0, Math.max(0, keys.length - 19))) localStorage.removeItem(key);
    } catch (_) { toast("Review order is held in this tab; this browser could not save it for reload."); }
    const startedQueue = state.focusedQueue;
    try { await selectRun(runId || rows[0].run_id, evidenceStep ? { trace: true, evidence: evidenceStep } : null); }
    catch (error) { if (state.focusedQueue === startedQueue) state.focusedQueue = null; throw error; }
  } finally {
    state.reviewStarting = false;
    if (state.view === "runs") { refreshWorklistActions(); const host = $("#trace-inspector-host"); if (host) renderTraceInspector(host); }
  }
}
function focusedRows() {
  const snapshot = state.focusedQueue;
  if (!snapshot) return [];
  const current = new Map((snapshot.runs || []).map(r => [r.run_id, r]));
  return snapshot.run_ids.map(id => current.get(id)).filter(Boolean);
}
function renderWorklistActions(host) {
  const rows = visibleWorklistRuns(), chosen = rows.filter(r => state.runsSelected.has(r.run_id));
  const hiddenSelection = state.runsSelected.size - chosen.length;
  const bar = el("div", "worklist-actions");
  const selectLabel = el("label", "worklist-selection");
  const all = el("input"); all.type = "checkbox"; all.setAttribute("aria-label", "Select all matching runs");
  all.checked = !!rows.length && chosen.length === rows.length; all.indeterminate = !!chosen.length && chosen.length < rows.length;
  all.disabled = !rows.length;
  all.addEventListener("change", () => {
    for (const run of rows) { if (all.checked) state.runsSelected.add(run.run_id); else state.runsSelected.delete(run.run_id); }
    refreshWorklistActions();
  });
  selectLabel.append(all, el("span", null, (chosen.length ? chosen.length + " selected in this view" : rows.length + " matching runs") + (hiddenSelection ? " · " + hiddenSelection + " selected outside this view" : "")));
  const groupLabel = el("label", "runs-view-picker"); groupLabel.append(el("span", null, "Group"));
  const group = el("select"); group.setAttribute("aria-label", "Group runs");
  for (const [value, label] of [["none", "None"], ["task", "Task"], ["session", "Session"]]) {
    const option = el("option", null, label); option.value = value; group.append(option);
  }
  group.value = state.runsGrouping;
  group.addEventListener("change", () => { state.runsGrouping = group.value; state.worklistGroupRevealed = null; state.runsShown = 0; state.runsScroll = 0; render(); });
  groupLabel.append(group);
  const start = workspaceButton("Start review · " + (chosen.length || rows.length), () => startFocusedReview(), !state.inspector.runId);
  start.classList.add("start-review"); start.disabled = !rows.length || !!state.inboxPromise || !!state.queueError || state.reviewStarting;
  bar.append(selectLabel, groupLabel, start);
  if (state.runsSelected.size) bar.append(workspaceButton("Clear selection", () => { state.runsSelected.clear(); refreshWorklistActions(); }));
  host.replaceChildren(bar);
}
function refreshWorklistActions() {
  const host = $("#worklist-actions"); if (host) renderWorklistActions(host);
  for (const box of document.querySelectorAll("[data-select-run]")) box.checked = state.runsSelected.has(box.dataset.selectRun);
}
async function openRunInspector(runId, restorePosition = false) {
  const selected = state.inspector, token = ++selected.token, project = state.projectId;
  if (!restorePosition && selected.runId !== runId && $("#runs-table-host")) state.runsScroll = $("#main").scrollTop;
  selected.runId = runId; selected.data = null; selected.error = null; selected.loading = true;
  state.runsAnchorId = runId;
  render();
  requestAnimationFrame(() => {
    const row = document.querySelector('.runs-row[data-run-id="' + CSS.escape(runId) + '"]');
    if (row && window.innerWidth > 1100) row.scrollIntoView({ block: "nearest" });
    const heading = $(".trace-inspector h2"); if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); } });
  try {
    const data = await api("/runs/" + encodeURIComponent(runId) + "/inspect");
    if (token !== selected.token || project !== state.projectId) return;
    selected.data = data;
  } catch (error) {
    if (token !== selected.token || project !== state.projectId) return;
    selected.error = error.message;
  }
  if (token !== selected.token || project !== state.projectId) return;
  selected.loading = false;
  const host = $("#trace-inspector-host"); if (host) renderTraceInspector(host);
}
function closeRunInspector() {
  const id = state.inspector.runId;
  state.inspector.token++; state.inspector.runId = null; state.inspector.data = null;
  render();
  requestAnimationFrame(() => { const row = document.querySelector('.runs-row[data-run-id="' + CSS.escape(id || "") + '"]'); if (row) { const target = row.checkVisibility() ? row : row.closest("details")?.querySelector("summary"); if (target) { target.focus({ preventScroll: true }); target.scrollIntoView({ block: "nearest" }); } } });
}
function renderTraceInspector(host) {
  const previous = host.querySelector(".trace-inspector"), scroll = previous ? previous.scrollTop : 0;
  const metadataWasOpen = previous && previous.querySelector("details[open]");
  const selected = state.inspector, data = selected.data;
  const panel = el("aside", "trace-inspector"); panel.setAttribute("aria-label", "Trace inspector"); panel.setAttribute("aria-busy", String(selected.loading));
  const head = el("div", "inspector-head");
  head.append(el("h2", null, data ? runDisplayName(data.identity) : "Trace inspector"), workspaceButton("Close inspector", closeRunInspector));
  panel.append(head);
  if (selected.loading) panel.append(el("p", "subline", "Loading preview…"));
  else if (selected.error) {
    const error = el("p", "engineer-error", "Could not load this preview. " + selected.error); error.setAttribute("role", "alert");
    panel.append(error, workspaceButton("Retry preview", () => openRunInspector(selected.runId)));
  } else if (data) {
    const inside = visibleWorklistRuns().some(r => r.run_id === data.run_id);
    if (!inside) panel.append(el("p", "notice", "Outside this filter"));
    const status = el("div", "inspector-status");
    status.append(el("span", "badge " + runsOutcomeBadgeClass(data.outcome.status), data.outcome.status || "Unknown"));
    status.append(el("span", null, "Human review: " + vocab("progress", data.workflow.review_progress || "unreviewed").label)); panel.append(status);
    const open = workspaceButton("Open full review", () => startFocusedReview(data.run_id), true); open.disabled = !!state.queueError || !!state.inboxPromise || state.reviewStarting; panel.append(open);
    for (const [label, text] of [["Main finding", data.finding || "No decisive finding"], ["Input", data.input || "Not captured"], ["Output", data.output || "Not captured"]]) {
      const section = el("section"); section.append(el("h3", null, label), el("p", "inspector-excerpt", text)); panel.append(section);
    }
    if (data.evidence.length) {
      const section = el("section"); section.append(el("h3", null, "Linked evidence"));
      for (const evidence of data.evidence) {
        const link = workspaceButton(evidence.event_id + " · " + humanizeToken(evidence.type), () => startFocusedReview(data.run_id, evidence.step_id));
        section.append(link, el("p", "inspector-excerpt", evidence.excerpt));
      }
      panel.append(section);
    }
    const metadata = el("details"); metadata.open = !!metadataWasOpen; metadata.append(el("summary", null, "Captured details"));
    const context = data.identity.source_context || {};
    for (const [label, value] of [["Session", context.sessionId], ["User", context.userId], ["Model", data.identity.model], ["Configuration", data.identity.configuration_id], ["Started", data.identity.started_at], ["Run ID", data.run_id]])
      if (value) metadata.append(el("p", "inspector-excerpt", label + ": " + value));
    if (data.identity.harness_version === "langfuse-export" && data.adapter_version === "langfuse-adapter-0.4")
      metadata.append(el("p", "engineer-hint", "This capture predates stored user IDs and root input/output. Re-import to capture available fields in a new revision; the original stays unchanged."));
    metadata.append(workspaceButton("Refresh preview", () => openRunInspector(data.run_id, true)));
    panel.append(metadata);
  }
  host.replaceChildren(panel); panel.scrollTop = scroll;
}
function renderFocusedChrome() {
  let nav = $("#focused-navigation");
  if (!nav) {
    nav = el("div", "focused-navigation"); nav.id = "focused-navigation";
    nav.append(workspaceButton("← Runs", goToRuns), workspaceButton("Home", () => goEngineer("home")));
    $("#sidebar").prepend(nav);
  }
  const focused = !!state.focusedQueue && !isWorkspaceView(state.view);
  let finder = $("#focused-finder");
  if (!finder) {
    finder = el("div", "focused-finder"); finder.id = "focused-finder";
    const input = el("input"); input.type = "search"; input.placeholder = "Find in this queue…";
    input.setAttribute("aria-label", "Find in review queue");
    input.addEventListener("input", () => { state.focusSearch = input.value; if (state.focusPages) state.focusPages.clear(); renderRunList(); });
    finder.append(input, el("small", null, "Find keeps the saved review order."));
    $("#queue").prepend(finder);
  }
  finder.hidden = !focused;
  finder.querySelector("input").value = state.focusSearch;
  document.body.classList.toggle("focused-review", focused);
}

function restoreFocusedDefinition(queue) {
  const definition = queue.definition || {};
  if (Array.isArray(definition.filters)) state.filters = new Set(definition.filters.filter(key => FILTER_CHIPS.some(([known]) => key === known)));
  state.sort = SORT_OPTIONS.some(([key]) => key === definition.sort) ? definition.sort : "triage";
  state.runsSearch = typeof definition.search === "string" ? definition.search : "";
  state.ai.filter = ["none", "active", "reviewed", "failed"].includes(definition.ai) ? definition.ai : "";
  state.runsGrouping = ["task", "session"].includes(definition.grouping) ? definition.grouping : "none";
  state.patternRunIds = Array.isArray(definition.pattern_ids) ? new Set(definition.pattern_ids) : null;
  state.importRunIds = Array.isArray(definition.import_ids) ? new Set(definition.import_ids) : null;
}

function queueErrorNotice() {
  const notice = el("div", "queue-error-notice"); notice.setAttribute("role", "alert");
  notice.append(el("p", null, "Could not load runs. " + (state.queue ? "Showing the last loaded results; updated filters have not loaded. " : "") + state.queueError),
    workspaceButton("Retry loading runs", () => loadInbox().then(render).catch(showInboxError)));
  return notice;
}

// A background import/AI refresh can supersede a navigation's inbox request.
// Paint its winning result here without destroying the search or saved-view draft.
function paintWorklistQueue() {
  const table = $("#runs-table-host");
  if (!table) { render(); return; }
  for (const chip of document.querySelectorAll(".runs-controls-row .fchip[data-filter]")) {
    const key = chip.dataset.filter, count = (state.queue.filter_counts || {})[key];
    chip.classList.toggle("on", state.filters.has(key)); chip.setAttribute("aria-pressed", String(state.filters.has(key)));
    chip.classList.toggle("is-empty", count === 0);
    let badge = chip.querySelector(".fchip-count");
    if (count) { if (!badge) { badge = el("span", "fchip-count"); chip.append(badge); } badge.textContent = String(count); }
    else if (badge) badge.remove();
  }
  const stat = $(".runs-stat-line"); if (stat) stat.replaceWith(renderRunsStatLine());
  renderRunsTable(table);
}
