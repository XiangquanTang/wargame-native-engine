"use strict";

// Pure presentation helpers for the capability-driven Human command panel.
// This module never computes legality; it consumes the side-safe capability
// projection returned by /api/product/capabilities.
(function installHumanCapabilityUI(root) {
  const SCHEMA = "human-command-capabilities/1.1";
  const SUPPORTED = Object.freeze(new Set([
    "MOVE",
    "DIRECT_FIRE",
    "GUIDED_FIRE",
    "INDIRECT_FIRE",
    "BOARD",
    "UNLOAD",
    "CAPTURE",
    "ENTER_CONCEALMENT",
    "STOP_MOVE",
    "WEAPON_UNFOLD",
    "WEAPON_LOCK", "WEAPON_UNLOCK",
    "WAIT",
  ]));

  function availableCapabilities(model) {
    if (!model || !Array.isArray(model.capabilities)) return [];
    return model.capabilities.filter((value) => (
      value && typeof value === "object"
      && value.status === "AVAILABLE"
      && SUPPORTED.has(value.capability_type)
      && typeof value.operation_type === "string"
    ));
  }

  function findCapability(model, capabilityType) {
    return availableCapabilities(model).find(
      (value) => value.capability_type === capabilityType,
    ) || null;
  }

  function directFireUnit(model, unitId) {
    const units = model?.direct_fire?.units;
    if (!Array.isArray(units)) return null;
    return units.find((value) => Number(value.unit_id) === Number(unitId)) || null;
  }

  function directFireWeapons(model, unitId) {
    const unit = directFireUnit(model, unitId);
    const weapons = Array.isArray(unit?.weapons) ? unit.weapons : [];
    if (Number.isInteger(unit?.locked_weapon_id)) {
      return weapons.filter((weapon) => weapon.weapon_id === unit.locked_weapon_id);
    }
    return unit?.weapon_locked ? [] : weapons;
  }

  function lockWeapons(model, unitId) {
    const unit = directFireUnit(model, unitId);
    const ids = new Set(unit?.lock_weapon_ids || []);
    return (unit?.weapons || []).filter((weapon) => ids.has(weapon.weapon_id));
  }

  function weaponTargetIds(model, unitId, weaponId) {
    if (!Number.isInteger(weaponId)) return [];
    const weapon = directFireWeapons(model, unitId).find(
      (value) => Number(value.weapon_id) === Number(weaponId),
    );
    return Array.isArray(weapon?.legal_target_ids)
      ? [...weapon.legal_target_ids] : [];
  }

  function weaponSelectableTargetIds(model, unitId, weaponId) {
    if (!Number.isInteger(weaponId) || !directFireWeapons(model, unitId).some(
      (value) => Number(value.weapon_id) === Number(weaponId)
    )) return [];
    const values = model?.direct_fire?.selectable_target_ids;
    return Array.isArray(values) ? [...values] : [];
  }

  function weaponEnvelopeHexes(overlay, weaponId) {
    if (!Number.isInteger(weaponId)) return [];
    const weapons = overlay?.weapon_envelope?.weapons;
    if (!Array.isArray(weapons)) return [];
    const weapon = weapons.find(
      (value) => Number(value.weapon_id) === Number(weaponId),
    );
    return Array.isArray(weapon?.hex_ids) ? [...weapon.hex_ids] : [];
  }

  function guidedFireOptions(model, guiderId) {
    const values = model?.guided_fire?.options;
    if (!Array.isArray(values)) return [];
    return values.filter(
      (value) => Number(value.guider_id) === Number(guiderId),
    );
  }

  function guidedFirePlatforms(model, guiderId) {
    const grouped = new Map();
    for (const value of guidedFireOptions(model, guiderId)) {
      const key = `${value.shooter_id}:${value.weapon_id}`;
      const current = grouped.get(key) || {
        guider_id: Number(value.guider_id),
        shooter_id: Number(value.shooter_id),
        shooter_display_name: value.shooter_display_name,
        weapon_id: Number(value.weapon_id),
        weapon_display_name: value.weapon_display_name,
        maximum_range: value.maximum_range,
        profile_id: value.profile_id ?? null,
        legal_target_ids: [],
      };
      current.legal_target_ids.push(Number(value.target_id));
      grouped.set(key, current);
    }
    return Array.from(grouped.values())
      .map((value) => ({
        ...value,
        legal_target_ids: Array.from(new Set(value.legal_target_ids))
          .sort((left, right) => left - right),
      }))
      .sort((left, right) => (
        left.shooter_id - right.shooter_id
        || left.weapon_id - right.weapon_id
      ));
  }

  function indirectFireOptions(model, shooterId) {
    const values = model?.indirect_fire?.options;
    if (!Array.isArray(values)) return [];
    return values.filter(
      (value) => Number(value.shooter_id) === Number(shooterId),
    ).sort((left, right) => (
      Number(left.weapon_id) - Number(right.weapon_id)
      || String(left.profile_id).localeCompare(String(right.profile_id))
    ));
  }

  function boardingOptions(model, operationType, actorId) {
    const values = model?.boarding?.options;
    if (!Array.isArray(values)) return [];
    return values.filter((value) => (
      value.operation_type === operationType
      && Number(value.actor_id) === Number(actorId)
    )).sort((left, right) => Number(left.target_id) - Number(right.target_id));
  }

  function captureOptions(model, actorIds = []) {
    const values = model?.capture?.options;
    if (!Array.isArray(values)) return [];
    const actors = new Set(actorIds.map(Number));
    return values.filter((value) => actors.has(Number(value.actor_id)))
      .sort((left, right) => (
        Number(left.objective_id) - Number(right.objective_id)
        || Number(left.actor_id) - Number(right.actor_id)
      ));
  }

  const publicApi = Object.freeze({
    schema: SCHEMA,
    supported_capabilities: SUPPORTED,
    availableCapabilities,
    findCapability,
    directFireUnit,
    directFireWeapons,
    lockWeapons,
    weaponTargetIds,
    weaponSelectableTargetIds,
    weaponEnvelopeHexes,
    guidedFireOptions,
    guidedFirePlatforms,
    indirectFireOptions,
    boardingOptions,
    captureOptions,
  });
  root.HumanCapabilityUI = publicApi;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = publicApi;
  }
}(typeof globalThis !== "undefined" ? globalThis : window));
