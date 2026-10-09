"use strict";

// Settings are user defaults; review selections and jobs belong to one project.
// Keys live only in this form until transmitted to the server session.
state.ai = { settings: null, draft: null, error: null, selected: new Set(),
  plan: null, job: null, jobs: [], force: false, token: 0, requestId: null };

async function loadAIReviewData() {
  const project = state.projectId, token = ++state.ai.token;
  try {
    const [settings, jobs] = await Promise.all([
      api("/workspace/ai-review"), api(projectPath("/ai-reviews"))
    ]);
    if (project !== state.projectId || token !== state.ai.token) return;
    state.ai.settings = settings; state.ai.jobs = jobs.jobs; state.ai.error = null;
    if (!state.ai.draft && !settings.read_only) {
      state.ai.draft = Object.fromEntries(["provider", "model", "base_url", "cost_budget_usd", "time_budget_s", "request_timeout_s"].map(k => [k, settings[k]]));
    }
    const jobId = new URLSearchParams(location.search).get("review_job");
    if (jobId && !state.ai.job) state.ai.job = jobs.jobs.find(j => j.id === jobId) || null;
    if (state.ai.job && ["queued", "running", "cancelling"].includes(state.ai.job.status)) pollAIReview();
  } catch (error) {
    if (project === state.projectId && token === state.ai.token) state.ai.error = error.message;
  }
}
function aiReadOnly() {
  const project = activeProject() || {};
  return state.serverReadOnly || !!project.sample || !!project.archived;
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
    card.append(el("p", null, "AI configuration and paid reviews are unavailable in sample or archived projects. Open an active personal project."));
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
    const defaults = settings.defaults[draft.provider];
    draft.model = defaults.model; draft.base_url = defaults.base_url;
    draft.api_key = ""; ai.plan = null; ai.testResult = null; render();
  });
  providerLabel.append(provider); form.append(providerLabel);
  workspaceField(form, "Reviewer model ID", draft.model, v => { draft.model = v; ai.plan = null; }).required = true;
  const endpoint = workspaceField(form, "Endpoint URL (change for a custom or local server)", draft.base_url, v => { draft.base_url = v; ai.plan = null; }, "url");
  endpoint.required = true;
  workspaceField(form, "API key (optional; server session only)", draft.api_key || "", v => draft.api_key = v, "password").autocomplete = "off";
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
  card.append(el("p", "ai-effective", "Effective: " + settings.provider + " · " + settings.model + " · " + settings.base_url));
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
    host.append(el("p", null, "AI review is unavailable in sample or archived projects.")); return;
  }
  aiError(host);
  if (ai.job) { renderAIJob(host, ai.job); return; }
  host.append(workspaceButton("Configure AI review", () => goEngineer("settings")));
  const settings = ai.settings;
  if (!settings || settings.read_only) { host.append(el("p", null, "Configure AI settings before starting a review.")); return; }
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
  if (ai.jobs.length) {
    host.append(el("h2", "engineer-section-title", "Review history"));
    for (const job of ai.jobs) {
      const row = el("div", "engineer-list-row");
      row.append(el("span", null, job.settings.model + " · " + job.status.replaceAll("_", " ") + " · " + job.results.length + "/" + job.total),
        workspaceButton("Open review job", () => { ai.job = job; pollAIReview(); render(); }));
      host.append(row);
    }
  }
}
function renderAIJob(host, job) {
  const active = ["queued", "running", "cancelling"].includes(job.status);
  const status = el("p", "engineer-status", job.status.replaceAll("_", " ") + " · " + job.results.length + "/" + job.total + " runs processed");
  status.setAttribute("role", "status"); host.append(status);
  host.append(el("p", "ai-effective", job.settings.model + " · " + job.settings.base_url));
  if (job.current_run) host.append(el("p", null, "Reviewing " + job.current_run));
  const progress = el("progress"); progress.max = job.total; progress.value = job.results.length;
  progress.setAttribute("aria-label", "AI review progress"); host.append(progress);
  if (job.error) host.append(el("p", "engineer-error", job.error));
  const actions = el("div", "engineer-actions");
  if (active) actions.append(workspaceButton("Cancel remaining reviews", () => aiAction(async () => {
    state.ai.job = await apiPost(projectPath("/ai-reviews/" + job.id + "/cancel")); pollAIReview();
  })));
  if (["failed", "completed_with_errors", "cancelled", "interrupted"].includes(job.status)) actions.append(workspaceButton("Retry unfinished reviews", () => aiAction(async () => {
    state.ai.job = await apiPost(projectPath("/ai-reviews/" + job.id + "/retry")); pollAIReview();
  })));
  actions.append(workspaceButton("Choose other runs", () => openAIReview()));
  actions.append(workspaceButton("AI settings", () => goEngineer("settings"))); host.append(actions);
  if (active) host.append(el("p", "engineer-hint", "Cancelling stops between runs; a provider request already in progress may finish and incur cost. You can leave this page and return through review history."));
  for (const result of job.results) {
    const row = el("div", "engineer-list-row"); row.append(el("span", null, result.run_id),
      el("span", ["failed", "incomplete"].includes(result.status) ? "engineer-error" : "engineer-hint", result.error || result.status),
      workspaceButton("Open run", () => selectRun(result.run_id)));
    host.append(row);
  }
}
let aiPollTimer;
async function pollAIReview() {
  clearTimeout(aiPollTimer);
  const project = state.projectId, job = state.ai.job;
  if (!job) return;
  try {
    const result = await api("/projects/" + encodeURIComponent(project) + "/ai-reviews/" + job.id);
    if (project !== state.projectId || !state.ai.job || state.ai.job.id !== job.id) return;
    const wasActive = ["queued", "running", "cancelling"].includes(job.status);
    state.ai.job = result;
    if (state.view === "ai-review") render();
    if (["queued", "running", "cancelling"].includes(result.status)) aiPollTimer = setTimeout(pollAIReview, 1200);
    else if (wasActive) {
      await loadInbox();
      if (state.view === "review") await refreshRun();
    }
  } catch (error) {
    if (project !== state.projectId) return;
    state.ai.error = error.message;
    if (state.view === "ai-review") render();
    aiPollTimer = setTimeout(pollAIReview, 3000);
  }
}

