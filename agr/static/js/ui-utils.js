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
