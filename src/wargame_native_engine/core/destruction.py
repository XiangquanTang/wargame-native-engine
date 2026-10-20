"""Shared authoritative destruction transition for Source-compatible units."""

from __future__ import annotations

from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.core.world import WorldState


def destroy_source_operator(
    world: WorldState,
    operator: OperatorState,
    *,
    consume_lifecycle: bool = False,
) -> None:
    """Destroy an operator and atomically terminate its in-flight runtime state."""

    segment_event_id = operator.segment_event_id
    if segment_event_id is not None:
        for evidence in reversed(world.movement_evidence):
            if evidence.get("event_id") != segment_event_id:
                continue
            evidence.update({
                "progress_after": operator.cur_pos,
                "arrived": False,
                "blocked": False,
                "terminated": True,
                "reason_code": "ACTOR_DESTROYED",
                "event_time": world.elapsed_time_ms,
            })
            break

    operator.blood = 0
    operator.health = 0.0
    operator.alive = False
    operator.state = "destroyed"
    operator.speed = 0.0
    operator.stopped = True
    operator.move_path.clear()
    operator.movement_action_id = None
    operator.movement_action_sequence = None
    operator.movement_stop_requested = False
    operator.movement_blocked_reason = None
    operator.movement_task_pause_reason = None
    operator.segment_from_hex = None
    operator.segment_to_hex = None
    operator.segment_elapsed_ms = 0
    operator.segment_traversal_ms = 0
    operator.segment_event_id = None
    operator.cur_pos_millionths = 0
    operator.move_to_stop_remaining_ms = 0
    operator.transition_kind = None
    operator.transition_remaining_ms = 0
    operator.movement_locked = False
    operator.suppression_remaining_ms = 0
    if "suppression_remaining_seconds" in operator.raw_attributes:
        operator.raw_attributes["suppression_remaining_seconds"] = 0
    if consume_lifecycle and operator.lifecycle_remaining_ms is not None:
        operator.lifecycle_remaining_ms = 0


__all__ = ["destroy_source_operator"]
