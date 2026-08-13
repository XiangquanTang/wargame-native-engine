"use strict";

let currentStepIndex = 0;
let sessionData = null;
let mapData = null;
let currentView = null;
let selectedUnitId = null;
let requestSequence = 0;
let mapRenderer = null;
let commandPending = false;

const $ = (id) => document.getElementById(id);

async function requestJson(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `${response.status} ${response.statusText}`);
  return body;
}

function actionLabel(item) {
  const action = item.action;
  const path = action.parameters?.path;
  return [
    action.action_type,
    `actor=${action.actor_id ?? "none"}`,
    action.target_id === null ? null : `target=${action.target_id}`,
    path ? `path=${path.join("→")}` : null,
    item.action_id,
  ].filter(Boolean).join(" · ");
}

function visibleActions() {
  const values = currentView?.valid_actions || [];
  return selectedUnitId === null
    ? values
    : values.filter((item) => item.action.actor_id === selectedUnitId);
}

function renderActions() {
  const select = $("action-select");
  select.replaceChildren();
  const noOp = document.createElement("option");
  noOp.value = "";
  noOp.textContent = "NoOp（本方不提交动作）";
  select.appendChild(noOp);
  for (const item of visibleActions()) {
    const option = document.createElement("option");
    option.value = item.action_id;
    option.textContent = actionLabel(item);
    select.appendChild(option);
  }
}

function renderHexInfo(value) {
  $("hex-info").textContent = value
    ? `row=${value.row} column=${value.column} position=${value.position_id} · terrain=${value.terrain_semantic} · elevation=${value.elevation ?? "UNKNOWN"} · units=${(value.units || []).map((unit) => unit.operator_id).join(",") || "none"}`
    : "移动鼠标查看六边形坐标";
}

function renderCurrent() {
  currentStepIndex = currentView.step_index;
  $("step-index").textContent = String(currentStepIndex);
  $("selected-unit").textContent = selectedUnitId === null
    ? "未选择单位（显示全部合法动作）"
    : `已选择单位 ${selectedUnitId}`;
  $("observation").textContent = JSON.stringify(currentView.observation, null, 2);
  renderActions();
  mapRenderer.render(currentView.frame, selectedUnitId);
  $("submit-action").disabled = currentView.terminal || commandPending;
  $("publish-match").textContent = currentView.terminal
    ? "发布终局 replay 与 analysis"
    : "发布截断 replay 与 analysis";
}

async function submitAction() {
  if (commandPending) return;
  commandPending = true;
  $("submit-action").disabled = true;
  const requestId = `web-${currentStepIndex}-${requestSequence++}`;
  const selected = $("action-select").value;
  try {
    const response = await requestJson("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        request_id: requestId,
        expected_step: currentStepIndex,
        valid_action_id: selected || null,
      }),
    });
    currentView = response.current;
    $("command-status").textContent = JSON.stringify(response.command, null, 2);
    if (selectedUnitId !== null && !currentView.frame.viewer_units.some((unit) => unit.operator_id === selectedUnitId)) {
      selectedUnitId = null;
    }
    renderCurrent();
  } catch (error) {
    showError(error.message);
  } finally {
    commandPending = false;
    if (currentView) $("submit-action").disabled = currentView.terminal;
  }
}

async function publishMatch() {
  try {
    const response = await requestJson("/api/publish", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ truncated: !currentView.terminal }),
    });
    $("command-status").textContent = JSON.stringify(response, null, 2);
  } catch (error) {
    showError(error.message);
  }
}

function showError(message) {
  const banner = $("error-banner");
  banner.textContent = message;
  banner.hidden = false;
  window.setTimeout(() => { banner.hidden = true; }, 5000);
}

async function initialize() {
  try {
    [sessionData, mapData, currentView] = await Promise.all([
      requestJson("/api/session"),
      requestJson("/api/map"),
      requestJson("/api/view"),
    ]);
    $("perspective-badge").textContent = `${sessionData.perspective} control`;
    $("perspective-badge").classList.add(
      sessionData.perspective === "RED" ? "no" : "yes",
    );
    $("map-mode").textContent = `${mapData.map_id} · odd-r · side facts only`;
    mapRenderer = new window.ReplayMapRenderer.MapRenderer($("battlefield"), {
      onUnitSelected: (operatorId) => {
        selectedUnitId = operatorId;
        renderCurrent();
      },
      onHexHovered: renderHexInfo,
      onHexSelected: renderHexInfo,
    });
    mapRenderer.setMap(mapData);
    renderCurrent();
  } catch (error) {
    showError(error.message);
  }
}

$("reset-map-view").addEventListener("click", () => mapRenderer?.resetView());
$("submit-action").addEventListener("click", submitAction);
$("publish-match").addEventListener("click", publishMatch);

initialize();
