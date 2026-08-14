from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from wargame_native_engine.adjudication import (
    AdjudicationTableSet, DiceRoller, DirectFireAdjudicator,
    DirectFireRequest, WeaponProfile,
)
from wargame_native_engine.compat.source_frame.models import ArmorClass, UnitDomain

from wargame_native_engine.core.action import (
    ACTION_PRIORITY, ActionDecision, ActionType, EngineAction, ValidAction,
    valid_action_sort_key,
)
from wargame_native_engine.core.action_router import ActionRouter, RoutedAction
from wargame_native_engine.core.event import EngineEvent, EventType, event_sort_key
from wargame_native_engine.core.observation import (
    OperatorObservation, SideObservation, objective_view,
)
from wargame_native_engine.core.result import GameResult, StepResult
from wargame_native_engine.core.rng import NativeRNG
from wargame_native_engine.core.world import Stage, WorldState
from wargame_native_engine.io.scenario import NativeScenario
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.movement.map_adapter import SourceHexMap
from wargame_native_engine.movement.models import MovementMode, RoutePlan
from wargame_native_engine.movement.planner import MovementPlanner
from wargame_native_engine.movement.policy import (
    SourceMovementPolicy, load_source_movement_policy,
)
from wargame_native_engine.movement.runtime import MovementRuntime
from wargame_native_engine.movement.route_planner import DeterministicRoutePlanner
from wargame_native_engine.rules.config import Ruleset, load_ruleset
from wargame_native_engine.rules.terminal_rules import TerminalRules
from wargame_native_engine.core.time import (
    require_positive_milliseconds,
    resolve_delta_time_ms,
)
from wargame_native_engine.systems.combat import validate_attack
from wargame_native_engine.systems.damage import apply_damage
from wargame_native_engine.systems.deployment import validate_deployment
from wargame_native_engine.systems.movement import (
    distance, move_toward, validate_move,
)
from wargame_native_engine.systems.terminal import evaluate_terminal
from wargame_native_engine.systems.visibility import can_see
from wargame_native_engine.visibility import (
    HexCell, HexMapProfile, ObservationProfiles, evaluate_visibility,
    within_maximum_observation_distance,
)


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
        self._source_policy: SourceMovementPolicy | None = None
        self._source_map: SourceHexMap | None = None
        self._source_visibility_map: HexMapProfile | None = None
        self._source_observation_profiles = ObservationProfiles()
        self._source_valid_actions_cache: dict[
            str, tuple[Mapping[str, Any], ...]
        ] = {}
        self._source_valid_actions_state_hash: str | None = None
        self._movement: MovementRuntime | None = None
        self._route_planner: DeterministicRoutePlanner | None = None
        self._action_router: ActionRouter | None = None
        self._adjudication_tables: AdjudicationTableSet | None = None
        if scenario.map.hexes:
            self._source_policy = load_source_movement_policy()
            self._source_map = SourceHexMap(scenario.map, self._source_policy)
            movement_planner = MovementPlanner(
                self._source_map, self._source_policy
            )
            self._movement = MovementRuntime(
                movement_planner,
                self._source_policy)
            self._route_planner = DeterministicRoutePlanner(movement_planner)
            self._action_router = ActionRouter()
            self._adjudication_tables = AdjudicationTableSet(
                Path(__file__).resolve().parents[3]
                / "config/adjudication/source_tables_v1/native_adjudication_tables_final.json")
            self._source_visibility_map = HexMapProfile({
                item.hex_id: HexCell(
                    item.hex_id, float(item.elevation),
                    self._source_policy.terrain(item.terrain).value)
                for item in scenario.map.hexes
            })

    def reset(self, seed: int = 42) -> WorldState:
        if self._closed:
            raise RuntimeError("engine_closed")
        self._seed = int(seed)
        self._rng = NativeRNG(self._seed)
        self._world = self.scenario.initial_world()
        self._world.elapsed_time_ms = self._world.tick * 1000
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
        self._source_valid_actions_cache.clear()
        self._source_valid_actions_state_hash = None
        if self._source_policy is not None:
            self._refresh_source_observation()
        return WorldState.from_dict(self._world.to_dict())

    def observe(self, side: str) -> SideObservation:
        world = self._require_world()
        self._require_side(side)
        friendly = tuple(OperatorObservation.from_state(item)
                         for item in self._friendly(side))
        visible = tuple(OperatorObservation.from_state(
                            item, include_raw_attributes=False)
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
        if self._source_policy is not None:
            state_hash = canonical_hash(world.to_dict())
            if state_hash != self._source_valid_actions_state_hash:
                self._source_valid_actions_cache.clear()
                self._source_valid_actions_state_hash = state_hash
            records = self._source_valid_actions_cache.get(side)
            if records is None:
                records = tuple(
                    item.to_dict() for item in self._source_valid_actions(side)
                )
                self._source_valid_actions_cache[side] = records
            return tuple(
                ValidAction(
                    EngineAction.from_dict(item["action"]),
                    int(item["priority"]),
                    str(item["source"]),
                )
                for item in records
            )
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

    def plan_move(
        self,
        side: str,
        unit_id: int,
        destination_hex: int,
        movement_mode: MovementMode | str = MovementMode.NORMAL,
    ) -> RoutePlan:
        """Read-only destination proposal using the execution cost model."""

        world = self._require_world()
        self._require_side(side)
        mode = (
            movement_mode
            if isinstance(movement_mode, MovementMode)
            else MovementMode(str(movement_mode).upper())
        )
        if world.done:
            return RoutePlan(
                False, "SESSION_TERMINAL", int(unit_id), None,
                int(destination_hex), mode,
            )
        try:
            operator = world.operators[int(unit_id)]
        except KeyError as exc:
            raise ValueError(f"unknown unit: {unit_id}") from exc
        if operator.side != side:
            raise ValueError("unit is not controlled by side")
        if self._route_planner is None:
            return RoutePlan(
                False, "HEX_MAP_REQUIRED", int(unit_id), operator.current_hex,
                int(destination_hex), mode,
            )
        return self._route_planner.plan(operator, int(destination_hex), mode, world)

    def step(self, actions: Mapping[str, EngineAction | None]
             | Sequence[EngineAction], *,
             delta_time_seconds: int | float | None = None,
             delta_time_ms: int | None = None) -> StepResult:
        return self._step_ms(
            actions, delta_time_ms=resolve_delta_time_ms(
                delta_time_seconds=delta_time_seconds,
                delta_time_ms=delta_time_ms,
            )
        )

    def step_ms(self, actions: Mapping[str, EngineAction | None]
                | Sequence[EngineAction], *, delta_time_ms: int) -> StepResult:
        """Advance once using the authoritative exact-millisecond contract."""

        return self.step(
            actions,
            delta_time_ms=require_positive_milliseconds(delta_time_ms),
        )

    def _step_ms(self, actions: Mapping[str, EngineAction | None]
                 | Sequence[EngineAction], *, delta_time_ms: int) -> StepResult:
        milliseconds = require_positive_milliseconds(delta_time_ms)
        if self._source_policy is not None:
            return self._step_source(actions, delta_time_ms=milliseconds)
        if milliseconds != 1_000:
            raise ValueError("legacy native-default step requires delta_time_ms=1000")
        if not isinstance(actions, Mapping):
            raise TypeError("legacy native-default actions must be a side mapping")
        return self._step_legacy(actions)

    def _step_legacy(self, actions: Mapping[str, EngineAction | None]) -> StepResult:
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

    def _step_source(self, actions: Mapping[str, EngineAction | None]
                     | Sequence[EngineAction], *,
                     delta_time_ms: int) -> StepResult:
        world = self._require_world()
        if world.done:
            raise RuntimeError("episode_already_finished")
        if world.stage is not Stage.COMBAT:
            raise RuntimeError("source-compatible actions require combat stage")
        if self._action_router is None or self._movement is None:
            raise RuntimeError("source-compatible runtime is unavailable")
        tick_before, stage_before = world.tick, world.stage
        routed = self._action_router.normalize(
            actions, side_order=self.scenario.side_order,
            start_sequence=world.next_action_sequence)
        world.next_action_sequence += len(routed)
        common_errors: dict[int, str | None] = {}
        seen_actors: set[int] = set()
        for item in routed:
            error = self._action_router.common_error(
                item, world, self.scenario.side_order)
            actor_id = item.action.actor_id
            if error is None and actor_id is not None:
                if actor_id in seen_actors:
                    error = "DUPLICATE_ACTOR_ACTION"
                seen_actors.add(actor_id)
            common_errors[item.action_sequence] = error

        accepted: list[RoutedAction] = []
        decisions: list[ActionDecision] = []
        events: list[EngineEvent] = []
        for item in routed:
            action = item.action
            error = common_errors[item.action_sequence]
            if error is None:
                error = self._dispatch_source_action(item, events)
            if error is None:
                accepted.append(item)
                self._action_counts[action.side] += 1
                decisions.append(ActionDecision(
                    action.action_id, True, "accepted",
                    "accepted by source-compatible action router"))
            else:
                if action.side in self._error_counts:
                    self._error_counts[action.side] += 1
                decisions.append(ActionDecision(
                    action.action_id, False, error,
                    "source-compatible validation rejected action"))
                events.append(self._source_event(
                    EventType.ACTION_REJECTED, action.side, action.actor_id,
                    action.target_id, item.action_sequence,
                    {"action_id": action.action_id, "code": error}))

        self._movement.advance(world, delta_time_ms, events)
        self._refresh_source_observation()
        world.last_actions = [item.action.to_dict() for item in routed]
        direct_event_types = {
            EventType.DIRECT_FIRE_REQUESTED, EventType.DIRECT_FIRE_VALIDATED,
            EventType.ATTACK_LEVEL_SELECTED, EventType.ATTACK_LEVEL_MODIFIED,
            EventType.DICE_ROLLED, EventType.ROLL_MODIFIERS_APPLIED,
            EventType.COMBAT_RESULT_LOOKED_UP, EventType.LOSSES_APPLIED,
            EventType.SUPPRESSION_APPLIED, EventType.UNIT_DESTROYED,
            EventType.WEAPON_COOLDOWN_STARTED, EventType.AMMUNITION_CONSUMED,
        }
        world.last_judge_info = [item.to_dict() for item in events
                                 if item.event_type in direct_event_types]
        if self.ruleset.terminal.mission_score_enabled:
            self._score_objectives(events)
        terminal = evaluate_terminal(world, self.terminal_rules,
                                     self.scenario.side_order)
        if terminal.terminal or terminal.truncated:
            world.terminal = terminal.terminal
            world.truncated = terminal.truncated
            world.winner = terminal.winner
            world.terminal_reason = terminal.reason
            world.stage = Stage.TERMINAL
            events.append(self._source_event(
                EventType.GAME_TERMINATED if terminal.terminal
                else EventType.GAME_TRUNCATED,
                terminal.winner, None, None, world.next_action_sequence,
                {"reason": terminal.reason, "winner": terminal.winner}))
        events.sort(key=event_sort_key)
        self._last_events = tuple(events)
        return StepResult(
            tick_before, world.tick, stage_before, world.stage,
            tuple(item.action for item in routed), tuple(decisions), tuple(events),
            world.terminal, world.truncated,
            tuple(item.action_sequence for item in routed), delta_time_ms, True)

    def _dispatch_source_action(self, routed: RoutedAction,
                                events: list[EngineEvent]) -> str | None:
        world = self._require_world()
        action, sequence = routed.action, routed.action_sequence
        if action.action_type is ActionType.WAIT:
            events.append(self._source_event(
                EventType.WAITED, action.side, action.actor_id, None, sequence,
                {"action_id": action.action_id}))
            return None
        operator = world.operators[int(action.actor_id)]
        if action.action_type is ActionType.MOVE:
            raw_path = action.parameters.get("path",
                                             action.parameters.get("move_path"))
            if not isinstance(raw_path, (list, tuple)):
                return "PATH_REQUIRED"
            try:
                path = [int(item) for item in raw_path]
                mode = MovementMode(str(action.parameters.get(
                    "movement_mode", operator.movement_mode)).upper())
            except (TypeError, ValueError):
                return "INVALID_MOVEMENT_PARAMETERS"
            legality = self._movement.planner.validate_path(
                operator, path, mode, world)
            if not legality.legal:
                return legality.reason_code
            self._movement.start(
                world, operator, path=path, mode=mode,
                action_id=action.action_id, action_sequence=sequence,
                events=events)
            return None
        if action.action_type is ActionType.STOP_MOVE:
            if operator.move_to_stop_remaining_ms > 0:
                return "STOP_ALREADY_IN_PROGRESS"
            if not operator.moving:
                return "ACTOR_NOT_MOVING"
            self._movement.request_stop(
                world, operator, action_id=action.action_id,
                action_sequence=sequence, events=events)
            return None
        if action.action_type is ActionType.SHOOT:
            return self._resolve_source_shoot(routed, events)
        if action.action_type is ActionType.WEAPON_LOCK:
            return self._start_source_transition(
                operator, "WEAPON_LOCK", sequence, action.action_id, events)
        if action.action_type is ActionType.WEAPON_UNFOLD:
            return self._start_source_transition(
                operator, "WEAPON_UNFOLD", sequence, action.action_id, events)
        if action.action_type is ActionType.CHANGE_STATE:
            return self._change_source_state(routed, events)
        return "UNSUPPORTED_ACTION_TYPE"

    def _start_source_transition(self, operator: Any, kind: str,
                                 action_sequence: int, action_id: str,
                                 events: list[EngineEvent]) -> str | None:
        if not operator.stopped or operator.moving:
            return "ACTOR_MUST_BE_STOPPED"
        if operator.transition_kind is not None:
            return "TRANSITION_IN_PROGRESS"
        if kind == "WEAPON_LOCK" and operator.weapon_locked:
            return "WEAPON_ALREADY_LOCKED"
        if kind == "WEAPON_UNFOLD" and operator.weapon_unfolded:
            return "WEAPON_ALREADY_UNFOLDED"
        operator.transition_kind = kind
        operator.transition_remaining_ms = int(self._source_policy.transition_ms)
        operator.state = "TRANSITIONING"
        operator.stopped = False
        if kind == "WEAPON_LOCK":
            operator.weapon_unfolded = False
            event_type = EventType.WEAPON_LOCK_STARTED
        else:
            event_type = EventType.WEAPON_UNFOLD_STARTED
        events.append(self._source_event(
            event_type, operator.side, operator.operator_id, None,
            action_sequence, {"action_id": action_id,
                              "duration_ms": operator.transition_remaining_ms,
                              "transition_kind": kind}))
        return None

    def _change_source_state(self, routed: RoutedAction,
                             events: list[EngineEvent]) -> str | None:
        if self._source_map is None or self._source_policy is None:
            return "SOURCE_MAP_UNAVAILABLE"
        action, sequence = routed.action, routed.action_sequence
        operator = self._require_world().operators[int(action.actor_id)]
        target = str(action.parameters.get("state", "")).upper()
        if target in {"MARCHING", "NORMAL"}:
            if not operator.stopped or operator.moving:
                return "ACTOR_MUST_BE_STOPPED"
            if operator.transition_kind is not None:
                return "TRANSITION_IN_PROGRESS"
            if target == "MARCHING":
                if operator.operator_type.upper() != "VEHICLE":
                    return "MARCHING_REQUIRES_VEHICLE"
                if not operator.weapon_locked:
                    return "WEAPON_NOT_LOCKED"
                if operator.current_hex is None or not any(
                        int(item) > 0
                        for item in self._source_map.cell(operator.current_hex).roads):
                    return "MARCHING_REQUIRES_ROAD"
                kind = "ENTER_MARCHING"
            else:
                if operator.movement_mode != MovementMode.MARCHING.value:
                    return "ACTOR_NOT_MARCHING"
                kind = "EXIT_MARCHING"
            operator.transition_kind = kind
            operator.transition_remaining_ms = self._source_policy.transition_ms
            operator.state = "TRANSITIONING"
            operator.stopped = False
            events.append(self._source_event(
                EventType.MOVEMENT_MODE_CHANGE_STARTED, operator.side,
                operator.operator_id, None, sequence,
                {"action_id": action.action_id, "target_state": target,
                 "transition_kind": kind,
                 "duration_ms": operator.transition_remaining_ms}))
            return None
        if target in {MovementMode.CHARGE_1.value,
                      MovementMode.CHARGE_2.value}:
            if operator.operator_type.upper() != "PERSONNEL":
                return "CHARGE_REQUIRES_PERSONNEL"
            if operator.fatigue_level >= 2:
                return "FATIGUE_PREVENTS_MOVEMENT"
            if target == MovementMode.CHARGE_2.value and operator.fatigue_level >= 1:
                return "FATIGUE_PREVENTS_CHARGE_2"
            before = operator.movement_mode
            operator.movement_mode = target
            events.append(self._source_event(
                EventType.OPERATOR_STATE_CHANGED, operator.side,
                operator.operator_id, None, sequence,
                {"action_id": action.action_id, "before": before,
                 "after": target}))
            return None
        return "UNSUPPORTED_STATE_CHANGE"

    def _resolve_source_shoot(self, routed: RoutedAction,
                              events: list[EngineEvent]) -> str | None:
        world = self._require_world()
        action, sequence = routed.action, routed.action_sequence
        if action.target_id is None or action.target_id not in world.operators:
            return "TARGET_NOT_FOUND"
        attacker = world.operators[int(action.actor_id)]
        target = world.operators[int(action.target_id)]
        try:
            request = self._direct_fire_request(action, attacker, target)
        except ValueError as exc:
            return str(exc)
        if self._rng is None or self._adjudication_tables is None:
            raise RuntimeError("source direct-fire runtime is unavailable")
        resolution = DirectFireAdjudicator(
            self._adjudication_tables, DiceRoller(self._rng)).resolve(request)
        for source_event in resolution.events:
            events.append(self._annotate_source_event(source_event, sequence))
        if resolution.outcome is None:
            return resolution.error or resolution.legality.reason_code
        ammo = self._ammunition(attacker)
        ammo[request.weapon.ammunition_key] = resolution.ammunition_after
        attacker.raw_attributes["ammunition"] = ammo
        source = attacker.raw_attributes.get("source_frame")
        if isinstance(source, dict):
            source["remain_bullet_nums"] = dict(ammo)
        attacker.weapon_cooldown_remaining_ms = resolution.cooldown_after * 1000
        if target.state == "SUPPRESSED":
            target.suppression_remaining_ms = self._source_policy.suppression_ms
        return None

    def _direct_fire_request(self, action: EngineAction, attacker: Any,
                             target: Any) -> DirectFireRequest:
        if (self._source_map is None or self._source_visibility_map is None
                or self._source_policy is None):
            raise ValueError("SOURCE_MAP_UNAVAILABLE")
        if attacker.current_hex is None or target.current_hex is None:
            raise ValueError("CURRENT_HEX_REQUIRED")
        weapon_id = action.parameters.get("weapon_id")
        if weapon_id is None:
            raise ValueError("WEAPON_REQUIRED")
        weapon_id = int(weapon_id)
        profile = self._source_weapon_profile(weapon_id)
        target_domain = _unit_domain(target.operator_type)
        armor_value = str(target.raw_attributes.get(
            "armor_class",
            target.raw_attributes.get("normalized_value", {}).get(
                "armor_class", "UNKNOWN"))).upper()
        try:
            armor = ArmorClass(armor_value)
        except ValueError as exc:
            raise ValueError("UNKNOWN_ARMOR_CLASS") from exc
        if target_domain is UnitDomain.VEHICLE and armor is ArmorClass.UNKNOWN:
            raise ValueError("UNKNOWN_ARMOR_CLASS")
        visibility = evaluate_visibility(
            self._source_visibility_map,
            observer_hex=attacker.current_hex, target_hex=target.current_hex,
            observer_domain=_unit_domain(attacker.operator_type),
            target_domain=target_domain,
            target_concealed=bool(target.raw_attributes.get("concealed", False)),
            target_terrain=self._source_map.terrain(target.current_hex).value)
        left = self._source_map.cell(attacker.current_hex)
        right = self._source_map.cell(target.current_hex)
        ammunition = self._ammunition(attacker)
        passengers = tuple(
            self._require_world().operators[int(item)]
            for item in target.raw_attributes.get("passenger_ids", [])
            if int(item) in self._require_world().operators)
        return DirectFireRequest(
            tick=self._require_world().tick, episode_seed=self._seed,
            action_id=action.action_id, attacker=attacker, target=target,
            weapon=profile, visibility=visibility, distance=visibility.distance,
            target_domain=target_domain, target_armor=armor,
            ammunition_before=ammunition.get(profile.ammunition_key, 0),
            weapon_cooldown_before=(attacker.weapon_cooldown_remaining_ms + 999) // 1000,
            weapon_unfolded=attacker.weapon_unfolded,
            elevation_difference=int(round(float(right.elevation)
                                           - float(left.elevation))),
            attacker_suppressed=attacker.suppression_remaining_ms > 0,
            attacker_moving=(not attacker.stopped
                             or attacker.movement_mode == MovementMode.MARCHING.value),
            target_terrain=self._source_map.terrain(target.current_hex).value,
            target_concealed=bool(target.raw_attributes.get("concealed", False)),
            target_moving=not target.stopped,
            target_stacked=sum(
                1 for item in self._require_world().operators.values()
                if item.alive and item.current_hex == target.current_hex) > 1,
            target_marching=target.movement_mode == MovementMode.MARCHING.value,
            attacker_transitioning=(attacker.transition_kind is not None
                                    or attacker.move_to_stop_remaining_ms > 0),
            target_external_direct_fire_prohibited=target.state == "CLOSE_COMBAT",
            carried_passengers=passengers)

    def _source_weapon_profile(self, weapon_id: int) -> WeaponProfile:
        source = self.scenario.metadata.get("source_compatible", {})
        weapons = source.get("weapons", {}) if isinstance(source, Mapping) else {}
        value = weapons.get(str(weapon_id), weapons.get(weapon_id))
        if not isinstance(value, Mapping):
            raise ValueError("WEAPON_PROFILE_UNAVAILABLE")
        try:
            domains = frozenset(_unit_domain(str(item))
                                for item in value["target_domains"])
            return WeaponProfile(
                weapon_id=weapon_id, weapon_class=str(value["weapon_class"]),
                maximum_range=int(value["maximum_range"]),
                target_domains=domains,
                ammunition_key=str(value.get("ammunition_key", "0")),
                cooldown_ticks=(None if value.get("cooldown_seconds") is None
                                else int(value["cooldown_seconds"])),
                requires_unfolded=bool(value.get("requires_unfolded", True)),
                can_fire_while_moving=bool(value.get("can_fire_while_moving", False)))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("INVALID_WEAPON_PROFILE") from exc

    @staticmethod
    def _ammunition(operator: Any) -> dict[str, int]:
        value = operator.raw_attributes.get("ammunition")
        if not isinstance(value, Mapping):
            source = operator.raw_attributes.get("source_frame", {})
            value = source.get("remain_bullet_nums", {}) if isinstance(
                source, Mapping) else {}
        return {str(key): int(item) for key, item in value.items()}

    def _annotate_source_event(self, event: EngineEvent,
                               action_sequence: int) -> EngineEvent:
        world = self._require_world()
        sequence = world.next_event_sequence
        world.next_event_sequence += 1
        return EngineEvent(
            tick=event.tick, event_type=event.event_type, side=event.side,
            actor_id=event.actor_id, target_id=event.target_id,
            data={**event.data, "event_time_ms": world.elapsed_time_ms,
                  "action_sequence": action_sequence,
                  "event_sequence": sequence})

    def _source_event(self, event_type: EventType, side: str | None,
                      actor_id: int | None, target_id: int | None,
                      action_sequence: int,
                      data: Mapping[str, Any]) -> EngineEvent:
        world = self._require_world()
        sequence = world.next_event_sequence
        world.next_event_sequence += 1
        return EngineEvent(
            tick=world.tick, event_type=event_type, side=side,
            actor_id=actor_id, target_id=target_id,
            data={**data, "event_time_ms": world.elapsed_time_ms,
                  "action_sequence": action_sequence,
                  "event_sequence": sequence})

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
        value = {
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
        if self._source_policy is not None:
            value["movement_policy"] = {
                "movement_policy_id": self._source_policy.policy_id,
                "movement_policy_version": self._source_policy.policy_version,
                "movement_policy_hash": self._source_policy.content_hash,
            }
        if self._adjudication_tables is not None:
            value["adjudication_table_set"] = {
                "adjudication_table_set_id": self._adjudication_tables.table_set_id,
                "adjudication_table_set_version": (
                    self._adjudication_tables.table_set_version),
                "adjudication_table_set_hash": (
                    self._adjudication_tables.content_hash),
            }
        return value

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
        movement = snapshot.get("movement_policy")
        if self._source_policy is not None:
            expected = {"movement_policy_id": self._source_policy.policy_id,
                        "movement_policy_version": self._source_policy.policy_version,
                        "movement_policy_hash": self._source_policy.content_hash}
            if movement != expected:
                raise ValueError("snapshot movement policy mismatch")
        elif movement is not None:
            raise ValueError("snapshot requires source-compatible movement")
        adjudication = snapshot.get("adjudication_table_set")
        if self._adjudication_tables is not None:
            expected_adjudication = {
                "adjudication_table_set_id": self._adjudication_tables.table_set_id,
                "adjudication_table_set_version": (
                    self._adjudication_tables.table_set_version),
                "adjudication_table_set_hash": self._adjudication_tables.content_hash,
            }
            if adjudication != expected_adjudication:
                raise ValueError("snapshot adjudication table set mismatch")
        elif adjudication is not None:
            raise ValueError("snapshot requires source-compatible adjudication")
        self._seed = int(snapshot["seed"])
        self._rng = NativeRNG.restore(snapshot["rng"])
        self._world = WorldState.from_dict(snapshot["world"])
        self._action_counts = {str(key): int(item)
                               for key, item in snapshot["action_counts"].items()}
        self._error_counts = {str(key): int(item)
                              for key, item in snapshot["error_counts"].items()}
        self._last_events = ()
        self._source_valid_actions_cache.clear()
        self._source_valid_actions_state_hash = None
        if self._source_policy is not None:
            self._refresh_source_observation()
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

    def _source_valid_actions(self, side: str) -> tuple[ValidAction, ...]:
        world = self._require_world()
        if self._movement is None or self._source_map is None:
            return ()
        values: list[ValidAction] = []
        for operator in self._friendly(side):
            if not operator.alive or not operator.deployed:
                continue
            if operator.moving:
                action = EngineAction(side, operator.operator_id,
                                      ActionType.STOP_MOVE)
                values.append(ValidAction(action, ACTION_PRIORITY[action.action_type],
                                          "source_action_router"))
            elif (operator.stopped and operator.transition_kind is None
                  and operator.current_hex is not None):
                try:
                    mode = MovementMode(operator.movement_mode)
                except ValueError:
                    mode = MovementMode.NORMAL
                for target in sorted(item for item in
                                     self._source_map.cell(operator.current_hex).neighbors
                                     if item >= 0):
                    legality = self._movement.planner.validate_path(
                        operator, [target], mode, world)
                    if legality.legal:
                        action = EngineAction(
                            side, operator.operator_id, ActionType.MOVE,
                            parameters={"path": [target],
                                        "movement_mode": mode.value})
                        values.append(ValidAction(
                            action, ACTION_PRIORITY[action.action_type],
                            "source_action_router"))
            visible_targets = {
                int(value)
                for value in operator.raw_attributes.get(
                    "see_enemy_bop_ids", ()
                )
            }
            for weapon_id in sorted(operator.weapon_ids):
                for target in sorted(world.operators.values(),
                                     key=lambda item: item.operator_id):
                    if (
                        target.side == side
                        or not target.alive
                        or target.operator_id not in visible_targets
                    ):
                        continue
                    action = EngineAction(
                        side, operator.operator_id, ActionType.SHOOT,
                        target_id=target.operator_id,
                        parameters={"weapon_id": weapon_id})
                    try:
                        request = self._direct_fire_request(action, operator, target)
                        legal = DirectFireAdjudicator(
                            self._adjudication_tables,
                            DiceRoller(self._rng)).validate(request)
                    except (TypeError, ValueError):
                        continue
                    if legal.legal:
                        values.append(ValidAction(
                            action, ACTION_PRIORITY[action.action_type],
                            "source_action_router"))
            if operator.stopped and operator.transition_kind is None:
                if not operator.weapon_locked:
                    action = EngineAction(side, operator.operator_id,
                                          ActionType.WEAPON_LOCK)
                    values.append(ValidAction(action, ACTION_PRIORITY[action.action_type],
                                              "source_action_router"))
                if not operator.weapon_unfolded:
                    action = EngineAction(side, operator.operator_id,
                                          ActionType.WEAPON_UNFOLD)
                    values.append(ValidAction(action, ACTION_PRIORITY[action.action_type],
                                              "source_action_router"))
            wait = EngineAction(side, operator.operator_id, ActionType.WAIT)
            values.append(ValidAction(wait, ACTION_PRIORITY[wait.action_type],
                                      "source_action_router"))
        if not values:
            action = EngineAction(side, None, ActionType.WAIT)
            values.append(ValidAction(action, ACTION_PRIORITY[action.action_type],
                                      "source_action_router"))
        return tuple(sorted(values, key=valid_action_sort_key))

    def _refresh_source_observation(self) -> None:
        if self._source_map is None or self._source_visibility_map is None:
            return
        world = self._require_world()
        self._source_valid_actions_cache.clear()
        self._source_valid_actions_state_hash = None
        for observer in sorted(world.operators.values(),
                               key=lambda item: item.operator_id):
            visible: list[int] = []
            if observer.alive and observer.deployed and observer.current_hex is not None:
                observer_domain = _unit_domain(observer.operator_type)
                for target in sorted(world.operators.values(),
                                     key=lambda item: item.operator_id):
                    if (target.side == observer.side or not target.alive
                            or not target.deployed or target.current_hex is None):
                        continue
                    target_domain = _unit_domain(target.operator_type)
                    if not within_maximum_observation_distance(
                        observer_hex=observer.current_hex,
                        target_hex=target.current_hex,
                        observer_domain=observer_domain,
                        target_domain=target_domain,
                        observation_profiles=self._source_observation_profiles,
                    ):
                        continue
                    result = evaluate_visibility(
                        self._source_visibility_map,
                        observer_hex=observer.current_hex,
                        target_hex=target.current_hex,
                        observer_domain=observer_domain,
                        target_domain=target_domain,
                        target_concealed=bool(
                            target.raw_attributes.get("concealed", False)),
                        target_terrain=self._source_map.terrain(
                            target.current_hex).value)
                    if result.observable:
                        visible.append(target.operator_id)
            observer.raw_attributes["see_enemy_bop_ids"] = visible

    def _visible_enemies(self, side: str) -> list[Any]:
        world = self._require_world()
        if self._source_policy is not None:
            visible_ids = {int(target_id)
                           for observer in self._friendly(side)
                           for target_id in observer.raw_attributes.get(
                               "see_enemy_bop_ids", [])}
            return sorted((item for item in world.operators.values()
                           if item.side != side and item.operator_id in visible_ids),
                          key=lambda item: item.operator_id)
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


def _unit_domain(value: str) -> UnitDomain:
    try:
        return UnitDomain(str(value).upper())
    except ValueError:
        return UnitDomain.UNKNOWN
