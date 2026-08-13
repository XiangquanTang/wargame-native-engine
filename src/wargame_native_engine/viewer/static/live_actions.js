"use strict";

/*
 * Presentation-only indexing of server-issued Native valid actions.
 * This module never creates an EngineAction, searches a path, or decides
 * legality. Every returned selection retains an exact server action_id.
 */
(function installLiveActionIndex(globalObject) {
  const GROUP_ORDER = [
    "MOVE", "SHOOT", "STOP_MOVE", "WEAPON_LOCK", "WEAPON_UNFOLD",
    "WAIT", "DEPLOY", "END_DEPLOYMENT", "CHANGE_STATE", "ATTACK",
  ];

  function sameId(left, right) {
    return left !== null && left !== undefined
      && right !== null && right !== undefined
      && String(left) === String(right);
  }

  function validItems(values) {
    return Array.isArray(values) ? values.filter((item) => (
      item && typeof item === "object"
      && typeof item.action_id === "string" && item.action_id.length > 0
      && item.action && typeof item.action === "object"
      && typeof item.action.action_type === "string"
    )) : [];
  }

  function actionsForActor(values, actorId) {
    return validItems(values).filter((item) => sameId(item.action.actor_id, actorId));
  }

  function groupedActions(values, actorId) {
    const groups = new Map();
    const candidates = actorId === null || actorId === undefined
      ? validItems(values) : actionsForActor(values, actorId);
    for (const item of candidates) {
      const kind = item.action.action_type.toUpperCase();
      if (!groups.has(kind)) groups.set(kind, []);
      groups.get(kind).push(item);
    }
    const orderedKinds = [
      ...GROUP_ORDER.filter((kind) => groups.has(kind)),
      ...Array.from(groups.keys()).filter((kind) => !GROUP_ORDER.includes(kind)).sort(),
    ];
    return orderedKinds.map((kind) => ({ kind, actions: groups.get(kind).slice() }));
  }

  function explicitMoveTarget(item) {
    if (item?.action?.action_type !== "MOVE") return null;
    const path = item.action.parameters?.path;
    if (!Array.isArray(path) || !path.length
        || !path.every((position) => Number.isInteger(position) && position >= 0)) {
      return null;
    }
    const positionId = path[path.length - 1];
    return {
      position_id: String(positionId),
      row: Math.floor(positionId / 100),
      column: positionId % 100,
      path: path.slice(),
      action_id: item.action_id,
    };
  }

  function actionTargets(values, actorId) {
    const moves = [];
    const shootTargetIds = [];
    for (const item of actionsForActor(values, actorId)) {
      const move = explicitMoveTarget(item);
      if (move) moves.push(move);
      if (item.action.action_type === "SHOOT"
          && item.action.target_id !== null && item.action.target_id !== undefined) {
        shootTargetIds.push(item.action.target_id);
      }
    }
    return { moves, shoot_target_ids: shootTargetIds };
  }

  function actionsForMoveTarget(values, actorId, positionId) {
    return actionsForActor(values, actorId).filter((item) => {
      const target = explicitMoveTarget(item);
      return target !== null && String(target.position_id) === String(positionId);
    });
  }

  function actionsForShootTarget(values, actorId, targetId) {
    return actionsForActor(values, actorId).filter((item) => (
      item.action.action_type === "SHOOT" && sameId(item.action.target_id, targetId)
    ));
  }

  function findExactAction(values, actionId) {
    if (actionId === null) return null;
    return validItems(values).find((item) => item.action_id === actionId) || null;
  }

  function actionDetail(item) {
    if (!item) return { action_id: null, action_type: "NO_OP" };
    const action = item.action;
    return {
      action_id: item.action_id,
      action_type: action.action_type,
      actor_id: action.actor_id ?? null,
      target_id: action.target_id ?? null,
      target_position: action.target_position ?? null,
      path: Array.isArray(action.parameters?.path)
        ? action.parameters.path.slice() : null,
      movement_mode: action.parameters?.movement_mode ?? null,
      weapon_id: action.parameters?.weapon_id ?? null,
      source: item.source ?? null,
    };
  }

  const api = {
    actionDetail,
    actionsForActor,
    actionsForMoveTarget,
    actionsForShootTarget,
    actionTargets,
    explicitMoveTarget,
    findExactAction,
    groupedActions,
    sameId,
  };
  globalObject.NativeLiveActions = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
}(typeof window !== "undefined" ? window : globalThis));
