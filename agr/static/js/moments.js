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
  const prev = el("button", "nav-arrow"); prev.append(icon("arrow-left")); prev.setAttribute("aria-label", "Previous key moment"); prev.addEventListener("click", () => moveMoment(-1));
  const next = el("button", "nav-arrow"); next.append(icon("arrow-right")); next.setAttribute("aria-label", "Next key moment"); next.addEventListener("click", () => moveMoment(1));
  nav.append(prev, next); top.append(nav);
  card.append(top);

  const summary = el("div", "moment-summary");
  const heading = el("div", "moment-heading");
  // R3: the generated "Grounded in quoted run evidence (evt_…)" boilerplate
  // reads as a headline-sized list of event ids — display-only, lead with the
  // run's verdict sentence instead; the anchors row below already links the
  // same evidence ids, so nothing else needs to change here.
  // R3 (coordinator review 2, item 2): the moment card's own headline, not
  // the run's overall verdict — see chapters.js's momentHeadline() comment.
  const boilerIds = groundedBoilerplateIds(moment.summary);
  const headingText = boilerIds ? momentHeadline(moment, rv) : moment.summary;
  heading.append(el("h2", null, headingText));
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
  // GR-3: the idea category and the basis behind the moment's label. The
  // category is derived by the system from the moment's tags (never named by
  // the model), and `basis` says whether the support is a mechanical fact or a
  // model interpretation — so a label never reads as more certain than it is.
  // A moment can cover more than one category (e.g. a recovery that also
  // contains a bad query). EVERY category renders with its own basis, so what
  // the card shows matches what the coverage count reports — a secondary
  // category is never counted but left invisible.
  const catDetails = (moment.category_details || []).length
    ? moment.category_details
    : (moment.category_label ? [{ label: moment.category_label, basis: moment.basis }] : []);
  if (catDetails.length || moment.label) {
    const row = el("div", "moment-category");
    row.append(el("span", "moment-anchors-label", catDetails.length ? "Category" : "Label"));
    if (catDetails.length) {
      for (const c of catDetails) {
        const chip = el("span", "moment-category-chip");
        chip.append(el("span", "moment-category-name", c.label));
        if (c.basis) chip.append(el("span", "moment-category-basis", c.basis + " basis"));
        row.append(chip);
      }
      if (moment.label) row.append(el("span", "moment-category-label", moment.label));
    } else {
      // A neutral, mechanically supported label with NO category (e.g. the
      // submission detector's "Requirement unresolved at submission"): it must
      // still carry its own basis, so the basis is never dropped just because
      // there is no category chip to hang it on.
      const chip = el("span", "moment-category-chip");
      chip.append(el("span", "moment-category-label", moment.label));
      if (moment.basis) chip.append(el("span", "moment-category-basis", moment.basis + " basis"));
      row.append(chip);
    }
    summary.append(row);
  }
  // GR-3: a failing tool call is linked to this run's argument-shape statistics
  // deterministically — showing a real count, never a model-stated one.
  const shape = moment.argument_shape_link;
  if (shape) {
    const row = el("div", "moment-shape");
    row.append(el("span", "moment-anchors-label", "Query shape"));
    const share = Math.round((shape.share || 0) * 100);
    row.append(el("span", null, shape.tool + " · shape " + (shape.shape_index + 1) +
      " · " + shape.count + " of " + shape.total_failing_calls + " failing calls (" + share + "%)"));
    summary.append(row);
  }
  const anchors = (moment.anchor_event_ids || []).filter(Boolean);
  if (anchors.length) {
    const row = el("div", "moment-anchors");
    row.append(el("span", "moment-anchors-label", "Evidence"));
    row.append(evidenceCell(anchors, f));
    summary.append(row);
  }
  // P2 (§B1): the finding states what its evidence does not establish, on the
  // card itself rather than only inside the expanded breakdown.
  if ((moment.limits || []).length) {
    const row = el("div", "moment-limits");
    row.append(el("span", "moment-anchors-label", "Limits"));
    const list = el("ul");
    for (const limit of moment.limits) list.append(el("li", null, limit));
    row.append(list);
    summary.append(row);
  }
  card.append(summary);

  const detail = el("details", "moment-detail disclosure");
  if (state.expandedMoments.has(moment.moment_id)) detail.open = true;
  detail.append(el("summary", null, "Full breakdown"));
  detail.addEventListener("toggle", () => {
    if (detail.open) state.expandedMoments.add(moment.moment_id);
    else state.expandedMoments.delete(moment.moment_id);
  });
  const body = el("div", "moment-body");

  body.append(mcBlock("Situation", "validated fact", situationText(moment), ""));
  // Root-cause follow-up: the failure's own raw text, collapsed by default,
  // right under the Situation line it can explain — present whenever the
  // fact carries one, not only for a fallback signature, since a confident
  // diagnostic can still benefit from the surrounding context (same choice
  // fleet.js already made for its own representative-episode disclosure).
  // facts[0], not a find() for the state_transition fact specifically — the
  // SAME fact situationText/toolFailureIdentity/recoveryText already read,
  // so this disclosure can never describe a different fact than the
  // headline above it when a moment carries more than one fact.
  const fact0 = (moment.facts || [])[0] || {};
  if (fact0.type === "state_transition" && fact0.raw_failure_text) body.append(rawFailureTextDetail(fact0));
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
  } else {
    // T2: a deterministic card has no AI interpretation at all — one combined
    // absent block, not two repeated "Not generated" blocks for Likely impact
    // and Better action separately.
    body.append(mcBlock("AI interpretation", "interpretation",
      "Not generated — this is a deterministic review.", "absent"));
  }
  // GR-2: alternatives are rendered independently of model enrichment — the
  // store-backed observed/validated kinds can attach to a deterministic moment,
  // and their absence must not hide them behind enrichment_source (PR #91
  // review). An enriched moment always gets the block (legacy better_action
  // fallback / explicit "none"); a deterministic one only when it has any.
  if ((moment.alternatives || []).length || moment.enrichment_source) {
    body.append(alternativesBlock(moment));
  }
  detail.append(body);
  card.append(detail);

  // Eval lesson (T1 / formerly its own chapter, §4.11): attached to the moment
  // that recommends it, so a reader finds it where the finding is instead of a
  // separate tab. Collapsed by default — most moments have none.
  if (moment === lessonMoment()) {
    const det = el("details", "moment-lesson disclosure");
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
// GR-2: the three kinds of grounded alternative, kept apart and each shown
// under its ACTUAL status. A suggestion carries its information cutoff (the
// decision it replaces and the information available before it); an observed
// alternative names how the passing sibling differs; a validated one shows the
// linked experiment's benefit, outcome checks and comparison limits. A
// "Validated by …" label is only ever rendered from a validated status the
// backend set — never inferred here.
function alternativesBlock(moment) {
  const alts = moment.alternatives || [];
  if (!alts.length) {
    if (!moment.better_action) {
      return mcBlock("Alternatives", "none", "None generated for this moment.", "absent");
    }
    const tested = moment.attribution_ceiling === "counterfactually_supported";
    return mcBlock("Better action", tested ? "replay-tested" : "hypothesis · not replay-tested",
      moment.better_action, "hypo");
  }
  const wrap = el("div", "mc-alternatives");
  wrap.append(el("div", "mc-alt-heading", "Alternatives"));
  for (const a of alts) {
    const cls = a.kind === "suggested" ? "hypo" : a.kind === "observed" ? "action" : "consequence";
    const block = el("div", "mc-block mc-alt " + cls);
    const label = el("div", "mc-label");
    label.append(document.createTextNode(a.label || a.kind));
    label.append(el("span", null, alternativeSub(a)));
    block.append(label);
    // One body cell: mc-block is a 2-column grid, so every line of an
    // alternative belongs in column 2, not as a new grid row.
    const altBody = el("div", "mc-alt-body");
    altBody.append(el("p", "mc-text", a.proposal || ""));
    if (a.kind === "suggested") {
      const infoIds = (a.information_available || []).map(i => i.event_id).filter(Boolean);
      const rdc = a.replaces_decision || {};
      altBody.append(el("p", "mc-alt-cutoff",
        "Replaces " + (rdc.event_id || "?")
        + (rdc.description ? " (" + rdc.description + ")" : "")
        + " · information available before it: " + (infoIds.length ? infoIds.join(", ") : "none recorded")));
    }
    if (a.kind === "observed" && a.sibling_diff) {
      altBody.append(el("p", "mc-alt-diff", "Differs from this run — " + diffText(a.sibling_diff)));
    }
    if (a.kind === "validated" && a.validation) {
      const v = a.validation, parts = [];
      if (v.stated_benefit) parts.push("Benefit: " + v.stated_benefit);
      if ((v.outcome_checks || []).length) parts.push("Outcome checks: " + v.outcome_checks.join("; "));
      if ((v.comparison_limits || []).length) parts.push("Comparison limits: " + v.comparison_limits.join("; "));
      if (parts.length) altBody.append(el("p", "mc-alt-validation", parts.join(" · ")));
    }
    for (const lim of (a.limits || [])) altBody.append(el("p", "mc-alt-limit", "Limit: " + lim));
    block.append(altBody);
    wrap.append(block);
  }
  return wrap;
}

function alternativeSub(a) {
  if (a.kind === "suggested") return "proposal · " + (a.source || "model");
  if (a.kind === "observed") return "example · " + (a.source || "comparable passing run");
  const status = (a.validation || {}).status || "proposed";
  return status.replace(/_/g, " ");
}

function diffText(diff) {
  return Object.keys(diff).map(k => {
    const d = diff[k] || {};
    const fv = d.failed_run == null ? "unset" : d.failed_run;
    const pv = d.passing_run == null ? "unset" : d.passing_run;
    return k + ": " + fv + " → " + pv;
  }).join(", ");
}

function mcBlock(label, sub, text, kind) {
  const b = el("div", "mc-block" + (kind ? " " + kind : ""));
  const l = el("div", "mc-label"); l.append(document.createTextNode(label)); l.append(el("span", null, sub)); b.append(l);
  b.append(el("p", "mc-text", text)); return b;
}
// Root-cause follow-up: the same collapsed "Raw failure text" disclosure
// fleet.js's representativeEpisodesBlock() already renders for a fleet-wide
// pattern, here for the single run this moment belongs to — the failure
// event's own text, verbatim, not just the one line error_signature/
// failure_diagnostic selected.
function rawFailureTextDetail(f) {
  const wrap = el("div", "moment-raw-failure");
  const raw = el("details", "disclosure-inline");
  raw.append(el("summary", null, "Raw failure text"));
  raw.append(el("pre", "moment-raw-failure-text disclosure-inline-text", f.raw_failure_text
    + (f.raw_failure_text_truncated ? "\n…[truncated]" : "")));
  wrap.append(raw);
  return wrap;
}
// Root-cause follow-up: a one-line lead-in clipped from a fallback_last_
// nonempty fact's raw_failure_text (RecoveryEpisode.raw_failure_text — the
// failure event's own text, unselected; fleet.js already shows the full
// text under a "Raw failure text" disclosure for the same reason). Used only
// when failure_diagnostic/error_signature are both the opaque fallback
// tier's single, possibly-wrong line — this is the only detail left in that
// case, so toolFailureIdentity falls back to it rather than showing nothing.
// Clipped to its first line and a short length, mirroring agr.recovery's
// own raw_failure_lead() (RAW_FAILURE_LEAD_LIMIT there == 160 here — keep
// these two in sync) — a whole traceback quoted inline in a one-line summary
// is unreadable, and the full text is still one click away via
// rawFailureTextDetail below. The line split matches Python's str.
// splitlines() separator set (not just "\n") so the SAME raw_failure_text
// never picks a different first line here than it does server-side.
const RAW_FAILURE_LEAD_LIMIT = 160;
const _LINE_SPLIT = new RegExp("\\r\\n|[\\n\\r\\v\\f\\x1c\\x1d\\x1e\\x85\\u2028\\u2029]");
function rawFailureLead(raw) {
  if (!raw) return null;
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const line = trimmed.split(_LINE_SPLIT)[0];
  if (!line) return null;
  return line.length <= RAW_FAILURE_LEAD_LIMIT ? line : line.slice(0, RAW_FAILURE_LEAD_LIMIT - 1) + "…";
}
// The (tool, diagnostic) identity of a state_transition fact — the same
// identity agr.fleet groups recovery episodes by (item 32 follow-up) — or
// null when the fact carries neither. `diag` falls back to the fact's own
// raw_failure_text for an opaque fallback signature (no traceback, no
// recognised diagnostic marker: the fleet view's "Unclassified" case) —
// showing the ONE line the mechanical selector fell back to as if it
// identified the failure would be misleading, but the failure's own raw
// text underneath it is still something a reader can root-cause from.
function toolFailureIdentity(f) {
  if (!f || f.type !== "state_transition") return null;
  const diag = f.failure_diagnostic || f.error_signature;
  const usable = !!diag && f.error_signature_basis !== "fallback_last_nonempty";
  return { tool: f.tool || null, diag: usable ? diag : rawFailureLead(f.raw_failure_text) };
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
  // P0-5: read-only demo mode hides write controls outright rather than
  // showing a button that would just come back with a 403 — the server
  // enforces the actual restriction independently (agr/api.py).
  const mkWrite = (label, cls, fn, title, sc) => {
    if (state.readOnly) return null;
    const loadingTitle = "Loading the selected run — try again once it finishes.";
    const b = mk(label, cls, fn, state.loading ? loadingTitle : title, sc);
    if (state.loading) b.disabled = true;
    return b;
  };
  wrap.append(mk(evidenceActionLabel(moment), "primary", () => focusEvidence(), "Focus the evidence for this moment’s claims.", "E"));
  for (const b of [
    mkWrite("Agree", "", b => submitFeedback(moment, { kind: "agree" }, b, "Agreement recorded — attribution unchanged"), "Records positive feedback; does not raise attribution."),
    mkWrite("Not decisive", "", b => submitFeedback(moment, { kind: "not_decisive" }, b, "Marked not decisive"), "Preserves the generated record; adds a human annotation."),
    mkWrite("Flag task/verifier", "", b => submitFeedback(moment, { kind: "flag_task_verifier" }, b, "Task/verifier concern flagged"), "Marks a possible task or verifier problem."),
    mkWrite("Correct label", "subtle", () => openCorrect(moment), "Quick relabel to a controlled taxonomy value."),
  ]) { if (b) wrap.append(b); }
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

