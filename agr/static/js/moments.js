"use strict";

function selectMoment(i) { state.momentIdx = i; trackMoment(); render(); }
function trackMoment() { const m = currentMoment();
  if (m) track("moment_viewed", { run_id: state.runId, moment_id: m.moment_id,
    detector: m.detector, position: state.momentIdx + 1 }); }
function moveMoment(dir) { const n = currentMoments().length; if (!n) return;
  state.momentIdx = (state.momentIdx + dir + n) % n; trackMoment(); render(); const c = $(".moment-card"); if (c) c.scrollIntoView({ behavior: "smooth", block: "center" }); }

function renderMomentCard(rv, f, moment) {
  const card = el("div", "moment-card");
  const top = el("div", "moment-topline");
  const kind = el("span", "moment-kind " + momentTagClass(moment), momentTypeLabel(moment));
  top.append(kind);
  const phase = (rv.phases.find(p => p.phase_id === moment.phase_id) || {}).label;
  top.append(el("span", "moment-pos", "Moment " + (state.momentIdx + 1) + " of " + currentMoments().length + (phase ? " · " + phase : "")));
  const nav = el("div", "moment-nav");
  const prev = el("button", "nav-arrow", "←"); prev.setAttribute("aria-label", "Previous key moment"); prev.addEventListener("click", () => moveMoment(-1));
  const next = el("button", "nav-arrow", "→"); next.setAttribute("aria-label", "Next key moment"); next.addEventListener("click", () => moveMoment(1));
  nav.append(prev, next); top.append(nav);
  card.append(top);

  const body = el("div", "moment-body");
  const heading = el("div", "moment-heading");
  heading.append(el("h2", null, moment.summary));
  heading.append(el("span", "moment-anchor", moment._stepIdx != null ? "trace step " + (moment._stepIdx + 1) : (moment.anchor_event_ids || [])[0] || ""));
  body.append(heading);

  body.append(mcBlock("Situation", "validated fact", situationText(moment), ""));
  body.append(mcBlock(moment.kind === "omission" ? "Agent omission" : "Agent action", "source event", actionText(moment, f), "action"));
  body.append(mcBlock("Observed consequence", "validated fact", consequenceText(moment), "consequence"));

  if (moment.enrichment_source && (moment.root_cause_candidates || []).length) {
    const rcc = moment.root_cause_candidates[0];
    // AGR-03: interpretation support is its own review status, kept apart from
    // the validated facts above — an explanation with no evidence link is
    // labelled as such, never rendered as if the run proved it.
    const support = (moment.gate_results || {}).explanation_support;
    const supportLabel = support === "evidence_linked" ? "evidence-linked"
      : support === "dangling_references" ? "references missing evidence"
      : support === "interpretation_only" ? "no evidence link — claim not validated"
      : vocab("attribution", moment.attribution_ceiling).label.toLowerCase();
    // F1 follow-up: the attribution flag is surfaced, not silently recorded —
    // wording that implies more causality than the ceiling licenses is marked
    // on the card where the claim is made.
    const overclaim = (moment.gate_results || {}).explanation_attribution === "overclaim";
    body.append(mcBlock("Likely impact",
      overclaim ? supportLabel + " · overclaims causality" : supportLabel,
      (rcc.rationale || "See root-cause analysis.") + " (locus: " + rcc.locus + ")", "interp"));
  } else {
    body.append(mcBlock("Likely impact", "interpretation", "Not generated — this is a deterministic review.", "absent"));
  }
  if (moment.enrichment_source && moment.better_action) {
    const tested = moment.attribution_ceiling === "counterfactually_supported";
    body.append(mcBlock("Better action", tested ? "replay-tested" : "hypothesis · not replay-tested", moment.better_action, "hypo"));
  } else {
    body.append(mcBlock("Better action", "hypothesis", "Not generated — this is a deterministic review.", "absent"));
  }
  card.append(body);

  card.append(renderMomentActions(moment));
  return card;
}
function mcBlock(label, sub, text, kind) {
  const b = el("div", "mc-block" + (kind ? " " + kind : ""));
  const l = el("div", "mc-label"); l.append(document.createTextNode(label)); l.append(el("span", null, sub)); b.append(l);
  b.append(el("p", "mc-text", text)); return b;
}
function situationText(m) { const f = (m.facts || [])[0] || {};
  if (f.type === "requirement_status") return "Requirement check " + f.check_id + " required " + fmtVal(f.expected) + ".";
  if (f.type === "absence") return "Declared artifact " + fmtVal(f.declared_artifact) + " was expected in the run.";
  if (f.type === "repetition") return "An action was available to advance the task.";
  if (f.type === "state_transition") return "A tool failure occurred at " + fmtVal(f.failure_event) + ".";
  return m.summary || "See evidence."; }
function actionText(m, f) { if (m.kind === "omission") return "The required action was not observed before submission.";
  const a = (m.anchor_event_ids || [])[0]; const idx = a != null ? f.steps.findIndex(s => (s.event_ids || []).includes(a)) : -1;
  if (idx >= 0) return "At trace step " + (idx + 1) + " the agent performed a " + (f.steps[idx].kind || "?") + " action.";
  return m.summary || "See evidence."; }
function consequenceText(m) { const f = (m.facts || [])[0] || {};
  if (f.type === "requirement_status") {
    // F1 follow-up: state timing honestly. The final verifier status is a
    // post-run result; "still failing at submission" is claimed only when the
    // validated fact carries the agent's own observed failure.
    if (f.agent_observed_failure) return "Check " + f.check_id + " was still failing at submission (observed " + fmtVal(f.observed) + ").";
    const observedBy = f.agent_observed_status && f.agent_observed_status !== "failed"
      ? "; the agent's trace last observed it " + f.agent_observed_status : "; the agent's trace records no observation of this check";
    return "Check " + f.check_id + " ended '" + (f.status || f.status_at_submission) + "' in the run's final verifier (observed " + fmtVal(f.observed) + ")" + observedBy + ".";
  }
  if (f.type === "state_transition") return f.resolution_event ? "The failure was later resolved by a strategy change." : "The failure was left unresolved before submission.";
  if (m.consequence) return m.consequence; return "See the evidence panel for the observed result."; }

const RELABEL_GROUPS = [["lost_requirement","Lost requirement"],["skipped_verification","Skipped verification"],
  ["poor_tool_or_query","Poor tool or query"],["premature_completion","Premature completion"],["failed_recovery","Failed recovery"],["other","Other"]];
// §4.13 after saving: the card shows `Human corrected`, the accepted values are
// what it displays, the generated version stays inspectable behind a toggle, and
// only lineage that actually exists is claimed.
function renderCorrectedStrip(moment) {
  const c = moment.correction || {};
  const strip = el("div", "corrected-strip");
  strip.append(el("span", "tag concern", "Human corrected"));
  strip.append(el("span", "lineage", "revision " + (c.correction_revision || 1)
    + (c.actor ? " · " + c.actor : "")
    + " · " + (c.corrected_fields || []).join(", ")));
  const peekId = "gen-" + moment.moment_id;
  const toggle = el("button", null, "Show generated version");
  toggle.addEventListener("click", () => {
    const peek = $("#" + CSS.escape(peekId));
    const open = peek.style.display !== "none";
    peek.style.display = open ? "none" : "block";
    toggle.textContent = open ? "Show generated version" : "Hide generated version";
  });
  strip.append(toggle);
  for (const line of c.lineage || []) strip.append(el("span", "lineage", "· " + line));
  if (c.adjudication_status === "unadjudicated")
    strip.append(el("span", "lineage", "· not yet eligible for the reviewer dataset"));
  const peek = el("div", "generated-peek");
  peek.id = peekId; peek.style.display = "none";
  const generated = moment.generated || {};
  peek.textContent = Object.keys(generated).length
    ? Object.entries(generated).map(([k, v]) => k + ": " + fmtVal(v)).join("\n")
    : "(the corrected fields had no generated value)";
  const frag = document.createDocumentFragment();
  frag.append(strip, peek);
  return frag;
}

function renderMomentActions(moment) {
  const wrap = el("div", "moment-actions");
  if (moment.corrected) wrap.append(renderCorrectedStrip(moment));
  const given = feedbackFor(moment.moment_id);
  if (given.length) {
    const sum = el("div", "mfb"); const kinds = new Set(given.map(f => f.kind));
    if (kinds.has("agree")) sum.append(el("span", "tag strength", "Agreed"));
    if (kinds.has("not_decisive")) sum.append(el("span", "tag neutral", "Marked not decisive"));
    if (kinds.has("flag_task_verifier")) sum.append(el("span", "tag concern", "Flagged task/verifier"));
    if (kinds.has("quick_relabel")) { const last = [...given].reverse().find(f => f.kind === "quick_relabel");
      sum.append(el("span", "tag concern", "Human corrected" + (last.replacement_label ? " → " + last.replacement_label : ""))); }
    wrap.append(sum);
  }
  const mk = (label, cls, fn, title, sc) => { const b = el("button", "button " + cls);
    b.append(document.createTextNode(label)); if (sc) b.append(el("span", "shortcut", sc));
    if (title) b.title = title; b.addEventListener("click", () => fn(b)); return b; };
  wrap.append(mk("View evidence", "primary", () => focusEvidence(), "Focus the evidence for this moment’s claims.", "E"));
  wrap.append(mk("Agree", "", b => submitFeedback(moment, { kind: "agree" }, b, "Agreement recorded — attribution unchanged"), "Records positive feedback; does not raise attribution."));
  wrap.append(mk("Not decisive", "", b => submitFeedback(moment, { kind: "not_decisive" }, b, "Marked not decisive"), "Preserves the generated record; adds a human annotation."));
  wrap.append(mk("Flag task/verifier", "", b => submitFeedback(moment, { kind: "flag_task_verifier" }, b, "Task/verifier concern flagged"), "Marks a possible task or verifier problem."));
  wrap.append(mk("Correct label", "subtle", () => openCorrect(moment), "Quick relabel to a controlled taxonomy value."));
  return wrap;
}
async function submitFeedback(moment, fields, btn, okMsg) {
  try {
    await apiPost("/runs/" + encodeURIComponent(state.runId) + "/feedback",
      Object.assign({ mutation_id: uid(), moment_id: moment.moment_id, actor: state.reviewer }, fields));
    track("feedback_submitted", { run_id: state.runId, moment_id: moment.moment_id, kind: fields.kind });
    await refreshRun(); toast(okMsg || "Saved");
  } catch (e) { if (btn) flash(btn, "failed"); }
}

