"use strict";

function selectMoment(i) { state.momentIdx = i; trackMoment(); render(); }
function trackMoment() { const m = currentMoment();
  if (m) track("moment_viewed", { run_id: state.runId, moment_id: m.moment_id,
    detector: m.detector, position: state.momentIdx + 1 }); }
function moveMoment(dir) { const n = currentMoments().length; if (!n) return;
  state.momentIdx = (state.momentIdx + dir + n) % n; trackMoment(); render(); const c = $(".moment-card"); if (c) c.scrollIntoView({ behavior: "smooth", block: "center" }); }

// T2: the moment card collapses to a scannable summary — finding headline,
// concise observed-behaviour line, and evidence anchors — with the full
// five-part breakdown (Situation/Action/Consequence/Likely impact/Better
// action) preserved exactly as before, just moved behind an expand toggle.
// `state.expandedMoments` remembers which moments a reader opened so an
// action that re-renders the card (Agree, a saved correction, …) does not
// silently re-collapse a detail view they were reading.
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

  const summary = el("div", "moment-summary");
  const heading = el("div", "moment-heading");
  heading.append(el("h2", null, moment.summary));
  heading.append(el("span", "moment-anchor", moment._stepIdx != null ? "trace step " + (moment._stepIdx + 1) : (moment.anchor_event_ids || [])[0] || ""));
  summary.append(heading);
  summary.append(el("p", "moment-observed", observedBehaviourSummary(moment, f)));
  // Kept apart from Situation/Consequence below: recovery status is its own
  // claim, scoped to what this moment's own evidence supports — never a
  // statement about the run's eventual task outcome.
  const rec = recoveryText(moment);
  if (rec) {
    const r = el("p", "moment-recovery " + rec.cls);
    r.append(el("strong", null, "Recovery: "), document.createTextNode(rec.text));
    summary.append(r);
  }
  const anchors = (moment.anchor_event_ids || []).filter(Boolean);
  if (anchors.length) {
    const row = el("div", "moment-anchors");
    row.append(el("span", "moment-anchors-label", "Evidence"));
    row.append(evidenceCell(anchors, f));
    summary.append(row);
  }
  card.append(summary);

  const detail = el("details", "moment-detail");
  if (state.expandedMoments.has(moment.moment_id)) detail.open = true;
  detail.append(el("summary", null, "Full breakdown"));
  detail.addEventListener("toggle", () => {
    if (detail.open) state.expandedMoments.add(moment.moment_id);
    else state.expandedMoments.delete(moment.moment_id);
  });
  const body = el("div", "moment-body");

  body.append(mcBlock("Situation", "validated fact", situationText(moment), ""));
  body.append(mcBlock(moment.kind === "omission" ? "Agent omission" : "Agent action", "source event", actionText(moment, f), "action"));
  body.append(mcBlock("Observed consequence", "validated fact", consequenceText(moment), "consequence"));

  if (moment.enrichment_source) {
    if ((moment.root_cause_candidates || []).length) {
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
      body.append(mcBlock("Likely impact", "interpretation", "Not generated for this moment.", "absent"));
    }
    if (moment.better_action) {
      const tested = moment.attribution_ceiling === "counterfactually_supported";
      body.append(mcBlock("Better action", tested ? "replay-tested" : "hypothesis · not replay-tested", moment.better_action, "hypo"));
    } else {
      body.append(mcBlock("Better action", "hypothesis", "Not generated for this moment.", "absent"));
    }
  } else {
    // T2: a deterministic card has no AI interpretation at all — one combined
    // absent block, not two repeated "Not generated" blocks for Likely impact
    // and Better action separately.
    body.append(mcBlock("AI interpretation", "interpretation",
      "Not generated — this is a deterministic review.", "absent"));
  }
  detail.append(body);
  card.append(detail);

  // Eval lesson (T1 / formerly its own chapter, §4.11): attached to the moment
  // that recommends it, so a reader finds it where the finding is instead of a
  // separate tab. Collapsed by default — most moments have none.
  if (moment === lessonMoment()) {
    const det = el("details", "moment-lesson");
    if (state.expandedLessons.has(moment.moment_id)) det.open = true;
    det.addEventListener("toggle", () => {
      if (det.open) state.expandedLessons.add(moment.moment_id);
      else state.expandedLessons.delete(moment.moment_id);
    });
    det.append(el("summary", null, "Eval lesson available"));
    const lessonBody = el("div", "moment-lesson-body");
    lessonBody.dataset.momentId = moment.moment_id;
    renderLessonInline(lessonBody, moment);
    det.append(lessonBody);
    card.append(det);
  }

  card.append(renderMomentActions(moment));
  return card;
}
function mcBlock(label, sub, text, kind) {
  const b = el("div", "mc-block" + (kind ? " " + kind : ""));
  const l = el("div", "mc-label"); l.append(document.createTextNode(label)); l.append(el("span", null, sub)); b.append(l);
  b.append(el("p", "mc-text", text)); return b;
}
// The (tool, diagnostic) identity of a state_transition fact — the same
// identity agr.fleet groups recovery episodes by (item 32 follow-up) — or
// null when the fact carries neither. `usable` is false for an opaque
// fallback signature (no traceback, no recognised diagnostic marker: the
// fleet view's "Unclassified" case), where showing the raw last-line text as
// if it identified the failure would be misleading.
function toolFailureIdentity(f) {
  if (!f || f.type !== "state_transition") return null;
  const diag = f.failure_diagnostic || f.error_signature;
  const usable = !!diag && f.error_signature_basis !== "fallback_last_nonempty";
  return { tool: f.tool || null, diag: usable ? diag : null };
}
function situationText(m) { const f = (m.facts || [])[0] || {};
  if (f.type === "requirement_status") return "Requirement check " + f.check_id + " required " + fmtVal(f.expected) + ".";
  if (f.type === "absence") return "Declared artifact " + fmtVal(f.declared_artifact) + " was expected in the run.";
  if (f.type === "repetition") return "An action was available to advance the task.";
  if (f.type === "state_transition") {
    const id = toolFailureIdentity(f);
    if (id && id.tool && id.diag) return id.tool + " failed: " + id.diag;
    if (id && id.tool) return "The " + id.tool + " call failed at " + fmtVal(f.failure_event) + ".";
    return "A tool call failed at " + fmtVal(f.failure_event) + ".";
  }
  return m.summary || "See evidence."; }
// The Overview "Recovery:" line (and reusable wherever a moment's recovery
// status needs stating on its own) — only for a tool-failure fact, and always
// scoped to what THIS moment's evidence supports, never the run's eventual
// outcome. Returns null for a moment with no recovery-relevant fact at all.
function recoveryText(m) {
  const f = (m.facts || [])[0] || {};
  if (f.type !== "state_transition") return null;
  if (f.resolution_event)
    return { cls: "pos", text: "Confirmed — a later strategy change resolved this failure before submission." };
  return { cls: "warn", text: "Not observed before submission. Whether this affected the final result is a "
    + "separate question — state only what the evidence below actually supports." };
}
// The Overview / moment-card evidence action label: names the concrete
// artifact a reader is about to open (a tool's request and response) rather
// than the generic "View evidence" whenever a tool identity is known.
function evidenceActionLabel(m) {
  const f = (m.facts || [])[0] || {};
  const id = toolFailureIdentity(f);
  return (id && id.tool) ? "View " + id.tool + " request and response →" : "View evidence →";
}
// T2: the exact captured detail for a source step — the tool, the target
// path, a short excerpt of its content/data, and an exit code when the
// capture has them — so a card reads "shell call ./build.sh" instead of a
// generic "performed a tool_call action." Returns null when the capture has
// none of these fields, so the caller can say so explicitly rather than
// inventing a description.
function truncateText(s, n) { return s.length > n ? s.slice(0, n - 1) + "…" : s; }
function actionDetail(step) {
  const c = (step && step.content) || {};
  const bits = [];
  if (c.tool) bits.push(c.tool + (c.direction ? " " + c.direction : ""));
  const path = c.path || c.artifact_path;
  if (path) bits.push(path);
  const text = c.content != null ? c.content : c.data != null ? c.data : c.summary != null ? c.summary : null;
  if (text != null && String(text).length) bits.push('"' + truncateText(String(text), 140) + '"');
  if (c.exit_code != null) bits.push("exit " + c.exit_code);
  return bits.length ? bits.join(" · ") : null;
}
// T2: the collapsed card's one-line "what did the agent actually do" —
// distinct from actionText's fuller sentence in the expanded detail, but
// drawing on the same captured detail so the two never contradict each other.
function observedBehaviourSummary(m, f) {
  if (m.kind === "omission") return "Required action not observed before submission.";
  const a = (m.anchor_event_ids || [])[0];
  const idx = a != null ? f.steps.findIndex(s => (s.event_ids || []).includes(a)) : -1;
  if (idx < 0) return "No captured action detail is available for this moment.";
  return actionDetail(f.steps[idx]) || "No captured action detail is available for this moment.";
}
function actionText(m, f) {
  if (m.kind === "omission") return "The required action was not observed before submission.";
  const a = (m.anchor_event_ids || [])[0];
  const idx = a != null ? f.steps.findIndex(s => (s.event_ids || []).includes(a)) : -1;
  if (idx < 0) return m.summary || "See evidence.";
  const step = f.steps[idx], kindLabel = (step.kind || "action").replace(/_/g, " ");
  const detail = actionDetail(step);
  return detail
    ? "At trace step " + (idx + 1) + ", " + kindLabel + " — " + detail
    : "At trace step " + (idx + 1) + " the agent performed a " + kindLabel + " action; no captured detail is available for it.";
}
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
  if (f.type === "state_transition") return f.resolution_event ? "The failure was later resolved by a strategy change." : "No resolving action was observed before submission — see the Recovery line above.";
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
  // F4: while the selected run (or reviewer) is still loading, the moment
  // cards on screen still belong to the PREVIOUS selection — writing a
  // mutation now would submit for state.runId/state.reviewerKey (already the
  // new selection) using a moment_id that belongs to the old one. Disable
  // every action that writes until the in-flight load settles; "View
  // evidence" is pure navigation over already-displayed data, so it stays
  // available.
  const mkWrite = (label, cls, fn, title, sc) => {
    const loadingTitle = "Loading the selected run — try again once it finishes.";
    const b = mk(label, cls, fn, state.loading ? loadingTitle : title, sc);
    if (state.loading) b.disabled = true;
    return b;
  };
  wrap.append(mk(evidenceActionLabel(moment), "primary", () => focusEvidence(), "Focus the evidence for this moment’s claims.", "E"));
  wrap.append(mkWrite("Agree", "", b => submitFeedback(moment, { kind: "agree" }, b, "Agreement recorded — attribution unchanged"), "Records positive feedback; does not raise attribution."));
  wrap.append(mkWrite("Not decisive", "", b => submitFeedback(moment, { kind: "not_decisive" }, b, "Marked not decisive"), "Preserves the generated record; adds a human annotation."));
  wrap.append(mkWrite("Flag task/verifier", "", b => submitFeedback(moment, { kind: "flag_task_verifier" }, b, "Task/verifier concern flagged"), "Marks a possible task or verifier problem."));
  wrap.append(mkWrite("Correct label", "subtle", () => openCorrect(moment), "Quick relabel to a controlled taxonomy value."));
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

