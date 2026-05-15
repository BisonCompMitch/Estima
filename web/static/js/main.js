/**
 * BisonScope Web — application logic.
 */

import { BisonViewer } from "./viewer3d.js";

// ── state ──────────────────────────────────────────────────────────────────
let viewer      = null;
let currentFile = null;
let lastResult  = null;

// ── DOM refs ───────────────────────────────────────────────────────────────
const dropZone      = document.getElementById("dropZone");
const fileInput     = document.getElementById("fileInput");
const dropLabel     = document.getElementById("dropLabel");
const estimateBtn   = document.getElementById("estimateBtn");
const statusText    = document.getElementById("statusText");
const costSqft      = document.getElementById("costPerSqFt");
const costLinear    = document.getElementById("costPerLinearFt");
const unitSelect    = document.getElementById("unitSelect");
const strictCheck   = document.getElementById("strictMode");
const resultArea    = document.getElementById("resultArea");
const detailsToggle = document.getElementById("detailsToggle");
const detailsBody   = document.getElementById("detailsBody");
const exportBtn     = document.getElementById("exportBtn");
const viewerWrap    = document.getElementById("viewerContainer");
const routeBanner   = document.getElementById("routeBanner");

const isCreateEstimateRoute = window.location.pathname.replace(/\/+$/, "") === "/create-estimate";

if (routeBanner) {
  routeBanner.classList.toggle("hidden", !isCreateEstimateRoute);
}

document.title = isCreateEstimateRoute
  ? "BisonScope — Create Estimate"
  : "BisonScope — CFS Estimator";

// ── init viewer ────────────────────────────────────────────────────────────
viewer = new BisonViewer(viewerWrap);

// ── drag-and-drop ──────────────────────────────────────────────────────────
dropZone.addEventListener("click", () => fileInput.click());

dropZone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropZone.classList.add("drag-over");
});

dropZone.addEventListener("dragleave", (e) => {
  if (!dropZone.contains(e.relatedTarget)) dropZone.classList.remove("drag-over");
});

dropZone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropZone.classList.remove("drag-over");
  const f = e.dataTransfer.files?.[0];
  if (f) handleFile(f);
});

fileInput.addEventListener("change", () => {
  const f = fileInput.files?.[0];
  if (f) handleFile(f);
});

function handleFile(f) {
  const ext = f.name.split(".").pop().toLowerCase();
  if (ext !== "ifc") {
    setStatus("Unsupported file. Use .ifc", true);
    return;
  }
  currentFile = f;
  dropLabel.textContent = f.name;
  estimateBtn.disabled = false;
  setStatus(`Loading ${f.name}…`);
  loadPreview();
}

// ── actions ────────────────────────────────────────────────────────────────
estimateBtn.addEventListener("click", runEstimate);
detailsToggle?.addEventListener("click", toggleDetails);
exportBtn?.addEventListener("click", exportJson);

async function loadPreview() {
  if (!currentFile) return;

  try {
    const fd = new FormData();
    fd.append("file", currentFile, currentFile.name);
    const res     = await fetch("/api/geometry", { method: "POST", body: fd });
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.detail || "Preview failed.");

    const info = viewer.loadGeometry(payload);
    if (info.type === "dxf") {
      setStatus(`${currentFile.name} loaded.`);
    } else {
      setStatus(`${currentFile.name} — ${info.groups} element group(s) loaded.`);
    }
  } catch (err) {
    setStatus(`Preview failed: ${err.message}`, true);
    console.error(err);
  }
}

async function runEstimate() {
  if (!currentFile) return;
  setStatus("Calculating…");
  estimateBtn.disabled = true;

  try {
    const fd = new FormData();
    fd.append("file", currentFile, currentFile.name);
    fd.append("cost_per_sqft",      costSqft.value   || "40");
    fd.append("cost_per_linear_ft", costLinear.value || "2.5");
    fd.append("source_length_unit", unitSelect?.value || "");
    fd.append("strict_mode",        strictCheck?.checked ? "true" : "false");

    const res  = await fetch("/api/estimate", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Estimate failed.");

    lastResult = data;
    renderResult(data);
    setStatus("Estimate complete.");
  } catch (err) {
    setStatus(`Estimate failed: ${err.message}`, true);
    console.error(err);
  } finally {
    estimateBtn.disabled = false;
  }
}

// ── render result ──────────────────────────────────────────────────────────
function renderResult(d) {
  resultArea.classList.remove("hidden");

  document.getElementById("rAreaCost").textContent   = money(d.area_cost);
  document.getElementById("rAreaSqft").textContent   = `${fmt(d.area_sqft)} sq ft`;
  document.getElementById("rLinearCost").textContent = money(d.linear_cost);
  document.getElementById("rLinearFt").textContent   = `${fmt(d.linear_ft)} linear ft`;

  const conf = d.confidence?.overall || "low";
  const confColor = conf === "high" ? "var(--success)" : conf === "medium" ? "var(--warning)" : "var(--danger)";
  const badge = document.getElementById("rConfBadge");
  badge.textContent       = conf.toUpperCase() + " CONFIDENCE";
  badge.style.color       = confColor;
  badge.style.borderColor = confColor;

  document.getElementById("dElements").textContent    = d.framing_element_count > 0 ? fmt(d.framing_element_count) : "—";
  document.getElementById("dAreaBasis").textContent   = d.area_basis   || "—";
  document.getElementById("dLinearBasis").textContent = d.linear_basis || "—";
  document.getElementById("dUnit").textContent        = d.unit         || "—";

  const warns = d.confidence?.warnings || [];
  const warnEl = document.getElementById("dWarnings");
  if (warns.length) {
    warnEl.innerHTML = warns.map(w => `<div class="warn-row">⚠ ${w}</div>`).join("");
    warnEl.classList.remove("hidden");
  } else {
    warnEl.classList.add("hidden");
  }
}

// ── details toggle ─────────────────────────────────────────────────────────
function toggleDetails() {
  const open = !detailsBody.classList.contains("hidden");
  detailsBody.classList.toggle("hidden", open);
  detailsToggle.textContent = open ? "Details ▾" : "Details ▴";
}

// ── helpers ────────────────────────────────────────────────────────────────
function setStatus(msg, isError = false) {
  statusText.textContent = msg;
  statusText.style.color = isError ? "var(--danger)" : "var(--muted)";
}

function money(n) {
  return Number(n || 0).toLocaleString(undefined, {
    style: "currency", currency: "USD", maximumFractionDigits: 0,
  });
}

function fmt(n) {
  return Number(n || 0).toLocaleString(undefined, { maximumFractionDigits: 1 });
}

function exportJson() {
  if (!lastResult) return;
  const blob = new Blob([JSON.stringify(lastResult, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `bisonscope-${Date.now()}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
}
