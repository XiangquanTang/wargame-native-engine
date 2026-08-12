"use strict";

const FACTORS = [
  ["V", "base_value"], ["H", "health"], ["W", "weapon"],
  ["A", "ammo"], ["R", "readiness"], ["M", "mobility"],
  ["I", "information"], ["S", "status"],
];

let currentFrameIndex = 0;
let frameCount = 0;
let replayMetadata = null;
let timelineData = null;
let currentFrame = null;
let currentAnalysis = null;
let selectedUnitId = null;
let requestToken = 0;
let playbackTimer = null;
let playbackRate = 1;
let hitTargets = [];

const $ = (id) => document.getElementById(id);

async function requestJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `${response.status} ${response.statusText}`);
  return body;
}

function formatNumber(value, digits = 4) {
  if (value === null || value === undefined) return "UNKNOWN";
  if (typeof value !== "number") return String(value);
  return Number.isInteger(value) ? String(value) : value.toFixed(digits).replace(/0+$/, "").replace(/\.$/, "");
}

async function setCurrentFrameIndex(index) {
  if (!Number.isInteger(index) || index < 0 || index >= frameCount) {
    showError(`Frame index must be between 0 and ${frameCount - 1}`);
    return;
  }
  currentFrameIndex = index;
  const token = ++requestToken;
  $("frame-slider").value = String(currentFrameIndex);
  $("frame-jump").value = String(currentFrameIndex);
  try {
    const [frame, analysis] = await Promise.all([
      requestJson(`/api/frame/${currentFrameIndex}`),
      requestJson(`/api/analysis/${currentFrameIndex}`),
    ]);
    if (token !== requestToken) return;
    currentFrame = frame;
    currentAnalysis = analysis;
    if (selectedUnitId !== null && !analysis.unit_analyses.some((unit) => unit.operator_id === selectedUnitId)) {
      selectedUnitId = null;
    }
    renderCurrentFrame();
  } catch (error) {
    if (token === requestToken) showError(error.message);
  }
}

function renderCurrentFrame() {
  $("frame-index").textContent = String(currentFrameIndex);
  $("cur-step").textContent = formatNumber(currentFrame.time?.cur_step);
  $("playback-position").textContent = `${currentFrameIndex} / ${frameCount - 1}`;
  $("actions").textContent = JSON.stringify(currentFrame.actions ?? [], null, 2);
  $("judge-info").textContent = JSON.stringify(currentFrame.judge_info ?? [], null, 2);
  $("warnings").textContent = JSON.stringify(currentAnalysis.warnings ?? [], null, 2);
  const sideByName = Object.fromEntries(currentAnalysis.side_analyses.map((side) => [side.side, side]));
  $("red-power").textContent = formatNumber(sideByName.red?.effective_power);
  $("blue-power").textContent = formatNumber(sideByName.blue?.effective_power);
  $("normalized-advantage").textContent = formatNumber(currentAnalysis.advantage.normalized_advantage);
  $("raw-advantage").textContent = `raw ${formatNumber(currentAnalysis.advantage.raw_advantage)}`;
  $("advantage-label").textContent = currentAnalysis.advantage.label;
  renderBattlefield();
  renderFactors();
  renderTimeline();
}

function fitCanvas(canvas) {
  const ratio = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(1, Math.round(rect.width * ratio));
  const height = Math.max(1, Math.round(rect.height * ratio));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width; canvas.height = height;
  }
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  return [context, rect.width, rect.height];
}

function renderBattlefield() {
  const canvas = $("battlefield");
  const [context, width, height] = fitCanvas(canvas);
  context.clearRect(0, 0, width, height);
  const units = currentFrame.viewer_units || [];
  hitTargets = [];
  if (!units.length) {
    context.fillStyle = "#8fa6b5"; context.fillText("No units in this frame", 20, 30); return;
  }
  const rows = units.map((unit) => unit.row);
  const columns = units.map((unit) => unit.column);
  const minRow = Math.min(...rows), maxRow = Math.max(...rows);
  const minColumn = Math.min(...columns), maxColumn = Math.max(...columns);
  const margin = 38;
  const x = (column) => margin + ((column - minColumn) / Math.max(1, maxColumn - minColumn)) * (width - margin * 2);
  const y = (row) => height - margin - ((row - minRow) / Math.max(1, maxRow - minRow)) * (height - margin * 2);
  for (const unit of units) {
    const px = x(unit.column), py = y(unit.row);
    const selected = unit.operator_id === selectedUnitId;
    context.beginPath();
    context.arc(px, py, selected ? 12 : 9, 0, Math.PI * 2);
    context.fillStyle = !unit.alive ? "#65717b" : unit.side === "red" ? "#ff5b62" : unit.side === "blue" ? "#45a8ff" : "#a2abb0";
    context.fill();
    if (unit.category === "passenger") {
      context.strokeStyle = "#ffc857"; context.lineWidth = 3; context.stroke();
    }
    if (selected) { context.strokeStyle = "#fff"; context.lineWidth = 2; context.stroke(); }
    if (unit.lose_control || unit.can_to_move === false) {
      context.strokeStyle = "#ffc857"; context.lineWidth = 2;
      context.beginPath(); context.moveTo(px - 10, py - 10); context.lineTo(px + 10, py + 10); context.stroke();
    }
    context.fillStyle = "#e8f1f5"; context.font = "11px ui-monospace";
    context.fillText(String(unit.operator_id), px + 12, py + 4);
    hitTargets.push({ x: px, y: py, radius: 15, operatorId: unit.operator_id });
  }
}

function renderFactors() {
  const unit = currentAnalysis.unit_analyses.find((item) => item.operator_id === selectedUnitId);
  const body = $("factor-table"); body.replaceChildren();
  if (!unit) {
    $("selected-unit").textContent = "点击战场单位";
    $("effective-power").textContent = "—";
    $("unit-validity").textContent = "";
    return;
  }
  $("selected-unit").textContent = `Unit ${unit.operator_id} · ${unit.side.toUpperCase()}`;
  $("effective-power").textContent = formatNumber(unit.effective_power);
  $("unit-validity").textContent = `valid=${unit.valid} · observed ${unit.observed_factor_count}/8`;
  const values = Object.fromEntries(unit.factor_values.map((factor) => [factor.factor_name, factor]));
  for (const [code, name] of FACTORS) {
    const factor = values[name];
    const row = document.createElement("tr");
    const display = factor.status === "UNKNOWN" ? "UNKNOWN" : formatNumber(factor.value);
    const nameCell = document.createElement("td");
    const codeLabel = document.createElement("span");
    codeLabel.className = "factor-code"; codeLabel.textContent = code;
    nameCell.append(codeLabel, document.createElement("br"), document.createTextNode(name));
    const valueCell = document.createElement("td");
    valueCell.className = factor.status === "UNKNOWN" ? "unknown" : "observed";
    valueCell.append(document.createTextNode(display), document.createElement("br"), document.createTextNode(factor.status));
    const evidenceCell = document.createElement("td");
    evidenceCell.textContent = `${factor.formula_id}\nwarnings: ${JSON.stringify(factor.warnings)}\nmissing: ${JSON.stringify(factor.missing_fields)}`;
    row.append(nameCell, valueCell, evidenceCell);
    body.appendChild(row);
  }
}

function renderTimeline() {
  if (!timelineData) return;
  const canvas = $("timeline-chart");
  const [context, width, height] = fitCanvas(canvas);
  context.clearRect(0, 0, width, height);
  const margin = 28;
  const x = (index) => margin + index / Math.max(1, frameCount - 1) * (width - margin * 2);
  drawSeries(context, timelineData.red_effective_power, x, width, height, "#ff5b62", margin, false);
  drawSeries(context, timelineData.blue_effective_power, x, width, height, "#45a8ff", margin, false);
  drawSeries(context, timelineData.normalized_advantages, x, width, height, "#ffc857", margin, true);
  context.strokeStyle = "#ffffffaa"; context.lineWidth = 1;
  context.beginPath(); context.moveTo(x(currentFrameIndex), margin); context.lineTo(x(currentFrameIndex), height - margin); context.stroke();
}

function drawSeries(context, values, x, width, height, color, margin, normalized) {
  const finite = values.filter((value) => typeof value === "number" && Number.isFinite(value));
  if (!finite.length) return;
  const min = normalized ? -1 : Math.min(...finite);
  const max = normalized ? 1 : Math.max(...finite);
  const y = (value) => height - margin - ((value - min) / Math.max(1e-12, max - min)) * (height - margin * 2);
  context.strokeStyle = color; context.lineWidth = 1.6; context.beginPath();
  let drawing = false;
  values.forEach((value, index) => {
    if (value === null || value === undefined || !Number.isFinite(value)) { drawing = false; return; }
    if (!drawing) { context.moveTo(x(index), y(value)); drawing = true; }
    else context.lineTo(x(index), y(value));
  });
  context.stroke();
}

function pausePlayback() {
  if (playbackTimer !== null) window.clearInterval(playbackTimer);
  playbackTimer = null; $("play-pause").textContent = "播放";
}

function startPlayback() {
  pausePlayback();
  if (currentFrameIndex >= frameCount - 1) setCurrentFrameIndex(0);
  $("play-pause").textContent = "暂停";
  playbackTimer = window.setInterval(() => {
    if (currentFrameIndex >= frameCount - 1) { pausePlayback(); return; }
    setCurrentFrameIndex(currentFrameIndex + 1);
  }, 1000 / playbackRate);
}

function showError(message) {
  const banner = $("error-banner"); banner.textContent = message; banner.hidden = false;
  window.setTimeout(() => { banner.hidden = true; }, 5000);
}

async function initialize() {
  try {
    [replayMetadata, timelineData] = await Promise.all([
      requestJson("/api/replay"), requestJson("/api/timeline"),
    ]);
    frameCount = replayMetadata.frame_count;
    $("frame-slider").max = String(frameCount - 1);
    $("frame-jump").max = String(frameCount - 1);
    const playbackBadge = document.createElement("span");
    playbackBadge.className = "badge yes"; playbackBadge.textContent = "Playback compatible";
    const strictBadge = document.createElement("span");
    strictBadge.className = `badge ${replayMetadata.strict_replay_supported ? "yes" : "no"}`;
    strictBadge.textContent = `Strict replay: ${replayMetadata.strict_replay_supported ? "Yes" : "No"}`;
    const modelBadge = document.createElement("span");
    modelBadge.className = "badge";
    modelBadge.textContent = `${replayMetadata.model_provenance.model_id} ${replayMetadata.model_provenance.model_version}`;
    $("compatibility").replaceChildren(playbackBadge, strictBadge, modelBadge);
    await setCurrentFrameIndex(0);
  } catch (error) { showError(error.message); }
}

$("battlefield").addEventListener("click", (event) => {
  const rect = event.currentTarget.getBoundingClientRect();
  const point = { x: event.clientX - rect.left, y: event.clientY - rect.top };
  const hit = hitTargets.find((target) => Math.hypot(target.x - point.x, target.y - point.y) <= target.radius);
  if (hit) { selectedUnitId = hit.operatorId; renderBattlefield(); renderFactors(); }
});
$("previous-frame").addEventListener("click", () => setCurrentFrameIndex(Math.max(0, currentFrameIndex - 1)));
$("next-frame").addEventListener("click", () => setCurrentFrameIndex(Math.min(frameCount - 1, currentFrameIndex + 1)));
$("play-pause").addEventListener("click", () => playbackTimer === null ? startPlayback() : pausePlayback());
$("frame-slider").addEventListener("input", (event) => setCurrentFrameIndex(Number(event.target.value)));
$("jump-button").addEventListener("click", () => setCurrentFrameIndex(Number($("frame-jump").value)));
$("playback-speed").addEventListener("change", (event) => {
  playbackRate = Number(event.target.value);
  if (playbackTimer !== null) startPlayback();
});
window.addEventListener("resize", () => { if (currentFrame) { renderBattlefield(); renderTimeline(); } });

initialize();
