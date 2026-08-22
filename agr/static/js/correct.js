"use strict";

// --- correct modal (Tier-2 → Tier-3) -----------------------------------------
//   §4.13 is progressive: the common action stays a one-click relabel, and the
//   confirmation offers "Add detail" to escalate into the expert editor. The
//   editor shows the generated value beside the proposed one for every field it
//   can change, validates tokens against the controlled taxonomy before saving,
//   and requires evidence when the correction restates a *fact* rather than an
//   interpretation — the same rule the write path enforces server-side.
let correctTarget = null;
function openCorrect(moment) {
  correctTarget = moment;
  const grid = $("#relabel-grid"); grid.textContent = "";
  for (const [val, label] of RELABEL_GROUPS) {
    const b = el("button", "relabel-option", label);
    b.addEventListener("click", () => {
      submitFeedback(moment, { kind: "quick_relabel", replacement_group: val }, b,
        "Correction saved as a new review revision");
      track("quick_relabel_submitted", { run_id: state.runId, moment_id: moment.moment_id, kind: val });
      closeModals();
    });
    grid.append(b);
  }
  const detail = $("#add-detail");
  detail.onclick = () => { closeModals(); openCorrectionEditor(moment); };
  openModal("#correct-modal");
}

// The nine editable fields (§4.13 Tier 3). `factual` marks the ones that restate
// what happened; correcting those requires evidence.
const CORRECTION_FIELDS = [
  { id: "behaviour_tags", label: "Behaviour tags", type: "tags", from: m => (m.behaviour_tags || []).join(", ") },
  { id: "consequence", label: "Consequence", type: "enum", factual: true, from: m => m.consequence },
  { id: "attribution_ceiling", label: "Attribution level", type: "enum", from: m => m.attribution_ceiling },
  { id: "decisive", label: "Decisive", type: "bool", from: m => "yes (surfaced as a key moment)" },
  { id: "better_action", label: "Better action", type: "text", from: m => m.better_action },
  { id: "root_cause_candidates", label: "Root cause (top)", type: "enum", from: m => ((m.root_cause_candidates || [])[0] || {}).locus },
  { id: "opportunity_window", label: "Opportunity window", type: "window", factual: true, from: m => (m.anchor_event_ids || []).join(", ") },
  { id: "linked_items", label: "Linked checks", type: "list", factual: true, from: m => (m.affected_checks || []).join(", ") },
  { id: "task_verifier_concern", label: "Task/verifier concern", type: "enum", from: () => null },
];
const TAXONOMY = {
  consequence: ["requirement_failed", "requirement_at_risk", "incorrect_state", "incomplete_artifact",
    "excess_cost", "excess_latency", "recovery_succeeded", "verifier_failure", "verifier_pass", "no_material_effect"],
  attribution_ceiling: ["hypothesized", "dependency_linked", "direct"],
  root_cause_candidates: ["base_model", "agent_policy", "agent_scaffold", "evaluation_harness", "tool",
    "environment", "task_instruction", "reference_solution", "verifier", "indeterminate"],
  task_verifier_concern: ["supported_concern", "possible_concern", "no_concern_detected", "insufficient_evidence"],
  behaviour_tags: ["lost_requirement", "missed_constraint", "skipped_verification", "insufficient_verification",
    "premature_submission", "failed_to_replan", "poor_query", "wrong_tool", "ignored_error",
    "repeated_unchanged_action", "stopped_enumeration", "incomplete_execution"],
};

function openCorrectionEditor(moment) {
  correctTarget = moment;
  const host = $("#correction-fields"); host.textContent = "";
  const draft = {};
  for (const field of CORRECTION_FIELDS) {
    const row = el("div", "corr-row");
    const head = el("div", "corr-head");
    head.append(el("span", "corr-label", field.label));
    if (field.factual) {
      const f = el("span", "corr-factual", "factual · needs evidence");
      f.title = "This restates what happened, so a correction must cite the events that show it.";
      head.append(f);
    }
    row.append(head);
    const generated = field.from(moment);
    row.append(el("div", "corr-generated", "generated: " + (generated || "— not generated —")));
    row.append(correctionInput(field, draft));
    host.append(row);
  }
  const ev = el("div", "corr-row");
  ev.append(el("div", "corr-head", "Evidence event ids"));
  ev.append(el("div", "corr-generated",
    "anchors on this moment: " + ((moment.anchor_event_ids || []).join(", ") || "none")));
  const evInput = el("input", "corr-input");
  evInput.placeholder = "evt_004, evt_007";
  evInput.value = (moment.anchor_event_ids || []).join(", ");
  evInput.id = "corr-evidence";
  ev.append(evInput);
  host.append(ev);

  $("#correction-save").onclick = () => saveCorrection(moment, draft, evInput);
  openModal("#correction-modal");
}

function correctionInput(field, draft) {
  if (field.type === "bool") {
    const sel = el("select", "corr-input");
    for (const [v, label] of [["", "— unchanged —"], ["true", "decisive"], ["false", "not decisive"]]) {
      const o = el("option", null, label); o.value = v; sel.append(o);
    }
    sel.addEventListener("change", () => {
      if (sel.value === "") delete draft[field.id]; else draft[field.id] = sel.value === "true";
    });
    return sel;
  }
  if (field.type === "enum" || field.type === "tags") {
    const sel = el("select", "corr-input");
    if (field.type === "tags") sel.multiple = true;
    else { const o = el("option", null, "— unchanged —"); o.value = ""; sel.append(o); }
    for (const token of TAXONOMY[field.id] || []) {
      const o = el("option", null, token); o.value = token; sel.append(o);
    }
    sel.addEventListener("change", () => {
      if (field.type === "tags") {
        const picked = [...sel.selectedOptions].map(o => o.value);
        if (picked.length) draft[field.id] = picked; else delete draft[field.id];
      } else if (!sel.value) delete draft[field.id];
      else if (field.id === "root_cause_candidates") draft[field.id] = [{ locus: sel.value }];
      else if (field.id === "task_verifier_concern") draft[field.id] = { assessment: sel.value };
      else draft[field.id] = sel.value;
    });
    return sel;
  }
  const input = el("input", "corr-input");
  input.placeholder = field.type === "window" ? "evt_004 → evt_009"
    : field.type === "list" ? "C1, C3" : "proposed value";
  input.addEventListener("input", () => {
    const raw = input.value.trim();
    if (!raw) { delete draft[field.id]; return; }
    if (field.type === "window") {
      const [a, b] = raw.split(/\s*(?:→|->|,)\s*/);
      if (a && b) draft[field.id] = { start_event_id: a, end_event_id: b };
    } else if (field.type === "list") {
      draft[field.id] = { check_ids: raw.split(/\s*,\s*/).filter(Boolean) };
    } else draft[field.id] = raw;
  });
  return input;
}

async function saveCorrection(moment, draft, evInput) {
  if (!Object.keys(draft).length) { toast("Nothing changed — set at least one field."); return; }
  const evidence = evInput.value.split(/\s*,\s*/).map(s => s.trim()).filter(Boolean);
  const generated = {
    behaviour_tags: moment.behaviour_tags || [], consequence: moment.consequence,
    attribution_ceiling: moment.attribution_ceiling, better_action: moment.better_action,
    root_cause_candidates: moment.root_cause_candidates || [],
  };
  try {
    await apiPost("/runs/" + encodeURIComponent(state.runId) + "/feedback", {
      mutation_id: uid(), moment_id: moment.moment_id, kind: "structured_correction",
      actor: state.reviewer, correction: draft, generated, evidence_event_ids: evidence,
    });
    track("correction_saved", { run_id: state.runId, moment_id: moment.moment_id,
      corrected_fields: Object.keys(draft) });
    closeModals();
    await refreshRun();
    toast("Correction saved as a new annotation revision");
  } catch (e) {
    const detail = e.data && e.data.detail ? e.data.detail : "Save failed";
    $("#correction-error").textContent = typeof detail === "string" ? detail : "Save failed";
  }
}
function openModal(sel) { const m = $(sel); m.classList.add("open"); m.setAttribute("aria-hidden", "false"); }
function closeModals() { for (const id of ["#correct-modal", "#help-modal", "#correction-modal", "#glossary-modal", "#intro-modal"]) {
  const m = $(id); m.classList.remove("open"); m.setAttribute("aria-hidden", "true"); }
  const err = $("#correction-error"); if (err) err.textContent = ""; }
document.querySelectorAll("[data-close-modal]").forEach(b => b.addEventListener("click", closeModals));
document.querySelectorAll(".modal-shade").forEach(s => s.addEventListener("click", e => { if (e.target === s) closeModals(); }));

