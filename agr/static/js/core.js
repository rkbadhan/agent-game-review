"use strict";

// A launch token stays in this tab's session and is removed from the address
// before shared links are generated. Fragments never reach server/proxy logs.
let workspaceSession = "";
try { workspaceSession = sessionStorage.getItem("agr-session") || ""; } catch (_) {}
function consumeLaunchSession() {
  const launchSession = new URLSearchParams(location.hash.slice(1)).get("agr-session");
  if (!launchSession) return false;
  workspaceSession = launchSession;
  try { sessionStorage.setItem("agr-session", workspaceSession); } catch (_) {}
  history.replaceState(history.state, "", location.pathname + location.search);
  return true;
}
consumeLaunchSession();
window.addEventListener("hashchange", () => { if (consumeLaunchSession()) location.reload(); });
function sessionHeaders(headers = {}) {
  return Object.assign({}, headers, workspaceSession ? { authorization: "Bearer " + workspaceSession } : {});
}

const $ = (sel, root = document) => (sel[0] === "#" && root === document) ? document.getElementById(sel.slice(1)) : root.querySelector(sel);
const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
function projectHeaders(path) {
  const headers = sessionHeaders(state.projectId ? { "x-agr-project": state.projectId } : {});
  const runPath = state.runId && "/runs/" + encodeURIComponent(state.runId);
  if (state.captureId && runPath && path && (path === runPath || path.startsWith(runPath + "/") || path.startsWith(runPath + "?"))) headers["x-agr-capture"] = state.captureId;
  return headers;
}
async function requestJSON(path, method = "GET", body) {
  const scoped = !path.startsWith("/projects") && !path.startsWith("/workspace") && !path.startsWith("/sources");
  const headers = Object.assign({ accept: "application/json" }, scoped ? projectHeaders(path) : sessionHeaders());
  if (body !== undefined) headers["content-type"] = "application/json";
  const r = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  let data = null; try { data = await r.json(); } catch (_) {}
  if (!r.ok) { const detail = data && data.detail; const e = new Error(typeof detail === "string" ? detail : detail && detail.message || "The request could not complete. Try again."); e.status = r.status; e.data = data; throw e; }
  return data;
}
async function api(path) { return requestJSON(path); }
async function apiPost(path, body = {}) { return requestJSON(path, "POST", body); }
async function apiPatch(path, body) { return requestJSON(path, "PATCH", body); }
async function apiDelete(path) { return requestJSON(path, "DELETE"); }
function uid() { return "m_" + Math.random().toString(36).slice(2) + Date.now().toString(36); }
