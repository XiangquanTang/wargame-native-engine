"use strict";

const token = new URLSearchParams(window.location.search).get("token");
const selectedUnitIds = new Set();
let current = null;
let capabilities = null;
let selectedCapability = null;
const selectedWeaponIds = new Map();
let selectedGuidedPlatform = null;
let selectedIndirectOption = null;
let activeTool = null;
let targetHex = null;
let targetUnitId = null;
let operationPreview = null;
let isPlanning = false;
let isExecuting = false;
let isRefreshing = false;
let lastError = null;
let lastStateReadError = null;
let lastNotice = null;
let lastFrameIndex = null;
let lastRequestId = null;
let previewSequence = 0;
let executeRequestId = null;
let interactionEpoch = 0;
let tacticalOverlay = null;
let visibilityOverlayEnabled = false;
let weaponEnvelopeEnabled = false;
let overlayRequestEpoch = 0;
const tacticalOverlayCache = new Map();
let mapRenderer = null;
let staticMapContext = null;
const capabilityUI = window.HumanCapabilityUI;
const pageNonce = globalThis.crypto?.randomUUID?.()
  || `${Date.now()}-${Math.floor(performance.now())}`;
const $ = (id) => document.getElementById(id);
const CAPABILITY_LABELS = {
  MOVE: "机动",
  DIRECT_FIRE: "直瞄射击",
  GUIDED_FIRE: "引导射击",
  INDIRECT_FIRE: "间接火力",
  BOARD: "搭载",
  UNLOAD: "下车",
  CAPTURE: "夺控",
  ENTER_CONCEALMENT: "进入隐蔽",
  STOP_MOVE: "停止",
  WEAPON_UNFOLD: "武器展开",
  WEAPON_LOCK: "武器锁定",
  WAIT: "等待",
};
const PRODUCT_STATE = {
  WAITING_SELECTION: ["等待选择", "请选择单位并下达操作。"],
  WAITING_OPPONENT: ["等待对方", "本方操作已提交，正在等待对方。"],
  COMMAND_PENDING: ["命令已入队", "命令将在目标权威 Tick 重新校验并执行；可以继续操作其他单位。"],
  COMMAND_REJECTED: ["命令未执行", "命令在应用时已不再合法；可以根据最新态势重新下达。"],
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
    && unit.map_visible !== false
    && !unit.destroyed;
}

function selectedIds() {
  return Array.from(selectedUnitIds).map(Number).sort((left, right) => left - right);
}

function advanceInteractionEpoch() {
  interactionEpoch += 1;
  return interactionEpoch;
}

function reconcileInteractionDraft() {
  const before = selectedIds();
  const retained = before.filter((unitId) => isFriendlySelectable(unitById(unitId)));
  if (retained.length === before.length) return false;
  selectedUnitIds.clear();
  retained.forEach((unitId) => selectedUnitIds.add(unitId));
  selectedCapability = null;
  selectedWeaponIds.clear();
  selectedGuidedPlatform = null;
  selectedIndirectOption = null;
  activeTool = null;
  targetHex = null;
  targetUnitId = null;
  operationPreview = null;
  executeRequestId = null;
  invalidateTacticalOverlay();
  advanceInteractionEpoch();
  return true;
}

function invalidateTacticalOverlay({ clearCache = false } = {}) {
  overlayRequestEpoch += 1;
  tacticalOverlay = null;
  if (clearCache) tacticalOverlayCache.clear();
}

function tacticalOverlayHexes(kind) {
  if (!tacticalOverlay) return [];
  if (kind === "visibility") {
    return tacticalOverlay.visibility_field?.hex_ids || [];
  }
  if (kind === "contacts") {
    return tacticalOverlay.confirmed_contacts?.hex_ids || [];
  }
  const unitIds = selectedIds();
  const selectedWeaponId = unitIds.length === 1
    ? selectedWeaponIds.get(unitIds[0]) : null;
  if (selectedCapability === "DIRECT_FIRE" && Number.isInteger(selectedWeaponId)) {
    return capabilityUI.weaponEnvelopeHexes(tacticalOverlay, selectedWeaponId);
  }
  return tacticalOverlay.weapon_envelope?.hex_ids || [];
}

function directFireUnit() {
  const unitIds = selectedIds();
  if (unitIds.length !== 1) return null;
  return capabilityUI.directFireUnit(capabilities, unitIds[0]);
}

function directFireWeapons() {
  return directFireUnit()?.weapons || [];
}

function activeShootTargetIds() {
  if (activeTool !== "SHOOT" || !selectedIds().length) return [];
  if (!selectedIds().every((unitId) => Number.isInteger(
    selectedWeaponIds.get(unitId)
  ))) return [];
  const values = capabilities?.direct_fire?.selectable_target_ids;
  return Array.isArray(values) ? [...values] : [];
}

function selectedWeaponChoices() {
  const unitIds = selectedIds();
  if (selectedCapability !== "DIRECT_FIRE" || !unitIds.length
      || !unitIds.every((unitId) => Number.isInteger(
        selectedWeaponIds.get(unitId)
      ))) return {};
  return { weapon_choices: Object.fromEntries(
    unitIds.map((unitId) => [unitId, selectedWeaponIds.get(unitId)])
  ) };
}

function guidedFirePlatforms() {
  const unitIds = selectedIds();
  if (unitIds.length !== 1) return [];
  return capabilityUI.guidedFirePlatforms(capabilities, unitIds[0]);
}

function activeGuidedTargetIds() {
  if (activeTool !== "GUIDED_FIRE" || !selectedGuidedPlatform) return [];
  return [...selectedGuidedPlatform.legal_target_ids];
}

function indirectFireOptions() {
  const unitIds = selectedIds();
  if (unitIds.length !== 1) return [];
  return capabilityUI.indirectFireOptions(capabilities, unitIds[0]);
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
  mapRenderer.setIndirectFireAreas(
    current.scene?.overlays?.indirect_fire_mission_areas
      || current.scene?.overlays?.indirect_fire_active_areas || [],
  );
  mapRenderer.setTacticalOverlay({
    side: current.side,
    visibility_hex_ids: visibilityOverlayEnabled
      ? tacticalOverlayHexes("visibility") : [],
    weapon_envelope_hex_ids: weaponEnvelopeEnabled
      ? tacticalOverlayHexes("weapon") : [],
    confirmed_contact_hex_ids: tacticalOverlayHexes("contacts"),
  });
  mapRenderer.setActionTargets({
    shoot_target_ids: activeTool === "SHOOT"
      ? activeShootTargetIds()
      : activeTool === "GUIDED_FIRE" ? activeGuidedTargetIds() : [],
    preview_move_position: activeTool === "MOVE" ? targetHex : null,
    preview_area_position: activeTool === "INDIRECT_FIRE" ? targetHex : null,
    preview_shoot_target_id: targetUnitId,
    planned_paths: plannedPaths(),
  });
  mapRenderer.render(current.scene.frame, selectedIds());
}

function statusText() {
  if (isPlanning) return ["正在规划", "服务端正在逐单位验证并生成预览。"];
  if (isExecuting) return ["正在提交", "正在安全提交本方操作，请勿重复点击。"];
  if (lastStateReadError) return ["页面状态读取失败", lastStateReadError];
  if (lastError) return ["操作失败", lastError];
  if (lastNotice) return ["命令已入队", lastNotice];
  const state = current?.product_status?.product_state || "WAITING_SELECTION";
  if (state === "COMMAND_REJECTED") {
    const code = current?.product_status?.last_command?.error_code;
    const reasons = {
      AMMUNITION_UNAVAILABLE: "所选武器弹药不足。",
      WEAPON_COOLDOWN_ACTIVE: "所选武器仍在冷却。",
      TARGET_OUT_OF_RANGE: "目标不在所选武器射程内。",
      TARGET_DOMAIN_UNSUPPORTED: "所选武器不适用于该目标类型。",
      WEAPON_NOT_UNFOLDED: "所选武器尚未展开。",
      ATTACKER_MOVING: "单位当前运动状态不允许该武器射击。",
    };
    return ["命令未执行", reasons[code] || `引擎拒绝攻击意图：${code || "条件不满足"}`];
  }
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
  if (selected.length === 1) {
    const carried = Array.isArray(selected[0].carried_units)
      ? selected[0].carried_units : [];
    if (carried.length) {
      const transport = document.createElement("small");
      transport.textContent = `已搭载：${carried.map((value) => (
        `${value.name || "单位"} ${value.operator_id} · ${value.boarding_state}`
      )).join("；")}`;
      root.appendChild(transport);
    }
    const boardingMessages = ["BOARD", "UNLOAD"].flatMap((kind) => (
      capabilities?.operations?.[kind]?.unavailable || []
    )).filter((value) => selectedUnitIds.has(Number(value.unit_id)))
      .map((value) => value.message).filter(Boolean);
    if (boardingMessages.length) {
      const status = document.createElement("small");
      status.textContent = Array.from(new Set(boardingMessages)).join("；");
      root.appendChild(status);
    }
  }
}

function operationCount(kind) {
  if (kind === "WAIT" && !selectedUnitIds.size) return "整方可用";
  const applicable = capabilities?.operations?.[kind]?.applicable_count || 0;
  const unavailable = capabilities?.operations?.[kind]?.unavailable || [];
  if (!applicable && selectedUnitIds.size && unavailable.length) {
    return unavailable[0].message || `${applicable}/${selectedUnitIds.size} 可用`;
  }
  return `${applicable}/${selectedUnitIds.size} 可用`;
}

function operationHint(kind) {
  const unavailable = capabilities?.operations?.[kind]?.unavailable || [];
  const messages = Array.from(new Set(unavailable.map((item) => item.message).filter(Boolean)));
  return messages.join("；");
}

function renderCapabilityPanel() {
  const root = $("capability-panel");
  root.replaceChildren();
  let values = capabilityUI.availableCapabilities(capabilities);
  if (!selectedUnitIds.size) {
    values = [{
      capability_type: "WAIT",
      operation_type: "WAIT",
      status: "AVAILABLE",
      metadata: { applicable_count: 1, whole_side: true },
    }];
  }
  for (const capability of values) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.capability = capability.capability_type;
    button.dataset.operation = capability.operation_type;
    button.classList.toggle(
      "active", selectedCapability === capability.capability_type,
    );
    const label = document.createElement("span");
    label.textContent = CAPABILITY_LABELS[capability.capability_type]
      || capability.capability_type;
    const count = document.createElement("small");
    count.textContent = operationCount(capability.operation_type);
    button.title = operationHint(capability.operation_type);
    button.append(label, count);
    root.appendChild(button);
  }
}

function renderToolbar() {
  renderCapabilityPanel();
  const canOperate = current?.product_status?.can_operate && !isPlanning && !isExecuting;
  for (const button of $("capability-panel").querySelectorAll("[data-capability]")) {
    button.disabled = !canOperate;
  }
  $("cancel-operation").disabled = !activeTool && !operationPreview;
  const partialReady = !operationPreview?.partial_confirmation_required
    || $("confirm-partial").checked;
  $("execute-operation").disabled = !canOperate
    || !operationPreview?.can_execute || !partialReady;
  const singleSelected = selectedUnitIds.size === 1;
  $("toggle-visibility-overlay").disabled = !singleSelected;
  $("toggle-weapon-envelope").disabled = !singleSelected;
  $("toggle-visibility-overlay").classList.toggle(
    "active", visibilityOverlayEnabled,
  );
  $("toggle-weapon-envelope").classList.toggle(
    "active", weaponEnvelopeEnabled,
  );
}

function renderDirectFirePanel() {
  const panel = $("direct-fire-panel");
  const root = $("weapon-choices");
  root.replaceChildren();
  const units = (capabilities?.direct_fire?.units || []).filter(
    (unit) => selectedUnitIds.has(Number(unit.unit_id))
  );
  if (!units.length) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  $("direct-fire-unit").textContent = units.length === 1
    ? units[0].display_name : `${units.length} 个单位的携行武器`;
  for (const unit of units) {
    const group = document.createElement("section");
    group.className = "weapon-unit-group";
    const heading = document.createElement("strong");
    heading.textContent = unit.display_name;
    group.appendChild(heading);
    for (const weapon of unit.weapons) {
    const selectedWeaponId = selectedWeaponIds.get(Number(unit.unit_id));
    const button = document.createElement("button");
    button.type = "button";
    button.className = "weapon-card";
    button.classList.toggle(
      "active", Number(weapon.weapon_id) === selectedWeaponId,
    );
    button.disabled = !current?.product_status?.can_operate
      || isPlanning || isExecuting;
    button.dataset.weaponId = String(weapon.weapon_id);
    button.dataset.unitId = String(unit.unit_id);
    const marker = document.createElement("span");
    marker.textContent = Number(weapon.weapon_id) === selectedWeaponId ? "◉" : "○";
    const content = document.createElement("span");
    const title = document.createElement("span");
    title.className = "weapon-title";
    title.textContent = `${weapon.display_name} · ID ${weapon.weapon_id}`;
    const facts = document.createElement("span");
    facts.className = "weapon-facts";
    const range = weapon.maximum_range === null
      ? "未知" : `${weapon.maximum_range} hex`;
    const domains = weapon.target_domains?.join(" / ") || "未知";
    const ammunition = weapon.ammunition === null ? "未知" : weapon.ammunition;
    const shared = weapon.shared_ammunition
      ? `（与武器 ${weapon.shared_with_weapon_ids.join(" / ")} 共用）` : "";
    const modes = weapon.attack_modes?.length
      ? weapon.attack_modes.join(" / ") : "模式未知";
    const cooldown = weapon.cooldown_remaining_seconds === null
      ? "未知" : `${weapon.cooldown_remaining_seconds} s`;
    facts.textContent = `模式 ${modes} · 射程 ${range} · 目标 ${domains} · 弹药 ${ammunition}${shared} · 冷却 ${cooldown} · `;
    const status = document.createElement("span");
    status.className = "weapon-status";
    status.textContent = `${weapon.ready_state} · 当前合法目标 ${weapon.legal_target_ids.length} 个 · 提交后由引擎裁决`;
    facts.appendChild(status);
    content.append(title, facts);
    button.append(marker, content);
    group.appendChild(button);
    }
    root.appendChild(group);
  }
}

function renderGuidedFirePanel() {
  const panel = $("guided-fire-panel");
  const root = $("guided-fire-choices");
  root.replaceChildren();
  const values = guidedFirePlatforms();
  if (selectedCapability !== "GUIDED_FIRE" || !values.length) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  for (const value of values) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "weapon-card";
    const active = selectedGuidedPlatform
      && selectedGuidedPlatform.shooter_id === value.shooter_id
      && selectedGuidedPlatform.weapon_id === value.weapon_id;
    button.classList.toggle("active", Boolean(active));
    button.disabled = !current?.product_status?.can_operate
      || isPlanning || isExecuting;
    button.dataset.guidedShooterId = String(value.shooter_id);
    button.dataset.guidedWeaponId = String(value.weapon_id);
    const marker = document.createElement("span");
    marker.textContent = active ? "◉" : "○";
    const content = document.createElement("span");
    const title = document.createElement("span");
    title.className = "weapon-title";
    title.textContent = `${value.shooter_display_name} · ${value.weapon_display_name}`;
    const facts = document.createElement("span");
    facts.className = "weapon-facts";
    const range = value.maximum_range == null ? "未知" : `${value.maximum_range} hex`;
    facts.textContent = `发射平台 ${value.shooter_id} · 武器 ${value.weapon_id} · 射程 ${range} · ${value.legal_target_ids.length} 个合法目标`;
    content.append(title, facts);
    button.append(marker, content);
    root.appendChild(button);
  }
}

function renderIndirectFirePanel() {
  const panel = $("indirect-fire-panel");
  const root = $("indirect-fire-choices");
  root.replaceChildren();
  const values = indirectFireOptions();
  if (selectedCapability !== "INDIRECT_FIRE" || !values.length) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  for (const value of values) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "weapon-card";
    const active = selectedIndirectOption
      && selectedIndirectOption.weapon_id === value.weapon_id;
    button.classList.toggle("active", Boolean(active));
    button.disabled = !current?.product_status?.can_operate
      || isPlanning || isExecuting;
    button.dataset.indirectWeaponId = String(value.weapon_id);
    const marker = document.createElement("span");
    marker.textContent = active ? "◉" : "○";
    const content = document.createElement("span");
    const title = document.createElement("span");
    title.className = "weapon-title";
    title.textContent = `${value.weapon_display_name} · ID ${value.weapon_id}`;
    const facts = document.createElement("span");
    facts.className = "weapon-facts";
    facts.textContent = `${value.artillery_class} · ${value.range_policy} · 目标由服务器按六边格验证`;
    content.append(title, facts);
    button.append(marker, content);
    root.appendChild(button);
  }
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
    renderDirectFirePanel();
    renderGuidedFirePanel();
    renderIndirectFirePanel();
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
  renderDirectFirePanel();
  renderGuidedFirePanel();
  renderIndirectFirePanel();
}

function render({ redrawMap = true } = {}) {
  if (!current) return;
  $("side-badge").textContent = current.side.toUpperCase();
  $("side-summary").textContent = current.control_mode === "realtime"
    ? `${current.side.toUpperCase()} 实时连续控制席位`
    : `${current.side.toUpperCase()} 控制席位`;
  $("frame-index").textContent = String(current.product_status.frame_index);
  $("time-summary").textContent = `仿真时间 ${current.product_status.sim_time_ms} ms`;
  $("map-mode").textContent = `${current.scene.map.mode} · ${current.scene.map.map_id}`;
  $("debug-session").textContent = current.session_id;
  $("debug-side").textContent = current.side.toUpperCase();
  $("debug-frame").textContent = String(current.product_status.frame_index);
  $("debug-sim-time").textContent = `${current.product_status.sim_time_ms} ms`;
  $("debug-request").textContent = lastRequestId || "—";
  $("execute-operation").textContent = current.control_mode === "realtime"
    ? "下达命令" : "执行";
  renderSelection();
  renderPreview();
  renderToolbar();
  if (redrawMap) renderMap();
  renderStatus();
}

function cachedStaticMap(state) {
  if (!state || !staticMapContext) return null;
  if (state.session_id !== staticMapContext.sessionId
      || state.side !== staticMapContext.side) return null;
  return staticMapContext.map;
}

function isStaticMap(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function hasStaticMapIdentity(state) {
  return typeof state?.session_id === "string" && state.session_id.length > 0
    && typeof state?.side === "string" && state.side.length > 0;
}

function clearStaticMapContext() {
  staticMapContext = null;
}

function installStaticMap(state) {
  if (!state?.scene) {
    throw new Error("页面状态响应缺少场景数据");
  }
  const cachedMap = cachedStaticMap(state);
  if (cachedMap) {
    state.scene.map = cachedMap;
    return true;
  }
  if (!isStaticMap(state.scene.map)) return false;
  if (!hasStaticMapIdentity(state)) {
    throw new Error("页面状态响应缺少会话或阵营标识");
  }

  try {
    const renderer = mapRenderer || new window.ReplayMapRenderer.MapRenderer(
      $("battlefield"),
      {
        onUnitSelected: handleUnitSelection,
        onHexHovered: renderHexInfo,
        onHexSelected: handleHexSelection,
      },
    );
    mapRenderer = renderer;
    renderer.setMap(state.scene.map);
    staticMapContext = {
      sessionId: state.session_id,
      side: state.side,
      map: state.scene.map,
    };
    return true;
  } catch (error) {
    clearStaticMapContext();
    throw error;
  }
}

async function loadDisplayState() {
  const includeMap = !cachedStaticMap(current);
  let next = await requestJson(
    `/api/product/bootstrap?token=${encodeURIComponent(token)}`
      + `&include_map=${includeMap ? "1" : "0"}`,
  );
  if (installStaticMap(next)) return next;

  if (staticMapContext && !cachedStaticMap(next)) clearStaticMapContext();
  next = await requestJson(
    `/api/product/bootstrap?token=${encodeURIComponent(token)}&include_map=1`,
  );
  if (!installStaticMap(next)) {
    clearStaticMapContext();
    throw new Error("当前会话的静态地图缺失（完整地图重取失败）");
  }
  return next;
}

function clearOperation({ clearSelection = false } = {}) {
  selectedCapability = null;
  selectedWeaponIds.clear();
  selectedGuidedPlatform = null;
  selectedIndirectOption = null;
  activeTool = null;
  targetHex = null;
  targetUnitId = null;
  operationPreview = null;
  executeRequestId = null;
  lastError = null;
  lastNotice = null;
  $("confirm-partial").checked = false;
  if (clearSelection) {
    selectedUnitIds.clear();
    invalidateTacticalOverlay();
  }
  advanceInteractionEpoch();
  $("tool-instruction").textContent = selectedUnitIds.size
    ? "请选择操作。" : "请选择一个或多个己方单位。";
  render();
}

async function loadCapabilities({ expectedEpoch = interactionEpoch } = {}) {
  const requestedActorIds = selectedIds();
  if (!current || !selectedUnitIds.size) {
    if (expectedEpoch !== interactionEpoch) return;
    capabilities = {
      schema: capabilityUI.schema,
      selected_count: 0,
      selected_units: [],
      operations: {},
      shoot_target_ids: [],
      capabilities: [],
      direct_fire: { units: [] },
      guided_fire: { options: [] },
      indirect_fire: { options: [] },
      capture: { options: [] },
    };
    render();
    return;
  }
  try {
    const nextCapabilities = await requestJson("/api/product/capabilities", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token, actor_ids: requestedActorIds }),
    });
    if (expectedEpoch !== interactionEpoch
        || JSON.stringify(requestedActorIds) !== JSON.stringify(selectedIds())) return;
    capabilities = nextCapabilities;
    for (const [unitId, weaponId] of selectedWeaponIds) {
      const weapons = capabilityUI.directFireWeapons(capabilities, unitId);
      if (!weapons.some((weapon) => Number(weapon.weapon_id) === weaponId)) {
        selectedWeaponIds.delete(unitId);
        targetUnitId = null;
        operationPreview = null;
        executeRequestId = null;
      }
    }
    lastError = null;
  } catch (error) {
    if (expectedEpoch !== interactionEpoch) return;
    lastError = `无法读取单位操作：${error.message}`;
  }
  render();
}

async function loadTacticalOverlay({ force = false } = {}) {
  const unitIds = selectedIds();
  if (!current || unitIds.length !== 1
      || (!visibilityOverlayEnabled && !weaponEnvelopeEnabled)) {
    invalidateTacticalOverlay();
    render();
    return;
  }
  const unitId = unitIds[0];
  const observationVersion = current.observation_version;
  const frameIndex = current.product_status.frame_index;
  const cacheKey = `${frameIndex}:${observationVersion}:${unitId}`;
  if (!force && tacticalOverlayCache.has(cacheKey)) {
    tacticalOverlay = tacticalOverlayCache.get(cacheKey);
    render();
    return;
  }
  const requestEpoch = ++overlayRequestEpoch;
  tacticalOverlay = null;
  render();
  try {
    const result = await requestJson("/api/product/tactical-overlay", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token,
        observation_version: observationVersion,
        unit_id: unitId,
      }),
    });
    if (requestEpoch !== overlayRequestEpoch
        || current?.observation_version !== observationVersion
        || selectedIds().length !== 1
        || selectedIds()[0] !== unitId) return;
    tacticalOverlayCache.set(cacheKey, result);
    tacticalOverlay = result;
    lastError = null;
  } catch (error) {
    if (requestEpoch !== overlayRequestEpoch) return;
    lastError = `无法读取战术覆盖：${error.message}`;
  }
  render();
}

async function toggleTacticalOverlay(kind) {
  if (selectedUnitIds.size !== 1) return;
  if (kind === "visibility") {
    visibilityOverlayEnabled = !visibilityOverlayEnabled;
  } else {
    weaponEnvelopeEnabled = !weaponEnvelopeEnabled;
  }
  if (!visibilityOverlayEnabled && !weaponEnvelopeEnabled) {
    invalidateTacticalOverlay();
    render();
    return;
  }
  await loadTacticalOverlay();
}

async function handleUnitSelection(operatorId, modifiers = {}) {
  const unit = unitById(operatorId);
  if (activeTool === "INDIRECT_FIRE" && selectedIndirectOption
      && Number.isInteger(unit?.current_hex)) {
    targetHex = Number(unit.current_hex);
    advanceInteractionEpoch();
    await previewOperation("INDIRECT_FIRE", {
      fire_mode: "INDIRECT",
      shooter_id: selectedIds()[0],
      weapon_id: selectedIndirectOption.weapon_id,
      target_hex: targetHex,
    });
    return;
  }
  if (activeTool === "MOVE" && Number.isInteger(unit?.current_hex)) {
    targetHex = Number(unit.current_hex);
    advanceInteractionEpoch();
    await previewOperation("MOVE", { destination_hex: targetHex });
    return;
  }
  if (activeTool === "SHOOT" && !isFriendlySelectable(unit)) {
    if (activeShootTargetIds().map(String).includes(String(operatorId))) {
      targetUnitId = Number(operatorId);
      advanceInteractionEpoch();
      await previewOperation("SHOOT", {
        target_id: targetUnitId,
        ...selectedWeaponChoices(),
      });
    }
    return;
  }
  if (activeTool === "GUIDED_FIRE" && !isFriendlySelectable(unit)) {
    if (activeGuidedTargetIds().map(String).includes(String(operatorId))) {
      targetUnitId = Number(operatorId);
      advanceInteractionEpoch();
      await previewOperation("GUIDED_FIRE", {
        fire_mode: "GUIDED",
        guider_id: selectedIds()[0],
        shooter_id: selectedGuidedPlatform.shooter_id,
        weapon_id: selectedGuidedPlatform.weapon_id,
        target_id: targetUnitId,
      });
    }
    return;
  }
  if (activeTool === "BOARD" || activeTool === "UNLOAD") {
    const options = capabilityUI.boardingOptions(
      capabilities, activeTool, selectedIds()[0],
    );
    if (options.some((value) => Number(value.target_id) === Number(operatorId))) {
      targetUnitId = Number(operatorId);
      advanceInteractionEpoch();
      await previewOperation(activeTool, { target_id: targetUnitId });
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
  invalidateTacticalOverlay();
  clearOperation();
  await loadCapabilities();
  await loadTacticalOverlay();
}

async function handleHexSelection(value) {
  renderHexInfo(value);
  if (activeTool === "MOVE") {
    targetHex = Number(value.position_id);
    advanceInteractionEpoch();
    await previewOperation("MOVE", { destination_hex: targetHex });
    return;
  }
  if (activeTool === "INDIRECT_FIRE" && selectedIndirectOption) {
    targetHex = Number(value.position_id);
    advanceInteractionEpoch();
    await previewOperation("INDIRECT_FIRE", {
      fire_mode: "INDIRECT",
      shooter_id: selectedIds()[0],
      weapon_id: selectedIndirectOption.weapon_id,
      target_hex: targetHex,
    });
    return;
  }
  if (activeTool === "CAPTURE") {
    const coord = Number(value.position_id);
    const matches = capabilityUI.captureOptions(capabilities, selectedIds())
      .filter((option) => Number(option.coord) === coord);
    const objectiveIds = Array.from(new Set(
      matches.map((option) => Number(option.objective_id)),
    )).sort((left, right) => left - right);
    if (!objectiveIds.length) return;
    let objectiveId = objectiveIds[0];
    if (objectiveIds.length > 1) {
      const chosen = window.prompt(
        `此格有多个夺控点，请输入目标 ID：${objectiveIds.join(", ")}`,
        String(objectiveIds[0]),
      );
      if (chosen === null) return;
      const normalizedChoice = chosen.trim();
      if (!/^-?\d+$/.test(normalizedChoice)) return;
      objectiveId = Number(normalizedChoice);
      if (!Number.isSafeInteger(objectiveId)
          || !objectiveIds.includes(objectiveId)) return;
    }
    targetUnitId = objectiveId;
    targetHex = coord;
    advanceInteractionEpoch();
    await previewOperation("CAPTURE", { target_id: objectiveId });
    return;
  }
}

async function selectDirectFireWeapon(unitId, weaponId) {
  if (isPlanning || isExecuting || !current?.product_status?.can_operate) return;
  const value = capabilityUI.directFireWeapons(capabilities, unitId).find(
    (weapon) => Number(weapon.weapon_id) === Number(weaponId),
  );
  if (!value) return;
  selectedWeaponIds.set(Number(unitId), Number(value.weapon_id));
  targetUnitId = null;
  operationPreview = null;
  executeRequestId = null;
  lastError = null;
  lastNotice = null;
  advanceInteractionEpoch();
  $("tool-instruction").textContent = "请点击地图上黄色高亮的合法射击目标。";
  render();
}

async function selectGuidedFirePlatform(shooterId, weaponId) {
  if (isPlanning || isExecuting || !current?.product_status?.can_operate) return;
  const value = guidedFirePlatforms().find((item) => (
    item.shooter_id === Number(shooterId)
    && item.weapon_id === Number(weaponId)
  ));
  if (!value) return;
  selectedGuidedPlatform = value;
  targetUnitId = null;
  operationPreview = null;
  executeRequestId = null;
  lastError = null;
  lastNotice = null;
  advanceInteractionEpoch();
  $("tool-instruction").textContent = "请点击地图上黄色高亮的合法引导射击目标。";
  render();
}

async function selectIndirectFireWeapon(weaponId) {
  if (isPlanning || isExecuting || !current?.product_status?.can_operate) return;
  const value = indirectFireOptions().find(
    (item) => Number(item.weapon_id) === Number(weaponId),
  );
  if (!value) return;
  selectedIndirectOption = value;
  targetHex = null;
  operationPreview = null;
  executeRequestId = null;
  lastError = null;
  lastNotice = null;
  advanceInteractionEpoch();
  $("tool-instruction").textContent = "请点击地图上的计划攻击六边格；合法性由服务器验证。";
  render();
}

async function activateCapability(capabilityType) {
  if (isPlanning || isExecuting || !current?.product_status?.can_operate) return;
  const capability = capabilityUI.findCapability(capabilities, capabilityType);
  const wholeSideWait = capabilityType === "WAIT" && !selectedUnitIds.size;
  if (capability === null && !wholeSideWait) return;
  const kind = capability?.operation_type || "WAIT";
  lastError = null;
  lastNotice = null;
  operationPreview = null;
  executeRequestId = null;
  targetHex = null;
  targetUnitId = null;
  selectedCapability = capabilityType;
  if (capabilityType !== "DIRECT_FIRE") selectedWeaponIds.clear();
  selectedGuidedPlatform = null;
  selectedIndirectOption = null;
  activeTool = kind;
  advanceInteractionEpoch();
  if (kind === "MOVE") {
    $("tool-instruction").textContent = "请在地图上点击目的六边形。";
  } else if (capabilityType === "DIRECT_FIRE") {
    const allChosen = selectedIds().every((unitId) => {
      const chosen = selectedWeaponIds.get(unitId);
      return Number.isInteger(chosen) && capabilityUI.directFireWeapons(
        capabilities, unitId,
      ).some((weapon) => Number(weapon.weapon_id) === chosen);
    });
    if (allChosen) {
      $("tool-instruction").textContent = "请点击地图上黄色高亮的可见攻击目标。";
    } else {
      $("tool-instruction").textContent = "请为每个单位明确选择携行武器。";
    }
  } else if (capabilityType === "GUIDED_FIRE") {
    const platforms = guidedFirePlatforms();
    if (platforms.length === 1) {
      selectedGuidedPlatform = platforms[0];
      $("tool-instruction").textContent = "请点击地图上黄色高亮的合法引导射击目标。";
    } else {
      $("tool-instruction").textContent = "请先选择引导射击发射平台和武器。";
    }
  } else if (capabilityType === "INDIRECT_FIRE") {
    const options = indirectFireOptions();
    if (options.length === 1) {
      selectedIndirectOption = options[0];
      $("tool-instruction").textContent = "请点击地图上的计划攻击六边格；合法性由服务器验证。";
    } else {
      $("tool-instruction").textContent = "请先选择间接火力武器。";
    }
  } else if (kind === "BOARD" || kind === "UNLOAD") {
    const options = capabilityUI.boardingOptions(
      capabilities, kind, selectedIds()[0],
    );
    if (options.length === 1) {
      targetUnitId = Number(options[0].target_id);
      await previewOperation(kind, { target_id: targetUnitId });
    } else {
      $("tool-instruction").textContent = kind === "BOARD"
        ? "请选择服务器标记的同格载具。"
        : "请选择服务器标记的搭载人员。";
    }
  } else if (kind === "CAPTURE") {
    const options = capabilityUI.captureOptions(capabilities, selectedIds());
    const objectiveIds = Array.from(new Set(
      options.map((value) => Number(value.objective_id)),
    )).sort((left, right) => left - right);
    if (objectiveIds.length === 1) {
      targetUnitId = objectiveIds[0];
      await previewOperation("CAPTURE", { target_id: targetUnitId });
    } else {
      $("tool-instruction").textContent = "请在地图上明确选择夺控旗帜所在格。";
    }
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
  const previewEpoch = interactionEpoch;
  isPlanning = true;
  lastError = null;
  lastNotice = null;
  operationPreview = null;
  render();
  try {
    const nextPreview = await requestJson("/api/product/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token,
        request_id: nextRequestId(`preview-${kind.toLowerCase()}`),
        actor_ids: extra.whole_side_wait ? [] : selectedIds(),
        operation_type: kind,
        movement_mode: "NORMAL",
        ...(current?.control_mode === "realtime" ? {
          observation_version: current.observation_version,
        } : {}),
        ...extra,
      }),
    });
    if (previewEpoch !== interactionEpoch) return;
    operationPreview = nextPreview;
    executeRequestId = nextRequestId("execute");
    $("tool-instruction").textContent = operationPreview.can_execute
      ? "请检查逐单位结果，然后点击执行。"
      : "当前没有单位可以执行该操作。";
  } catch (error) {
    if (previewEpoch !== interactionEpoch) return;
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
    selectedCapability = null;
    selectedWeaponIds.clear();
    selectedGuidedPlatform = null;
    selectedIndirectOption = null;
    activeTool = null;
    targetHex = null;
    targetUnitId = null;
    executeRequestId = null;
    advanceInteractionEpoch();
    if (current?.control_mode === "realtime" && result.accepted) {
      lastNotice = `命令已接纳，将在 Tick ${result.target_tick} 应用；无需等待对方。`;
    } else if (result.product_state === "WAITING_OPPONENT") {
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
  if (isRefreshing) return;
  const refreshEpoch = interactionEpoch;
  isRefreshing = true;
  try {
    let next;
    if (current && cachedStaticMap(current)) {
      const productStatus = await requestJson(
        `/api/product/status?token=${encodeURIComponent(token)}`,
      );
      if (productStatus.frame_index === lastFrameIndex) {
        current.product_status = productStatus;
        lastStateReadError = null;
        render({ redrawMap: false });
        $("error-banner").hidden = true;
        return;
      }
    }
    next = await loadDisplayState();
    const frameChanged = lastFrameIndex !== null
      && next.product_status.frame_index !== lastFrameIndex;
    lastFrameIndex = next.product_status.frame_index;
    current = next;
    lastStateReadError = null;
    const retained = selectedIds().filter((unitId) => (
      isFriendlySelectable(unitById(unitId))
    ));
    const selectionInvalidated = retained.length !== selectedUnitIds.size;
    if (selectionInvalidated) reconcileInteractionDraft();
    if (frameChanged) {
      lastNotice = null;
      invalidateTacticalOverlay({ clearCache: true });
      if (refreshEpoch === interactionEpoch && !selectionInvalidated) {
        await loadCapabilities({ expectedEpoch: interactionEpoch });
      } else {
        render();
      }
      await loadTacticalOverlay({ force: true });
    } else {
      render();
    }
    $("error-banner").hidden = true;
  } catch (error) {
    lastStateReadError = error.message;
    $("error-banner").hidden = false;
    $("error-banner").textContent = `页面状态读取失败：${error.message}`;
    renderStatus();
  } finally {
    isRefreshing = false;
  }
}

$("capability-panel").addEventListener("click", (event) => {
  const button = event.target instanceof Element
    ? event.target.closest("[data-capability]") : null;
  if (button) activateCapability(button.dataset.capability);
});
$("weapon-choices").addEventListener("click", (event) => {
  const button = event.target instanceof Element
    ? event.target.closest("[data-weapon-id]") : null;
  if (button) selectDirectFireWeapon(
    Number(button.dataset.unitId), Number(button.dataset.weaponId),
  );
});
$("guided-fire-choices").addEventListener("click", (event) => {
  const button = event.target instanceof Element
    ? event.target.closest("[data-guided-shooter-id]") : null;
  if (button) selectGuidedFirePlatform(
    Number(button.dataset.guidedShooterId),
    Number(button.dataset.guidedWeaponId),
  );
});
$("indirect-fire-choices").addEventListener("click", (event) => {
  const button = event.target instanceof Element
    ? event.target.closest("[data-indirect-weapon-id]") : null;
  if (button) selectIndirectFireWeapon(Number(button.dataset.indirectWeaponId));
});
$("cancel-operation").addEventListener("click", () => clearOperation());
$("execute-operation").addEventListener("click", executeOperation);
$("confirm-partial").addEventListener("change", renderToolbar);
$("reset-map-view").addEventListener("click", () => mapRenderer?.resetView());
$("toggle-visibility-overlay").addEventListener(
  "click", () => toggleTacticalOverlay("visibility"),
);
$("toggle-weapon-envelope").addEventListener(
  "click", () => toggleTacticalOverlay("weapon"),
);
window.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (activeTool || operationPreview) clearOperation();
  else clearOperation({ clearSelection: true });
});

refresh();
setInterval(() => { if (!isPlanning && !isExecuting) refresh(); }, 1000);
