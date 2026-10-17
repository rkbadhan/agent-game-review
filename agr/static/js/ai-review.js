"use strict";

// Settings are user defaults; review selections and jobs belong to one project.
// Keys live only in this form until transmitted to the server session.
state.ai = { settings: null, draft: null, error: null, selected: new Set(),
  plan: null, job: null, jobs: [], force: false, token: 0, requestId: null, filter: "", pollToken: 0, watching: new Set() };

// ---- Where AI review stands: one source of truth for the sidebar badge, the Runs page and the AI review page ----
const AI_ACTIVE = ["queued", "running", "cancelling"];
const aiJobActive = job => AI_ACTIVE.includes(job.status);
const AI_STRIP_LABELS = { reviewing: "reviewing", queued: "queued", reviewed: "AI reviewed", failed: "review failed", none: "not reviewed" };
const AI_STATES = { reviewing: "Reviewing", queued: "Queued", reviewed: "AI reviewed", failed: "Review failed", none: "Not reviewed" };
function activeAIJob() { return state.ai.jobs.find(aiJobActive) || null; }
// Jobs seen working stay in state.ai.watching until a load shows them settled, so a job that finishes between two
// polls is announced by whichever loader notices it first, not missed because another loader replaced the list.
function settleAIJobs() {
  const active = new Set(state.ai.jobs.filter(aiJobActive).map(j => j.id));
  const finished = state.ai.jobs.filter(j => state.ai.watching.has(j.id) && !active.has(j.id));
  state.ai.watching = active;
  return finished;
}
async function announceAIFinished(finished) {
  if (!finished.length) return;
  toast("AI review finished · " + finished.map(aiJobSummary).join("; "));
  await loadInbox();
  if (state.view === "review") await refreshRun(); else if (state.view === "runs") render();
}
// Rebuilds a host only when what it shows changed, and gives focus back to the same control afterwards, so a poll
// never drops the keyboard focus from a button or makes a screen reader read an unchanged status again.
function aiPaint(host, signature, build) {
  if (host._aiSignature === signature) return;
  const focused = host.contains(document.activeElement) && document.activeElement !== host ? document.activeElement : null;
  const key = focused && focused.dataset.aiKey;
  host._aiSignature = signature; build();
  // Only a control with a stable key gets focus back, and always the same one: its label or its position may have changed.
  if (key) {
    const again = Array.from(host.querySelectorAll("[data-ai-key]")).find(e => e.dataset.aiKey === key);
    if (again) again.focus();
  }
}
function aiKeyed(button, key) { button.dataset.aiKey = key; return button; }
// What AI review has done for one run: the active job wins, then the newest job that touched it.
function aiRunState(runId, run) {
  for (const job of state.ai.jobs) {
    if (!(job.run_ids || []).includes(runId)) continue;
    const result = (job.results || []).find(r => r.run_id === runId);
    if (!result && aiJobActive(job)) return job.current_run === runId ? "reviewing" : "queued";
    if (result) return ["completed", "skipped"].includes(result.status) ? "reviewed" : "failed";
  }
  return run && run.review_mode === "model_enriched" ? "reviewed" : "none";
}
function aiChip(runId, run) {
  const chip = el("span"); chip.dataset.aiRun = runId; paintAIChip(chip, aiRunState(runId, run), run); return chip;
}
function paintAIChip(chip, status, run) {
  chip.className = "ai-chip ai-" + status;
  chip.textContent = "";
  if (status === "reviewing") chip.append(el("span", "ai-dot"));
  chip.append(document.createTextNode(AI_STATES[status]));
  chip.title = status === "reviewed" ? "Reviewed by the configured model" : status === "failed" ? "The last AI review of this run did not finish" : AI_STATES[status];
}
function aiCounts(runs) {
  const counts = { reviewing: 0, queued: 0, reviewed: 0, failed: 0, none: 0 };
  for (const run of runs) counts[aiRunState(run.run_id, run)]++;
  return counts;
}
function aiProgressBar(job) {
  const bar = el("progress", "ai-progress"); bar.max = job.total || 1; bar.value = (job.results || []).length;
  bar.setAttribute("aria-label", "AI review progress"); bar.title = (job.results || []).length + " of " + job.total + " runs processed"; return bar;
}
function renderAIBadge() {
  const badge = $("#ai-review-badge"), button = $("#ai-review-button");
  if (!badge || !button) return;
  button.hidden = aiReadOnly() || !!state.workspace.unavailable;
  const active = activeAIJob(), failed = state.ai.jobs.some(j => !aiJobActive(j) && ["failed", "completed_with_errors", "interrupted"].includes(j.status));
  badge.hidden = !active && !failed;
  badge.className = "sidebar-badge" + (active ? " running" : " attention");
  badge.textContent = active ? (active.results || []).length + "/" + active.total : "!";
  button.setAttribute("aria-label", "AI review" + (active ? ", running, " + (active.results || []).length + " of " + active.total + " runs done" : failed ? ", some reviews need attention" : ""));
  button.title = active ? "AI review is running: " + (active.results || []).length + " of " + active.total + " runs" : "Track AI reviews";
}
// In-place updates for everything that shows AI state, so polling never rebuilds a page the reader is using.
function refreshAIViews() {
  renderAIBadge();
  document.querySelectorAll("[data-ai-run]").forEach(chip => paintAIChip(chip, aiRunState(chip.dataset.aiRun, ((state.queue && state.queue.runs) || []).find(r => r.run_id === chip.dataset.aiRun)), null));
  document.querySelectorAll(".ai-strip-host").forEach(renderAIStrip);
  const activity = $("#ai-activity"); if (activity) renderAIActivity(activity);
  const job = $("#ai-job"); if (job && state.ai.job) renderAIJob(job, state.ai.job);
}
function aiWhen(iso) { const d = iso ? new Date(iso) : null; return d && !isNaN(d) ? d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : ""; }
function aiJobSummary(job) {
  const results = job.results || [], reviewed = results.filter(r => ["completed", "skipped"].includes(r.status)).length, failed = results.filter(r => ["failed", "incomplete"].includes(r.status)).length;
  const waiting = aiJobActive(job) ? Math.max(0, job.total - results.length) : 0;
  return [reviewed + " reviewed", failed && failed + " failed", waiting && waiting + " waiting"].filter(Boolean).join(" · ");
}

async function loadAIReviewData() {
  const project = state.projectId, token = ++state.ai.token;
  try {
    const [settingsResult, jobsResult] = await Promise.allSettled([
      api("/workspace/ai-review"), api(projectPath("/ai-reviews"))
    ]);
    if (project !== state.projectId || token !== state.ai.token) return;
    const settings = settingsResult.status === "fulfilled" ? settingsResult.value : null;
    state.ai.settings = settings;
    state.ai.error = settingsResult.status === "rejected" ? settingsResult.reason.message
      : jobsResult.status === "rejected" ? jobsResult.reason.message : null;
    if (jobsResult.status === "fulfilled") state.ai.jobs = sortAIJobs(jobsResult.value.jobs);
    if (settings && !state.ai.draft && !settings.read_only) {
      state.ai.draft = Object.fromEntries(["provider", "model", "base_url", "cost_budget_usd", "time_budget_s", "request_timeout_s"].map(k => [k, settings[k]]));
    }
    const jobId = new URLSearchParams(location.search).get("review_job");
    if (jobId && !state.ai.job) state.ai.job = state.ai.jobs.find(j => j.id === jobId) || null;
    const finished = settleAIJobs();
    refreshAIViews();
    if (state.ai.jobs.some(aiJobActive)) pollAIReview();
    announceAIFinished(finished);
  } catch (error) {
    if (project === state.projectId && token === state.ai.token) state.ai.error = error.message;
  }
}
function sortAIJobs(jobs) { return jobs.slice().sort((a, b) => (b.created_at || "").localeCompare(a.created_at || "")); }
// Load effective settings too: the Runs CTA must know whether a model is selected.
async function loadAIJobs() {
  if (aiReadOnly() || (state.workspace && state.workspace.unavailable) || !state.projectId) return;
  const project = state.projectId, token = ++state.ai.pollToken;
  try {
    // A malformed configuration must not hide jobs already accepted with
    // their own saved settings, or stop their badge/chips/polling updates.
    const [jobsResult, settingsResult] = await Promise.allSettled([
      api("/projects/" + encodeURIComponent(project) + "/ai-reviews"), api("/workspace/ai-review")
    ]);
    if (project !== state.projectId || token !== state.ai.pollToken) return;
    state.ai.settings = settingsResult.status === "fulfilled" ? settingsResult.value : null;
    if (jobsResult.status === "fulfilled") state.ai.jobs = sortAIJobs(jobsResult.value.jobs);
    if (state.ai.job) state.ai.job = state.ai.jobs.find(j => j.id === state.ai.job.id) || state.ai.job;
    const finished = settleAIJobs();
    refreshAIViews();
    if (state.ai.jobs.some(aiJobActive)) pollAIReview();
    announceAIFinished(finished);
  } catch (_) { /* the badge and chips simply keep what they had */ }
}
// Settings are shared defaults and reviews only add snapshots, so every project
// of a writable local server can configure and run AI review.
async function configureAIReview() {
  await goEngineer("settings");
  // goEngineer focuses the page heading on the next frame; focus the model
  // after that navigation has settled, provided the reader is still here.
  await new Promise(requestAnimationFrame);
  const model = state.view === "settings" && $("#ai-review-model");
  if (model) { model.scrollIntoView({ block: "center" }); model.focus(); }
}
function aiReadOnly() {
  return state.serverReadOnly;
}
function aiError(host) {
  if (!state.ai.error) return;
  const message = el("p", "engineer-error", state.ai.error);
  message.setAttribute("role", "alert"); host.append(message);
}
async function aiAction(action) {
  state.ai.error = null;
  await workspaceAction(async () => {
    try { await action(); } catch (error) { state.ai.error = error.message; }
  });
}
function renderAISettings(host) {
  const ai = state.ai, settings = ai.settings;
  const card = el("section", "engineer-card ai-settings");
  card.append(el("h2", null, "AI review"), el("p", "engineer-hint",
    "Default for local reviews, shared with the CLI. Saving makes no model calls. Imports continue to use local deterministic analysis."));
  if (aiReadOnly()) {
    card.append(el("p", null, "AI configuration and paid reviews are unavailable on a read-only server. Run agr serve locally to use them."));
    host.append(card); return;
  }
  aiError(card);
  if (!settings || !ai.draft) {
    card.append(el("p", null, "AI settings could not be loaded."));
    card.append(workspaceButton("Reload AI settings", async () => { await loadAIReviewData(); render(); }));
    card.append(workspaceButton("Reset saved AI settings", () => aiAction(async () => {
      ai.settings = await apiDelete("/workspace/ai-review"); ai.draft = null; await loadAIReviewData();
    })));
    host.append(card); return;
  }
  const draft = ai.draft;
  const form = el("form");
  const providerLabel = el("label", "engineer-field"); providerLabel.append(el("span", null, "API provider"));
  const provider = el("select"); provider.setAttribute("aria-label", "API provider");
  for (const [value, label] of [["anthropic", "Anthropic"], ["openai", "OpenAI / compatible endpoint"]]) {
    const option = el("option", null, label); option.value = value; provider.append(option);
  }
  provider.value = draft.provider;
  provider.addEventListener("change", () => {
    draft.provider = provider.value;
    // A model ID belongs to one provider; the user chooses the new one.
    draft.model = ""; draft.base_url = settings.defaults[draft.provider].base_url;
    draft.api_key = ""; ai.plan = null; ai.testResult = null; ai.models = null; render();
  });
  providerLabel.append(provider); form.append(providerLabel);
  const endpoint = workspaceField(form, "Endpoint URL (change for a custom or local server)", draft.base_url, v => { draft.base_url = v; ai.plan = null; ai.models = null; }, "url");
  endpoint.required = true;
  workspaceField(form, "API key (optional; server session only)", draft.api_key || "", v => draft.api_key = v, "password").autocomplete = "off";
  const model = workspaceField(form, "Reviewer model ID", draft.model, v => { draft.model = v; ai.plan = null; });
  model.id = "ai-review-model";
  model.required = true; model.placeholder = "Load models or enter a model ID";
  const pick = el("div", "engineer-actions");
  pick.append(workspaceButton("Load models", () => aiAction(async () => {
    if (!endpoint.reportValidity()) return;
    // The draft model keeps a saved session key for this configuration usable.
    const result = await apiPost("/workspace/ai-review/models", draft);
    ai.models = result.models;
    if (!ai.models.length) throw new Error("The endpoint reported no models. Enter the model ID instead.");
  })));
  if (ai.models && ai.models.length) {
    const choose = el("select"); choose.setAttribute("aria-label", "Available models");
    const prompt = el("option", null, "Choose from " + ai.models.length + " available models"); prompt.value = ""; choose.append(prompt);
    for (const id of ai.models) { const option = el("option", null, id); option.value = id; choose.append(option); }
    choose.value = ai.models.includes(draft.model) ? draft.model : "";
    choose.addEventListener("change", () => { if (choose.value) { draft.model = choose.value; ai.plan = null; render(); } });
    pick.append(choose);
  }
  form.append(pick);
  form.append(el("p", "engineer-hint", "Credentials: " + settings.credentials + ". For a local server that ignores authentication, use a non-empty placeholder key. Session keys disappear when the server restarts."));
  const advanced = el("details", "engineer-advanced");
  advanced.append(el("summary", null, "Advanced review limits"));
  for (const [key, label, minimum] of [
    ["cost_budget_usd", "Estimated cost target per run (USD; 0 disables)", 0],
    ["time_budget_s", "Elapsed-time target per run (seconds; 0 disables)", 0],
    ["request_timeout_s", "Timeout per provider request (seconds)", 0.1]
  ]) {
    const field = workspaceField(advanced, label, draft[key], v => draft[key] = v, "number");
    field.min = minimum; field.step = "any"; field.required = true;
    if (draft[key] === 0) field.value = "0";
  }
  advanced.append(el("p", "engineer-hint", "Targets are checked between calls. An in-flight call can exceed a target; actual billing is unavailable. Each provider request has a separate timeout."));
  form.append(advanced);
  const actions = el("div", "engineer-actions");
  const save = el("button", "button primary", "Save AI settings"); save.type = "submit"; actions.append(save);
  actions.append(workspaceButton("Test connection · uses tokens", () => aiAction(async () => {
    if (!form.reportValidity()) return;
    const result = await apiPost("/workspace/ai-review/test", draft);
    if (result.status === "verified") draft.api_key = "";
    ai.testResult = result;
    await loadAIReviewData();
  })));
  if (settings.credentials === "session") actions.append(workspaceButton("Forget session keys", () => aiAction(async () => {
    ai.settings = await apiDelete("/workspace/ai-review/credentials"); ai.testResult = null;
  })));
  form.append(actions);
  form.addEventListener("submit", event => {
    event.preventDefault(); aiAction(async () => {
      ai.settings = await apiPatch("/workspace/ai-review", draft);
      draft.api_key = ""; ai.plan = null; ai.testResult = null;
      toast("AI settings saved. No model call was made.");
    });
  });
  card.append(form);
  card.append(el("p", "ai-effective", "Effective: " + settings.provider + " · " + (settings.model || "no model selected") + " · " + settings.base_url));
  const overrides = Object.entries(settings.origins || {}).filter(([, source]) => source !== "saved" && source !== "default");
  if (overrides.length) card.append(el("p", "engineer-hint", "Environment overrides: " + overrides.map(([key, source]) => key + " from " + source).join(", ") + ". Change the server environment to remove these overrides."));
  const test = ai.testResult || settings.test || { status: "untested" };
  const status = el("p", test.status === "failed" ? "engineer-error" : "engineer-status",
    "Connection: " + test.status + (test.tested_at ? " · " + test.tested_at : "") + (test.message ? " · " + test.message : ""));
  status.setAttribute("role", "status"); card.append(status);
  if (!settings.sdk_installed) card.append(el("p", "engineer-hint", "Install the provider SDK before testing or reviewing: python -m pip install '.[model-" + settings.provider + "]'"));
  card.append(workspaceButton("Review project runs", () => openAIReview()));
  host.append(card);
}
async function openAIReview(runIds) {
  state.ai.selected = new Set(runIds || []); state.ai.plan = null; state.ai.job = null;
  state.ai.requestId = null; state.ai.force = false;
  await goEngineer("ai-review");
}
function renderAIReview(host) {
  const ai = state.ai;
  workspaceHeading(host, "AI review", "Review captured behavior with the configured model. A redacted evidence packet is sent only when you start a review.");
  if (aiReadOnly()) {
    host.append(el("p", null, "AI review is unavailable on a read-only server.")); return;
  }
  aiError(host);
  if (ai.job) { const box = el("div"); box.id = "ai-job"; host.append(box); renderAIJob(box, ai.job); return; }
  const activity = el("section"); activity.id = "ai-activity"; host.append(activity); renderAIActivity(activity);
  host.append(workspaceButton("Configure AI review", () => goEngineer("settings")));
  const settings = ai.settings;
  if (!settings || settings.read_only) { host.append(el("p", null, "Configure AI settings before starting a review.")); return; }
  if (!settings.model) { host.append(el("p", null, "Choose a reviewer model in AI settings before starting a review.")); return; }
  host.append(el("p", "ai-effective", "Reviewer: " + settings.model + " · " + settings.base_url));
  const runs = (state.workspace.detail || {}).runs || [];
  if (!runs.length) { host.append(el("p", null, "No runs in this project yet. Import runs before reviewing.")); return; }
  const selection = el("section", "engineer-card ai-selection");
  selection.append(el("h2", null, "Select runs"));
  const count = el("p", "engineer-hint", ai.selected.size + " selected");
  const invalidate = () => {
    ai.plan = null; ai.requestId = null; count.textContent = ai.selected.size + " selected";
    const preview = $("#ai-plan"); if (preview) preview.remove();
  };
  const all = workspaceButton("Select all project runs", () => {
    ai.selected = new Set(runs.slice(0, 1000).map(r => r.run_id)); invalidate(); render();
  });
  const none = workspaceButton("Clear selection", () => { ai.selected.clear(); invalidate(); render(); });
  selection.append(all, none, count);
  const list = el("div", "ai-run-list"); list.setAttribute("role", "group"); list.setAttribute("aria-label", "Project runs");
  for (const run of runs.slice(0, 1000)) {
    const label = el("label", "ai-run-option");
    const checkbox = el("input"); checkbox.type = "checkbox"; checkbox.checked = ai.selected.has(run.run_id);
    checkbox.addEventListener("change", () => { checkbox.checked ? ai.selected.add(run.run_id) : ai.selected.delete(run.run_id); invalidate(); });
    label.append(checkbox, el("span", null, (run.task_id || run.run_id) + " · " + run.run_id));
    list.append(label);
  }
  selection.append(list);
  if (runs.length > 1000) selection.append(el("p", null, "Up to 1000 runs can be selected per job."));
  selection.append(workspaceButton("Preview AI review", () => aiAction(async () => {
    if (!ai.selected.size) throw new Error("Select at least one run.");
    ai.plan = await apiPost(projectPath("/ai-reviews/plan"), { run_ids: [...ai.selected] });
    ai.requestId = uid();
  }), true)); host.append(selection);
  if (ai.plan) {
    const plan = ai.plan, card = el("section", "engineer-card"); card.id = "ai-plan";
    const costTarget = plan.settings.cost_budget_usd ? "$" + plan.settings.cost_budget_usd : "disabled";
    const elapsedTarget = plan.settings.time_budget_s ? plan.settings.time_budget_s + "s" : "disabled";
    card.append(el("h2", null, "Ready to review"),
      el("p", null, plan.total + " selected · " + plan.already_reviewed + " already reviewed with this configuration"),
      el("p", null, plan.settings.model + " · " + plan.settings.base_url),
      el("p", "engineer-hint", plan.cost_note + " Cost target: " + costTarget + "; elapsed target: " + elapsedTarget + ". A review may use multiple calls."));
    const label = el("label", "ai-run-option"), force = el("input"); force.type = "checkbox"; force.checked = ai.force;
    force.addEventListener("change", () => { ai.force = force.checked; ai.requestId = uid(); });
    label.append(force, el("span", null, "Rerun successful reviews for this configuration")); card.append(label);
    card.append(workspaceButton("Start AI review · uses tokens", () => aiAction(async () => {
      const project = state.projectId;
      const job = await apiPost(projectPath("/ai-reviews"), { run_ids: [...ai.selected], configuration_id: plan.settings.configuration_id,
        plan_token: plan.plan_token, idempotency_key: ai.requestId, force: ai.force });
      if (project !== state.projectId) return;
      ai.job = job; ai.plan = null; pollAIReview();
    }), true)); host.append(card);
  }
}
function renderAIActivity(host) {
  const jobs = state.ai.jobs;
  aiPaint(host, JSON.stringify(jobs.slice(0, 9)), () => paintAIActivity(host, jobs));
}
function paintAIActivity(host, jobs) {
  host.textContent = "";
  host.append(el("h2", "engineer-section-title", "Activity"));
  if (!jobs.length) { host.append(el("p", "engineer-hint", "No AI reviews yet. Select runs below, or use Review with AI on the Runs page, to start one. Reviews run in the background and show up here.")); return; }
  for (const job of jobs.slice(0, 8)) {
    const active = aiJobActive(job), card = el("div", "ai-job-card" + (active ? " active" : ""));
    const top = el("div", "ai-job-top");
    top.append(el("span", "ai-job-status ai-job-" + job.status, job.status.replaceAll("_", " ")), el("strong", null, (job.results || []).length + " of " + job.total + " runs"), el("span", "engineer-hint", aiJobSummary(job)));
    const open = aiKeyed(workspaceButton(active ? "Watch progress" : "View details", () => { state.ai.job = job; pollAIReview(); render(); }, active), "activity:" + job.id);
    top.append(open); card.append(top, aiProgressBar(job));
    const meta = [job.settings && job.settings.model, aiWhen(job.created_at)].filter(Boolean).join(" · ");
    card.append(el("p", "engineer-hint", meta + (active && job.current_run ? " · reviewing " + job.current_run : "")));
    if (job.error) card.append(el("p", "engineer-error", job.error));
    host.append(card);
  }
  if (jobs.length > 8) host.append(el("p", "engineer-hint", "Showing the 8 most recent reviews."));
}
function renderAIJob(host, job) {
  aiPaint(host, JSON.stringify(job), () => paintAIJob(host, job));
}
function paintAIJob(host, job) {
  host.textContent = "";
  const active = aiJobActive(job), results = job.results || [], byRun = new Map(results.map(r => [r.run_id, r]));
  host.append(aiKeyed(workspaceButton("All AI reviews", () => { state.ai.job = null; render(); }), "job:back"));
  const status = el("p", "engineer-status", job.status.replaceAll("_", " ") + " · " + results.length + "/" + job.total + " runs processed" + (aiJobSummary(job) ? " · " + aiJobSummary(job) : ""));
  status.setAttribute("role", "status"); host.append(status);
  host.append(el("p", "ai-effective", (job.settings && job.settings.model) + " · " + (job.settings && job.settings.base_url) + (job.created_at ? " · started " + aiWhen(job.created_at) : "")));
  if (job.current_run) host.append(el("p", "ai-now", "Reviewing now: " + job.current_run));
  host.append(aiProgressBar(job));
  if (job.error) host.append(el("p", "engineer-error", job.error));
  const actions = el("div", "engineer-actions");
  if (active) actions.append(aiKeyed(workspaceButton("Cancel remaining reviews", () => aiAction(async () => {
    state.ai.job = await apiPost(projectPath("/ai-reviews/" + job.id + "/cancel")); pollAIReview();
  })), "job:cancel"));
  if (["failed", "completed_with_errors", "cancelled", "interrupted"].includes(job.status)) actions.append(aiKeyed(workspaceButton("Retry unfinished reviews", () => aiAction(async () => {
    state.ai.job = await apiPost(projectPath("/ai-reviews/" + job.id + "/retry")); pollAIReview();
  })), "job:retry"));
  actions.append(aiKeyed(workspaceButton("Choose other runs", () => openAIReview()), "job:choose"));
  actions.append(aiKeyed(workspaceButton("AI settings", () => goEngineer("settings")), "job:settings")); host.append(actions);
  if (active) host.append(el("p", "engineer-hint", "Cancelling stops between runs; a provider request already in progress may finish and incur cost. You can leave this page; progress stays in the sidebar and on the Runs page."));
  // Every run of the job, including the ones still waiting, so it is clear what is being done where.
  const list = el("div", "ai-run-states"); list.setAttribute("role", "list");
  for (const runId of job.run_ids || []) {
    const result = byRun.get(runId);
    const status = result ? (["completed", "skipped"].includes(result.status) ? "reviewed" : "failed") : active ? (job.current_run === runId ? "reviewing" : "queued") : "none";
    const row = el("div", "engineer-list-row ai-run-row"); row.setAttribute("role", "listitem");
    const chip = el("span"); paintAIChip(chip, status, null); if (!result && !active) chip.textContent = "Not run";
    row.append(chip, el("span", "ai-run-id", runId));
    if (result && result.error) row.append(el("span", "engineer-error", result.error));
    else if (result && result.status === "skipped") row.append(el("span", "engineer-hint", "already reviewed with this configuration"));
    row.append(aiKeyed(workspaceButton("Open run", () => selectRun(runId)), "run:" + runId));
    list.append(row);
  }
  host.append(list);
}
// The Runs page's AI review summary: counts, the running job, and a button scoped to the runs on screen.
function renderAIStrip(host) {
  if (aiReadOnly() || (state.workspace && state.workspace.unavailable)) { aiPaint(host, "hidden", () => { host.textContent = ""; }); return; }
  const runs = ((state.queue && state.queue.runs) || []).filter(r => !state.importRunIds || state.importRunIds.has(r.run_id));
  const counts = aiCounts(runs), active = activeAIJob();
  const todo = runs.filter(r => ["none", "failed"].includes(aiRunState(r.run_id, r)));
  const signature = JSON.stringify([state.ai.settings ? !!state.ai.settings.model : null, counts, active && [active.id, active.total, (active.results || []).length, active.current_run], todo.map(r => r.run_id)]);
  aiPaint(host, signature, () => paintAIStrip(host, runs, counts, active, todo));
}
function paintAIStrip(host, runs, counts, active, todo) {
  host.textContent = "";
  if (!runs.length) return;
  const strip = el("div", "ai-strip");
  const head = el("div", "ai-strip-head"); head.append(el("strong", null, "AI review"));
  const parts = [["reviewed", counts.reviewed], ["reviewing", counts.reviewing], ["queued", counts.queued], ["failed", counts.failed], ["none", counts.none]].filter(([, n]) => n);
  for (const [status, n] of parts) { const chip = el("span", "ai-chip ai-" + status); chip.append(document.createTextNode(n.toLocaleString() + " " + AI_STRIP_LABELS[status])); head.append(chip); }
  strip.append(head);
  if (active) {
    const line = el("div", "ai-strip-active");
    line.append(el("span", null, (active.results || []).length + " of " + active.total + " runs" + (active.current_run ? " · reviewing " + active.current_run : "")), aiProgressBar(active),
      aiKeyed(workspaceButton("View progress", () => { state.ai.job = active; goEngineer("ai-review"); }), "strip:progress"));
    strip.append(line);
  }
  const actions = el("div", "ai-strip-actions");
  if (state.ai.settings && !state.ai.settings.model) {
    actions.append(aiKeyed(workspaceButton("Configure AI review", configureAIReview, true), "strip:configure"));
  } else if (state.ai.settings && todo.length) actions.append(aiKeyed(workspaceButton("Review " + todo.length.toLocaleString() + " not yet reviewed with AI", () => openAIReview(todo.map(r => r.run_id).slice(0, 1000)), !active), "strip:review"));
  actions.append(aiKeyed(workspaceButton("AI review activity", () => { state.ai.job = null; goEngineer("ai-review"); }), "strip:activity"));
  strip.append(actions); host.append(strip);
}
let aiPollTimer;
// Starts (or restarts) tracking: reload the project's jobs now, keep reloading while any is working, and refresh
// everything that shows AI state in place. When a job finishes, the runs are reloaded so findings appear.
async function pollAIReview() {
  clearTimeout(aiPollTimer);
  const project = state.projectId, token = ++state.ai.pollToken;
  // The job in hand is the newest copy the server returned (a retry or cancel answer); it wins over the list's older one.
  if (state.ai.job) state.ai.jobs = sortAIJobs([state.ai.job, ...state.ai.jobs.filter(j => j.id !== state.ai.job.id)]);
  state.ai.jobs.filter(aiJobActive).forEach(j => state.ai.watching.add(j.id));
  try {
    const data = await api("/projects/" + encodeURIComponent(project) + "/ai-reviews");
    if (project !== state.projectId || token !== state.ai.pollToken) return;
    state.ai.jobs = sortAIJobs(data.jobs); state.ai.error = null;
    if (state.ai.job) state.ai.job = state.ai.jobs.find(j => j.id === state.ai.job.id) || state.ai.job;
    const finished = settleAIJobs(), stillActive = new Set(state.ai.watching);
    refreshAIViews();
    await announceAIFinished(finished);
    if (stillActive.size) aiPollTimer = setTimeout(pollAIReview, 1500);
  } catch (error) {
    if (project !== state.projectId) return;
    state.ai.error = error.message;
    if (state.view === "ai-review") render();
    aiPollTimer = setTimeout(pollAIReview, 3000);
  }
}
