/**
 * BisonScope Web — application logic.
 */

import { BisonViewer } from "./viewer3d.js";

// ── state ──────────────────────────────────────────────────────────────────
let viewer             = null;
let currentFile        = null;
let lastResult         = null;
let baseSurfaceArea    = 0;
let removedSurfaceArea = 0;
let _loadingDone       = false;
let _loadingTimers     = [];
let _methodsData       = null;
let _activeMethod      = "default";
let _lastPayload       = null;

const API_BASE = (window.BISONSCOPE_API_BASE || "").replace(/\/+$/, "");
const APP_BASE = (() => {
  const path = window.location.pathname;
  const marker = "/create-estimate";
  const markerAt = path.indexOf(marker);
  if (markerAt >= 0) return path.slice(0, markerAt + 1);
  if (path.endsWith("/")) return path;
  return path.slice(0, path.lastIndexOf("/") + 1) || "/";
})();

function apiPath(path) {
  return API_BASE ? `${API_BASE}${path}` : `${APP_BASE.replace(/\/$/, "")}${path}`;
}

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
const viewerWrap        = document.getElementById("viewerContainer");
const routeBanner       = document.getElementById("routeBanner");
const surfaceAreaSection = document.getElementById("surfaceAreaSection");
const rSurfaceArea       = document.getElementById("rSurfaceArea");
const surfaceToggle      = document.getElementById("surfaceToggle");
const deletePlanesRow    = document.getElementById("deletePlanesRow");
const deletePlanesBtn    = document.getElementById("deletePlanesBtn");
const loadingPanel    = document.getElementById("loadingPanel");
const loadingFilename = document.getElementById("loadingFilename");
const loadingStepEls  = Array.from(document.querySelectorAll(".ls-item"));
const methodBtns      = document.getElementById("methodBtns");

const normalizedPath = window.location.pathname.replace(/\/+$/, "");
const isCreateEstimateRoute = normalizedPath.endsWith("/create-estimate");

if (routeBanner) {
  routeBanner.classList.toggle("hidden", !isCreateEstimateRoute);
}

document.title = isCreateEstimateRoute
  ? "BisonScope — Create Estimate"
  : "BisonScope — CFS Estimator";

// ── init viewer ────────────────────────────────────────────────────────────
viewer = new BisonViewer(viewerWrap);

viewer.onSurfaceClick = ({ selectedCount }) => {
  if (deletePlanesBtn) {
    deletePlanesBtn.disabled = selectedCount === 0;
    deletePlanesBtn.textContent = selectedCount > 0
      ? `Delete ${selectedCount} Plane${selectedCount > 1 ? "s" : ""}`
      : "Delete Selected";
  }
};

surfaceToggle?.addEventListener("click", () => {
  const isOn = surfaceToggle.dataset.on === "true";
  const next = !isOn;
  surfaceToggle.dataset.on = next;
  surfaceToggle.textContent = next ? "Hide Surfaces" : "Show Surfaces";
  viewer.setSurfacesVisible(next);
  deletePlanesRow?.classList.toggle("hidden", !next);
});

deletePlanesBtn?.addEventListener("click", () => {
  const removed = viewer.deleteSelectedPlanes();
  removedSurfaceArea += removed;
  updateSurfaceArea();
  if (deletePlanesBtn) {
    deletePlanesBtn.disabled = true;
    deletePlanesBtn.textContent = "Delete Selected";
  }
});

// ── drag-and-drop ──────────────────────────────────────────────────────────
dropZone.addEventListener("click", (event) => {
  if (event.target !== fileInput) fileInput.click();
});

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

fileInput.addEventListener("click", () => {
  fileInput.value = "";
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
  cancelLoading();
  setStatus("IFC ready. Click Run Estimate to calculate.");
}

// ── actions ────────────────────────────────────────────────────────────────
estimateBtn.addEventListener("click", runEstimate);
detailsToggle?.addEventListener("click", toggleDetails);
exportBtn?.addEventListener("click", exportJson);

methodBtns?.addEventListener("click", (e) => {
  const btn = e.target.closest(".method-btn");
  if (!btn) return;
  switchMethod(btn.dataset.method);
});

function updateSurfaceArea() {
  const area = Math.max(0, baseSurfaceArea - removedSurfaceArea);
  if (rSurfaceArea) rSurfaceArea.textContent = `${fmt(area)} sq ft`;
}

function _syncMethodBtns() {
  methodBtns?.querySelectorAll(".method-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.method === _activeMethod);
  });
}

function switchMethod(method) {
  if (method !== "default" && !_methodsData?.[method]) return;
  _activeMethod = method;
  removedSurfaceArea = 0;
  const surfOn = surfaceToggle?.dataset.on === "true";
  if (method === "default") {
    baseSurfaceArea = _lastPayload?.external_surface_sqft || 0;
    viewer.loadSurfacePlanes(_lastPayload?.surface_planes || []);
  } else {
    const m = _methodsData[method];
    baseSurfaceArea = m.sqft;
    viewer.loadSurfacePlanes(m.planes || []);
  }
  viewer.setSurfacesVisible(surfOn);
  if (deletePlanesBtn) { deletePlanesBtn.disabled = true; deletePlanesBtn.textContent = "Delete Selected"; }
  updateSurfaceArea();
  _syncMethodBtns();
}

async function loadPreview() {
  if (!currentFile) return;

  try {
    const fd = new FormData();
    fd.append("file", currentFile, currentFile.name);
    const res     = await fetch(apiPath("/api/geometry"), { method: "POST", body: fd });
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.detail || "Preview failed.");

    finishLoading();

    const info = viewer.loadGeometry(payload);

    _lastPayload       = payload;
    _methodsData       = payload.methods || null;
    _activeMethod      = "default";
    baseSurfaceArea    = payload.external_surface_sqft || 0;
    removedSurfaceArea = 0;
    viewer.loadSurfacePlanes(payload.surface_planes || []);
    if (surfaceToggle) { surfaceToggle.dataset.on = "false"; surfaceToggle.textContent = "Show Surfaces"; }
    viewer.setSurfacesVisible(false);
    deletePlanesRow?.classList.add("hidden");
    if (deletePlanesBtn) { deletePlanesBtn.disabled = true; deletePlanesBtn.textContent = "Delete Selected"; }
    if (payload.type === "ifc" && surfaceAreaSection) {
      surfaceAreaSection.classList.remove("hidden");
      updateSurfaceArea();
      _syncMethodBtns();
    } else if (surfaceAreaSection) {
      surfaceAreaSection.classList.add("hidden");
    }

    if (info.type === "dxf") {
      setStatus(`${currentFile.name} loaded.`);
    } else {
      setStatus(`${info.groups} element group(s) loaded.`);
    }
  } catch (err) {
    cancelLoading();
    const msg = err?.message || "Preview failed.";
    if (msg.includes("3D geometry preview is disabled") || msg.includes("3D preview is disabled")) {
      setStatus("IFC ready. 3D preview is unavailable here; click Run Estimate to calculate.");
    } else {
      setStatus(`Preview failed: ${msg}`, true);
    }
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

    const res  = await fetch(apiPath("/api/estimate"), { method: "POST", body: fd });
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

// ── loading panel ──────────────────────────────────────────────────────────
function _applyStep(n) {
  loadingStepEls.forEach((el, i) => {
    el.dataset.state = i < n ? "done" : i === n ? "active" : "pending";
  });
}

function startLoading(filename) {
  _loadingDone = false;
  _loadingTimers.forEach(clearTimeout);
  _loadingTimers = [];
  loadingFilename.textContent = filename;
  loadingPanel.classList.remove("hidden");
  _applyStep(0);
  _loadingTimers.push(setTimeout(() => { if (!_loadingDone) _applyStep(1); }, 900));
  _loadingTimers.push(setTimeout(() => { if (!_loadingDone) _applyStep(2); }, 3200));
}

function finishLoading() {
  _loadingDone = true;
  _loadingTimers.forEach(clearTimeout);
  _applyStep(3);
  setTimeout(() => {
    _applyStep(loadingStepEls.length);
    setTimeout(() => loadingPanel.classList.add("hidden"), 500);
  }, 350);
}

function cancelLoading() {
  _loadingDone = true;
  _loadingTimers.forEach(clearTimeout);
  loadingPanel.classList.add("hidden");
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
