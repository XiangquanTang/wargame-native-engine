"use strict";

const FACTORS = [
  ["V", "base_value"], ["H", "health"], ["W", "weapon"],
  ["A", "ammo"], ["R", "readiness"], ["M", "mobility"],
  ["I", "information"], ["S", "status"],
];

let currentFrameIndex = 0;
let requestedFrameIndex = 0;
let frameCount = 0;
let replayMetadata = null;
let timelineData = null;
let mapData = null;
let currentFrame = null;
let currentAnalysis = null;
let currentOwnUnitAnalyses = [];
let selectedUnitId = null;
let replayView = "all";
let replayPerspectives = null;
let requestToken = 0;
let playbackTimer = null;
let playbackRate = 1;
let mapRenderer = null;
let timelineChart = null;
let frameController = null;
let playbackActive = false;
let playbackGeneration = 0;
let sliderTimer = null;
const frameCache = new Map();
const FRAME_CACHE_LIMIT = 16;

const $ = (id) => document.getElementById(id);

async function requestJson(path, signal) {
  const response = await fetch(ViewerUrls.resolve(path), { cache: "no-store", signal });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `${response.status} ${response.statusText}`);
  return body;
}

function formatNumber(value, digits = 4) {
  if (value === null || value === undefined) return "UNKNOWN";
  if (typeof value !== "number") return String(value);
  if (!Number.isFinite(value)) return "UNKNOWN";
  return Number.isInteger(value) ? String(value) : value.toFixed(digits).replace(/0+$/, "").replace(/\.$/, "");
}

function replayPerspective(view = replayView) {
  return view === "all" ? "GREEN" : view.toUpperCase();
}

function replayViewName(view = replayView) {
  return {all: "全局", red: "红方", blue: "蓝方"}[view];
}

function frameCacheKey(view, index) {
  return `${replayPerspective(view)}:${index}`;
}

function clearMapInteraction(clearSelection = true) {
  if (clearSelection) selectedUnitId = null;
  if (mapRenderer) {
    mapRenderer.hoveredHexKey = null;
    mapRenderer.selectedHex = null;
  }
  renderHexInfo(null);
}

function renderViewStatus(frame = null) {
  $("perspective-heading").textContent = `LOCAL · READ ONLY · ${replayPerspective()}`;
  $("replay-view-status").textContent = replayView === "all"
    ? "全局视角"
    : `${replayViewName()}视角 · ${frame?.visibility_status === "derived_from_recorded_visibility"
      ? "来源：可见记录重建"
      : "来源：阵营记录"}`;
}

function renderEvidence() {
  if (replayView !== "all") {
    for (const id of ["actions", "judge-info", "warnings"]) {
      $(id).textContent = "全局记录未显示";
    }
    return;
  }
  $("actions").textContent = JSON.stringify(currentFrame?.actions ?? [], null, 2);
  $("judge-info").textContent = JSON.stringify(currentFrame?.judge_info ?? [], null, 2);
  $("warnings").textContent = JSON.stringify({
    frame: currentAnalysis?.warnings ?? [],
    advantage: currentAnalysis?.advantage?.warnings ?? [],
    sides: Object.fromEntries((currentAnalysis?.side_analyses || []).map((side) => [side.side, side.warnings ?? []])),
  }, null, 2);
}

function renderGlobalSummary(analysis) {
  const sideByName = Object.fromEntries((analysis?.side_analyses || []).map((side) => [side.side, side]));
  $("red-power").textContent = formatNumber(sideByName.red?.effective_power);
  $("blue-power").textContent = formatNumber(sideByName.blue?.effective_power);
  $("normalized-advantage").textContent = formatNumber(analysis?.advantage?.normalized_advantage);
  $("raw-advantage").textContent = `raw ${formatNumber(analysis?.advantage?.raw_advantage)}`;
  $("advantage-label").textContent = analysis?.advantage?.label ?? "UNKNOWN";
  const coverage = (side) => typeof side?.coverage === "number" ? `${(side.coverage * 100).toFixed(1)}%` : "UNKNOWN";
  $("analysis-coverage").textContent = analysis
    ? `全局有效单位覆盖率：红 ${coverage(sideByName.red)}（总计 ${sideByName.red?.unit_count ?? 0}） · 蓝 ${coverage(sideByName.blue)}（总计 ${sideByName.blue?.unit_count ?? 0}）`
    : "本帧全局汇总未加载";
  const missing = new Map();
  for (const unit of analysis?.unit_analyses || []) for (const factor of unit.factor_values || []) {
    if (factor.status === "UNKNOWN") for (const field of factor.missing_fields || []) missing.set(field, (missing.get(field) || 0) + 1);
  }
  $("analysis-missing").textContent = !analysis ? "全局八因子缺失字段：UNKNOWN" : missing.size
    ? `全局缺失字段（单位数）：${Array.from(missing, ([field, count]) => `${field} ${count}`).join(" · ")}`
    : "全局八因子缺失字段：无";
}

function clearCurrentFrame() {
  currentFrame = null;
  currentAnalysis = null;
  currentOwnUnitAnalyses = [];
  clearMapInteraction();
  renderBattlefield();
  renderFactors();
  renderEvidence();
  renderGlobalSummary(null);
  $("cur-step").textContent = "UNKNOWN";
}

async function setCurrentFrameIndex(index) {
  if (!Number.isInteger(index) || index < 0 || index >= frameCount) {
    showError(`Frame index must be between 0 and ${frameCount - 1}`);
    return false;
  }
  const view = replayView;
  const perspective = replayPerspective(view);
  const key = frameCacheKey(view, index);
  requestedFrameIndex = index;
  timelineChart?.setRequestedFrame(index);
  const token = ++requestToken;
  frameController?.abort();
  frameController = new AbortController();
  // Keep the last fully committed frame, its labels and scoped details together
  // until the replacement is validated. View changes clear it synchronously in
  // setReplayView; clearing here would flash an empty map on every playback tick.
  renderViewStatus(currentFrame);
  $("error-banner").hidden = true;
  $("loading-status").textContent = `正在读取${replayViewName(view)} Frame ${index}…`;
  try {
    const availability = replayPerspectives?.[perspective];
    if (view !== "all" && availability?.status === "unavailable") {
      throw new Error(availability.reason || `${replayViewName(view)}逐帧可见记录不可用。`);
    }
    let bundle = frameCache.get(key);
    if (!bundle) {
      const suffix = view === "all" ? "" : `?perspective=${perspective}`;
      bundle = await requestJson(`/api/frame-bundle/${index}${suffix}`, frameController.signal);
      // Abort is advisory: ignore late responses before they can mutate the cache.
      if (token !== requestToken || view !== replayView) return false;
    }
    if (token !== requestToken || view !== replayView) return false;
    if (view !== "all") {
      if (bundle?.frame?.perspective !== perspective || bundle.frame.frame_index !== index ||
          !["recorded", "derived_from_recorded_visibility"].includes(bundle.frame.visibility_status) ||
          !Array.isArray(bundle.frame.viewer_units)) {
        throw new Error(`${replayViewName(view)}逐帧可见记录无效。`);
      }
      // Only explicitly scoped model-derived friendly factors may cross this boundary.
      // Revalidate cache hits too; never label another frame's contacts as this frame.
      const ownIds = new Set(bundle.frame.viewer_units
        .filter((unit) => unit.operator_id != null && unit.side === view)
        .map((unit) => String(unit.operator_id)));
      const ownAnalyses = bundle.own_analysis_source === "GLOBAL_MODEL_DERIVED_OWN_FACTORS"
        && Array.isArray(bundle.own_unit_analyses) ? bundle.own_unit_analyses : [];
      bundle = {
        frame: bundle.frame, analysis: null,
        own_unit_analyses: ownAnalyses.filter((unit) => unit.side === view && ownIds.has(String(unit.operator_id))),
        own_analysis_source: "GLOBAL_MODEL_DERIVED_OWN_FACTORS",
      };
    }
    frameCache.delete(key); frameCache.set(key, bundle);
    while (frameCache.size > FRAME_CACHE_LIMIT) frameCache.delete(frameCache.keys().next().value);
    // Commit all frame-dependent state in one task, without an intermediate blank.
    clearMapInteraction(false);
    currentFrameIndex = index;
    currentFrame = bundle.frame;
    currentAnalysis = view === "all" ? bundle.analysis : null;
    currentOwnUnitAnalyses = view === "all" ? [] : bundle.own_unit_analyses || [];
    const selectableUnits = view === "all" ? currentAnalysis?.unit_analyses : currentFrame?.viewer_units;
    if (selectedUnitId !== null && !(selectableUnits || []).some((unit) => String(unit.operator_id) === String(selectedUnitId))) {
      selectedUnitId = null;
    }
    renderCurrentFrame();
    $("loading-status").textContent = `${replayViewName(view)} Frame ${index} 已就绪 · 内存缓存 ${frameCache.size}/${FRAME_CACHE_LIMIT} 帧`;
    return true;
  } catch (error) {
    if (token === requestToken && view === replayView && error.name !== "AbortError") {
      // Missing evidence is an explicit empty state, never an omniscient fallback.
      frameCache.delete(key);
      clearCurrentFrame();
      $("frame-index").textContent = String(index);
      $("playback-position").textContent = `${index} / ${frameCount - 1}`;
      $("frame-slider").value = String(index);
      $("frame-jump").value = String(index);
      $("loading-status").textContent = `${replayViewName(view)} Frame ${index} 读取失败：${error.message}`;
      $("replay-view-status").textContent = `${replayViewName(view)}视角不可用：${error.message}`;
      showError(error.message);
    }
    return false;
  }
}

function renderCurrentFrame() {
  $("frame-index").textContent = String(currentFrameIndex);
  $("frame-slider").value = String(currentFrameIndex);
  $("frame-jump").value = String(currentFrameIndex);
  $("cur-step").textContent = formatNumber(currentFrame.time?.cur_step);
  $("playback-position").textContent = `${currentFrameIndex} / ${frameCount - 1}`;
  renderViewStatus(currentFrame);
  renderEvidence();
  // Only a same-frame, explicitly global summary is allowed alongside side views.
  renderGlobalSummary(replayView === "all" ? currentAnalysis : frameCache.get(frameCacheKey("all", currentFrameIndex))?.analysis);
  renderBattlefield();
  renderFactors();
  renderTimeline();
}

function renderBattlefield() {
  if (selectedUnitId !== null && !(currentFrame?.viewer_units || []).some((unit) => String(unit.operator_id) === String(selectedUnitId))) {
    selectedUnitId = null;
  }
  if (mapRenderer) mapRenderer.render(currentFrame, selectedUnitId);
}

async function setReplayView(view) {
  if (!["all", "red", "blue"].includes(view)) return false;
  if (view === replayView && currentFrame) return true;
  pausePlayback();
  window.clearTimeout(sliderTimer);
  sliderTimer = null;
  replayView = view;
  // Invalidate and clear synchronously, before even an already-cached replacement.
  ++requestToken;
  frameController?.abort();
  clearCurrentFrame();
  renderViewStatus();
  for (const name of ["all", "red", "blue"]) {
    $("replay-view-" + name).setAttribute("aria-pressed", String(name === view));
  }
  return setCurrentFrameIndex(requestedFrameIndex);
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
    `terrain=${value.terrain_semantic} (${value.terrain_code ?? value.terrain_source ?? "UNKNOWN"})${value.roadblock === true ? " · roadblock=yes" : ""}`,
    `elevation=${formatNumber(value.elevation)} level=${formatNumber(value.terrain_level)}`,
    `roads=${window.ReplayMapRenderer.formatEdgeFacts(value.roads)}`,
    `rivers=${window.ReplayMapRenderer.formatEdgeFacts(value.rivers)}`,
    `units=${unitIds.length ? unitIds.join(", ") : "none"}`,
  ].join(" · ");
}

function isDestroyedUnit(frame, operatorId) {
  const state = (frame?.viewer_units || []).find((item) => String(item.operator_id) === String(operatorId));
  return !!state && (state.alive === false || state.destroyed === true ||
    (typeof state.blood === "number" && Number.isFinite(state.blood) && state.blood <= 0));
}

function renderFactors() {
  const sideView = replayView !== "all";
  const unit = (sideView ? currentFrame?.viewer_units : currentAnalysis?.unit_analyses || [])
    ?.find((item) => String(item.operator_id) === String(selectedUnitId));
  const body = $("factor-table"); body.replaceChildren();
  if (!unit) {
    $("selected-unit").textContent = "点击战场单位";
    $("effective-power").textContent = "—";
    $("unit-validity").textContent = "";
    $("dead-unit-note").textContent = "";
    return;
  }
  const recordedUnit = (currentFrame?.viewer_units || []).find(
    (value) => String(value.operator_id) === String(unit.operator_id));
  const lockLabel = Number.isInteger(recordedUnit?.locked_weapon_id)
    ? ` · 锁定武器 ID ${recordedUnit.locked_weapon_id}`
    : recordedUnit?.weapon_locked === true ? " · 已锁定（旧记录未绑定武器）" : "";
  $("selected-unit").textContent = `Unit ${unit.operator_id} · ${String(unit.side || "UNKNOWN").toUpperCase()}${lockLabel}`;
  // Verify both projection ownership and analysis ownership before rendering factors.
  const factorUnit = !sideView ? unit : unit.side === replayView
    ? currentOwnUnitAnalyses.find((item) => item.side === replayView && String(item.operator_id) === String(unit.operator_id))
    : null;
  const derivedOwn = sideView && !!factorUnit;
  const sourceNote = "己方因子来源：全局模型";
  const destroyed = !!factorUnit && isDestroyedUnit(currentFrame, unit.operator_id);
  $("effective-power").textContent = !factorUnit ? "UNKNOWN" : destroyed ? "0" : formatNumber(factorUnit.effective_power);
  $("dead-unit-note").textContent = destroyed
    ? "已阵亡 · 当前有效贡献 0"
    : "";
  const identityNote = unit.identity_source === "SCENARIO_DERIVED_IDENTITY"
    ? " · 身份来源：场景静态标签"
    : unit.identity_source === "UNKNOWN" ? " · 身份来源：UNKNOWN" : "";
  $("unit-validity").textContent = sideView
    ? `${derivedOwn ? sourceNote : "八因子：UNKNOWN"} · 名称 ${unit.name ?? "UNKNOWN"} · 坐标 ${formatNumber(unit.current_hex)} · 生命 ${formatNumber(unit.blood)}/${formatNumber(unit.max_blood)} · 状态 ${unit.state ?? "UNKNOWN"}${identityNote}${derivedOwn ? ` · valid=${factorUnit.valid} · observed ${factorUnit.observed_factor_count}/8` : ""}`
    : `valid=${unit.valid} · observed ${unit.observed_factor_count}/8`;
  const values = Object.fromEntries((factorUnit?.factor_values || []).map((factor) => [factor.factor_name, factor]));
  for (const [code, name] of FACTORS) {
    const factor = values[name] || {status: "UNKNOWN", value: null, formula_id: "unavailable", warnings: ["factor_not_present"], missing_fields: [name]};
    const row = document.createElement("tr");
    const display = destroyed ? "0" : factor.status === "UNKNOWN" ? "UNKNOWN" : formatNumber(factor.value);
    const nameCell = document.createElement("td");
    const codeLabel = document.createElement("span");
    codeLabel.className = "factor-code"; codeLabel.textContent = code;
    nameCell.append(codeLabel, document.createElement("br"), document.createTextNode(name));
    const valueCell = document.createElement("td");
    valueCell.className = destroyed ? "inactive-factor" : factor.status === "UNKNOWN" ? "unknown" : "observed";
    valueCell.append(document.createTextNode(display), document.createElement("br"), document.createTextNode(destroyed ? "不参与" : factor.status));
    const evidenceCell = document.createElement("td");
    const evidence = sideView && !factorUnit ? "八因子：UNKNOWN"
      : `${derivedOwn ? `${sourceNote}\n` : ""}${factor.formula_id}\nwarnings: ${JSON.stringify(factor.warnings)}\nmissing: ${JSON.stringify(factor.missing_fields)}`;
    if (destroyed) {
      const details = document.createElement("details");
      const summary = document.createElement("summary"); summary.textContent = "原始因子审计";
      const raw = document.createElement("p"); raw.textContent = `原始值 ${formatNumber(factor.value)} · ${factor.status}\n${evidence}`;
      details.append(summary, raw); evidenceCell.appendChild(details);
    } else evidenceCell.textContent = evidence;
    row.append(nameCell, valueCell, evidenceCell);
    body.appendChild(row);
  }
}

function renderTimeline() {
  if (timelineChart) timelineChart.setFrame(currentFrameIndex);
}

function pausePlayback() {
  playbackActive = false; playbackGeneration++;
  if (playbackTimer !== null) window.clearTimeout(playbackTimer);
  playbackTimer = null; $("play-pause").textContent = "播放";
}

async function startPlayback() {
  pausePlayback(); playbackActive = true;
  const generation = playbackGeneration;
  $("play-pause").textContent = "暂停";
  if (currentFrameIndex >= frameCount - 1) await setCurrentFrameIndex(0);
  async function next() {
    if (!playbackActive || generation !== playbackGeneration) return;
    if (currentFrameIndex >= frameCount - 1) { pausePlayback(); return; }
    const loaded = await setCurrentFrameIndex(currentFrameIndex + 1);
    if (!playbackActive || generation !== playbackGeneration) return;
    if (!loaded) { pausePlayback(); return; }
    playbackTimer = window.setTimeout(next, 1000 / playbackRate);
  }
  if (playbackActive && generation === playbackGeneration) playbackTimer = window.setTimeout(next, 1000 / playbackRate);
}

function showError(message) {
  const banner = $("error-banner"); banner.textContent = message; banner.hidden = false;
  window.setTimeout(() => { banner.hidden = true; }, 5000);
}

async function initialize() {
  try {
    // Availability, full timeline and map JSON must not block the first frame.
    requestJson("/api/perspectives").then((data) => {
      replayPerspectives = data;
      for (const view of ["red", "blue"]) {
        const availability = data[replayPerspective(view)];
        $("replay-view-" + view).title = availability?.status === "unavailable"
          ? availability.reason || "缺少该阵营逐帧可见记录"
          : "阵营视角";
      }
    }).catch(() => {
      // A frame request can still provide the precise availability/error reason.
      replayPerspectives = null;
    });
    requestJson("/api/timeline").then((data) => {
      timelineData = data; timelineChart.setData(data); renderTimeline();
      $("timeline-status").textContent = "全局曲线 · 点击跳转帧 · 悬停读数 · 键盘左右键逐帧";
    }).catch((error) => { $("timeline-status").textContent = `曲线读取失败：${error.message}`; });
    requestJson("/api/map").then((data) => {
      mapData = data; initializeMap(); if (currentFrame) renderBattlefield();
    }).catch((error) => { $("map-mode").textContent = `地图读取失败：${error.message}`; });
    replayMetadata = await requestJson("/api/replay");
    frameCount = replayMetadata.frame_count;
    $("frame-slider").max = String(frameCount - 1);
    $("frame-jump").max = String(frameCount - 1);
    const playbackBadge = document.createElement("span");
    playbackBadge.className = "badge yes"; playbackBadge.textContent = "Playback compatible";
    const strictStatus = replayMetadata.strict_replay_evidence_status;
    const strictBadge = document.createElement("span");
    strictBadge.className = `badge ${strictStatus === "Available" ? "yes" : strictStatus === "Incomplete" ? "warn" : "no"}`;
    strictBadge.textContent = `Strict replay evidence: ${strictStatus}`;
    const modelBadge = document.createElement("span");
    modelBadge.className = "badge";
    modelBadge.textContent = `${replayMetadata.model_provenance.model_id} ${replayMetadata.model_provenance.model_version}`;
    $("compatibility").replaceChildren(playbackBadge, strictBadge, modelBadge);
    await setCurrentFrameIndex(0);
  } catch (error) { showError(error.message); }
}

function initializeMap() {
    $("map-mode").textContent = mapData.mode === "configured-hex-map"
      ? `Odd-r hex map · ${mapData.map_id ?? "unnamed"} · ${mapData.rows}×${mapData.columns}`
      : "Coordinate map";
    window.ReplayMapRenderer.renderElevationLegend($("elevation-legend"), mapData);
    const layers = window.ReplayMapRenderer.summarizeMapLayers(mapData);
    $("map-layer-summary").textContent = mapData.mode === "configured-hex-map"
      ? `Facts: ${layers.configured_cell_count} cells · elevation ${layers.elevation_cell_count} · road edges ${layers.road_edge_count} [${layers.road_types.join(",") || "none"}] · river edges ${layers.river_edge_count} [${layers.river_types.join(",") || "none"}]`
      : "Map layers: unavailable";
    $("scenario-mode").textContent = mapData.scenario
      ? `Scenario ${mapData.scenario.scenario_id} · ${mapData.scenario.features.length} static features`
      : "Scenario overlay: unavailable";
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
}

for (const view of ["all", "red", "blue"]) {
  $("replay-view-" + view).addEventListener("click", () => setReplayView(view));
}

$("reset-map-view").addEventListener("click", () => mapRenderer?.resetView());
$("previous-frame").addEventListener("click", () => setCurrentFrameIndex(Math.max(0, requestedFrameIndex - 1)));
$("next-frame").addEventListener("click", () => setCurrentFrameIndex(Math.min(frameCount - 1, requestedFrameIndex + 1)));
$("play-pause").addEventListener("click", () => playbackActive ? pausePlayback() : startPlayback());
$("frame-slider").addEventListener("input", (event) => {
  pausePlayback(); window.clearTimeout(sliderTimer);
  const index = Number(event.target.value);
  sliderTimer = window.setTimeout(() => setCurrentFrameIndex(index), 70);
});
$("jump-button").addEventListener("click", () => setCurrentFrameIndex(Number($("frame-jump").value)));
$("playback-speed").addEventListener("change", (event) => {
  playbackRate = Number(event.target.value);
  if (playbackActive) startPlayback();
});
window.addEventListener("resize", () => { if (currentFrame) { renderBattlefield(); renderTimeline(); } });

timelineChart = new window.ReplayTimelineChart.TimelineChart($("timeline-chart"), $("timeline-tooltip"), (frame) => {
  pausePlayback(); setCurrentFrameIndex(frame);
});
for (const name of ["red", "blue", "advantage"]) $("show-" + name).addEventListener("change", (event) => timelineChart.setVisible(name, event.target.checked));
initialize();
