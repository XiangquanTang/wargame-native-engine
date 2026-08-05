from __future__ import annotations

from typing import Any

from wargame_native_engine.adjudication.scheduler import EventQueue, ScheduledCommand
from wargame_native_engine.core.event import EngineEvent, EventType
from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.core.world import WorldState
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.movement.models import MovementMode, SegmentPlan
from wargame_native_engine.movement.planner import MovementPlanner
from wargame_native_engine.movement.policy import SourceMovementPolicy


class MovementRuntime:
    """Internal deterministic segment executor; never an external control API."""

    def __init__(self, planner: MovementPlanner,
                 policy: SourceMovementPolicy) -> None:
        self.planner = planner
        self.policy = policy

    def start(self, world: WorldState, operator: OperatorState, *, path: list[int],
              mode: MovementMode, action_id: str, action_sequence: int,
              events: list[EngineEvent]) -> None:
        operator.move_path = list(path)
        operator.movement_mode = mode.value
        operator.movement_action_id = action_id
        operator.movement_action_sequence = action_sequence
        operator.movement_stop_requested = False
        operator.movement_blocked_reason = None
        operator.stopped = False
        operator.state = mode.value if mode is MovementMode.MARCHING else "MOVING"
        self._start_next_segment(world, operator, world.elapsed_time_ms, events)

    def request_stop(self, world: WorldState, operator: OperatorState, *,
                     action_id: str, action_sequence: int,
                     events: list[EngineEvent]) -> None:
        operator.movement_stop_requested = True
        events.append(self._event(world, EventType.STOP_MOVE_REQUESTED, operator,
                                  action_sequence, {"action_id": action_id,
                                                    "reason_code": "ACCEPTED"}))
        if not operator.moving:
            operator.move_path.clear()
            self._start_stopping(world, operator, action_sequence, events,
                                 preserve_path=False)

    def advance(self, world: WorldState, delta_time_ms: int,
                events: list[EngineEvent]) -> None:
        end_time = world.elapsed_time_ms + delta_time_ms
        while world.elapsed_time_ms < end_time:
            remaining = end_time - world.elapsed_time_ms
            boundaries = [remaining]
            for operator in world.operators.values():
                if operator.moving:
                    boundaries.append(operator.segment_traversal_ms
                                      - operator.segment_elapsed_ms)
                if operator.move_to_stop_remaining_ms > 0:
                    boundaries.append(operator.move_to_stop_remaining_ms)
                if operator.transition_remaining_ms > 0:
                    boundaries.append(operator.transition_remaining_ms)
                if operator.stopped and operator.fatigue_level > 0:
                    boundaries.append(self.policy.fatigue_recovery_ms
                                      - operator.fatigue_recovery_elapsed_ms)
            duration = min(item for item in boundaries if item > 0)
            self._advance_duration(world, duration)
            world.elapsed_time_ms += duration
            world.tick = world.elapsed_time_ms // 1000
            self._complete_arrivals(world, events)
            self._complete_timers(world, events)

    def _advance_duration(self, world: WorldState, duration: int) -> None:
        for operator in world.operators.values():
            if operator.moving:
                operator.segment_elapsed_ms += duration
                operator.cur_pos_millionths = min(
                    1_000_000,
                    operator.segment_elapsed_ms * 1_000_000
                    // operator.segment_traversal_ms)
                self._update_evidence(world, operator.segment_event_id,
                                      progress_after=operator.cur_pos)
                if (operator.operator_type.upper() == "PERSONNEL"
                        and (operator.suppression_remaining_ms > 0
                             or operator.state == "SUPPRESSED")):
                    operator.movement_stop_requested = True
            if operator.move_to_stop_remaining_ms > 0:
                operator.move_to_stop_remaining_ms = max(
                    0, operator.move_to_stop_remaining_ms - duration)
            if operator.transition_remaining_ms > 0:
                operator.transition_remaining_ms = max(
                    0, operator.transition_remaining_ms - duration)
            operator.weapon_cooldown_remaining_ms = max(
                0, operator.weapon_cooldown_remaining_ms - duration)
            if operator.suppression_remaining_ms > 0:
                operator.suppression_remaining_ms = max(
                    0, operator.suppression_remaining_ms - duration)
            if operator.stopped and operator.fatigue_level > 0:
                operator.fatigue_recovery_elapsed_ms += duration
            elif not operator.stopped:
                operator.fatigue_recovery_elapsed_ms = 0

    def _complete_arrivals(self, world: WorldState,
                           events: list[EngineEvent]) -> None:
        queue = EventQueue()
        operators = {item.operator_id: item for item in world.operators.values()}
        for operator in operators.values():
            if (not operator.moving
                    or operator.segment_elapsed_ms < operator.segment_traversal_ms):
                continue
            event_sequence = next(
                int(item["event_sequence"])
                for item in reversed(world.movement_evidence)
                if item.get("event_id") == operator.segment_event_id)
            queue.push(ScheduledCommand(
                event_time=world.elapsed_time_ms,
                arrival_index=int(operator.movement_action_sequence or 0),
                action_id=str(operator.segment_event_id),
                submitted_at=world.elapsed_time_ms - operator.segment_traversal_ms,
                payload={"unit_id": operator.operator_id,
                         "event_sequence": event_sequence}))
        arrivals = queue.pop_ready(world.elapsed_time_ms)
        for scheduled in arrivals:
            operator = operators[int(scheduled.payload["unit_id"])]
            target = int(operator.segment_to_hex)
            action_sequence = int(operator.movement_action_sequence or 0)
            if self.planner.friendly_ground_count(
                    world, target, side=operator.side,
                    exclude=operator.operator_id) >= (
                        self.policy.friendly_ground_stack_limit):
                self._update_evidence(world, operator.segment_event_id,
                                      progress_after=1.0, arrived=False,
                                      blocked=True,
                                      reason_code="FRIENDLY_STACK_LIMIT",
                                      event_time=world.elapsed_time_ms)
                operator.movement_blocked_reason = "FRIENDLY_STACK_LIMIT"
                operator.cur_pos_millionths = 0
                self._clear_segment(operator)
                operator.speed = 0.0
                operator.state = "BLOCKED"
                events.append(self._event(
                    world, EventType.MOVEMENT_BLOCKED, operator,
                    action_sequence,
                    {"action_id": operator.movement_action_id,
                     "target_hex": target,
                     "reason_code": "FRIENDLY_STACK_LIMIT"}))
                self._start_stopping(world, operator, action_sequence, events,
                                     preserve_path=True)
                continue
            from_hex = int(operator.segment_from_hex)
            operator.current_hex = target
            row, column = divmod(target, 100)
            operator.x, operator.y = float(column), float(row)
            if operator.move_path and operator.move_path[0] == target:
                operator.move_path.pop(0)
            self._update_evidence(world, operator.segment_event_id,
                                  progress_after=1.0, arrived=True,
                                  blocked=False, reason_code="ARRIVED",
                                  event_time=world.elapsed_time_ms)
            event_id = operator.segment_event_id
            self._clear_segment(operator)
            if (operator.operator_type.upper() == "PERSONNEL"
                    and operator.movement_mode in {
                        MovementMode.CHARGE_1.value,
                        MovementMode.CHARGE_2.value}):
                before = operator.fatigue_level
                operator.fatigue_level = min(2, operator.fatigue_level + 1)
                events.append(self._event(
                    world, EventType.FATIGUE_CHANGED, operator,
                    action_sequence, {"before": before,
                                      "after": operator.fatigue_level,
                                      "reason_code": "CHARGE_HEX_COMPLETED"}))
            events.append(self._event(
                world, EventType.ARRIVED_HEX, operator, action_sequence,
                {"action_id": operator.movement_action_id,
                 "movement_event_id": event_id,
                 "from_hex": from_hex, "to_hex": target,
                 "reason_code": "ARRIVED"}))
            suppressed_stop = (operator.operator_type.upper() == "PERSONNEL"
                               and operator.suppression_remaining_ms > 0)
            if operator.movement_stop_requested or suppressed_stop:
                self._start_stopping(world, operator, action_sequence, events,
                                     preserve_path=suppressed_stop)
            elif operator.move_path:
                self._start_next_segment(world, operator,
                                         world.elapsed_time_ms, events)
            else:
                self._start_stopping(world, operator, action_sequence, events,
                                     preserve_path=False)

    def _complete_timers(self, world: WorldState,
                         events: list[EngineEvent]) -> None:
        for operator in sorted(world.operators.values(),
                               key=lambda item: item.operator_id):
            action_sequence = int(operator.movement_action_sequence or 0)
            if (operator.move_to_stop_remaining_ms == 0
                    and operator.state == "TRANSITIONING_TO_STOP"):
                operator.stopped = True
                operator.speed = 0.0
                operator.state = ("SUPPRESSED" if
                                  operator.suppression_remaining_ms > 0
                                  else "STOPPED")
                operator.movement_stop_requested = False
                events.append(self._event(
                    world, EventType.STOPPING_COMPLETED, operator,
                    action_sequence, {"reason_code": "STOPPED"}))
            if operator.transition_kind is not None and operator.transition_remaining_ms == 0:
                kind = operator.transition_kind
                operator.transition_kind = None
                if kind == "WEAPON_LOCK":
                    operator.weapon_locked = True
                    event_type = EventType.WEAPON_LOCKED
                elif kind == "WEAPON_UNFOLD":
                    operator.weapon_unfolded = True
                    operator.weapon_locked = False
                    event_type = EventType.WEAPON_UNFOLDED
                elif kind == "ENTER_MARCHING":
                    operator.movement_mode = MovementMode.MARCHING.value
                    event_type = EventType.MOVEMENT_MODE_CHANGED
                elif kind == "EXIT_MARCHING":
                    operator.movement_mode = MovementMode.NORMAL.value
                    event_type = EventType.MOVEMENT_MODE_CHANGED
                else:
                    raise RuntimeError(f"unknown transition kind: {kind}")
                operator.state = "STOPPED"
                operator.stopped = True
                events.append(self._event(world, event_type, operator,
                                          action_sequence,
                                          {"transition_kind": kind,
                                           "reason_code": "COMPLETED"}))
            while (operator.stopped and operator.fatigue_level > 0
                   and operator.fatigue_recovery_elapsed_ms >=
                   self.policy.fatigue_recovery_ms):
                before = operator.fatigue_level
                operator.fatigue_level -= 1
                operator.fatigue_recovery_elapsed_ms -= self.policy.fatigue_recovery_ms
                events.append(self._event(
                    world, EventType.FATIGUE_CHANGED, operator,
                    action_sequence, {"before": before,
                                      "after": operator.fatigue_level,
                                      "reason_code": "STOPPED_RECOVERY"}))
            if (operator.suppression_remaining_ms == 0
                    and operator.state == "SUPPRESSED" and operator.stopped):
                operator.state = "STOPPED"

    def _start_next_segment(self, world: WorldState, operator: OperatorState,
                            event_time_ms: int,
                            events: list[EngineEvent]) -> None:
        if not operator.move_path or operator.current_hex is None:
            return
        mode = MovementMode(operator.movement_mode)
        legality, plan = self.planner.plan_segment(
            operator, operator.current_hex, operator.move_path[0], mode)
        if not legality.legal or plan is None:
            operator.movement_blocked_reason = legality.reason_code
            operator.speed = 0.0
            operator.state = "BLOCKED"
            events.append(self._event(
                world, EventType.MOVEMENT_BLOCKED, operator,
                int(operator.movement_action_sequence or 0),
                {"action_id": operator.movement_action_id,
                 "target_hex": operator.move_path[0],
                 "reason_code": legality.reason_code}))
            self._start_stopping(
                world, operator, int(operator.movement_action_sequence or 0),
                events, preserve_path=True)
            return
        operator.segment_from_hex = plan.from_hex
        operator.segment_to_hex = plan.to_hex
        operator.segment_elapsed_ms = 0
        operator.segment_traversal_ms = plan.traversal_ms
        operator.cur_pos_millionths = 0
        operator.speed = plan.effective_speed_hex_per_second
        event_sequence = self._next_event_sequence(world)
        event_id = "movement-" + canonical_hash({
            "action_id": operator.movement_action_id,
            "action_sequence": operator.movement_action_sequence,
            "event_sequence": event_sequence,
            "from_hex": plan.from_hex, "to_hex": plan.to_hex,
            "movement_policy_hash": self.policy.content_hash,
        })[:24]
        operator.segment_event_id = event_id
        evidence = {
            "event_id": event_id,
            "action_id": operator.movement_action_id,
            "action_sequence": operator.movement_action_sequence,
            "event_sequence": event_sequence,
            "event_time": event_time_ms + plan.traversal_ms,
            "unit_id": operator.operator_id,
            **plan.to_dict(),
            "progress_before": 0.0, "progress_after": 0.0,
            "arrived": False, "blocked": False, "reason_code": "IN_PROGRESS",
            "movement_policy_id": self.policy.policy_id,
            "movement_policy_version": self.policy.policy_version,
            "movement_policy_hash": self.policy.content_hash,
        }
        world.movement_evidence.append(evidence)
        events.append(EngineEvent(
            tick=event_time_ms // 1000, event_type=EventType.MOVEMENT_STARTED,
            side=operator.side, actor_id=operator.operator_id,
            data={**evidence, "event_time_ms": event_time_ms}))

    def _start_stopping(self, world: WorldState, operator: OperatorState,
                        action_sequence: int, events: list[EngineEvent], *,
                        preserve_path: bool) -> None:
        if not preserve_path:
            operator.move_path.clear()
        operator.speed = 0.0
        operator.stopped = False
        operator.state = "TRANSITIONING_TO_STOP"
        operator.move_to_stop_remaining_ms = self.policy.transition_ms
        events.append(self._event(
            world, EventType.STOPPING_STARTED, operator, action_sequence,
            {"duration_ms": self.policy.transition_ms,
             "preserved_path": preserve_path,
             "reason_code": "TRANSITION_STARTED"}))

    @staticmethod
    def _clear_segment(operator: OperatorState) -> None:
        operator.segment_from_hex = None
        operator.segment_to_hex = None
        operator.segment_elapsed_ms = 0
        operator.segment_traversal_ms = 0
        operator.segment_event_id = None
        operator.cur_pos_millionths = 0

    @staticmethod
    def _update_evidence(world: WorldState, event_id: str | None,
                         **changes: Any) -> None:
        if event_id is None:
            return
        for evidence in reversed(world.movement_evidence):
            if evidence.get("event_id") == event_id:
                evidence.update(changes)
                return
        raise RuntimeError(f"movement evidence not found: {event_id}")

    def _event(self, world: WorldState, event_type: EventType,
               operator: OperatorState, action_sequence: int,
               data: dict[str, Any]) -> EngineEvent:
        event_sequence = self._next_event_sequence(world)
        return EngineEvent(
            tick=world.elapsed_time_ms // 1000, event_type=event_type,
            side=operator.side, actor_id=operator.operator_id,
            data={**data, "event_time_ms": world.elapsed_time_ms,
                  "action_sequence": action_sequence,
                  "event_sequence": event_sequence})

    @staticmethod
    def _next_event_sequence(world: WorldState) -> int:
        value = world.next_event_sequence
        world.next_event_sequence += 1
        return value
