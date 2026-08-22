"use strict";

// --- §4.16 compare surface --------------------------------------------------
//   Side-by-side view of two reviews of the same capture. The backend (§6.12)
//   aligns moments by anchor overlap into matched / added / removed / redundant
//   pairs with field-level diffs; this view renders those pairs reusing the
//   same moment-card blocks as the Key-moments chapter so the visual language
//   is identical. Shown only when two or more reviews exist; otherwise the
//   Compare control stays disabled with an honest tooltip.
const REVIEW_LABELS = { deterministic: "Deterministic baseline" };
function reviewerLabel(key) { return REVIEW_LABELS[key] || (key && key.startsWith("model:") ? key.slice(6) : key); }
async function renderCompare(main) {
  const avail = (state.review && state.review.available_reviews) || [];
  if (avail.length < 2) {
    main.append(el("div", "empty", "Compare needs a second review of this run (e.g. a model-reviewer pass)."));
    return;
  }
  // Default pair: deterministic baseline on the left, the most-enriched (first
  // non-deterministic) review on the right — the "baseline → model" reading order.
  // A pair carried in from a shared link is honoured only if both keys still
  // exist on this capture; otherwise it falls back to the default rather than
  // asking the API for a review that is not there.
  let left = state.compare && state.compare.left, right = state.compare && state.compare.right;
  if (!avail.includes(left) || !avail.includes(right)) {
    left = avail.includes("deterministic") ? "deterministic" : avail[0];
    right = avail.find(k => k !== left) || avail[0];
    state.compare = { left, right };
    syncUrl();
  }

  const wrap = el("div", "section");
  wrap.append(el("div", "subline", "Loading comparison…")); main.append(wrap);
  let cmp; try {
    cmp = await api("/runs/" + encodeURIComponent(state.runId) +
      "/compare?left=" + encodeURIComponent(left) + "&right=" + encodeURIComponent(right));
  } catch (e) { wrap.textContent = ""; wrap.append(el("div", "empty", "Failed: " + e.message)); return; }
  wrap.textContent = "";

  // Reviewer picker + summary counts.
  const head = el("div", "compare-head");
  const pick = el("div", "compare-pickers");
  pick.append(el("span", "eyebrow", "Left"), pickerSelect(avail, left, "left"),
    el("span", "eyebrow", "Right"), pickerSelect(avail, right, "right"));
  head.append(pick);
  const c = cmp.counts || {};
  const stats = el("div", "compare-stats");
  for (const [k, label] of [["matched","Matched"],["added","Added"],["removed","Removed"],["redundant","Redundant"]]) {
    const d = el("div", "cmp-stat " + k); d.append(el("strong", null, String(c[k] || 0)), el("span", null, label)); stats.append(d);
  }
  head.append(stats);
  wrap.append(head);

  if (!cmp.pairs || !cmp.pairs.length) {
    wrap.append(el("div", "empty", "Both reviews produced identical moments — no differences to show."));
    return;
  }

  const list = el("div", "compare-list");
  for (const p of cmp.pairs) list.append(renderComparePair(p, cmp.left_key, cmp.right_key));
  wrap.append(list);
  main.append(wrap);
  renderEvidencePanel();
}
function pickerSelect(avail, selected, side) {
  const s = el("select", "compare-select");
  for (const k of avail) {
    const o = el("option", null, reviewerLabel(k)); o.value = k; if (k === selected) o.selected = true; s.append(o);
  }
  s.addEventListener("change", () => {
    state.compare = state.compare || { left: null, right: null };
    state.compare[side] = s.value;
    render();
  });
  return s;
}
function renderComparePair(p, leftKey, rightKey) {
  const row = el("div", "cmp-pair " + p.status);
  const badge = el("span", "cmp-badge " + p.status, p.status);
  row.append(badge);
  const lc = p.left ? compareMomentCard(p.left, p.status === "removed") : notSurfacedCard();
  const gutter = el("div", "cmp-gutter");
  if (p.status === "matched") {
    const d = p.diffs || {}, sd = p.set_diffs || {};
    if (d.attribution_ceiling && d.attribution_ceiling.changed) {
      const dir = p.attribution_direction || "same";
      gutter.append(el("div", "cmp-delta " + dir, "attribution " + dir + " · " + (d.attribution_ceiling.left || "?") + " → " + (d.attribution_ceiling.right || "?")));
    }
    if (sd.behaviour_tags && sd.behaviour_tags.changed) {
      if (sd.behaviour_tags.added.length) gutter.append(el("div", "cmp-delta up", "+ tags: " + sd.behaviour_tags.added.join(", ")));
      if (sd.behaviour_tags.removed.length) gutter.append(el("div", "cmp-delta down", "− tags: " + sd.behaviour_tags.removed.join(", ")));
    }
    if (d.better_action && d.better_action.changed) gutter.append(el("div", "cmp-delta up", "+ better action"));
    if (d.summary && d.summary.changed) gutter.append(el("div", "cmp-delta", "summary rewritten"));
    if (!gutter.childNodes.length) gutter.append(el("div", "dim", "no field changes"));
  } else {
    // Which side surfaced it — named from the pair actually being compared, not
    // from an assumed baseline→model orientation.
    gutter.append(el("div", "dim", reviewerLabel(p.status === "added" ? rightKey : leftKey) + " only"));
  }
  const rc = p.right ? compareMomentCard(p.right, p.status === "added") : notSurfacedCard();
  row.append(lc, gutter, rc);
  return row;
}
// The side that did not surface this moment. Built by append, not by `el`'s text
// argument — that argument sets textContent, so passing a node stringifies it.
function notSurfacedCard() {
  const c = el("div", "cmp-card empty"); c.append(el("div", "dim", "— not surfaced —")); return c;
}
function compareMomentCard(m, dim) {
  const card = el("div", "cmp-card" + (dim ? " dim" : ""));
  card.append(el("div", "cmp-card-kind", m.kind || "?"));
  card.append(el("p", "cmp-card-summary", m.summary || m.rendered_statement || "(no statement)"));
  const tags = (m.behaviour_tags || []).join(", ");
  if (tags) card.append(el("div", "cmp-card-tags", tags));
  if (m.attribution_ceiling) card.append(el("div", "cmp-card-attr", "attribution: " + m.attribution_ceiling));
  if (m.better_action) card.append(el("div", "cmp-card-better", "better action: " + m.better_action));
  return card;
}

