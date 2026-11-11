"""Small deterministic ground-seat baseline; no world, RNG or model access.

The host supplies the existing detached own-weapon inventory and public weapon
catalog in the capability summary, plus a seat-bound read-only MOVE preview.
Every returned intent still crosses the normal product/adjudication boundary.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
import math
from typing import Any

from wargame_native_engine.agents.protocol import AgentResetContext
from wargame_native_engine.agents.seat import AgentDecisionContext, SeatDecision, SeatOperation
from wargame_native_engine.visibility.line_of_sight import hex_distance


Preview = Callable[[AgentDecisionContext, SeatOperation], Mapping[str, Any]]
GROUND = frozenset({"PERSONNEL", "VEHICLE"})


class SimpleCaptureSeatAgent:
    """Lock a carried ground-direct weapon, approach a point, shoot contacts.

    One independent instance per seat. Target priority is shortest hex distance,
    then target ID. Weapon priority retains an existing supported lock, otherwise
    longest range then weapon ID. Objective priority is nearest non-owned point,
    then objective ID, skipping destinations rejected by the public planner.
    This intentionally has no transport/unload, air, guided or indirect tactics.
    """

    def __init__(self, *, preview: Preview, agent_id: str = "simple-capture") -> None:
        if not callable(preview):
            raise TypeError("preview must be a side-bound read-only callable")
        self.agent_id = agent_id
        self._preview = preview
        self._side: str | None = None
        self._status: dict[int, str] = {}
        self._stop_requested: set[int] = set()

    @property
    def status(self) -> dict[int, str]:
        """Last decision reasons, for host diagnostics (not engine outcomes)."""
        return dict(self._status)

    def reset(self, context: AgentResetContext) -> None:
        self._side = context.side.lower()
        self._status.clear()
        self._stop_requested.clear()

    def close(self) -> None:
        return None

    def act(self, *, context: AgentDecisionContext) -> SeatDecision:
        if self._side is not None and context.side != self._side:
            raise ValueError("simple_capture_seat_mismatch")
        if context.observation.get("side") != context.side:
            raise ValueError("simple_capture_observation_side_mismatch")
        summary = context.operation_capabilities.operation_summary
        inventories = {v["operator_id"]: v for v in summary.get("policy_own_weapon_inventory", ())}
        catalog = {v["weapon_id"]: v for v in summary.get("policy_weapon_catalog", {}).get("weapons", ())}
        actions: dict[int, list[Mapping[str, Any]]] = {}
        for record in context.operation_capabilities.frozen_actions:
            action = record.get("action", {})
            if action.get("side") == context.side:
                actions.setdefault(action.get("actor_id"), []).append(action)
        lifecycle = ({v.operator_reference: v for v in context.decision_semantic.own_operators}
                     if context.decision_semantic is not None else {})
        enemies = {v["operator_id"]: v for v in context.observation.get("visible_enemies", ())
                   if v.get("alive") and not v.get("destroyed") and v.get("operator_type") in GROUND}
        enemy_hexes = summary.get("policy_visible_enemy_hexes", {})
        enemies = {key: dict(value, policy_current_hex=enemy_hexes.get(key, enemy_hexes.get(str(key))))
                   for key, value in enemies.items()}
        objectives = [v for v in context.observation.get("objectives", ())
                      if v.get("controlling_side") != context.side]
        operations = []
        self._status = {}
        for actor in sorted(context.observation.get("friendly_operators", ()), key=lambda v: v["operator_id"]):
            actor_id = actor["operator_id"]
            state = lifecycle.get(actor_id)
            if (actor_id not in context.operation_capabilities.controllable_actor_ids
                    or actor.get("side") != context.side or not actor.get("alive")
                    or not actor.get("deployed")
                    or actor.get("state") == "ON_BOARD"
                    or (state is not None and not state.deployment.spatially_present)):
                self._status[actor_id] = "NOT_DEPLOYED_OR_NOT_CONTROLLABLE"
                continue
            if actor.get("operator_type") not in GROUND:
                self._status[actor_id] = "UNSUPPORTED_NON_GROUND_UNIT"
                continue
            origin = state.deployment.current_hex if state is not None else _position_hex(actor.get("position"))
            if origin is None:
                self._status[actor_id] = "NO_AUTHORITATIVE_HEX"
                continue
            operation, reason = self._actor_operation(
                context, actor, origin, inventories.get(actor_id, {}), catalog,
                actions.get(actor_id, []), enemies, objectives,
            )
            self._status[actor_id] = reason
            if operation is not None:
                operations.append(operation)
        # Never append whole-side WAIT: one complete bundled decision per tick.
        return SeatDecision(tuple(operations))

    def _actor_operation(self, context, actor, origin, inventory, catalog, actions, enemies, objectives):
        actor_id = actor["operator_id"]
        if actor.get("stopped") and not actor.get("has_active_segment"):
            self._stop_requested.discard(actor_id)
        carried = {v["weapon_id"]: v for v in inventory.get("weapons", ())}
        supported = {key: value for key, value in carried.items()
                     if "DIRECT" in value.get("attack_modes", ())
                     and "DIRECT" in catalog.get(key, {}).get("fire_modes", ())
                     and GROUND.intersection(catalog[key].get("target_domains", ()))
                     and type(catalog[key].get("maximum_range")) is int}
        if not supported:
            return None, "NO_SUPPORTED_CARRIED_GROUND_DIRECT_WEAPON"
        locked_id = inventory.get("locked_weapon_id")
        weapon_id = (locked_id if inventory.get("weapon_locked") and locked_id in supported
                     else min(supported, key=lambda key: (-catalog[key]["maximum_range"], key)))
        kinds = {v.get("action_type") for v in actions}
        if actor.get("transition_kind") == "WEAPON_LOCK":
            return None, "WAIT_FOR_LOCK_COMPLETION"
        if not inventory.get("weapon_locked") or locked_id != weapon_id:
            locks = {v.get("parameters", {}).get("weapon_id") for v in actions
                     if v.get("action_type") == "WEAPON_LOCK"}
            if weapon_id in locks:
                return SeatOperation("WEAPON_LOCK", (actor_id,), weapon_choices=((actor_id, weapon_id),)), "LOCK_WEAPON"
            return None, "WAIT_FOR_WEAPON_LOCK_CAPABILITY"

        # Only exact server-issued target capabilities authorize targeted fire.
        # A general SHOOT preview admits intents, including out-of-range empty
        # fire, so it is deliberately NOT treated as readiness evidence here.
        resource = supported[weapon_id]
        ammunition = resource.get("ammunition")
        cost = resource.get("ammunition_cost") or 1
        if (isinstance(ammunition, (int, float)) and ammunition >= cost
                and not actor.get("weapon_cooldown_remaining_ms", 0)):
            candidates = []
            for action in actions:
                target_id = action.get("target_id")
                enemy = enemies.get(target_id)
                if (action.get("action_type") != "SHOOT" or enemy is None
                        or action.get("parameters", {}).get("weapon_id") != weapon_id):
                    continue
                target_hex = enemy.get("policy_current_hex")
                if type(target_hex) is not int:
                    target_hex = _position_hex(enemy.get("position"))
                if target_hex is None or enemy.get("operator_type") not in catalog[weapon_id].get("target_domains", ()):
                    continue
                distance = hex_distance(origin, target_hex)
                if distance <= catalog[weapon_id]["maximum_range"]:
                    candidates.append((distance, target_id))
            if candidates:
                target_id = min(candidates)[1]
                return SeatOperation("SHOOT", (actor_id,), target_id=target_id,
                                     weapon_choices=((actor_id, weapon_id),)), "SHOOT_VISIBLE_READY_TARGET"

        ordered = sorted((v for v in objectives if _objective_hex(v) is not None),
                         key=lambda v: (hex_distance(origin, _objective_hex(v)), v["objective_id"]))
        nearby = [v for v in ordered if hex_distance(origin, _objective_hex(v)) <= 1]
        moving = actor.get("has_active_segment") or actor.get("has_remaining_movement_task") or not actor.get("stopped", True)
        if nearby:
            if moving and "STOP_MOVE" in kinds and actor_id not in self._stop_requested:
                self._stop_requested.add(actor_id)
                return SeatOperation("STOP_MOVE", (actor_id,)), "STOP_AT_OBJECTIVE"
            if moving or actor.get("transition_kind") or actor.get("move_to_stop_remaining_ms", 0):
                return None, "WAIT_FOR_STOP_COMPLETION"
            capture_ids = {v.get("target_id") for v in actions if v.get("action_type") == "CAPTURE"}
            for objective in nearby:
                if objective["objective_id"] in capture_ids:
                    return SeatOperation("CAPTURE", (actor_id,), target_id=objective["objective_id"]), "CAPTURE_OBJECTIVE"
            return None, "WAIT_AT_CONTESTED_OR_UNAVAILABLE_OBJECTIVE"
        if moving:
            return None, "KEEP_EXISTING_ROUTE"
        if actor.get("transition_kind") or actor.get("move_to_stop_remaining_ms", 0):
            return None, "WAIT_FOR_TRANSITION"
        if "MOVE" not in kinds:
            return None, "MOVE_UNAVAILABLE"
        for objective in ordered:
            operation = SeatOperation("MOVE", (actor_id,), destination_hex=_objective_hex(objective))
            preview = self._preview(context, operation)
            if preview.get("can_execute"):
                return operation, "APPROACH_OBJECTIVE"
        return None, "NO_REACHABLE_NON_OWNED_OBJECTIVE" if ordered else "NO_NON_OWNED_OBJECTIVE"


def _position_hex(position: object) -> int | None:
    if not isinstance(position, (tuple, list)) or len(position) != 2:
        return None
    x, y = position
    if not all(type(v) in (int, float) and math.isfinite(v) and v >= 0 and v == int(v) for v in (x, y)):
        return None
    return int(y) * 100 + int(x)


def _objective_hex(value: Mapping[str, Any]) -> int | None:
    return _position_hex((value.get("x"), value.get("y")))
