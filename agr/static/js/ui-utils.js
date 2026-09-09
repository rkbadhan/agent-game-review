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

// --- toast -------------------------------------------------------------------
let toastTimer = null;
function toast(msg) { const n = $("#toast"); n.textContent = msg; n.classList.add("show");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => n.classList.remove("show"), 1800); }
function flash(node, msg) { const old = node.textContent; node.textContent = msg; node.classList.add("flashed");
  setTimeout(() => { node.textContent = old; node.classList.remove("flashed"); }, 1500); }
