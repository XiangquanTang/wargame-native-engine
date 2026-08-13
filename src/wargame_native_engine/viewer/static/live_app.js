"use strict";

let currentStepIndex = 0;
let sessionData = null;
let mapData = null;
let currentView = null;
let selectedUnitId = null;
let selectedActionId = null;
let requestSequence = 0;
let mapRenderer = null;
let commandPending = false;
let publicationComplete = false;

const $ = (id) => document.getElementById(id);
const ActionIndex = window.NativeLiveActions;

async function requestJson(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `${response.status} ${response.statusText}`);
  return body;
}

function currentActions() {
  return currentView?.valid_actions || [];
}

function actionLabel(item) {
  const detail = ActionIndex.actionDetail(item);
  return [
    detail.action_type,
    `actor=${detail.actor_id ?? "none"}`,
    detail.target_id === null ? null : `target=${detail.target_id}`,
    detail.path ? `path=${detail.path.join("→")}` : null,
    detail.movement_mode ? `mode=${detail.movement_mode}` : null,
    detail.weapon_id === null ? null : `weapon=${detail.weapon_id}`,
    item.action_id,
  ].filter(Boolean).join(" · ");
}

function visibleActions() {
  return selectedUnitId === null
    ? currentActions()
    : ActionIndex.actionsForActor(currentActions(), selectedUnitId);
}

function selectedAction() {
  return selectedActionId === null
    ? null : ActionIndex.findExactAction(visibleActions(), selectedActionId);
}

function selectAction(item, status = null) {
  if (item !== null && !ActionIndex.findExactAction(visibleActions(), item.action_id)) {
    showError("所选 action_id 不属于当前单位的服务器 valid_actions");
    return false;
  }
  selectedActionId = item?.action_id ?? null;
  if (status) $("command-status").textContent = status;
  renderCurrent();
  return true;
}

function actionButton(item) {
  const button = document.createElement("button");
  const kind = item.action.action_type.toLowerCase();
  button.type = "button";
  button.className = `action-button ${kind}`;
  button.dataset.actionId = item.action_id;
  button.textContent = actionLabel(item);
  if (item.action_id === selectedActionId) button.classList.add("selected");
  button.disabled = currentView.terminal || commandPending || publicationComplete;
  button.addEventListener("click", () => selectAction(item, "已预选服务器动作；确认后推进一次。"));
  return button;
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
    option.selected = item.action_id === selectedActionId;
    select.appendChild(option);
  }
  if (selectedActionId === null) select.value = "";

  const groups = $("action-groups");
  groups.replaceChildren();
  const noOpButton = document.createElement("button");
  noOpButton.type = "button";
  noOpButton.className = `action-button wait${selectedActionId === null ? " selected" : ""}`;
  noOpButton.textContent = "NO_OP · 本方不提交动作";
  noOpButton.disabled = currentView.terminal || commandPending || publicationComplete;
  noOpButton.addEventListener("click", () => selectAction(null, "已预选 NoOp；确认后与对手 NoOp 联合推进一次。"));
  groups.appendChild(noOpButton);
  for (const group of ActionIndex.groupedActions(currentActions(), selectedUnitId)) {
    const section = document.createElement("section");
    section.className = "action-group";
    const title = document.createElement("h3");
    title.textContent = `${group.kind} (${group.actions.length})`;
    const buttons = document.createElement("div");
    buttons.className = "action-buttons";
    group.actions.forEach((item) => buttons.appendChild(actionButton(item)));
    section.append(title, buttons);
    groups.appendChild(section);
  }
}

function renderHexInfo(value) {
  $("hex-info").textContent = value
    ? `row=${value.row} column=${value.column} position=${value.position_id} · terrain=${value.terrain_semantic} (${value.terrain_code ?? "UNKNOWN"}) · elevation=${value.elevation ?? "UNKNOWN"} level=${value.terrain_level ?? "UNKNOWN"} · roads=${window.ReplayMapRenderer.formatEdgeFacts(value.roads)} · rivers=${window.ReplayMapRenderer.formatEdgeFacts(value.rivers)} · units=${(value.units || []).map((unit) => unit.operator_id).join(",") || "none"}`
    : "移动鼠标查看六边形坐标";
}

function renderSelectedUnit() {
  const unit = (currentView.frame.viewer_units || []).find((item) => (
    ActionIndex.sameId(item.operator_id, selectedUnitId)
  ));
  $("selected-unit").textContent = unit
    ? `已选择 ${unit.side.toUpperCase()} ${unit.category} ${unit.operator_id}`
    : "未选择可操作己方单位（显示全局/全部动作）";
  $("selected-unit-detail").textContent = unit ? JSON.stringify({
    operator_id: unit.operator_id,
    side: unit.side,
    category: unit.category,
    position: unit.current_hex,
    row: unit.row,
    column: unit.column,
    blood: unit.blood,
    max_blood: unit.max_blood,
    alive: unit.alive,
    destroyed: unit.destroyed,
    move_state: unit.move_state,
    stopped: unit.stopped,
    lose_control: unit.lose_control,
    can_to_move: unit.can_to_move,
  }, null, 2) : "请选择具有当前 valid_actions 的己方 operator";
}

function renderActionPreview() {
  const item = selectedAction();
  $("action-preview").textContent = JSON.stringify(ActionIndex.actionDetail(item), null, 2);
  const targets = ActionIndex.actionTargets(currentActions(), selectedUnitId);
  const detail = ActionIndex.actionDetail(item);
  mapRenderer.setActionTargets({
    ...targets,
    preview_move_position: detail.path?.at(-1) ?? null,
    preview_shoot_target_id: detail.target_id,
  });
}

function renderCurrent() {
  currentStepIndex = currentView.step_index;
  if (selectedUnitId !== null
      && ActionIndex.actionsForActor(currentActions(), selectedUnitId).length === 0) {
    selectedUnitId = null;
    selectedActionId = null;
  }
  if (selectedActionId !== null && selectedAction() === null) selectedActionId = null;
  $("step-index").textContent = String(currentStepIndex);
  renderSelectedUnit();
  $("observation").textContent = JSON.stringify(currentView.observation, null, 2);
  renderActions();
  renderActionPreview();
  mapRenderer.render(currentView.frame, selectedUnitId);
  $("submit-action").disabled = currentView.terminal || commandPending || publicationComplete;
  $("publish-match").textContent = currentView.terminal
    ? "发布终局 replay 与 analysis"
    : "发布截断 replay 与 analysis";
}

function actionForMoveHex(value) {
  if (selectedUnitId === null) return null;
  return ActionIndex.actionsForMoveTarget(
    currentActions(), selectedUnitId, value.position_id,
  )[0] || null;
}

function actionForShootUnit(operatorId) {
  if (selectedUnitId === null) return null;
  return ActionIndex.actionsForShootTarget(
    currentActions(), selectedUnitId, operatorId,
  )[0] || null;
}

function handleUnitSelected(operatorId) {
  const ownActions = ActionIndex.actionsForActor(currentActions(), operatorId);
  if (ownActions.length) {
    selectedUnitId = operatorId;
    selectedActionId = null;
    $("command-status").textContent = `已选择己方可操作单位 ${operatorId}`;
    renderCurrent();
    return;
  }
  const shoot = actionForShootUnit(operatorId);
  if (shoot) selectAction(shoot, `已预选射击目标 ${operatorId}；请核对 weapon_id 后确认。`);
}

function handleHexSelected(value) {
  renderHexInfo(value);
  const move = actionForMoveHex(value);
  if (move) selectAction(move, `已预选合法MOVE目标 ${value.position_id}；双击该格或点击确认提交。`);
}

async function submitAction(actionId = selectedActionId) {
  if (commandPending || currentView.terminal || publicationComplete) return;
  const exact = actionId === null
    ? null : ActionIndex.findExactAction(visibleActions(), actionId);
  if (actionId !== null && exact === null) {
    showError("动作已过期或不属于当前选择；未提交");
    return;
  }
  commandPending = true;
  $("submit-action").disabled = true;
  const requestId = `web-${currentStepIndex}-${requestSequence++}`;
  try {
    const response = await requestJson("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        request_id: requestId,
        expected_step: currentStepIndex,
        valid_action_id: exact?.action_id ?? null,
      }),
    });
    currentView = response.current;
    selectedActionId = null;
    $("command-status").textContent = JSON.stringify({
      ...response.command,
      submitted_action: ActionIndex.actionDetail(exact),
      terminal: currentView.terminal,
      winner: currentView.winner,
      outcome: currentView.outcome,
    }, null, 2);
    renderCurrent();
  } catch (error) {
    showError(error.message);
    $("command-status").textContent = `提交失败：${error.message}`;
  } finally {
    commandPending = false;
    if (currentView) renderCurrent();
  }
}

async function publishMatch() {
  try {
    const response = await requestJson("/api/publish", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ truncated: !currentView.terminal }),
    });
    publicationComplete = true;
    $("submit-action").disabled = true;
    $("publish-match").disabled = true;
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
    const layers = window.ReplayMapRenderer.summarizeMapLayers(mapData);
    $("map-layer-summary").textContent = mapData.mode === "configured-hex-map"
      ? `Elevation ${mapData.elevation?.minimum ?? "UNKNOWN"}..${mapData.elevation?.maximum ?? "UNKNOWN"} · road edges ${layers.road_edge_count} [${layers.road_types.join(",") || "none"}] · river edges ${layers.river_edge_count} [${layers.river_types.join(",") || "none"}]`
      : "Coordinate-only fallback · map facts unavailable";
    mapRenderer = new window.ReplayMapRenderer.MapRenderer($("battlefield"), {
      onUnitSelected: handleUnitSelected,
      onUnitActivated: (operatorId) => {
        const shoot = actionForShootUnit(operatorId);
        if (shoot) {
          selectAction(shoot);
          submitAction(shoot.action_id);
        } else {
          handleUnitSelected(operatorId);
        }
      },
      onHexHovered: renderHexInfo,
      onHexSelected: handleHexSelected,
      onHexActivated: (value) => {
        const move = actionForMoveHex(value);
        if (move) {
          selectAction(move);
          submitAction(move.action_id);
        }
      },
    });
    mapRenderer.setMap(mapData);
    renderCurrent();
  } catch (error) {
    showError(error.message);
  }
}

$("reset-map-view").addEventListener("click", () => mapRenderer?.resetView());
$("action-select").addEventListener("change", (event) => {
  const actionId = event.target.value || null;
  const item = actionId === null ? null : ActionIndex.findExactAction(visibleActions(), actionId);
  selectAction(item, "已通过精确动作选择器预选；请确认提交。");
});
$("submit-action").addEventListener("click", () => submitAction());
$("publish-match").addEventListener("click", publishMatch);

initialize();
