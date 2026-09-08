"use strict";

const $ = (sel, root = document) => (sel[0] === "#" && root === document) ? document.getElementById(sel.slice(1)) : root.querySelector(sel);
const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
async function api(path) {
  const r = await fetch(path, { headers: { accept: "application/json" } });
  if (!r.ok) { const e = new Error(path + " -> " + r.status); e.status = r.status; throw e; }
  return r.json();
}
async function apiPost(path, body) {
  const r = await fetch(path, { method: "POST", headers: { accept: "application/json", "content-type": "application/json" }, body: JSON.stringify(body) });
  let data = null; try { data = await r.json(); } catch (_) {}
  if (!r.ok) { const e = new Error(path + " -> " + r.status); e.status = r.status; e.data = data; throw e; }
  return data;
}
function uid() { return "m_" + Math.random().toString(36).slice(2) + Date.now().toString(36); }
