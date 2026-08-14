"use strict";

const token = new URLSearchParams(window.location.search).get("token");
const selectedUnitIds = new Set();
let current = null;
let capabilities = null;
let activeTool = null;
let targetHex = null;
let targetUnitId = null;
let operationPreview = null;
let isPlanning = false;
let isExecuting = false;
let lastError = null;
let lastFrameIndex = null;
let lastRequestId = null;
let previewSequence = 0;
let executeRequestId = null;
let mapRenderer = null;
const pageNonce = globalThis.crypto?.randomUUID?.()
  || `${Date.now()}-${Math.floor(performance.now())}`;
const $ = (id) => document.getElementById(id);
const operationButtons = Array.from(document.querySelectorAll("[data-operation]"));
const OPERATION_LABELS = {
  MOVE: "机动",
  SHOOT: "射击",
  STOP_MOVE: "停止",
  WEAPON_UNFOLD: "武器展开",
  WEAPON_LOCK: "武器锁定",
  WAIT: "等待",
};
const PRODUCT_STATE = {
  WAITING_SELECTION: ["等待选择", "请选择单位并下达操作。"],
  WAITING_OPPONENT: ["等待对方", "本方操作已提交，正在等待对方。"],
  ADVANCING: ["推进中", "双方操作已齐备，引擎正在推进。"],
  TERMINAL: ["已终局", "对局已经结束。"],
};

async function requestJson(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const body = await response.json();
  if (!response.ok) {
    const error = new Error(body.error || `${response.status}`);
    error.code = body.code || "request_failed";
    throw error;
  }
  return body;
}

function nextRequestId(kind) {
  const value = `web-${pageNonce}-${kind}-${previewSequence++}`;
  lastRequestId = value;
  return value;
}

function visibleUnits() {
  return current?.scene?.frame?.viewer_units || [];
}

function unitById(unitId) {
  return visibleUnits().find((unit) => String(unit.operator_id) === String(unitId));
}

function isFriendlySelectable(unit) {
  return Boolean(unit)
    && String(unit.side).toLowerCase() === current?.side
    && unit.category === "operator"
    && !unit.destroyed;
}

function selectedIds() {
  return Array.from(selectedUnitIds).map(Number).sort((left, right) => left - right);
}

function renderHexInfo(value) {
  $("hex-info").textContent = value
    ? `row=${value.row} column=${value.column} position=${value.position_id} · terrain=${value.terrain_semantic} (${value.terrain_code ?? "UNKNOWN"}) · elevation=${value.elevation ?? "UNKNOWN"} · roads=${window.ReplayMapRenderer.formatEdgeFacts(value.roads)} · rivers=${window.ReplayMapRenderer.formatEdgeFacts(value.rivers)} · units=${(value.units || []).map((unit) => unit.operator_id).join(",") || "none"}`
    : "移动鼠标查看六边形坐标";
}

function plannedPaths() {
  if (!operationPreview) return [];
  return operationPreview.per_unit_results
    .filter((result) => result.accepted_for_preview && result.path.length)
    .map((result) => {
      const source = unitById(result.unit_id)?.current_hex;
      return {
        unit_id: result.unit_id,
        path: [source, ...result.path].filter(Number.isInteger),
      };
    });
}

function renderMap() {
  if (!mapRenderer || !current?.scene) return;
  mapRenderer.setActionTargets({
    shoot_target_ids: activeTool === "SHOOT"
      ? capabilities?.shoot_target_ids || [] : [],
    preview_move_position: targetHex,
    preview_shoot_target_id: targetUnitId,
    planned_paths: plannedPaths(),
  });
  mapRenderer.render(current.scene.frame, selectedIds());
}

function statusText() {
  if (isPlanning) return ["正在规划", "服务端正在逐单位验证并生成预览。"];
  if (isExecuting) return ["正在提交", "正在安全提交本方操作，请勿重复点击。"];
  if (lastError) return ["操作失败", lastError];
  const state = current?.product_status?.product_state || "WAITING_SELECTION";
  return PRODUCT_STATE[state] || PRODUCT_STATE.WAITING_SELECTION;
}

function renderStatus() {
  const [label, message] = statusText();
  $("status-label").textContent = label;
  $("status-message").textContent = message;
  $("product-state-badge").textContent = label;
  $("product-state-badge").className = `badge ${label === "已终局" ? "no" : "warn"}`;
}

function renderSelection() {
  const root = $("selection-summary");
  const selected = selectedIds().map(unitById).filter(Boolean);
  if (!selected.length) {
    root.textContent = "尚未选择己方单位";
    return;
  }
  const groups = new Map();
  for (const unit of selected) {
    const label = unit.operator_type || unit.subtype || unit.icon_key || "单位";
    groups.set(label, (groups.get(label) || 0) + 1);
  }
  root.replaceChildren();
  const title = document.createElement("strong");
  title.textContent = `已选择 ${selected.length} 个单位`;
  const details = document.createElement("span");
  details.textContent = Array.from(groups.entries())
    .map(([name, count]) => `${name} × ${count}`).join(" · ");
  root.append(title, details);
}

function operationCount(kind) {
  if (kind === "WAIT" && !selectedUnitIds.size) return "整方可用";
  const applicable = capabilities?.operations?.[kind]?.applicable_count || 0;
  return `${applicable}/${selectedUnitIds.size} 可用`;
}

function renderToolbar() {
  const canOperate = current?.product_status?.can_operate && !isPlanning && !isExecuting;
  for (const button of operationButtons) {
    const kind = button.dataset.operation;
    button.classList.toggle("active", activeTool === kind);
    button.querySelector("small").textContent = operationCount(kind);
    const applicable = kind === "WAIT" && !selectedUnitIds.size
      ? 1 : capabilities?.operations?.[kind]?.applicable_count || 0;
    button.disabled = !canOperate || applicable === 0;
  }
  $("cancel-operation").disabled = !activeTool && !operationPreview;
  const partialReady = !operationPreview?.partial_confirmation_required
    || $("confirm-partial").checked;
  $("execute-operation").disabled = !canOperate
    || !operationPreview?.can_execute || !partialReady;
}

function renderWeaponChoices() {
  const root = $("weapon-choices");
  root.replaceChildren();
  if (!operationPreview || operationPreview.operation_type !== "SHOOT") {
    root.hidden = true;
    return;
  }
  const ambiguous = operationPreview.per_unit_results.filter((result) => (
    result.reject_code === "weapon_choice_required"
    && result.weapon_options.length > 1
  ));
  if (!ambiguous.length) {
    root.hidden = true;
    return;
  }
  root.hidden = false;
  const heading = document.createElement("strong");
  heading.textContent = "请选择武器后重新预览";
  root.appendChild(heading);
  for (const result of ambiguous) {
    const label = document.createElement("label");
    label.textContent = `${result.display_name}：`;
    const select = document.createElement("select");
    select.dataset.weaponUnit = String(result.unit_id);
    for (const option of result.weapon_options) {
      const element = document.createElement("option");
      element.value = String(option.weapon_id);
      element.textContent = option.summary;
      select.appendChild(element);
    }
    label.appendChild(select);
    root.appendChild(label);
  }
  const apply = document.createElement("button");
  apply.type = "button";
  apply.textContent = "应用武器选择";
  apply.addEventListener("click", () => previewOperation("SHOOT", {
    target_id: targetUnitId,
    weapon_choices: Object.fromEntries(
      Array.from(root.querySelectorAll("select")).map((select) => (
        [select.dataset.weaponUnit, Number(select.value)]
      )),
    ),
  }));
  root.appendChild(apply);
}

function renderPreview() {
  const root = $("operation-preview");
  const confirmation = $("partial-confirmation");
  if (!operationPreview) {
    root.className = "operation-preview empty";
    root.textContent = "选择功能和目标后，这里会显示逐单位的路径、时间或失败原因。";
    $("preview-counts").textContent = "尚无预览";
    confirmation.hidden = true;
    $("confirm-partial").checked = false;
    renderWeaponChoices();
    return;
  }
  root.className = "operation-preview";
  root.replaceChildren();
  $("preview-counts").textContent = operationPreview.whole_side_wait
    ? "整方本轮等待"
    : `已选 ${operationPreview.selected_count} · 可执行 ${operationPreview.executable_count} · 不可执行 ${operationPreview.rejected_count}`;
  for (const result of operationPreview.per_unit_results) {
    const item = document.createElement("article");
    item.className = `unit-preview ${result.accepted_for_preview ? "accepted" : "rejected"}`;
    const title = document.createElement("strong");
    title.textContent = result.display_name;
    const summary = document.createElement("p");
    summary.textContent = result.accepted_for_preview
      ? result.human_readable_summary
      : `不可执行：${result.reject_message || "当前条件不满足"}`;
    item.append(title, summary);
    if (result.path.length) {
      const path = document.createElement("p");
      path.className = "path-summary";
      path.textContent = `路径 ${result.path.join(" → ")} · 预计 ${result.estimated_duration_ms} ms`;
      item.appendChild(path);
    }
    root.appendChild(item);
  }
  if (operationPreview.whole_side_wait) {
    const item = document.createElement("article");
    item.className = "unit-preview accepted";
    item.textContent = "本方本轮不下达新操作。";
    root.appendChild(item);
  }
  confirmation.hidden = !operationPreview.partial_confirmation_required;
  if (!operationPreview.partial_confirmation_required) {
    $("confirm-partial").checked = false;
  }
  renderWeaponChoices();
}

function render() {
  if (!current) return;
  $("side-badge").textContent = current.side.toUpperCase();
  $("side-summary").textContent = `${current.side.toUpperCase()} 控制席位`;
  $("frame-index").textContent = String(current.product_status.frame_index);
  $("time-summary").textContent = `仿真时间 ${current.product_status.sim_time_ms} ms`;
  $("map-mode").textContent = `${current.scene.map.mode} · ${current.scene.map.map_id}`;
  $("debug-session").textContent = current.session_id;
  $("debug-side").textContent = current.side.toUpperCase();
  $("debug-frame").textContent = String(current.product_status.frame_index);
  $("debug-sim-time").textContent = `${current.product_status.sim_time_ms} ms`;
  $("debug-request").textContent = lastRequestId || "—";
  renderSelection();
  renderPreview();
  renderToolbar();
  renderMap();
  renderStatus();
}

function clearOperation({ clearSelection = false } = {}) {
  activeTool = null;
  targetHex = null;
  targetUnitId = null;
  operationPreview = null;
  executeRequestId = null;
  lastError = null;
  $("confirm-partial").checked = false;
  if (clearSelection) selectedUnitIds.clear();
  $("tool-instruction").textContent = selectedUnitIds.size
    ? "请选择操作。" : "请选择一个或多个己方单位。";
  render();
}

async function loadCapabilities() {
  if (!current || !selectedUnitIds.size) {
    capabilities = {
      selected_count: 0,
      selected_units: [],
      operations: {},
      shoot_target_ids: [],
    };
    render();
    return;
  }
  try {
    capabilities = await requestJson("/api/product/capabilities", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token, actor_ids: selectedIds() }),
    });
    lastError = null;
  } catch (error) {
    lastError = `无法读取单位操作：${error.message}`;
  }
  render();
}

async function handleUnitSelection(operatorId, modifiers = {}) {
  const unit = unitById(operatorId);
  if (activeTool === "SHOOT" && !isFriendlySelectable(unit)) {
    if ((capabilities?.shoot_target_ids || []).map(String).includes(String(operatorId))) {
      targetUnitId = Number(operatorId);
      await previewOperation("SHOOT", { target_id: targetUnitId });
    }
    return;
  }
  if (!isFriendlySelectable(unit)) return;
  const key = Number(operatorId);
  if (modifiers.ctrlKey || modifiers.metaKey) {
    if (selectedUnitIds.has(key)) selectedUnitIds.delete(key);
    else selectedUnitIds.add(key);
  } else if (modifiers.shiftKey) {
    selectedUnitIds.add(key);
  } else {
    selectedUnitIds.clear();
    selectedUnitIds.add(key);
  }
  clearOperation();
  await loadCapabilities();
}

async function handleHexSelection(value) {
  renderHexInfo(value);
  if (activeTool === "MOVE") {
    targetHex = Number(value.position_id);
    await previewOperation("MOVE", { destination_hex: targetHex });
    return;
  }
  if (!activeTool) {
    selectedUnitIds.clear();
    await loadCapabilities();
  }
}

async function activateTool(kind) {
  if (isPlanning || isExecuting || !current?.product_status?.can_operate) return;
  lastError = null;
  operationPreview = null;
  executeRequestId = null;
  targetHex = null;
  targetUnitId = null;
  activeTool = kind;
  if (kind === "MOVE") {
    $("tool-instruction").textContent = "请在地图上点击目的六边形。";
  } else if (kind === "SHOOT") {
    $("tool-instruction").textContent = "请点击地图上高亮的可见敌方目标。";
  } else if (kind === "WAIT" && !selectedUnitIds.size) {
    if (!window.confirm("确认本方本轮不下达新操作？")) {
      clearOperation();
      return;
    }
    await previewOperation("WAIT", { whole_side_wait: true });
  } else {
    await previewOperation(kind);
  }
  render();
}

async function previewOperation(kind, extra = {}) {
  if (isPlanning || isExecuting) return;
  isPlanning = true;
  lastError = null;
  operationPreview = null;
  render();
  try {
    operationPreview = await requestJson("/api/product/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token,
        request_id: nextRequestId(`preview-${kind.toLowerCase()}`),
        actor_ids: extra.whole_side_wait ? [] : selectedIds(),
        operation_type: kind,
        movement_mode: "NORMAL",
        ...extra,
      }),
    });
    executeRequestId = nextRequestId("execute");
    $("tool-instruction").textContent = operationPreview.can_execute
      ? "请检查逐单位结果，然后点击执行。"
      : "当前没有单位可以执行该操作。";
  } catch (error) {
    lastError = error.message;
  } finally {
    isPlanning = false;
    render();
  }
}

async function executeOperation() {
  if (isExecuting || !operationPreview?.can_execute || !executeRequestId) return;
  if (operationPreview.partial_confirmation_required
      && !$("confirm-partial").checked) {
    lastError = "请先确认仅执行可执行单位。";
    render();
    return;
  }
  isExecuting = true;
  lastError = null;
  render();
  try {
    const result = await requestJson("/api/product/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token,
        preview_token: operationPreview.preview_token,
        request_id: executeRequestId,
        confirm_partial: $("confirm-partial").checked,
      }),
    });
    operationPreview = null;
    activeTool = null;
    targetHex = null;
    targetUnitId = null;
    if (result.product_state === "WAITING_OPPONENT") {
      lastError = null;
    }
    await refresh();
  } catch (error) {
    lastError = error.message;
  } finally {
    isExecuting = false;
    render();
  }
}

async function refresh() {
  try {
    const next = await requestJson(
      `/api/product/bootstrap?token=${encodeURIComponent(token)}`,
    );
    const frameChanged = lastFrameIndex !== null
      && next.product_status.frame_index !== lastFrameIndex;
    lastFrameIndex = next.product_status.frame_index;
    current = next;
    if (!mapRenderer) {
      mapRenderer = new window.ReplayMapRenderer.MapRenderer($("battlefield"), {
        onUnitSelected: handleUnitSelection,
        onHexHovered: renderHexInfo,
        onHexSelected: handleHexSelection,
      });
      mapRenderer.setMap(current.scene.map);
    }
    const retained = selectedIds().filter((unitId) => (
      isFriendlySelectable(unitById(unitId))
    ));
    selectedUnitIds.clear();
    retained.forEach((unitId) => selectedUnitIds.add(unitId));
    if (current.product_status.terminal) {
      selectedUnitIds.clear();
      clearOperation();
    } else if (frameChanged) {
      activeTool = null;
      targetHex = null;
      targetUnitId = null;
      operationPreview = null;
      executeRequestId = null;
      await loadCapabilities();
    } else {
      render();
    }
    $("error-banner").hidden = true;
  } catch (error) {
    lastError = error.message;
    $("error-banner").hidden = false;
    $("error-banner").textContent = `页面状态读取失败：${error.message}`;
    renderStatus();
  }
}

operationButtons.forEach((button) => button.addEventListener(
  "click", () => activateTool(button.dataset.operation),
));
$("cancel-operation").addEventListener("click", () => clearOperation());
$("execute-operation").addEventListener("click", executeOperation);
$("confirm-partial").addEventListener("change", renderToolbar);
$("reset-map-view").addEventListener("click", () => mapRenderer?.resetView());
window.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (activeTool || operationPreview) clearOperation();
  else clearOperation({ clearSelection: true });
});

refresh();
setInterval(() => { if (!isPlanning && !isExecuting) refresh(); }, 1000);
