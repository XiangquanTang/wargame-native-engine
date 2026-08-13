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
let mapData = null;
let currentFrame = null;
let currentAnalysis = null;
let selectedUnitId = null;
let requestToken = 0;
let playbackTimer = null;
let playbackRate = 1;
let mapRenderer = null;

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
  if (mapRenderer) mapRenderer.render(currentFrame, selectedUnitId);
}

function renderHexInfo(value, mode) {
  const target = $("hex-info");
  if (!value) {
    target.textContent = "移动鼠标或点击六边形查看真实坐标与地形";
    return;
  }
  const unitIds = (value.units || []).map((unit) => unit.operator_id);
  target.textContent = [
    `${mode} row=${value.row} column=${value.column} position=${value.position_id}`,
    `terrain=${value.terrain_semantic} (${formatNumber(value.terrain_code)})`,
    `elevation=${formatNumber(value.elevation)} level=${formatNumber(value.terrain_level)}`,
    `roads=${window.ReplayMapRenderer.formatEdgeFacts(value.roads)}`,
    `rivers=${window.ReplayMapRenderer.formatEdgeFacts(value.rivers)}`,
    `units=${unitIds.length ? unitIds.join(", ") : "none"}`,
  ].join(" · ");
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
    [replayMetadata, timelineData, mapData] = await Promise.all([
      requestJson("/api/replay"), requestJson("/api/timeline"), requestJson("/api/map"),
    ]);
    frameCount = replayMetadata.frame_count;
    $("frame-slider").max = String(frameCount - 1);
    $("frame-jump").max = String(frameCount - 1);
    const playbackBadge = document.createElement("span");
    playbackBadge.className = "badge yes"; playbackBadge.textContent = "Playback compatible";
    const strictStatus = replayMetadata.strict_replay_evidence_status;
    const strictBadge = document.createElement("span");
    strictBadge.className = `badge ${strictStatus === "Available" ? "yes" : strictStatus === "Incomplete" ? "warn" : "no"}`;
    strictBadge.textContent = `Strict replay evidence: ${strictStatus}`;
    strictBadge.title = replayMetadata.strict_replay_evidence_note;
    const modelBadge = document.createElement("span");
    modelBadge.className = "badge";
    modelBadge.textContent = `${replayMetadata.model_provenance.model_id} ${replayMetadata.model_provenance.model_version}`;
    $("compatibility").replaceChildren(playbackBadge, strictBadge, modelBadge);
    $("strict-evidence-note").textContent = replayMetadata.strict_replay_evidence_note;
    $("map-mode").textContent = mapData.mode === "configured-hex-map"
      ? `Odd-r hex map · ${mapData.map_id ?? "unnamed"} · ${mapData.rows}×${mapData.columns}`
      : "Coordinate fallback · no terrain configuration";
    const elevation = mapData.elevation;
    $("map-elevation").textContent = elevation?.mode === "episode"
      ? `Elevation ${formatNumber(elevation.minimum)}..${formatNumber(elevation.maximum)} · ${elevation.levels} levels`
      : "Elevation unavailable";
    const layers = window.ReplayMapRenderer.summarizeMapLayers(mapData);
    $("map-layer-summary").textContent = mapData.mode === "configured-hex-map"
      ? `Facts: ${layers.configured_cell_count} cells · elevation ${layers.elevation_cell_count} · road edges ${layers.road_edge_count} [${layers.road_types.join(",") || "none"}] · river edges ${layers.river_edge_count} [${layers.river_types.join(",") || "none"}]`
      : "Facts: coordinate-only fallback; terrain, elevation, roads and rivers unavailable";
    $("scenario-mode").textContent = mapData.scenario
      ? `Scenario ${mapData.scenario.scenario_id} · ${mapData.scenario.features.length} static features · dynamic units from replay`
      : "No explicit Scenario overlay";
    mapRenderer = new window.ReplayMapRenderer.MapRenderer($("battlefield"), {
      onUnitSelected: (operatorId) => {
        selectedUnitId = operatorId;
        renderBattlefield();
        renderFactors();
      },
      onHexHovered: (value) => renderHexInfo(value, "hover"),
      onHexSelected: (value) => renderHexInfo(value, "selected"),
    });
    mapRenderer.setMap(mapData);
    await setCurrentFrameIndex(0);
  } catch (error) { showError(error.message); }
}

$("reset-map-view").addEventListener("click", () => mapRenderer?.resetView());
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
