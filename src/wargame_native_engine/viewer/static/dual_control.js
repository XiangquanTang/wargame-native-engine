"use strict";

const token = new URLSearchParams(window.location.search).get("token");
const ActionIndex = window.NativeLiveActions;
let current = null;
let currentWindowIndex = null;
let selectedUnitId = null;
let selectedShootTargetId = null;
let moveSelectionMode = false;
let currentProposal = null;
let currentCommands = null;
let requestSequence = 0;
let submitting = false;
let mapRenderer = null;
const $ = (id) => document.getElementById(id);

async function requestJson(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `${response.status}`);
  return body;
}

function nextRequestId(kind) {
  return `web-${current.side}-${current.window.decision_window_index}-${kind}-${requestSequence++}`;
}

function visibleActions() {
  const values = current?.view?.valid_actions || [];
  return selectedUnitId === null
    ? values : ActionIndex.actionsForActor(values, selectedUnitId);
}

function stagedActionIds() {
  return (current?.staged_actions || []).map((item) => item.action_id);
}

function renderHexInfo(value) {
  $("hex-info").textContent = value
    ? `row=${value.row} column=${value.column} position=${value.position_id} · terrain=${value.terrain_semantic} (${value.terrain_code ?? "UNKNOWN"}) · elevation=${value.elevation ?? "UNKNOWN"} · roads=${window.ReplayMapRenderer.formatEdgeFacts(value.roads)} · rivers=${window.ReplayMapRenderer.formatEdgeFacts(value.rivers)} · units=${(value.units || []).map((unit) => unit.operator_id).join(",") || "none"}`
    : "移动鼠标查看六边形坐标";
}

function renderMap() {
  if (!mapRenderer || !current?.scene) return;
  const targets = ActionIndex.actionTargets(current.view.valid_actions, selectedUnitId);
  const selected = (current.scene.frame.viewer_units || []).find(
    (item) => ActionIndex.sameId(item.operator_id, selectedUnitId),
  );
  const planned = currentProposal?.reachable
    ? [selected?.current_hex, ...currentProposal.ordered_path].filter(Number.isInteger)
    : [];
  mapRenderer.setActionTargets({
    ...targets,
    preview_move_position: currentProposal?.destination_hex ?? null,
    preview_shoot_target_id: selectedShootTargetId,
    planned_path: planned,
  });
  mapRenderer.render(current.scene.frame, selectedUnitId);
}

function actionButton(item) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "action-button";
  button.dataset.actionId = item.action_id;
  const detail = ActionIndex.actionDetail(item);
  button.textContent = [
    detail.action_type,
    detail.target_id === null ? null : `target=${detail.target_id}`,
    detail.weapon_id === null ? null : `weapon=${detail.weapon_id}`,
    detail.path ? `path=${detail.path.join("→")}` : null,
  ].filter(Boolean).join(" · ");
  if (stagedActionIds().includes(item.action_id)) button.classList.add("selected");
  button.disabled = current.window.submitted || current.view.terminal || submitting;
  button.addEventListener("click", () => stageAction(item.action_id));
  return button;
}

function renderActions() {
  const root = $("actions");
  root.replaceChildren();
  if (selectedUnitId === null) {
    root.textContent = "点击己方可操作算子后显示命令";
  } else {
    for (const group of ActionIndex.groupedActions(
      current.view.valid_actions, selectedUnitId,
    )) {
      const section = document.createElement("section");
      section.className = "action-group";
      const title = document.createElement("h3");
      title.textContent = `${group.kind} (${group.actions.length})`;
      const buttons = document.createElement("div");
      buttons.className = "action-buttons";
      if (group.kind === "MOVE") {
        const plan = document.createElement("button");
        plan.type = "button";
        plan.className = `action-button move${moveSelectionMode ? " selected" : ""}`;
        plan.textContent = "MOVE · 选择任意可规划目的格";
        plan.addEventListener("click", () => {
          moveSelectionMode = true;
          currentProposal = null;
          $("status").textContent = "MOVE规划模式：点击地图目的六边形；服务器计算并逐段校验。";
          render();
        });
        buttons.appendChild(plan);
      }
      group.actions.forEach((item) => buttons.appendChild(actionButton(item)));
      section.append(title, buttons);
      root.appendChild(section);
    }
  }
  $("batch-preview").textContent = JSON.stringify(current.staged_actions || [], null, 2);
  $("submit-batch").disabled = current.window.submitted || current.view.terminal || submitting;
  $("submit-empty").disabled = current.window.submitted || current.view.terminal || submitting;
  $("confirm-proposal").disabled = !currentProposal?.reachable
    || !currentProposal.action_id || current.window.submitted || submitting;
}

function render() {
  $("side-badge").textContent = current.side.toUpperCase();
  $("window-badge").textContent = `Window ${current.window.decision_window_index} · ${current.window.lifecycle_state}`;
  $("frame-index").textContent = String(current.view.frame_index);
  $("deadline").textContent = current.window.deadline_remaining_ms === null
    ? "同步阻塞模式：必须显式提交"
    : `服务器deadline剩余 ${current.window.deadline_remaining_ms} ms`;
  $("map-mode").textContent = `${current.scene.map.mode} · ${current.scene.map.map_id}`;
  $("selected-unit").textContent = selectedUnitId === null
    ? "请选择己方算子"
    : `已选择 ${current.side.toUpperCase()} 算子 ${selectedUnitId}`;
  $("unit-commands").textContent = JSON.stringify(currentCommands || {}, null, 2);
  $("proposal-preview").textContent = currentProposal
    ? JSON.stringify(currentProposal, null, 2) : "尚未规划路径";
  $("observation").textContent = JSON.stringify(current.view.observation, null, 2);
  renderActions();
  renderMap();
}

async function selectUnit(operatorId) {
  const actions = ActionIndex.actionsForActor(current.view.valid_actions, operatorId);
  if (!actions.length) {
    const shoots = ActionIndex.actionsForShootTarget(
      current.view.valid_actions, selectedUnitId, operatorId,
    );
    if (!shoots.length) return;
    selectedShootTargetId = operatorId;
    $("status").textContent = shoots.length === 1
      ? `目标 ${operatorId} 只有一个合法射击选项，已暂存。`
      : `目标 ${operatorId} 有 ${shoots.length} 个服务器合法武器选项，请在侧栏选择。`;
    if (shoots.length === 1) await stageAction(shoots[0].action_id);
    render();
    return;
  }
  selectedUnitId = operatorId;
  selectedShootTargetId = null;
  moveSelectionMode = false;
  currentProposal = null;
  try {
    currentCommands = await requestJson(
      `/api/commands?token=${encodeURIComponent(token)}&unit_id=${encodeURIComponent(operatorId)}`,
    );
  } catch (error) {
    $("status").textContent = `命令查询失败：${error.message}`;
  }
  render();
}

async function planMove(hex) {
  if (!moveSelectionMode || selectedUnitId === null || submitting) return;
  submitting = true;
  try {
    currentProposal = await requestJson("/api/plan-move", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token,
        request_id: nextRequestId("plan"),
        expected_window: current.window.decision_window_index,
        unit_id: selectedUnitId,
        destination_hex: Number(hex.position_id),
        movement_mode: "NORMAL",
      }),
    });
    $("status").textContent = currentProposal.reachable
      ? `路径已生成：${currentProposal.ordered_path.join("→")}，预计 ${currentProposal.estimated_duration_ms} ms；请确认。`
      : `不可达：${currentProposal.blocked_reason}`;
  } catch (error) {
    $("status").textContent = `规划失败：${error.message}`;
  } finally {
    submitting = false;
    render();
  }
}

async function stageAction(actionId) {
  if (!actionId || submitting) return;
  submitting = true;
  try {
    const response = await requestJson("/api/batch/stage", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token,
        request_id: nextRequestId("stage"),
        expected_window: current.window.decision_window_index,
        action_id: actionId,
      }),
    });
    current.staged_actions = response.staged_actions;
    $("status").textContent = "动作已在服务端暂存；同一actor的新选择会替换旧选择。";
  } catch (error) {
    $("status").textContent = `暂存失败：${error.message}`;
  } finally {
    submitting = false;
    render();
  }
}

async function refresh() {
  try {
    const next = await requestJson(`/api/bootstrap?token=${encodeURIComponent(token)}`);
    if (currentWindowIndex !== null
        && next.window.decision_window_index !== currentWindowIndex) {
      selectedUnitId = null;
      selectedShootTargetId = null;
      moveSelectionMode = false;
      currentProposal = null;
      currentCommands = null;
    }
    currentWindowIndex = next.window.decision_window_index;
    current = next;
    if (!mapRenderer) {
      mapRenderer = new window.ReplayMapRenderer.MapRenderer($("battlefield"), {
        onUnitSelected: selectUnit,
        onHexHovered: renderHexInfo,
        onHexSelected: (value) => { renderHexInfo(value); planMove(value); },
      });
      mapRenderer.setMap(current.scene.map);
    }
    render();
  } catch (error) {
    $("error-banner").hidden = false;
    $("error-banner").textContent = error.message;
  }
}

async function submit(staged) {
  if (submitting || current.window.submitted || current.view.terminal) return;
  submitting = true;
  renderActions();
  try {
    const endpoint = staged ? "/api/batch/submit" : "/api/batch";
    const body = staged ? {
      token,
      request_id: nextRequestId("submit"),
      expected_window: current.window.decision_window_index,
    } : {
      token,
      request_id: nextRequestId("empty"),
      expected_window: current.window.decision_window_index,
      ordered_action_ids: [],
    };
    const response = await requestJson(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    $("status").textContent = JSON.stringify(response.result, null, 2);
    await refresh();
  } catch (error) {
    $("status").textContent = `提交失败：${error.message}`;
  } finally {
    submitting = false;
    if (current) renderActions();
  }
}

$("confirm-proposal").addEventListener("click", () => (
  stageAction(currentProposal?.action_id)
));
$("submit-batch").addEventListener("click", () => submit(true));
$("submit-empty").addEventListener("click", () => submit(false));
$("reset-map-view").addEventListener("click", () => mapRenderer?.resetView());
refresh();
setInterval(refresh, 1000);
