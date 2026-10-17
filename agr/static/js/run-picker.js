"use strict";

// Native dialog supplies focus containment, Escape dismissal, and focus return.
// Fetch the complete project list: queue filters must not hide destinations.
const runPickerDialog = el("dialog", "run-picker");
runPickerDialog.setAttribute("aria-labelledby", "run-picker-title");
const runPickerHeading = el("h2", null, "Jump to a run"); runPickerHeading.id = "run-picker-title";
const runPickerInput = el("input");
runPickerInput.type = "search";
runPickerInput.placeholder = "Search task names or run IDs";
runPickerInput.setAttribute("aria-label", "Search runs");
runPickerInput.setAttribute("role", "combobox");
runPickerInput.setAttribute("aria-autocomplete", "list");
runPickerInput.setAttribute("aria-controls", "run-picker-results");
runPickerInput.setAttribute("aria-expanded", "true");
const runPickerStatus = el("p", "run-picker-status"); runPickerStatus.setAttribute("role", "status");
const runPickerList = el("div", "run-picker-list"); runPickerList.id = "run-picker-results";
runPickerList.setAttribute("role", "listbox"); runPickerList.setAttribute("aria-label", "Matching runs");
const runPickerClose = el("button", "button", "Close"); runPickerClose.type = "button";
runPickerClose.addEventListener("click", closeRunPicker);
runPickerDialog.append(runPickerHeading, runPickerInput, runPickerStatus, runPickerList, runPickerClose);
document.body.append(runPickerDialog);
let runPickerRows = [], runPickerMatches = [], runPickerIndex = 0, runPickerToken = 0;
let runPickerLoading = false, runPickerError = null, runPickerCache = null;
function closeRunPicker() { ++runPickerToken; runPickerDialog.close(); }
runPickerDialog.addEventListener("cancel", () => { ++runPickerToken; });
// Search inputs can consume Escape to clear their text before the native
// dialog handles it. Dismiss explicitly and keep the page underneath still.
runPickerDialog.addEventListener("keydown", e => {
  if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeRunPicker(); }
});
runPickerDialog.addEventListener("click", e => { if (e.target === runPickerDialog) {
  const box = runPickerDialog.getBoundingClientRect();
  if (e.clientX < box.left || e.clientX > box.right || e.clientY < box.top || e.clientY > box.bottom) closeRunPicker();
} });
$("#run-picker-button").addEventListener("click", openRunPicker);
async function openRunPicker() {
  if (runPickerDialog.open || !state.projectId && !state.workspace.unavailable) return;
  closeModals(); closeSidebarDrawer();
  const token = ++runPickerToken, project = state.projectId, queue = state.queue;
  runPickerRows = []; runPickerMatches = []; runPickerList.textContent = "";
  runPickerInput.value = ""; runPickerInput.removeAttribute("aria-activedescendant");
  runPickerLoading = true; runPickerError = null;
  runPickerStatus.textContent = "Loading runs…";
  runPickerDialog.showModal(); runPickerInput.focus();
  // Reopening against the same inbox can reuse its list briefly. An import
  // refreshes the inbox, invalidating this cache; external changes age out.
  if (runPickerCache && runPickerCache.project === project && runPickerCache.queue === queue
      && Date.now() - runPickerCache.at < 30000) {
    runPickerRows = runPickerCache.rows; runPickerLoading = false; paintRunPicker(); return;
  }
  try {
    const rows = await api("/runs");
    if (token !== runPickerToken || project !== state.projectId || !runPickerDialog.open) return;
    runPickerCache = { project, queue, rows, at: Date.now() };
    runPickerRows = rows; runPickerLoading = false; paintRunPicker();
  } catch (error) {
    if (token === runPickerToken && runPickerDialog.open) {
      runPickerLoading = false; runPickerError = "Could not load runs: " + error.message; paintRunPicker();
    }
  }
}
function paintRunPicker() {
  if (runPickerLoading || runPickerError) {
    runPickerStatus.textContent = runPickerLoading ? "Loading runs…" : runPickerError;
    return;
  }
  const matches = runPickerRows.filter(r => runsMatchesSearch(r, runPickerInput.value));
  runPickerMatches = matches.slice(0, 50); runPickerIndex = 0; runPickerList.textContent = "";
  runPickerStatus.textContent = matches.length ? matches.length + " matching runs" + (matches.length > 50 ? " · showing first 50; type to narrow" : "") : "No matching runs";
  runPickerMatches.forEach((run, index) => {
    const option = el("button", "run-picker-option"); option.type = "button"; option.tabIndex = -1;
    option.id = "run-picker-option-" + index; option.setAttribute("role", "option");
    option.append(el("strong", null, run.task_id || run.run_id), el("span", null, run.run_id));
    option.addEventListener("click", () => chooseRunPicker(index));
    runPickerList.append(option);
  });
  markRunPicker();
}
function markRunPicker() {
  Array.from(runPickerList.children).forEach((option, i) => option.setAttribute("aria-selected", String(i === runPickerIndex)));
  const active = runPickerList.children[runPickerIndex];
  if (active) { runPickerInput.setAttribute("aria-activedescendant", active.id); active.scrollIntoView({ block: "nearest" }); }
  else runPickerInput.removeAttribute("aria-activedescendant");
}
function chooseRunPicker(index) {
  const run = runPickerMatches[index]; if (!run) return;
  closeRunPicker();
  selectRun(run.run_id).catch(error => toast("Could not open run: " + error.message));
}
runPickerInput.addEventListener("input", paintRunPicker);
runPickerInput.addEventListener("keydown", e => {
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    if (runPickerMatches.length) runPickerIndex = (runPickerIndex + (e.key === "ArrowDown" ? 1 : -1) + runPickerMatches.length) % runPickerMatches.length;
    markRunPicker();
  } else if (e.key === "Enter") { e.preventDefault(); chooseRunPicker(runPickerIndex); }
});
