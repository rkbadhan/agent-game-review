"use strict";

// --- §4.19 controlled vocabulary --------------------------------------------
const LABELS = {
  review_mode: {
    deterministic_only: ["Deterministic review", "Mechanically derived facts and templates; AI interpretation was not run"],
    model_enriched: ["AI-enriched review", "Deterministic baseline supplemented by model-assisted interpretation and validated moment selection"],
    not_reviewable: ["Not reviewable", "Captured evidence is insufficient for a trustworthy review"],
  },
  attribution: {
    direct: ["Direct result", "The action directly produced the captured state consumed by the verifier"],
    dependency_linked: ["Linked to outcome", "On the validated evidence path to the outcome; replay has not established causality"],
    counterfactually_supported: ["Replay-supported", "A controlled branch changing this action changed the relevant outcome"],
    hypothesized: ["Hypothesis", "A plausible explanation not established by the available evidence"],
  },
  evidence: {
    strong: ["Strong", "Required facts mechanically validated from sufficiently complete capture"],
    moderate: ["Moderate", "Facts validated, but some observability is partial or indirect"],
    limited: ["Limited", "Materially depends on provisional interpretation or incomplete capture"],
  },
  disposition: {
    diagnosis_accepted: ["Diagnosis accepted", "The generated diagnosis was reviewed and accepted"],
    corrected: ["Corrected", "A human correction was recorded for this run"],
    task_or_verifier_issue: ["Task/verifier issue", "The concern lies in the task or verifier, not the agent"],
    needs_followup: ["Needs follow-up", "Flagged for further investigation"],
    no_action: ["No action", "Reviewed; no action required"],
  },
  progress: {
    unreviewed: ["Unreviewed", "No human has opened this review"],
    in_progress: ["In progress", "Opened or edited, not yet dispositioned"],
    handled: ["Handled", "An explicit disposition was recorded"],
  },
  opportunity_status: {
    measured: ["Measured", "The behaviour was evaluated from validated evidence in this window"],
    not_measured: ["No opportunity", "The relevant opportunity never occurred"],
    not_reviewed: ["Not reviewed", "The opportunity occurred, but this review mode did not evaluate the behaviour"],
    not_evaluable: ["Not evaluable", "The opportunity occurred, but required observability was missing from the capture"],
  },
  contract_status: {
    not_observed: ["No evidence yet", "The contract item has not yet been established or contradicted"],
    in_progress: ["In progress", "Work toward the item is captured, but completion is not established"],
    evidenced_satisfied: ["Evidenced complete", "Captured evidence supports completion of this item"],
    at_risk: ["At risk", "Captured evidence indicates the item may remain unresolved"],
    evidenced_violated: ["Contradicted", "Captured evidence shows the item was violated"],
    unknown: ["Evidence unavailable", "Available capture cannot establish the item state"],
  },
};
function vocab(kind, value) { const m = (LABELS[kind] || {})[value]; return m ? { label: m[0], tip: m[1] } : { label: value || "—", tip: value || "" }; }

// --- §4.19 compact glossary --------------------------------------------------
// A plain-language glossary of the controlled vocabulary, reachable from every
// surface via the app bar. It is built from the same LABELS map the badges use,
// so the glossary and the inline tooltips can never drift apart.
const GLOSSARY_GROUPS = [
  ["review_mode", "Review mode"],
  ["evidence", "Evidence grade"],
  ["attribution", "Cause — how far a cause is established (attribution)"],
  ["opportunity_status", "Ability opportunity"],
  ["contract_status", "Contract item state"],
  ["disposition", "Disposition"],
  ["progress", "Review progress"],
];
function buildGlossary() {
  const host = $("#glossary-body"); if (!host) return;
  host.textContent = "";
  for (const [kind, title] of GLOSSARY_GROUPS) {
    const group = LABELS[kind]; if (!group) continue;
    const sec = el("section", "gloss-group");
    sec.append(el("h3", null, title));
    const dl = el("dl", "gloss-list");
    for (const key of Object.keys(group)) {
      const [label, tip] = group[key];
      dl.append(el("dt", null, label), el("dd", null, tip));
    }
    sec.append(dl); host.append(sec);
  }
}
function openGlossary() { buildGlossary(); openModal("#glossary-modal"); }

// --- §4.19 orientation overlay ------------------------------------------------
// A short overlay explaining review mode, evidence grade, attribution, and the
// fact/interpretation split. T1: it no longer blocks first use — it opens only
// on demand, from Help's "What do these terms mean?" — and the glossary
// carries the same content afterwards.
function dismissIntro() { closeModals(); }
