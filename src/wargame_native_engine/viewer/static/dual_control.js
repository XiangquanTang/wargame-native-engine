"use strict";

const token = new URLSearchParams(window.location.search).get("token");
let current = null;
let selectedByActor = new Map();
let selectionOrder = [];
let requestSequence = 0;
let submitting = false;
const $ = (id) => document.getElementById(id);

async function requestJson(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `${response.status}`);
  return body;
}

function actorKey(item) {
  const actor = item.action.actor_id;
  return actor === null ? `global:${item.action.action_type}` : `actor:${actor}`;
}

function selectAction(item) {
  const key = actorKey(item);
  if (!selectedByActor.has(key)) selectionOrder.push(key);
  selectedByActor.set(key, item.action_id);
  renderActions();
}

function orderedBatch() {
  return selectionOrder
    .filter((key) => selectedByActor.has(key))
    .map((key) => selectedByActor.get(key));
}

function renderActions() {
  const root = $("actions");
  root.replaceChildren();
  for (const item of current.view.valid_actions) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "action-button";
    button.dataset.actionId = item.action_id;
    button.textContent = `${item.action.action_type} · actor=${item.action.actor_id ?? "none"} · ${item.action_id}`;
    if (selectedByActor.get(actorKey(item)) === item.action_id) button.classList.add("selected");
    button.disabled = current.window.submitted || current.view.terminal || submitting;
    button.addEventListener("click", () => selectAction(item));
    root.appendChild(button);
  }
  $("batch-preview").textContent = JSON.stringify(orderedBatch(), null, 2);
  $("submit-batch").disabled = current.window.submitted || current.view.terminal || submitting;
  $("submit-empty").disabled = current.window.submitted || current.view.terminal || submitting;
}

function render() {
  $("side-badge").textContent = current.side.toUpperCase();
  $("window-badge").textContent = `Window ${current.window.decision_window_index} · ${current.window.lifecycle_state}`;
  $("deadline").textContent = current.window.deadline_remaining_ms === null
    ? "同步阻塞模式：必须显式提交"
    : `服务器deadline剩余 ${current.window.deadline_remaining_ms} ms`;
  $("observation").textContent = JSON.stringify(current.view.observation, null, 2);
  renderActions();
}

async function refresh() {
  try {
    const next = await requestJson(`/api/bootstrap?token=${encodeURIComponent(token)}`);
    if (!current || next.window.decision_window_index !== current.window.decision_window_index) {
      selectedByActor = new Map();
      selectionOrder = [];
    }
    current = next;
    render();
  } catch (error) {
    $("error-banner").hidden = false;
    $("error-banner").textContent = error.message;
  }
}

async function submit(actionIds) {
  if (submitting || current.window.submitted || current.view.terminal) return;
  submitting = true;
  renderActions();
  const requestId = `web-${current.side}-${current.window.decision_window_index}-${requestSequence++}`;
  try {
    const response = await requestJson("/api/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token,
        request_id: requestId,
        expected_window: current.window.decision_window_index,
        ordered_action_ids: actionIds,
      }),
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

$("submit-batch").addEventListener("click", () => submit(orderedBatch()));
$("submit-empty").addEventListener("click", () => submit([]));
refresh();
setInterval(refresh, 1000);
