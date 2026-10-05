"use strict";

const ENGINEER_VIEWS = ["welcome", "home", "sources", "add-runs", "import", "investigations", "settings"];
function isEngineerView(view) { return ENGINEER_VIEWS.includes(view); }
function engineerViewTitle(view) { return ({ welcome: "Welcome", home: "Home", sources: "Sources", "add-runs": "Add runs", import: "Import", investigations: "Investigations", settings: "Settings" })[view]; }
function activeProject() { return state.projects.find(p => p.id === state.projectId); }
function projectPath(suffix) { return "/projects/" + encodeURIComponent(state.projectId) + suffix; }
function workspaceButton(text, callback, primary = false) {
  const button = el("button", "button" + (primary ? " primary" : ""), text);
  button.type = "button";
  button.addEventListener("click", () => Promise.resolve(callback()).catch(e => { state.workspace.error = e.message; render(); }));
  return button;
}
function workspaceHeading(host, title, subtitle) {
  const heading = el("div", "engineer-heading");
  heading.append(el("p", "eyebrow", activeProject() && activeProject().sample ? "Sample data · read-only" : "Local workspace"), el("h1", null, title));
  if (subtitle) heading.append(el("p", "engineer-subtitle", subtitle));
  host.append(heading);
}
function workspaceField(host, label, value, onchange, type = "text") {
  const wrapper = el("label", "engineer-field"); wrapper.append(el("span", null, label));
  const input = el(type === "textarea" ? "textarea" : "input");
  if (type !== "textarea") input.type = type;
  input.value = value || "";
  input.addEventListener("input", () => onchange(input.value));
  wrapper.append(input); host.append(wrapper); return input;
}
function workspaceError(host) {
  if (!state.workspace.error) return;
  const error = el("p", "engineer-error", state.workspace.error); error.setAttribute("role", "alert"); host.append(error);
}
async function workspaceAction(action) {
  if (state.workspace.busy) return;
  state.workspace.busy = true; state.workspace.error = null; render();
  try { await action(); } catch (e) { state.workspace.error = e.message; }
  finally { state.workspace.busy = false; render(); }
}
async function initializeWorkspace(want) {
  const [settings, catalog] = await Promise.all([api("/workspace"), api("/sources/catalog")]);
  state.projects = settings.projects;
  state.reviewer = settings.reviewer;
  state.workspace.catalog = catalog.sources;
  state.workspace.limits = catalog.limits;
  let selected = want.project || (want.run && !want.project ? "existing" : settings.last_project);
  if (!state.projects.some(p => p.id === selected)) { selected = "existing"; toast("That project is unavailable. Opening the existing workspace."); }
  state.projectId = selected;
  state.readOnly = state.serverReadOnly || !!activeProject().sample || !!activeProject().archived;
  syncProjectChrome();
}
function syncProjectChrome() {
  $("#crumb-sweep").textContent = (activeProject() || {}).name || "Workspace";
  const chooser = $("#project-switcher"); chooser.textContent = "";
  state.projects.filter(p => !p.archived || p.id === state.projectId).forEach(p => {
    const option = el("option", null, p.name + (p.sample ? " · sample" : p.archived ? " · archived" : "")); option.value = p.id; chooser.append(option);
  });
  if (!state.serverReadOnly) { const option = el("option", null, "+ New project"); option.value = "new"; chooser.append(option); }
  chooser.value = state.projectId;
  chooser.disabled = state.workspace.busy || !!state.workspace.unavailable;
  $("#add-runs-button").disabled = state.serverReadOnly || state.workspace.busy || !!state.workspace.unavailable;
  $("#add-runs-button").textContent = activeProject() && activeProject().sample ? "Analyze my runs" : "Add runs";
  $("#reviewer-avatar").textContent = state.reviewer.split(/\s+/).slice(0, 2).map(x => x[0]).join("").toUpperCase();
  $("#reviewer-avatar").title = state.reviewer + " · local display attribution";
  for (const [id, view] of [["home", "home"], ["sources", "sources"], ["investigations", "investigations"], ["settings", "settings"]]) $("#" + id + "-button").classList.toggle("current", state.view === view);
  for (const id of ["home", "sources", "investigations", "settings", "runs", "fleet", "versions"]) $("#" + id + "-button").disabled = state.workspace.busy;
  if (state.workspace.unavailable) for (const id of ["home", "sources", "investigations", "settings"]) $("#" + id + "-button").disabled = true;
}
async function switchProject(projectId, navigate = true) {
  const project = state.projects.find(p => p.id === projectId);
  if (!project) throw new Error("This project is unavailable.");
  state.workspace.token++; state.loadToken++; state.queueLoadToken++;
  state.projectId = projectId; state.readOnly = state.serverReadOnly || !!project.sample || !!project.archived;
  state.view = "home";
  state.runId = null; state.captureId = null; state.importRunIds = null; state.review = null; state.forensic = null; state.sibling = null; state.compare = null;
  state.filters = new Set(); state.sort = "triage"; state.runsSearch = ""; state.runsShown = 0;
  state.versions = { configurations: null, baseline: null, candidate: null, axis: "evaluation_harness", keys: ["task_id", "task_version", "verifier_version", "environment_image_digest", "task_parameters", "seed"], result: null, pending: false, savedId: null };
  state.fleet.loadToken++;
  state.fleet = { groupBy: "tool,error_signature", episodes: null, pending: false, error: null, loadToken: 0, usageSummary: null, executionQuality: null, total: null, hasMore: false, loadingMore: false, argumentShapes: null };
  Object.assign(state.workspace, { detail: null, files: [], preview: null, job: null, error: null, selected: new Set(), busy: false, connectionId: null, connectionForm: null, reviewerDraft: null, projectDraft: null });
  closeTrace();
  if (!state.serverReadOnly) await apiPatch("/workspace", { last_project: projectId });
  await loadInbox();
  if (navigate) await goEngineer("home");
}
async function loadEngineerData() {
  if (state.workspace.unavailable) return;
  const projectId = state.projectId, token = ++state.workspace.token;
  const prefix = "/projects/" + encodeURIComponent(projectId);
  const [detail, connections, investigations] = await Promise.all([api(prefix), api(prefix + "/connections"), api(prefix + "/investigations")]);
  if (projectId !== state.projectId || token !== state.workspace.token) return;
  state.workspace.detail = detail; state.workspace.connections = connections.connections; state.workspace.investigations = investigations.investigations;
}
async function goEngineer(view) {
  state.captureId = null;
  state.readOnly = state.serverReadOnly || !!(activeProject() || {}).sample || !!(activeProject() || {}).archived;
  state.view = view; state.workspace.error = null; closeTrace(); render();
  await loadEngineerData();
  if (state.view === view) { render(); requestAnimationFrame(() => { const heading = $("#main h1"); if (heading) { heading.tabIndex = -1; heading.focus(); } }); }
}
async function startAddingRuns() {
  if (state.serverReadOnly) { toast("This public sample is read-only. Run AGR locally to analyze your own data."); return; }
  if (state.readOnly) return goEngineer("welcome");
  Object.assign(state.workspace, { source: null, files: [], preview: null, job: null, selected: new Set(), error: null });
  await goEngineer("add-runs");
}

function renderEngineerSurface(host) {
  const wrap = el("section", "engineer-surface"); host.append(wrap);
  if (state.view === "welcome") renderWelcome(wrap);
  else if (state.view === "home") renderProjectHome(wrap);
  else if (state.view === "sources") renderSources(wrap);
  else if (state.view === "add-runs") renderAddRuns(wrap);
  else if (state.view === "import") renderImportJob(wrap);
  else if (state.view === "investigations") renderInvestigations(wrap);
  else if (state.view === "settings") renderWorkspaceSettings(wrap);
  workspaceError(wrap);
  if (state.workspace.busy) {
    const status = el("p", "engineer-status", state.workspace.uploadProgress || "Working…"); status.setAttribute("role", "status"); wrap.append(status);
    wrap.querySelectorAll("button, input, select, textarea").forEach(n => n.disabled = true);
  }
}
function renderWelcome(host) {
  workspaceHeading(host, "Understand what your agent did", "Bring in a run from your tools, inspect the evidence, and decide what to change next.");
  const layout = el("div", "engineer-cards");
  const own = el("form", "engineer-card"); own.append(el("h2", null, "Analyze my runs"), el("p", null, "Create a local project. Your runs stay on this computer; no account is required."));
  workspaceField(own, "Project name", state.workspace.newProjectName || "", v => state.workspace.newProjectName = v).required = true;
  const create = el("button", "button primary", "Create project"); create.type = "submit"; own.append(create);
  own.addEventListener("submit", e => { e.preventDefault(); workspaceAction(async () => {
    const project = await apiPost("/projects", { name: state.workspace.newProjectName }); state.projects.push(project);
    await switchProject(project.id, false); state.view = "add-runs"; state.workspace.source = null;
  }); });
  if (state.serverReadOnly) { own.textContent = "Run agr serve locally to analyze your own captures. This public workspace is a read-only sample."; }
  const sample = el("div", "engineer-card"); sample.append(el("h2", null, "Explore an example"), el("p", null, "See reviewed runs and their evidence before bringing in your own data."));
  sample.append(workspaceButton("Explore sample data", () => workspaceAction(async () => {
    if (state.serverReadOnly) { state.view = "runs"; return; }
    const project = await apiPost("/projects/sample"); if (!state.projects.some(p => p.id === project.id)) state.projects.push(project);
    await switchProject(project.id); state.view = "runs";
  })));
  layout.append(own, sample); host.append(layout);
  host.append(el("p", "engineer-hint", "Import → inspect a finding → record a next action. Evaluation results are optional; missing checks never become a pass."));
}
function renderProjectHome(host) {
  const project = activeProject(), detail = state.workspace.detail;
  workspaceHeading(host, project.name, project.sample ? "Explore sample captures. Start a personal project to analyze your own runs." : "Continue your investigations or bring in your next batch of runs.");
  const actions = el("div", "engineer-actions"); actions.append(workspaceButton(project.sample ? "Analyze my runs" : "Add runs", startAddingRuns, true));
  if (!state.serverReadOnly) actions.append(workspaceButton("New project", () => goEngineer("welcome")));
  host.append(actions);
  if (!detail) { host.append(el("p", null, "Loading project…")); return; }
  if (!detail.runs.length) { host.append(emptyState("list-tree", "No runs yet. Add a run from your tools to start investigating.", workspaceButton("Choose a source", startAddingRuns))); return; }
  const stats = el("div", "engineer-stat-row");
  const counts = { Runs: detail.runs.length, Failed: detail.runs.filter(r => r.outcome.status === "FAILED").length, Unverified: detail.runs.filter(r => r.outcome.status === "UNVERIFIED").length, Undetermined: detail.runs.filter(r => r.outcome.status === "UNDETERMINED").length };
  for (const [label, value] of Object.entries(counts)) { const card = el("div", "engineer-stat"); card.append(el("strong", null, String(value)), el("span", null, label)); stats.append(card); } host.append(stats);
  let last; try { last = localStorage.getItem("agr-last-" + state.projectId); } catch (_) {}
  if (last) {
    const p = new URLSearchParams(last); const runId = p.get("run");
    if (detail.runs.some(r => r.run_id === runId)) host.append(workspaceButton("Continue investigation", () => selectRun(runId, { chapter: p.get("chapter"), moment: p.get("moment"), trace: p.get("trace") === "1", evidence: p.get("evidence") }), true));
  }
  host.append(workspaceButton("Open runs", () => goToRuns()));
  host.append(el("h2", "engineer-section-title", "Recent imports"));
  if (!detail.imports.length) host.append(el("p", "engineer-hint", "Existing captures are available in Runs. New browser imports will appear here."));
  for (const job of detail.imports) host.append(importHistoryRow(job));
  host.append(el("h2", "engineer-section-title", "Next actions"));
  if (!detail.investigations.length) host.append(el("p", "engineer-hint", "Save an evidence-linked next action while reviewing a run."));
  for (const investigation of detail.investigations) host.append(investigationRow(investigation));
}
function importHistoryRow(job) {
  const row = el("div", "engineer-list-row"); row.append(el("strong", null, job.label), el("span", "engineer-hint", job.status.replaceAll("_", " ") + " · " + job.results.length + "/" + job.total));
  row.append(workspaceButton("View import", async () => { state.workspace.job = job; await goEngineer("import"); pollImport(); })); return row;
}
function renderSources(host) {
  workspaceHeading(host, "Bring runs in from your tools", "File imports for all supported sources, plus single-trace retrieval from Langfuse v4. Sources do not sync automatically.");
  const cards = el("div", "source-grid");
  for (const source of state.workspace.catalog || []) {
    const card = el("div", "engineer-card source-card"); card.append(el("h2", null, source.name), el("p", null, source.help), el("span", "engineer-hint", source.formats));
    const button = workspaceButton("Add " + source.name + " runs", async () => { await startAddingRuns(); if (state.view === "add-runs") { state.workspace.source = source.id; render(); } });
    button.disabled = state.serverReadOnly; card.append(button); cards.append(card);
  } host.append(cards);
  host.append(el("h2", "engineer-section-title", "Langfuse connections"));
  if (!state.workspace.connections.length) host.append(el("p", "engineer-hint", "No connections configured. Add Langfuse runs to configure a session-only connection."));
  for (const connection of state.workspace.connections) {
    const row = el("div", "engineer-list-row"); row.append(el("strong", null, connection.name), el("span", "engineer-hint", connection.status.replaceAll("_", " ") + (connection.last_test_at ? " · " + connection.last_test_at : "")));
    if (!state.readOnly) row.append(workspaceButton("Disconnect", () => workspaceAction(async () => { await apiDelete(projectPath("/connections/" + connection.id)); await loadEngineerData(); })));
    host.append(row);
  }
  host.append(el("h2", "engineer-section-title", "Import history"));
  for (const job of (state.workspace.detail || {}).imports || []) host.append(importHistoryRow(job));
}
function renderAddRuns(host) {
  const ws = state.workspace;
  workspaceHeading(host, "Add runs to " + activeProject().name, "Choose a source, preview what was captured, then import. Analysis runs locally without model calls.");
  if (state.readOnly) { host.append(workspaceButton("Create a personal project", () => goEngineer("welcome"), true)); return; }
  const steps = el("p", "engineer-steps", ws.preview ? "1. Source  →  2. Select  →  3. Preview and import" : ws.source ? "1. Source  →  2. Select captures" : "1. Choose source"); host.append(steps);
  if (!ws.source) {
    const grid = el("div", "source-grid");
    for (const source of ws.catalog || []) {
      const button = workspaceButton(source.name, () => { ws.source = source.id; ws.files = []; ws.preview = null; ws.options = {}; render(); });
      button.classList.add("source-choice"); button.append(el("span", "engineer-hint", source.help)); grid.append(button);
    } host.append(grid); return;
  }
  const source = ws.catalog.find(s => s.id === ws.source);
  host.append(el("h2", null, source.name), el("p", "engineer-subtitle", source.help));
  host.append(workspaceButton("Change source", () => { ws.source = null; ws.files = []; ws.preview = null; ws.error = null; ws.options = {}; render(); }));
  if (ws.preview) { renderImportPreview(host); return; }
  if (source.id === "langfuse") renderLangfuseFetch(host);
  const transfer = el("div", "upload-zone"); transfer.append(el("h3", null, "Import files"), el("p", null, "Drop files here, or choose them below. Nothing is imported until you confirm the preview."));
  function picker(label, directory = false) {
    const wrapper = el("label", "button"); wrapper.append(document.createTextNode(label));
    const input = el("input", "upload-picker"); input.type = "file"; input.multiple = true;
    input.setAttribute("aria-label", label);
    if (directory) { input.setAttribute("webkitdirectory", ""); input.setAttribute("directory", ""); }
    else input.accept = ".json,.jsonl,.zip";
    input.addEventListener("change", () => receiveFiles(input.files)); wrapper.append(input); transfer.append(wrapper);
  }
  picker("Choose files"); if (source.methods.includes("folder")) picker("Choose a folder", true);
  transfer.addEventListener("dragover", e => { e.preventDefault(); transfer.classList.add("dragging"); });
  transfer.addEventListener("dragleave", () => transfer.classList.remove("dragging"));
  transfer.addEventListener("drop", e => { e.preventDefault(); transfer.classList.remove("dragging"); receiveFiles(e.dataTransfer.files); }); host.append(transfer);
  if (source.methods.includes("folder")) host.append(el("p", "engineer-hint", "Folder selection transfers only the files you select below. If your browser cannot choose folders, upload a ZIP for Harbor or select session files individually."));
  if (ws.files.length) {
    host.append(el("h3", null, "Selected files (" + ws.files.length + ")"));
    const list = el("div", "selected-file-list");
    ws.files.forEach((entry, index) => {
      const label = el("label", "file-selection"); const checkbox = el("input"); checkbox.type = "checkbox"; checkbox.checked = entry.selected;
      checkbox.addEventListener("change", () => entry.selected = checkbox.checked);
      label.append(checkbox, el("span", null, entry.name), el("span", "engineer-hint", (entry.file.size / 1024).toFixed(1) + " KiB")); list.append(label);
    }); host.append(list);
    const advanced = el("details", "engineer-advanced"); advanced.append(el("summary", null, "Optional task and evaluation details"));
    ws.options = ws.options || {};
    workspaceField(advanced, "Task identity override", ws.options.task_id, v => ws.options.task_id = v);
    workspaceField(advanced, "Configuration identity", ws.options.configuration_id, v => ws.options.configuration_id = v);
    workspaceField(advanced, "Task instruction", ws.options.instruction, v => ws.options.instruction = v, "textarea");
    workspaceField(advanced, "Evaluation sidecar filename (one capture only)", ws.options.verifier_file, v => ws.options.verifier_file = v);
    host.append(advanced);
    host.append(workspaceButton("Preview selected runs", () => workspaceAction(uploadAndPreview), true));
  }
}
function receiveFiles(fileList) {
  if (state.workspace.busy) return;
  state.workspace.files = Array.from(fileList).map(file => ({ file, name: file.webkitRelativePath || file.name, selected: true }));
  state.workspace.error = null; state.workspace.preview = null; render();
}
async function uploadAndPreview() {
  const ws = state.workspace, files = ws.files.filter(f => f.selected), projectId = state.projectId;
  if (!files.length) throw new Error("Select at least one file.");
  const prefix = "/projects/" + encodeURIComponent(projectId);
  const preview = await apiPost(prefix + "/import-previews", { source: ws.source, options: ws.options || {} });
  let uploaded = 0, total = files.reduce((n, f) => n + f.file.size, 0);
  for (const entry of files) {
    ws.uploadProgress = "Transferring " + entry.name + " · " + (total ? Math.round(uploaded / total * 100) : 0) + "%"; render();
    const response = await fetch(prefix + "/import-previews/" + preview.id + "/files?name=" + encodeURIComponent(entry.name), { method: "PUT", headers: sessionHeaders({ "content-type": "application/octet-stream" }), body: entry.file });
    if (!response.ok) { const error = await response.json(); throw new Error(error.detail && error.detail.message || "File transfer failed. Try a smaller batch."); }
    uploaded += entry.file.size;
  }
  ws.uploadProgress = "Reading captures and checking available evidence…"; render();
  const result = await apiPost(prefix + "/import-previews/" + preview.id + "/inspect");
  if (projectId !== state.projectId) return;
  ws.preview = result; ws.files = []; ws.selected = new Set(result.items.filter(i => i.status !== "invalid").map(i => i.id)); ws.uploadProgress = null;
}
function renderLangfuseFetch(host) {
  const ws = state.workspace;
  ws.connectionForm = ws.connectionForm || { name: "Langfuse", host: "", public_key: "", secret_key: "", trace_id: "", from_time: new Date(Date.now() - 7 * 86400000).toISOString().slice(0, 16), to_time: new Date().toISOString().slice(0, 16) };
  const values = ws.connectionForm;
  const form = el("form", "engineer-card"); form.append(el("h3", null, "Fetch one trace · Langfuse v4"), el("p", "engineer-hint", "Retrieves the trace for preview; it does not import or invoke a model. Keys stay in this server session and are not saved to disk. Older instances can use JSON file import."));
  if (ws.connections.length) {
    const label = el("label", "engineer-field"); label.append(el("span", null, "Connection")); const select = el("select");
    const fresh = el("option", null, "Configure a new connection"); fresh.value = ""; select.append(fresh);
    for (const c of ws.connections) { const option = el("option", null, c.name + " · session-only credentials"); option.value = c.id; select.append(option); }
    select.value = ws.connectionId || ""; select.addEventListener("change", () => { ws.connectionId = select.value; render(); }); label.append(select); form.append(label);
  }
  if (!ws.connectionId) {
    for (const [key, label, type] of [["name", "Connection name", "text"], ["host", "Langfuse host (for example, https://cloud.langfuse.com)", "url"], ["public_key", "Public key (optional for the server's configured Langfuse host)", "text"], ["secret_key", "Secret key (optional for the server's configured Langfuse host)", "password"]]) workspaceField(form, label, values[key], v => values[key] = v, type).required = key === "host";
  }
  workspaceField(form, "Trace ID", values.trace_id, v => values.trace_id = v).required = true;
  // These fields explicitly use UTC so their meaning is stable across devices.
  workspaceField(form, "From (UTC)", values.from_time, v => values.from_time = v, "datetime-local").required = true;
  workspaceField(form, "To (UTC)", values.to_time, v => values.to_time = v, "datetime-local").required = true;
  const submit = el("button", "button primary", "Test connection and preview trace"); submit.type = "submit"; form.append(submit);
  form.addEventListener("submit", e => { e.preventDefault(); workspaceAction(async () => {
    if (!ws.connectionId) { const connection = await apiPost(projectPath("/connections"), values); ws.connectionId = connection.id; values.secret_key = ""; values.public_key = ""; await loadEngineerData(); }
    ws.preview = await apiPost(projectPath("/connections/" + ws.connectionId + "/trace-previews"), { trace_id: values.trace_id, from_time: new Date(values.from_time + "Z").toISOString(), to_time: new Date(values.to_time + "Z").toISOString() });
    ws.selected = new Set(ws.preview.items.filter(i => i.status !== "invalid").map(i => i.id));
  }); }); host.append(form);
}
function renderImportPreview(host) {
  const ws = state.workspace, preview = ws.preview;
  host.append(el("h3", null, "Preview · " + preview.items.length + " logical runs"));
  host.append(el("p", "engineer-hint", "Import success, task outcome, and captured evidence are separate. Missing evaluation results never mean the task passed."));
  for (const item of preview.items) {
    const card = el("div", "preview-item"); const title = el("label", "preview-title"); const check = el("input"); check.type = "checkbox"; check.disabled = item.status === "invalid"; check.checked = ws.selected.has(item.id); check.setAttribute("aria-label", "Select " + (item.title || item.file));
    check.addEventListener("change", () => check.checked ? ws.selected.add(item.id) : ws.selected.delete(item.id));
    title.append(check, el("strong", null, item.title || item.file)); card.append(title, el("p", "engineer-hint", item.file + " · " + item.status.replaceAll("_", " ") + (item.steps ? " · " + item.steps + " captured steps" : "")));
    if (item.status === "invalid") card.append(el("p", "engineer-error", item.error));
    else {
      card.append(el("p", null, item.evaluation_available ? "Evaluation results captured; outcome is determined by the recorded checks." : "Task outcome unverified; available behavior can still be reviewed."));
      const coverage = el("div", "coverage-chips");
      for (const [key, label] of [["messages", "Messages"], ["tool_calls", "Tool calls"], ["tool_results", "Tool results"], ["generation_usage", "Usage"], ["generation_timestamps", "Timing"]]) coverage.append(el("span", "coverage-chip", label + ": " + ((item.coverage || {})[key] || "unavailable")));
      card.append(coverage);
    }
    if (item.warnings.length) { const warnings = el("details"); warnings.append(el("summary", null, item.warnings.length + " capture notes")); const list = el("ul"); for (const warning of item.warnings) list.append(el("li", null, warning)); warnings.append(list); card.append(warnings); }
    host.append(card);
  }
  if (preview.ignored.length) { const ignored = el("details", "engineer-advanced"); ignored.append(el("summary", null, preview.ignored.length + " auxiliary or unsupported files")); preview.ignored.forEach(i => ignored.append(el("p", "engineer-hint", i.file + " · " + i.reason))); host.append(ignored); }
  workspaceField(host, "Import batch label", ws.batchLabel || "Imported runs", v => ws.batchLabel = v);
  const actions = el("div", "engineer-actions");
  actions.append(workspaceButton("Choose different files", () => { ws.preview = null; ws.error = null; render(); }));
  const submit = workspaceButton("Import selected runs", () => workspaceAction(async () => {
    if (!ws.selected.size) throw new Error("Select at least one valid run.");
    const requestSignature = JSON.stringify([preview.id, preview.manifest_hash, [...ws.selected]]);
    if (ws.requestSignature !== requestSignature) { ws.requestKey = uid(); ws.requestSignature = requestSignature; }
    ws.requestKey = ws.requestKey || uid();
    ws.job = await apiPost(projectPath("/imports"), { preview_id: preview.id, manifest_hash: preview.manifest_hash, selected: [...ws.selected], label: ws.batchLabel || "Imported runs", idempotency_key: ws.requestKey });
    ws.requestKey = null; state.view = "import"; pollImport();
  }), true); submit.disabled = !preview.items.some(i => i.status !== "invalid"); actions.append(submit); host.append(actions);
}
let importPollTimer;
async function pollImport() {
  clearTimeout(importPollTimer);
  const job = state.workspace.job, projectId = state.projectId;
  if (!job) return;
  try {
    const result = await api("/projects/" + encodeURIComponent(projectId) + "/imports/" + job.id);
    if (projectId !== state.projectId || !state.workspace.job || state.workspace.job.id !== job.id) return;
    state.workspace.job = result;
    if (state.view === "import") render();
    if (["queued", "importing", "cancelling"].includes(result.status)) importPollTimer = setTimeout(pollImport, 800);
    else { await loadInbox(); await loadEngineerData(); if (state.view === "import") render(); }
  } catch (e) { if (state.projectId === projectId) { state.workspace.error = e.message; if (state.view === "import") render(); } }
}
function renderImportJob(host) {
  const job = state.workspace.job;
  workspaceHeading(host, job ? job.label : "Import", "Selected captures are analyzed locally. Completed runs remain available if another item fails.");
  if (!job) { host.append(workspaceButton("View import history", () => goEngineer("sources"))); return; }
  const active = ["queued", "importing", "cancelling"].includes(job.status);
  const status = el("p", "engineer-status", job.status.replaceAll("_", " ") + " · " + job.results.length + "/" + job.total + " selected runs processed"); status.setAttribute("role", "status"); host.append(status);
  const progress = el("progress"); progress.max = job.total; progress.value = job.results.length; progress.setAttribute("aria-label", "Selected runs processed"); host.append(progress);
  const counts = { new: 0, new_capture: 0, duplicate: 0, failed: 0 };
  for (const result of job.results) counts[result.status] = (counts[result.status] || 0) + 1;
  host.append(el("p", null, counts.new + " new runs · " + counts.new_capture + " new captures · " + counts.duplicate + " already imported · " + counts.failed + " failed"));
  const actions = el("div", "engineer-actions");
  const successes = job.results.filter(r => r.run_id);
  if (successes.length) actions.append(workspaceButton(successes.length === 1 ? "Open imported run" : "Open imported runs", async () => {
    state.filters.clear(); state.runsSearch = ""; await loadInbox();
    if (successes.length === 1) await selectRun(successes[0].run_id);
    else { state.importRunIds = new Set(successes.map(r => r.run_id)); goToRuns(); }
  }, true));
  if (active) actions.append(workspaceButton("Cancel remaining work", () => workspaceAction(async () => { state.workspace.job = await apiPost(projectPath("/imports/" + job.id + "/cancel")); pollImport(); })));
  else if (["failed", "completed_with_errors", "cancelled", "interrupted"].includes(job.status)) actions.append(workspaceButton("Retry unfinished work", () => workspaceAction(async () => { state.workspace.job = await apiPost(projectPath("/imports/" + job.id + "/retry")); pollImport(); })));
  if (!active) actions.append(workspaceButton("Import more", startAddingRuns));
  host.append(actions);
  for (const result of job.results) { const row = el("div", "engineer-list-row"); row.append(el("span", null, result.run_id || result.item_id), el("span", result.status === "failed" ? "engineer-error" : "engineer-hint", result.error || result.status.replaceAll("_", " "))); host.append(row); }
  const originals = el("details", "engineer-advanced"); originals.append(el("summary", null, "Original input files")); host.append(originals);
  originals.addEventListener("toggle", async () => {
    if (!originals.open || originals.dataset.loaded) return;
    try {
      const preview = await api(projectPath("/import-previews/" + job.preview_id));
      for (const file of preview.files) {
        const link = el("a", "engineer-original", file.name); link.href = projectPath("/imports/" + job.id + "/originals?name=" + encodeURIComponent(file.name)); link.download = file.name.split("/").pop();
        link.addEventListener("click", e => { e.preventDefault(); workspaceAction(async () => {
          const response = await fetch(link.href, { headers: sessionHeaders() });
          if (!response.ok) throw new Error("Could not download the original file. Reopen the server session link and try again.");
          const url = URL.createObjectURL(await response.blob()); const download = el("a"); download.href = url; download.download = link.download; download.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
        }); }); originals.append(link);
      } originals.dataset.loaded = "true";
    } catch (error) { originals.append(el("p", "engineer-error", error.message)); }
  });
}
function investigationRow(record) {
  const row = el("div", "engineer-list-row investigation-row"); const copy = el("div"); copy.append(el("strong", null, record.title), el("p", null, record.action), el("span", "engineer-hint", record.status.replaceAll("_", " ") + " · " + record.actor)); row.append(copy);
  row.append(workspaceButton("Open evidence", () => selectRun(record.run_id, { capture: record.capture_id, chapter: record.moment_id ? "moments" : "outcome", moment: record.moment_id })));
  if (!state.readOnly) { const status = el("select"); status.setAttribute("aria-label", "Status for " + record.title); for (const [value, label] of [["open", "Open"], ["in_progress", "In progress"], ["resolved", "Resolved"]]) { const option = el("option", null, label); option.value = value; status.append(option); } status.value = record.status;
    status.addEventListener("change", () => workspaceAction(async () => { await apiPatch(projectPath("/investigations/" + record.id), Object.assign({}, record, { status: status.value })); await loadEngineerData(); })); row.append(status); }
  return row;
}
function renderInvestigations(host) {
  workspaceHeading(host, "Investigations", "Keep evidence-linked next actions. Resolving an investigation records your decision; it does not prove that a fix worked.");
  if (!state.workspace.investigations.length) host.append(emptyState("book-open", "No saved actions yet. Open a run and choose Save next action.", workspaceButton("Open runs", () => goToRuns())));
  state.workspace.investigations.forEach(r => host.append(investigationRow(r)));
}
function renderWorkspaceSettings(host) {
  workspaceHeading(host, "Settings", "Projects and reviewer names are local. A display name is not an authenticated account.");
  const form = el("form", "engineer-card");
  workspaceField(form, "Reviewer display name", state.workspace.reviewerDraft || state.reviewer, v => state.workspace.reviewerDraft = v).required = true;
  const project = activeProject(); workspaceField(form, "Project name", state.workspace.projectDraft || project.name, v => state.workspace.projectDraft = v).required = true;
  const save = el("button", "button primary", "Save settings"); save.type = "submit"; save.disabled = state.serverReadOnly; form.append(save);
  form.addEventListener("submit", e => { e.preventDefault(); workspaceAction(async () => {
    const result = await apiPatch("/workspace", { reviewer: state.workspace.reviewerDraft || state.reviewer }); state.reviewer = result.reviewer;
    const resultProject = await apiPatch(projectPath(""), { name: state.workspace.projectDraft || project.name }); state.projects = state.projects.map(p => p.id === project.id ? resultProject : p); toast("Settings saved");
  }); }); host.append(form);
  host.append(el("p", "engineer-hint", "Connection keys last for the server session. Original input files are retained locally with imports. Hosted login, automatic sync, and external notifications are not enabled."));
  if (!state.serverReadOnly) host.append(workspaceButton(project.archived ? "Restore project" : "Archive project", () => workspaceAction(async () => { const result = await apiPatch(projectPath(""), { archived: !project.archived }); state.projects = state.projects.map(p => p.id === result.id ? result : p); state.readOnly = state.serverReadOnly || !!result.sample || result.archived; })));
  host.append(el("h2", "engineer-section-title", "Automate local imports"));
  host.append(el("p", "engineer-hint", "Run a CLI import against this project's store. The command uses the same adapters and deterministic engine as browser import. The storage path is shown below; source files stay under your control."));
  const storePath = (state.workspace.detail || {}).store_path;
  const command = el("pre", "engineer-command", "agr --store " + (storePath ? '"' + storePath + '"' : "<project-store>") + " ingest-from --adapter <source> <capture-file>"); host.append(command);
}

function renderInvestigationComposer(host) {
  if (state.loading || state.readOnly || !state.review) return;
  const disclosure = el("details", "engineer-action-composer"); disclosure.append(el("summary", null, "Save next action"));
  const form = el("form"); const runId = state.runId, projectId = state.projectId, captureId = state.review.capture.capture_id;
  const draft = { title: "", action: "" };
  form.append(el("p", "engineer-hint", "Pin this capture and the selected finding to an action you can revisit. Saving does not execute a change or approve an experiment."));
  workspaceField(form, "Investigation title", "", v => draft.title = v).required = true;
  workspaceField(form, "Next action", "", v => draft.action = v, "textarea").required = true;
  const save = el("button", "button primary", "Save next action"); save.type = "submit"; form.append(save);
  form.addEventListener("submit", async e => {
    e.preventDefault(); save.disabled = true;
    const moment = state.chapter === "moments" ? currentMoment() : null;
    try { await apiPost("/projects/" + encodeURIComponent(projectId) + "/investigations", Object.assign({}, draft, { run_id: runId, capture_id: captureId, moment_id: moment && moment.moment_id })); disclosure.open = false; toast("Next action saved"); }
    catch (error) { const message = el("p", "engineer-error", error.message); message.setAttribute("role", "alert"); form.append(message); }
    finally { save.disabled = false; }
  }); disclosure.append(form); host.append(disclosure);
}

  $("#project-switcher").addEventListener("change", e => { if (e.target.value === "new") goEngineer("welcome").catch(e => toast(e.message)); else switchProject(e.target.value).catch(e => toast(e.message)); });
$("#add-runs-button").addEventListener("click", () => startAddingRuns().catch(e => toast(e.message)));
for (const view of ["home", "sources", "investigations", "settings"]) $("#" + view + "-button").addEventListener("click", () => goEngineer(view).catch(e => toast(e.message)));
$("#reviewer-avatar").addEventListener("click", () => goEngineer("settings").catch(e => toast(e.message)));
