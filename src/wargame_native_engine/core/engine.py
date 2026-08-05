from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.core.action import (
    ACTION_PRIORITY, ActionDecision, ActionType, EngineAction, ValidAction,
    valid_action_sort_key,
)
from wargame_native_engine.core.event import EngineEvent, EventType, event_sort_key
from wargame_native_engine.core.observation import (
    OperatorObservation, SideObservation, objective_view,
)
from wargame_native_engine.core.result import GameResult, StepResult
from wargame_native_engine.core.rng import NativeRNG
from wargame_native_engine.core.world import Stage, WorldState
from wargame_native_engine.io.scenario import NativeScenario
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.rules.config import Ruleset, load_ruleset
from wargame_native_engine.rules.terminal_rules import TerminalRules
from wargame_native_engine.systems.combat import validate_attack
from wargame_native_engine.systems.damage import apply_damage
from wargame_native_engine.systems.deployment import validate_deployment
from wargame_native_engine.systems.movement import (
    distance, move_toward, validate_move,
)
from wargame_native_engine.systems.terminal import evaluate_terminal
from wargame_native_engine.systems.visibility import can_see


class NativeEngine:
    """Authoritative runtime for the documented Native simplified rules."""

    def __init__(self, scenario: NativeScenario, *,
                 max_ticks: int | None = None,
                 ruleset: Ruleset | str | Path | None = None) -> None:
        self.scenario = scenario
        self.ruleset = (ruleset if isinstance(ruleset, Ruleset)
                        else load_ruleset(ruleset))
        self.terminal_rules = TerminalRules(
            max_ticks=(scenario.terminal_rules.max_ticks if max_ticks is None
                       else int(max_ticks)),
            elimination=scenario.terminal_rules.elimination,
            objective_score_to_win=scenario.terminal_rules.objective_score_to_win,
            mission_score_enabled=self.ruleset.terminal.mission_score_enabled,
            mutual_elimination_result=self.ruleset.terminal.mutual_elimination_result,
            tie_result=self.ruleset.terminal.tie_result,
            evaluation_order=self.ruleset.terminal.evaluation_order,
        )
        self._world: WorldState | None = None
        self._rng: NativeRNG | None = None
        self._seed = 0
        self._closed = False
        self._action_counts = {side: 0 for side in scenario.side_order}
        self._error_counts = {side: 0 for side in scenario.side_order}
        self._last_events: tuple[EngineEvent, ...] = ()

    def reset(self, seed: int = 42) -> WorldState:
        if self._closed:
            raise RuntimeError("engine_closed")
        self._seed = int(seed)
        self._rng = NativeRNG(self._seed)
        self._world = self.scenario.initial_world()
        self._action_counts = {side: 0 for side in self.scenario.side_order}
        self._error_counts = {side: 0 for side in self.scenario.side_order}
        self._last_events = (EngineEvent(
            0, EventType.ENGINE_RESET,
            data={"scenario_id": self.scenario.scenario_id,
                  "scenario_hash": self.scenario.scenario_hash,
                  "seed": self._seed,
                  "ruleset_id": self.ruleset.ruleset_id,
                  "ruleset_version": self.ruleset.ruleset_version,
                  "ruleset_hash": self.ruleset.content_hash},
        ),)
        return WorldState.from_dict(self._world.to_dict())

    def observe(self, side: str) -> SideObservation:
        world = self._require_world()
        self._require_side(side)
        friendly = tuple(OperatorObservation.from_state(item)
                         for item in self._friendly(side))
        visible = tuple(OperatorObservation.from_state(item)
                        for item in self._visible_enemies(side))
        actions = self.valid_actions(side)
        return SideObservation(
            side=side, tick=world.tick, stage=world.stage,
            friendly_operators=friendly, visible_enemies=visible,
            objectives=tuple(objective_view(item)
                             for item in sorted(world.objectives.values(),
                                                key=lambda item: item.objective_id)),
            legal_action_ids=tuple(item.action_id for item in actions),
            mission={"terminal_rules": {
                "elimination": self.terminal_rules.elimination,
                "objective_score_to_win": self.terminal_rules.objective_score_to_win,
                "max_ticks": self.terminal_rules.max_ticks,
            }},
        )

    def valid_actions(self, side: str) -> tuple[ValidAction, ...]:
        world = self._require_world()
        self._require_side(side)
        if world.done:
            return ()
        values: list[ValidAction] = []
        if world.stage is Stage.DEPLOYMENT:
            for operator in self._friendly(side):
                if operator.alive and not operator.deployed:
                    position = self.scenario.deployment_positions[side][operator.operator_id]
                    action = EngineAction(side, operator.operator_id,
                                          ActionType.DEPLOY,
                                          target_position=position)
                    values.append(ValidAction(action, ACTION_PRIORITY[action.action_type]))
            if (not values
                    and not world.sides[side].deployment_complete):
                action = EngineAction(side, None, ActionType.END_DEPLOYMENT)
                values.append(ValidAction(action, ACTION_PRIORITY[action.action_type]))
            if world.sides[side].deployment_complete:
                action = EngineAction(side, None, ActionType.WAIT)
                values.append(ValidAction(action, ACTION_PRIORITY[action.action_type]))
        elif world.stage is Stage.COMBAT:
            enemies = [item for item in world.operators.values()
                       if item.side != side and item.alive and item.deployed]
            for operator in self._friendly(side):
                if not operator.alive or not operator.deployed:
                    continue
                weapon_id = operator.weapon_ids[0] if operator.weapon_ids else None
                targets = ([] if weapon_id is None else [
                    item for item in enemies if validate_attack(
                        operator, item, combat_rules=self.ruleset.combat,
                        visibility_rules=self.ruleset.visibility,
                        weapon_id=weapon_id) is None])
                for target in sorted(targets, key=lambda item: item.operator_id):
                    action = EngineAction(side, operator.operator_id,
                                          ActionType.ATTACK,
                                          target_id=target.operator_id,
                                          parameters={"weapon_id": weapon_id})
                    values.append(ValidAction(action, ACTION_PRIORITY[action.action_type]))
                if not targets and enemies:
                    target = min(enemies, key=lambda item: (
                        distance(operator.position, item.position), item.operator_id))
                    configured_range = self.ruleset.movement.for_operator(operator)
                    limit = min(configured_range,
                                operator.max_speed or configured_range)
                    destination = move_toward(operator.position, target.position, limit)
                    if destination != operator.position and validate_move(
                        operator, destination, width=self.scenario.map.width,
                        height=self.scenario.map.height,
                        blocked_positions=self.scenario.map.blocked_positions,
                        rules=self.ruleset.movement,
                        occupied_positions=self._occupied_positions(
                            exclude=operator.operator_id),
                    ) is None:
                        action = EngineAction(side, operator.operator_id,
                                              ActionType.MOVE,
                                              target_position=destination)
                        values.append(ValidAction(action,
                                                  ACTION_PRIORITY[action.action_type]))
                wait = EngineAction(side, operator.operator_id, ActionType.WAIT)
                values.append(ValidAction(wait, ACTION_PRIORITY[wait.action_type]))
            if not values:
                wait = EngineAction(side, None, ActionType.WAIT)
                values.append(ValidAction(wait, ACTION_PRIORITY[wait.action_type]))
        return tuple(sorted(values, key=valid_action_sort_key))

    def step(self, actions: Mapping[str, EngineAction | None]) -> StepResult:
        world = self._require_world()
        if world.done:
            raise RuntimeError("episode_already_finished")
        if not isinstance(actions, Mapping):
            raise TypeError("actions must be a side-to-action mapping")
        tick_before = world.tick
        stage_before = world.stage
        available = {side: {item.action_id: item
                            for item in self.valid_actions(side)}
                     for side in self.scenario.side_order}
        submitted: list[EngineAction] = []
        accepted: list[EngineAction] = []
        decisions: list[ActionDecision] = []
        events: list[EngineEvent] = []
        for side in self.scenario.side_order:
            action = actions.get(side)
            if action is None:
                action = EngineAction(side, None, ActionType.WAIT)
            if not isinstance(action, EngineAction):
                raise TypeError(f"action for {side} must be EngineAction or None")
            submitted.append(action)
            code = None
            if action.side != side:
                code = "side_mismatch"
            elif action.action_id not in available[side]:
                code = "action_not_in_valid_actions"
            if code is not None:
                self._error_counts[side] += 1
                decisions.append(ActionDecision(action.action_id, False, code,
                                                "native validation rejected action"))
                events.append(EngineEvent(
                    tick_before, EventType.ACTION_REJECTED, side,
                    action.actor_id, action.target_id,
                    {"action_id": action.action_id, "code": code},
                ))
            else:
                self._action_counts[side] += 1
                decisions.append(ActionDecision(action.action_id, True, "accepted",
                                                "accepted for native resolution"))
                accepted.append(action)

        accepted.sort(key=lambda item: (
            ACTION_PRIORITY[item.action_type], item.side,
            -1 if item.actor_id is None else item.actor_id,
            -1 if item.target_id is None else item.target_id,
            item.action_id))
        for action in accepted:
            if action.action_type is ActionType.DEPLOY:
                self._apply_deploy(action, events)
            elif action.action_type is ActionType.END_DEPLOYMENT:
                world.sides[action.side].deployment_complete = True
                events.append(EngineEvent(tick_before, EventType.DEPLOYMENT_ENDED,
                                          action.side,
                                          data={"action_id": action.action_id}))
            elif action.action_type is ActionType.MOVE:
                self._apply_move(action, events)
            elif action.action_type is ActionType.CHANGE_STATE:
                self._apply_state(action, events)
            elif action.action_type is ActionType.WAIT:
                events.append(EngineEvent(tick_before, EventType.WAITED,
                                          action.side, action.actor_id,
                                          data={"action_id": action.action_id}))

        if (world.stage is Stage.DEPLOYMENT
                and all(item.deployment_complete for item in world.sides.values())):
            world.stage = Stage.COMBAT
            events.append(EngineEvent(tick_before, EventType.COMBAT_STARTED,
                                      data={"completed_sides": list(self.scenario.side_order)}))

        damage: dict[int, float] = defaultdict(float)
        attackers: dict[int, list[int]] = defaultdict(list)
        for action in accepted:
            if action.action_type is not ActionType.ATTACK:
                continue
            attacker = world.operators[action.actor_id]  # validated binding
            target = world.operators[action.target_id]
            events.append(EngineEvent(
                tick_before, EventType.ATTACK_DECLARED, action.side,
                attacker.operator_id, target.operator_id,
                {"action_id": action.action_id,
                 "weapon_id": action.parameters.get("weapon_id")},
            ))
            weapon_id = int(action.parameters["weapon_id"])
            attack_damage = self.ruleset.combat.damage(attacker, weapon_id)
            damage[target.operator_id] += attack_damage
            attackers[target.operator_id].append(attacker.operator_id)
            events.append(EngineEvent(
                tick_before, EventType.TARGET_HIT, action.side,
                attacker.operator_id, target.operator_id,
                {"damage": attack_damage},
            ))
        for target_id in sorted(damage):
            target = world.operators[target_id]
            before, after, destroyed = apply_damage(
                target, damage[target_id], self.ruleset.combat)
            events.append(EngineEvent(
                tick_before, EventType.OPERATOR_DAMAGED, target.side,
                target_id=target_id,
                data={"before": before, "after": after,
                      "damage": damage[target_id],
                      "attacker_ids": sorted(attackers[target_id])},
            ))
            if destroyed:
                events.append(EngineEvent(
                    tick_before, EventType.OPERATOR_DESTROYED, target.side,
                    target_id=target_id,
                    data={"attacker_ids": sorted(attackers[target_id])},
                ))

        world.tick += 1
        if (world.stage is Stage.COMBAT
                and self.ruleset.terminal.mission_score_enabled):
            self._score_objectives(events)
        terminal = evaluate_terminal(world, self.terminal_rules,
                                     self.scenario.side_order)
        if terminal.terminal or terminal.truncated:
            world.terminal = terminal.terminal
            world.truncated = terminal.truncated
            world.winner = terminal.winner
            world.terminal_reason = terminal.reason
            world.stage = Stage.TERMINAL
            event_type = (EventType.GAME_TERMINATED if terminal.terminal
                          else EventType.GAME_TRUNCATED)
            events.append(EngineEvent(
                world.tick, event_type,
                side=terminal.winner,
                data={"reason": terminal.reason,
                      "winner": terminal.winner},
            ))
        events.sort(key=event_sort_key)
        self._last_events = tuple(events)
        return StepResult(tick_before, world.tick, stage_before, world.stage,
                          tuple(sorted(submitted, key=lambda item: (
                              item.side, item.actor_id is None,
                              -1 if item.actor_id is None else item.actor_id,
                              item.action_id))),
                          tuple(sorted(decisions, key=lambda item: item.action_id)),
                          tuple(events),
                          world.terminal, world.truncated)

    def is_terminal(self) -> bool:
        return self._require_world().done

    def result(self) -> GameResult:
        world = self._require_world()
        loser = None
        if world.winner is not None and len(self.scenario.side_order) == 2:
            loser = next(side for side in self.scenario.side_order
                         if side != world.winner)
        outcome = ("truncated" if world.truncated else
                   "ongoing" if not world.terminal else
                   "draw" if world.winner is None else "win")
        return GameResult(
            terminal=world.terminal, truncated=world.truncated,
            winner=world.winner, loser=loser, outcome=outcome,
            reason=world.terminal_reason, final_stage=world.stage,
            ticks=world.tick, action_counts=dict(self._action_counts),
            error_counts=dict(self._error_counts), seed=self._seed,
            ruleset_id=self.ruleset.ruleset_id,
            ruleset_version=self.ruleset.ruleset_version,
            ruleset_hash=self.ruleset.content_hash,
            world_hash=canonical_hash(world.to_dict()),
        )

    def snapshot(self) -> Mapping[str, Any]:
        world = self._require_world()
        if self._rng is None:
            raise RuntimeError("engine_not_reset")
        return {
            "schema_version": "native-snapshot/2.0",
            "scenario_hash": self.scenario.scenario_hash,
            "ruleset_id": self.ruleset.ruleset_id,
            "ruleset_version": self.ruleset.ruleset_version,
            "ruleset_hash": self.ruleset.content_hash,
            "seed": self._seed, "rng": self._rng.snapshot(),
            "world": world.to_dict(),
            "action_counts": dict(self._action_counts),
            "error_counts": dict(self._error_counts),
        }

    def restore(self, snapshot: Mapping[str, Any]) -> WorldState:
        if self._closed:
            raise RuntimeError("engine_closed")
        if snapshot.get("schema_version") != "native-snapshot/2.0":
            raise ValueError("unsupported snapshot schema")
        if snapshot.get("scenario_hash") != self.scenario.scenario_hash:
            raise ValueError("snapshot scenario hash mismatch")
        if (snapshot.get("ruleset_id") != self.ruleset.ruleset_id
                or snapshot.get("ruleset_version") != self.ruleset.ruleset_version
                or snapshot.get("ruleset_hash") != self.ruleset.content_hash):
            raise ValueError("snapshot ruleset mismatch")
        self._seed = int(snapshot["seed"])
        self._rng = NativeRNG.restore(snapshot["rng"])
        self._world = WorldState.from_dict(snapshot["world"])
        self._action_counts = {str(key): int(item)
                               for key, item in snapshot["action_counts"].items()}
        self._error_counts = {str(key): int(item)
                              for key, item in snapshot["error_counts"].items()}
        self._last_events = ()
        return WorldState.from_dict(self._world.to_dict())

    def close(self) -> None:
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    def _apply_deploy(self, action: EngineAction,
                      events: list[EngineEvent]) -> None:
        world = self._require_world()
        operator = world.operators[action.actor_id]
        position = action.target_position
        if position is None:
            raise RuntimeError("validated deploy missing position")
        error = validate_deployment(
            position, width=self.scenario.map.width,
            height=self.scenario.map.height,
            blocked_positions=self.scenario.map.blocked_positions,
            rules=self.ruleset.movement,
            occupied_positions=self._occupied_positions(
                exclude=operator.operator_id),
        )
        if error is not None:
            raise RuntimeError(f"validated deploy failed:{error}")
        before = operator.position
        operator.x, operator.y = position
        operator.deployed = True
        events.append(EngineEvent(
            world.tick, EventType.OPERATOR_DEPLOYED, action.side,
            operator.operator_id,
            data={"before": list(before), "after": list(position),
                  "action_id": action.action_id},
        ))

    def _apply_move(self, action: EngineAction,
                    events: list[EngineEvent]) -> None:
        world = self._require_world()
        operator = world.operators[action.actor_id]
        position = action.target_position
        if position is None:
            raise RuntimeError("validated move missing position")
        error = validate_move(
            operator, position, width=self.scenario.map.width,
            height=self.scenario.map.height,
            blocked_positions=self.scenario.map.blocked_positions,
            rules=self.ruleset.movement,
            occupied_positions=self._occupied_positions(
                exclude=operator.operator_id),
        )
        if error is not None:
            raise RuntimeError(f"validated move failed:{error}")
        before = operator.position
        operator.x, operator.y = position
        operator.speed = distance(before, position)
        events.append(EngineEvent(
            world.tick, EventType.OPERATOR_MOVED, action.side,
            operator.operator_id,
            data={"before": list(before), "after": list(position),
                  "distance": operator.speed, "action_id": action.action_id},
        ))

    def _apply_state(self, action: EngineAction,
                     events: list[EngineEvent]) -> None:
        world = self._require_world()
        operator = world.operators[action.actor_id]
        before = operator.state
        operator.state = str(action.parameters.get("state", "ready"))
        events.append(EngineEvent(
            world.tick, EventType.OPERATOR_STATE_CHANGED, action.side,
            operator.operator_id,
            data={"before": before, "after": operator.state,
                  "action_id": action.action_id},
        ))

    def _score_objectives(self, events: list[EngineEvent]) -> None:
        world = self._require_world()
        for objective in sorted(world.objectives.values(),
                                key=lambda item: item.objective_id):
            occupying = {operator.side for operator in world.operators.values()
                         if operator.alive and operator.deployed
                         and distance(operator.position, (objective.x, objective.y))
                         <= objective.radius
                         + self.ruleset.movement.float_tolerance}
            controller = next(iter(occupying)) if len(occupying) == 1 else None
            if controller != objective.controlling_side:
                objective.controlling_side = controller
                events.append(EngineEvent(
                    world.tick, EventType.OBJECTIVE_CONTROLLED, controller,
                    target_id=objective.objective_id,
                    data={"controlling_side": controller},
                ))
            if controller is not None:
                world.sides[controller].objective_score += objective.value

    def _friendly(self, side: str) -> list[Any]:
        world = self._require_world()
        return sorted((item for item in world.operators.values()
                       if item.side == side), key=lambda item: item.operator_id)

    def _visible_enemies(self, side: str) -> list[Any]:
        world = self._require_world()
        friendly = self._friendly(side)
        return sorted((target for target in world.operators.values()
                       if target.side != side
                       and any(can_see(observer, target, self.ruleset.visibility)
                               for observer in friendly)),
                      key=lambda item: item.operator_id)

    def _occupied_positions(self, *, exclude: int) -> tuple[tuple[float, float], ...]:
        world = self._require_world()
        return tuple(sorted(item.position for item in world.operators.values()
                            if item.operator_id != exclude and item.deployed
                            and item.alive))

    def _require_world(self) -> WorldState:
        if self._world is None:
            raise RuntimeError("engine_not_reset")
        return self._world

    def _require_side(self, side: str) -> None:
        if side not in self.scenario.side_order:
            raise ValueError(f"unknown side: {side}")
