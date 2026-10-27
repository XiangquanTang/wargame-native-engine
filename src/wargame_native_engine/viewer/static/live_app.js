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
let selectedKind = null;
let radialOpen = true;
let targetActionIds = null;
const KIND_LABELS = {
  MOVE: "机动", SHOOT: "射击", ATTACK: "攻击", FIRE: "间瞄", STOP_MOVE: "停止",
  BOARD: "搭载", UNLOAD: "下车", CAPTURE: "夺控", ENTER_CONCEALMENT: "隐蔽",
  WEAPON_LOCK: "锁定", WEAPON_UNLOCK: "解锁", WEAPON_UNFOLD: "展开", WAIT: "等待", DEPLOY: "部署",
  END_DEPLOYMENT: "就绪", CHANGE_STATE: "状态", NO_OP: "等待",
};

const $ = (id) => document.getElementById(id);
const ActionIndex = window.NativeLiveActions;

async function requestJson(path, options = {}) {
  const response = await fetch(ViewerUrls.resolve(path), { cache: "no-store", ...options });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `${response.status} ${response.statusText}`);
  return body;
}

function currentActions() {
  return currentView?.valid_actions || [];
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

function canOperate() {
  return !!currentView && !currentView.terminal && !commandPending && !publicationComplete;
}

function actorUnit(operatorId) {
  return (currentView?.frame?.viewer_units || []).find(unit => (
    ActionIndex.sameId(unit.operator_id, operatorId)
  ));
}

function canSelectActor(operatorId) {
  const unit = actorUnit(operatorId);
  return unit && unit.alive !== false && unit.destroyed !== true
    && String(unit.side).toLowerCase() === String(sessionData?.perspective).toLowerCase()
    && ActionIndex.actionsForActor(currentActions(), operatorId).length > 0;
}

function draftCandidates() {
  return visibleActions().filter(item => item.action.action_type === selectedKind
    && (!targetActionIds || targetActionIds.includes(item.action_id)));
}

function cancelDraft() {
  if (commandPending) return;
  selectedKind = null;
  selectedActionId = null;
  targetActionIds = null;
  $("command-status").textContent = "已取消";
  if (currentView) renderCurrent();
}

function chooseKind(kind) {
  if (!canOperate()) return;
  const items = visibleActions().filter(item => item.action.action_type === kind);
  if (!items.length) return;
  selectedKind = kind;
  targetActionIds = null;
  selectedActionId = null;
  const needsTarget = kind === "MOVE" || items.some(item => item.action.target_id !== null
    && item.action.target_id !== undefined);
  if (!needsTarget && kind !== "WEAPON_LOCK" && items.length === 1) selectedActionId = items[0].action_id;
  $("command-status").textContent = needsTarget ? "选择目标，再点击 √" : "选择参数，再点击 √";
  renderCurrent();
}

function chooseCandidates(items) {
  if (!canOperate() || !items.length) return false;
  targetActionIds = items.map(item => item.action_id);
  selectedActionId = items.length === 1 ? items[0].action_id : null;
  $("command-status").textContent = items.length === 1 ? "点击 √ 确认" : "选择武器或参数，再点击 √";
  renderCurrent();
  return true;
}

function compactAction(item) {
  const detail = ActionIndex.actionDetail(item);
  return [KIND_LABELS[detail.action_type] || detail.action_type,
    detail.weapon_id === null ? null : `武器 ${detail.weapon_id}`,
    detail.target_id === null ? null : `目标 ${detail.target_id}`,
    detail.path ? `至 ${detail.path.at(-1)}` : null,
    detail.target_position ? `位置 ${detail.target_position.join(",")}` : null,
    detail.movement_mode,
    !detail.path && detail.weapon_id === null ? JSON.stringify(item.action.parameters || {}) : null,
  ].filter(value => value && value !== "{}").join(" · ");
}

function renderActions() {
  const radial = $("unit-actions");
  radial.replaceChildren();
  if (canSelectActor(selectedUnitId)) {
    for (const group of ActionIndex.groupedActions(currentActions(), selectedUnitId)) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = KIND_LABELS[group.kind] || group.kind;
      button.className = group.kind === selectedKind ? "active" : "";
      button.disabled = !canOperate();
      button.addEventListener("click", () => chooseKind(group.kind));
      radial.appendChild(button);
    }
  }
  const select = $("action-select");
  select.replaceChildren();
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "选择武器或动作参数";
  select.appendChild(placeholder);
  const candidates = draftCandidates();
  for (const item of candidates) {
    const option = document.createElement("option");
    option.value = item.action_id;
    option.textContent = compactAction(item);
    option.selected = item.action_id === selectedActionId;
    select.appendChild(option);
  }
  select.value = selectedActionId || "";
  select.hidden = selectedKind === null || selectedKind === "NO_OP" || (candidates.length < 2 && selectedKind !== "WEAPON_LOCK");
  select.disabled = !canOperate();
  $("cancel-action").disabled = commandPending || selectedKind === null;
  $("wait-step").disabled = !canOperate();
  const globals = $("global-actions");
  globals.replaceChildren();
  for (const item of currentActions().filter(item => item.action.actor_id == null)) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = KIND_LABELS[item.action.action_type] || item.action.action_type;
    button.disabled = !canOperate();
    button.addEventListener("click", () => {
      if (!canOperate()) return;
      selectedUnitId = null;
      selectedKind = item.action.action_type;
      targetActionIds = [item.action_id];
      selectAction(item, "点击 √ 确认");
    });
    globals.appendChild(button);
  }
}

function positionRadialActions() {
  if (!radialOpen) { $("unit-actions").hidden = true; return; }
  window.UnitRadialActions.layout($("unit-actions"), mapRenderer,
    selectedUnitId === null ? [] : [selectedUnitId]);
}

function renderHexInfo(value) {
  $("hex-info").textContent = value
    ? `row=${value.row} column=${value.column} position=${value.position_id} · terrain=${value.terrain_semantic} (${value.terrain_code ?? value.terrain_source ?? "UNKNOWN"})${value.roadblock === true ? " · roadblock=yes" : ""} · elevation=${value.elevation ?? "UNKNOWN"} level=${value.terrain_level ?? "UNKNOWN"} · roads=${window.ReplayMapRenderer.formatEdgeFacts(value.roads)} · rivers=${window.ReplayMapRenderer.formatEdgeFacts(value.rivers)} · units=${(value.units || []).map((unit) => unit.operator_id).join(",") || "none"}`
    : "移动鼠标查看六边形坐标";
}

function renderSelectedUnit() {
  const unit = (currentView.frame.viewer_units || []).find((item) => (
    ActionIndex.sameId(item.operator_id, selectedUnitId)
  ));
  $("selected-unit").textContent = unit
    ? `已选择 ${unit.side.toUpperCase()} ${unit.category} ${unit.operator_id}`
    : "点击己方算子选择操作";
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
    keep: unit.keep,
    keep_remain_time: unit.keep_remain_time,
    lose_control: unit.lose_control,
    can_to_move: unit.can_to_move,
  }, null, 2) : "请选择具有当前 valid_actions 的己方 operator";
}

function renderActionPreview() {
  const item = selectedAction();
  $("action-preview").textContent = item ? compactAction(item)
    : selectedKind === "NO_OP" ? "本方等待一步" : selectedKind ? "选择目标或动作参数" : "";
  const targets = ActionIndex.actionTargets(draftCandidates(), selectedUnitId);
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
      && !canSelectActor(selectedUnitId)) {
    selectedUnitId = null;
    selectedActionId = null;
    selectedKind = null;
    targetActionIds = null;
  }
  if (selectedActionId !== null && selectedAction() === null) {
    selectedActionId = null;
    selectedKind = null;
    targetActionIds = null;
    $("command-status").textContent = "动作已更新，请重新选择";
  }
  if (selectedKind && selectedKind !== "NO_OP" && draftCandidates().length === 0) {
    selectedKind = null;
    targetActionIds = null;
  }
  $("step-index").textContent = String(currentStepIndex);
  renderSelectedUnit();
  $("observation").textContent = JSON.stringify(currentView.observation, null, 2);
  renderActions();
  renderActionPreview();
  mapRenderer.render(currentView.frame, selectedUnitId);
  positionRadialActions();
  $("submit-action").disabled = !canOperate() || (selectedKind !== "NO_OP" && !selectedAction());
  $("publish-match").textContent = "保存复盘";
  $("publish-match").disabled = commandPending || publicationComplete;
}

function handleUnitSelected(operatorId) {
  if (!canOperate()) return;
  if (ActionIndex.sameId(selectedUnitId, operatorId)) {
    radialOpen = !radialOpen; renderCurrent(); return;
  }
  radialOpen = true;
  if (selectedKind !== null) {
    const targets = visibleActions().filter(item => item.action.action_type === selectedKind
      && ActionIndex.sameId(item.action.target_id, operatorId));
    if (chooseCandidates(targets)) return;
  }
  if (!canSelectActor(operatorId)) return;
  selectedUnitId = operatorId;
  selectedActionId = null;
  selectedKind = null;
  targetActionIds = null;
  $("command-status").textContent = "选择算子周围的动作";
  renderCurrent();
}

function handleHexSelected(value) {
  renderHexInfo(value);
  if (selectedKind !== "MOVE") return;
  chooseCandidates(ActionIndex.actionsForMoveTarget(currentActions(), selectedUnitId, value.position_id));
}

async function submitAction(actionId = selectedActionId) {
  if (!canOperate() || (actionId === null && selectedKind !== "NO_OP")) return;
  const exact = actionId === null
    ? null : ActionIndex.findExactAction(visibleActions(), actionId);
  if (actionId !== null && exact === null) {
    showError("动作已过期或不属于当前选择；未提交");
    return;
  }
  commandPending = true;
  renderCurrent();
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
    selectedKind = null;
    targetActionIds = null;
    $("command-status").textContent = currentView.terminal ? "对局结束" : "已执行";
    $("command-detail").textContent = JSON.stringify({
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
    selectedActionId = null;
    selectedKind = null;
    targetActionIds = null;
    try { currentView = await requestJson("/api/view"); } catch (_) { /* Keep the last view; never retry a command. */ }
  } finally {
    commandPending = false;
    if (currentView) renderCurrent();
  }
}

async function publishMatch() {
  if (!currentView || commandPending || publicationComplete) return;
  commandPending = true;
  renderCurrent();
  try {
    const response = await requestJson("/api/publish", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ truncated: !currentView.terminal }),
    });
    publicationComplete = true;
    $("submit-action").disabled = true;
    $("publish-match").disabled = true;
    $("command-status").textContent = "复盘已保存";
    $("command-detail").textContent = JSON.stringify(response, null, 2);
    renderCurrent();
  } catch (error) {
    showError(error.message);
  } finally {
    commandPending = false;
    renderCurrent();
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
    $("map-mode").textContent = `${mapData.map_id} · odd-r · 阵营视角`;
    window.ReplayMapRenderer.renderElevationLegend($("elevation-legend"), mapData);
    const layers = window.ReplayMapRenderer.summarizeMapLayers(mapData);
    $("map-layer-summary").textContent = mapData.mode === "configured-hex-map"
      ? `Road edges ${layers.road_edge_count} [${layers.road_types.join(",") || "none"}] · river edges ${layers.river_edge_count} [${layers.river_types.join(",") || "none"}]`
      : "Map layers: unavailable";
    mapRenderer = new window.ReplayMapRenderer.MapRenderer($("battlefield"), {
      onUnitSelected: handleUnitSelected,
      onHexHovered: renderHexInfo,
      onHexSelected: handleHexSelected,
      onRender: positionRadialActions,
    });
    mapRenderer.setMap(mapData);
    renderCurrent();
  } catch (error) {
    showError(error.message);
  }
}

$("reset-map-view").addEventListener("click", () => mapRenderer?.resetView());
$("action-select").addEventListener("change", (event) => {
  if (!canOperate()) return;
  const actionId = event.target.value || null;
  const item = actionId === null ? null : ActionIndex.findExactAction(draftCandidates(), actionId);
  if (actionId !== null && !item) return;
  selectAction(item, "点击 √ 确认");
});
$("submit-action").addEventListener("click", () => submitAction());
$("cancel-action").addEventListener("click", cancelDraft);
$("wait-step").addEventListener("click", () => {
  if (!canOperate()) return;
  selectedKind = "NO_OP";
  selectedActionId = null;
  targetActionIds = null;
  $("command-status").textContent = "点击 √ 确认等待";
  renderCurrent();
});
window.addEventListener("keydown", event => {
  if (event.key === "Escape") cancelDraft();
});
$("publish-match").addEventListener("click", publishMatch);

initialize();
