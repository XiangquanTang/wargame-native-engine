"""Post-validation boarding mutations for the Native runtime.

Capability and request validation remain outside this module.  These helpers
operate on the working ``WorldState`` supplied by ``NativeEngine`` and yield
event descriptions so the engine retains authoritative event sequencing.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from wargame_native_engine.adjudication.boarding import (
    BoardRequest,
    UnloadRequest,
)
from wargame_native_engine.core.action_router import RoutedAction
from wargame_native_engine.core.boarding import (
    BoardingTransition,
    BoardingTransitionStatus,
    BoardingTransitionType,
    advance_boarding_transitions,
    boarding_transition_id,
    normalize_boarding_transitions,
)
from wargame_native_engine.core.event import EventType
from wargame_native_engine.core.world import WorldState

from wargame_native_engine.rules.mounted_position import MOUNTED_POSITION_POLICY, MOUNTED_POSITION_POLICIES
from wargame_native_engine.rules.boarding import BoardingAuthority


@dataclass(frozen=True, slots=True)
class SourceBoardingIntent:
    routed: RoutedAction
    transition_type: BoardingTransitionType
    request: BoardRequest | UnloadRequest


@dataclass(frozen=True, slots=True)
class SourceBoardingEvent:
    event_type: EventType
    side: str | None
    actor_id: int
    target_id: int
    action_sequence: int
    data: Mapping[str, object]


def apply_source_boarding_intents(
    world: WorldState,
    intents: Sequence[SourceBoardingIntent],
    authority: BoardingAuthority,
) -> Iterator[SourceBoardingEvent]:
    """Apply accepted relation changes and create active transitions."""

    transitions = list(normalize_boarding_transitions(world.boarding_transitions))
    used_ids = {item.transition_id for item in transitions}
    for intent in sorted(
        intents,
        key=lambda item: item.routed.action_sequence,
    ):
        request = intent.request
        carrier = world.operators[request.carrier_id]
        passenger = world.operators[request.passenger_id]
        profile = authority.profile(request.profile_id)
        if profile is None:
            raise RuntimeError("BOARDING_PROFILE_UNAVAILABLE")
        # Action IDs identify reusable command content, not executions. Keep the
        # historical ID for the first execution (old snapshots/replays), then
        # allocate from retained history, including this batch. No hidden counter
        # or randomness: restoring the same world reproduces the same IDs.
        base_id = boarding_transition_id(request.action_id, intent.transition_type)
        transition_id = base_id
        occurrence = 2
        while transition_id in used_ids:
            transition_id = f"{base_id}-{occurrence}"
            occurrence += 1
        transition = BoardingTransition(
            transition_id=transition_id,
            transition_type=intent.transition_type,
            actor_id=request.actor_id,
            carrier_id=request.carrier_id,
            passenger_id=request.passenger_id,
            remaining_ms=profile.transition_duration_ms,
            transition_start_tick=world.tick,
            completed_tick=None,
            status=BoardingTransitionStatus.ACTIVE,
            profile_id=profile.profile_id,
            authority_version=request.authority_version,
            authority_hash=request.authority_hash,
        )
        # Unique execution IDs must not legitimize simultaneous transitions for
        # the same carrier/passenger. Validate before changing their relation.
        normalize_boarding_transitions((*transitions, transition))
        used_ids.add(transition_id)
        carrier.movement_locked = True
        if intent.transition_type is BoardingTransitionType.BOARD:
            carrier.passenger_id = passenger.operator_id
            passenger.mounted_on = carrier.operator_id
            passenger.deployed = False
            passenger.current_hex = None
            passenger.state = "ON_BOARD"
            passenger.stopped = True
            passenger.speed = 0.0
            event_type = EventType.BOARD_STARTED
        else:
            event_type = EventType.UNLOAD_STARTED
        transitions.append(transition)
        yield SourceBoardingEvent(
            event_type=event_type,
            side=request.side,
            actor_id=request.actor_id,
            target_id=request.carrier_id,
            action_sequence=intent.routed.action_sequence,
            data={
                "action_id": request.action_id,
                "transition_id": transition.transition_id,
                "carrier_id": request.carrier_id,
                "passenger_id": request.passenger_id,
                "duration_ms": transition.remaining_ms,
                "profile_id": request.profile_id,
                "authority_version": request.authority_version,
                "authority_hash": request.authority_hash,
            },
        )
    world.boarding_transitions = normalize_boarding_transitions(transitions)


def sync_mounted_passenger_positions(world: WorldState, *, include_progress: bool = False) -> None:
    """Carry coordinates with the explicit relation; passengers stay off-map.

    The legacy policy calls this after movement; the current policy also
    initializes carried coordinates on reset and includes segment progress.
    It never creates a passenger MOVE, grants visibility, or changes deployment.
    """
    for passenger in sorted(world.operators.values(), key=lambda item: item.operator_id):
        if passenger.mounted_on is None or not passenger.alive:
            continue
        carrier = world.operators.get(passenger.mounted_on)
        if carrier is None or carrier.passenger_id != passenger.operator_id or carrier.side != passenger.side:
            raise RuntimeError("BOARDING_CARRIER_RELATION_MISMATCH")
        passenger.x, passenger.y = carrier.x, carrier.y
        if include_progress:
            # current_hex is the independent occupancy slot, not the carried
            # location: keep it None and never copy the carrier's active route
            # or segment timers (which would execute a second movement task).
            passenger.cur_pos_millionths = carrier.cur_pos_millionths


def advance_source_boarding_transitions(
    world: WorldState,
    delta_time_ms: int,
) -> Iterator[SourceBoardingEvent]:
    """Advance existing timers and apply completed relation changes."""

    transitions, completed = advance_boarding_transitions(
        world.boarding_transitions,
        delta_time_ms,
        completed_tick=world.tick,
    )
    world.boarding_transitions = transitions
    for transition in completed:
        carrier = world.operators.get(transition.carrier_id)
        passenger = world.operators.get(transition.passenger_id)
        if carrier is None or passenger is None:
            raise RuntimeError("BOARDING_TRANSITION_OPERATOR_MISSING")
        if (
            carrier.passenger_id != passenger.operator_id
            or passenger.mounted_on != carrier.operator_id
        ):
            raise RuntimeError("BOARDING_TRANSITION_RELATION_MISMATCH")
        carrier.movement_locked = False
        if transition.transition_type is BoardingTransitionType.BOARD:
            event_type = EventType.BOARD_COMPLETED
        else:
            carrier.passenger_id = None
            passenger.mounted_on = None
            if passenger.alive:
                passenger.deployed = True
                passenger.current_hex = carrier.current_hex
                passenger.x = carrier.x
                passenger.y = carrier.y
                passenger.state = "ACTIVE"
            else:
                passenger.deployed = False
                passenger.current_hex = None
                passenger.state = "destroyed"
            passenger.stopped = True
            passenger.speed = 0.0
            event_type = EventType.UNLOAD_COMPLETED
        event_data: dict[str, object] = {
            "transition_id": transition.transition_id,
            "carrier_id": carrier.operator_id,
            "passenger_id": passenger.operator_id,
            "profile_id": transition.profile_id,
            "reason_code": "COMPLETED",
            "transition_start_tick": transition.transition_start_tick,
            "completed_tick": transition.completed_tick,
        }
        if transition.transition_type is BoardingTransitionType.UNLOAD:
            event_data["hex"] = passenger.current_hex
        yield SourceBoardingEvent(
            event_type=event_type,
            side=carrier.side,
            actor_id=transition.actor_id,
            target_id=carrier.operator_id,
            action_sequence=world.next_action_sequence,
            data=event_data,
        )


__all__ = [
    "SourceBoardingEvent",
    "sync_mounted_passenger_positions",
    "SourceBoardingIntent",
    "advance_source_boarding_transitions",
    "apply_source_boarding_intents",
]
