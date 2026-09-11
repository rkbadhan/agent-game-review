"use strict";

// --- theme -------------------------------------------------------------------
$("#theme-toggle").addEventListener("click", () => {
  const cur = document.documentElement.getAttribute("data-theme");
  const dark = cur ? cur === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
  const next = dark ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  // Remember the choice so it survives a reload (applied early in index.html).
  try { localStorage.setItem("agr-theme", next); } catch (e) {}
});

// --- outcome narrative ---------------------------------------------------
// F3: the ONE place that turns the backend's reconciled outcome (rv.outcome,
// built by agr/checks.py:outcome() from each check's effective_status — which
// already excludes superseded checks and demotes stale passes / unknown
// requirement coverage to UNDETERMINED) into plain-language copy. The header
// verdict (render.js) and the Overview headline (chapters.js) both call this
// instead of each re-deriving its own verdict from raw check.status, which is
// what previously let a superseded FAILED check, a stale PASSED check, or an
// unestablished requirement coverage still read as "Passed." on one surface
// while rv.outcome.status correctly said UNDETERMINED on another.
function outcomeNarrative(rv) {
  if (!rv) return null;
  const o = rv.outcome || {}, checks = rv.checks || [];
  const byId = {}; checks.forEach(c => { byId[c.check_id] = c; });
  const nameOf = id => { const c = byId[id]; return c ? id + " (" + c.name + ")" : id; };
  if (rv.review_mode === "not_reviewable")
    return { tone: "warn", headline: "Not reviewable —",
      detail: "the captured evidence is insufficient for a trustworthy review." };
  if (o.status === "UNVERIFIED" || !checks.length)
    return { tone: "warn", headline: "Unverified —",
      detail: "no verifier checks were recorded, so nothing here should be read as a pass." };
  if (o.status === "FAILED") {
    const failed = o.failed_checks || [];
    return { tone: "fail", headline: "Failed —",
      detail: failed.length + " of " + (o.total ?? checks.length) + " current checks failed: "
        + failed.map(nameOf).join("; ") + "." };
  }
  if (o.status === "UNDETERMINED") {
    const undetermined = o.undetermined_checks || [];
    let detail;
    if (o.coverage_unknown)
      detail = "every current check succeeded, but the task declared no requirements to check " +
        "coverage against — requirement coverage is unestablished, not proven.";
    else if ((o.coverage_gaps || []).length)
      detail = "every current check succeeded, but " + o.coverage_gaps.length +
        " declared requirement(s) were never mapped to a check.";
    else if (undetermined.length)
      detail = "no current check failed, but " + undetermined.map(nameOf).join(", ") + " recorded no verdict.";
    else
      detail = "no current check failed, but the result could not be established as a clean pass.";
    return { tone: "warn", headline: "Undetermined —", detail };
  }
  if (o.status === "PASSED")
    // Every check.effective_status was "passed" (superseded checks
    // excluded, stale passes and unknown coverage already demoted above).
    return { tone: "pass", headline: "Passed —",
      detail: "all " + (o.total ?? checks.length) + " requirement " +
        ((o.total ?? checks.length) === 1 ? "check" : "checks") + " evidenced." };
  // Review of PR #64: an rv.outcome that is missing or carries a status
  // this function does not recognize must NEVER fall through to "Passed" by
  // default — that would silently re-introduce the "reads as pass when not
  // proven" bug F3 exists to close. The safe default for anything unproven
  // is the same cautious copy as UNDETERMINED, not a pass.
  return { tone: "warn", headline: "Undetermined —",
    detail: "the run's outcome could not be established as a clean pass." };
}

// --- toast -------------------------------------------------------------------
let toastTimer = null;
function toast(msg) { const n = $("#toast"); n.textContent = msg; n.classList.add("show");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => n.classList.remove("show"), 1800); }
function flash(node, msg) { const old = node.textContent; node.textContent = msg; node.classList.add("flashed");
  setTimeout(() => { node.textContent = old; node.classList.remove("flashed"); }, 1500); }

// --- shared display helpers (Runs + Patterns) --------------------------------
// A readable label for a run id, so a table cell can show a task name and a
// short, copyable id fragment instead of a full "namespace/task__uuid" string
// that wraps into a stack of unreadable fragments. Convention: the LAST
// "__"-separated segment of the final path component is the run's own random
// suffix (a uuid, or a short synthetic id); everything before it is the task
// name. A run id with no "__" has no separable suffix, so the whole thing is
// the task name and there is no short id to show.
function runIdParts(runId) {
  const last = (runId || "").split("/").pop() || runId || "";
  const bits = last.split("__");
  if (bits.length < 2) return { task: last, id: null };
  return { task: bits.slice(0, -1).join("__"), id: bits[bits.length - 1] };
}
function shortRunId(runId, n) {
  const { id } = runIdParts(runId);
  if (!id) return null;
  return id.length > (n || 8) ? id.slice(0, n || 8) : id;
}
// A small inline "copy" affordance next to a truncated id — copies the FULL
// value (never the shortened display text) so a reader can paste the exact
// run id elsewhere.
function copyButton(text, title) {
  const b = el("button", "copy-btn", "⧉");
  b.type = "button";
  const label = title || "Copy " + text;
  b.title = label; b.setAttribute("aria-label", label);
  b.addEventListener("click", async (e) => {
    e.stopPropagation();
    try { await navigator.clipboard.writeText(text); flash(b, "✓"); }
    catch (err) { toast("Could not copy — clipboard unavailable"); }
  });
  return b;
}
// Runs §item "useful finding summaries": a table row needs a short lead, not
// a naive character slice that can cut a long rendered statement mid-word or
// mid-enumeration ("...ended: terminal_bench_reward, test_ou…"). Takes the
// first sentence (never a trailing ". No agent action was captured..." aside)
// and, only if that alone is still long, trims at the nearest natural
// boundary (", " or ": " or a space) before the limit rather than mid-word.
// The full statement is always what the run's own Overview/moment card shows
// once opened — this is presentation-only, never a different value.
function leadFinding(text, limit) {
  limit = limit || 150;
  if (!text) return text;
  // A ". "/"! "/"? " is only a sentence boundary when followed by a
  // capitalized word — a bare split(". ")[0] cut through "e.g. missing dep"
  // (and any other lowercase-continuation abbreviation) as if it ended the
  // sentence there. No match (e.g. a single-sentence statement with nothing
  // after its own final period) keeps the whole text for the boundary-slice
  // trimming below, same as before.
  const boundary = /[.!?](?=\s+[A-Z])/.exec(text);
  let s = (boundary ? text.slice(0, boundary.index + 1) : text).trim();
  if (!/[.!?]$/.test(s)) s += ".";
  if (s.length <= limit) return s;
  const slice = s.slice(0, limit);
  const cut = Math.max(slice.lastIndexOf(", "), slice.lastIndexOf(": "), slice.lastIndexOf(" "));
  return (cut > 40 ? slice.slice(0, cut) : slice) + "…";
}
// Compact large-number formatting (Patterns §item 4): 203147393 -> "203.1M".
// Never applied to a value that could be mistaken for an exact count a reader
// would want to compare precisely (checks passed, run counts stay exact) —
// only to large aggregate magnitudes like token usage.
const _COMPACT_SCALES = [[1e3, "K"], [1e6, "M"], [1e9, "B"], [1e12, "T"]];
function fmtCompact(n) {
  if (n == null) return "—";
  const sign = n < 0 ? "-" : "";
  const abs = Math.abs(n);
  let i = -1;
  for (let k = 0; k < _COMPACT_SCALES.length; k++) if (abs >= _COMPACT_SCALES[k][0]) i = k;
  if (i < 0) return sign + String(abs);
  // Rounding to one decimal can round up INTO the next scale (999950 would
  // otherwise show "1000.0K" instead of "1.0M") — escalate the scale when
  // that happens rather than ever showing a 4-digit mantissa.
  while (i < _COMPACT_SCALES.length - 1
    && Math.round((abs / _COMPACT_SCALES[i][0]) * 10) / 10 >= 1000) i++;
  const v = (abs / _COMPACT_SCALES[i][0]).toFixed(1).replace(/\.0$/, "");
  return sign + v + _COMPACT_SCALES[i][1];
}
