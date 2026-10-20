"""Objective capture and scoring mutations for the Native runtime.

Eligibility remains owned by :mod:`rules.objective_capture`.  These helpers
operate on the working ``WorldState`` supplied by ``NativeEngine`` and return
event descriptions so the engine retains authoritative event sequencing.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from wargame_native_engine.core.action_router import RoutedAction
from wargame_native_engine.core.world import WorldState
from wargame_native_engine.rules.objective_capture import (
    ObjectiveCapturePolicy,
    ObjectiveCaptureService,
)
from wargame_native_engine.systems.movement import distance


@dataclass(frozen=True, slots=True)
class SourceCaptureIntent:
    routed: RoutedAction
    objective_id: int


@dataclass(frozen=True, slots=True)
class SourceCaptureEvent:
    side: str
    actor_id: int | None
    objective_id: int
    action_sequence: int
    data: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ObjectiveControlEvent:
    controlling_side: str | None
    objective_id: int


def stage_source_capture(
    capture_service: ObjectiveCaptureService | None,
    routed: RoutedAction,
    pre_state: WorldState,
) -> tuple[str | None, SourceCaptureIntent | None]:
    """Validate one explicit capture against the joint tick pre-state."""

    action = routed.action
    if capture_service is None:
        return "CAPTURE_POLICY_UNAVAILABLE", None
    if action.target_position is not None or action.parameters:
        return "INVALID_CAPTURE_REQUEST", None
    assessment = capture_service.assess(
        pre_state,
        side=action.side,
        actor_id=action.actor_id,
        objective_id=action.target_id,
    )
    if not assessment.allowed:
        return assessment.reason, None
    assert action.target_id is not None
    return None, SourceCaptureIntent(routed, action.target_id)


def apply_source_capture_intents(
    world: WorldState,
    intents: Sequence[SourceCaptureIntent],
    capture_policy: ObjectiveCapturePolicy,
) -> Iterator[SourceCaptureEvent]:
    """Apply accepted captures to the current working state in stable order."""

    for intent in sorted(
        intents,
        key=lambda item: (
            item.objective_id,
            item.routed.action.side,
            item.routed.action_sequence,
        ),
    ):
        action = intent.routed.action
        objective = world.objectives[intent.objective_id]
        if objective.controlling_side == action.side:
            continue
        before = objective.controlling_side
        objective.controlling_side = action.side
        yield SourceCaptureEvent(
            side=action.side,
            actor_id=action.actor_id,
            objective_id=objective.objective_id,
            action_sequence=intent.routed.action_sequence,
            data={
                "action_id": action.action_id,
                "controlling_side_before": before,
                "controlling_side": action.side,
                "capture_policy": capture_policy.identity_record(),
            },
        )


def score_objectives(
    world: WorldState,
    *,
    float_tolerance: float,
) -> Iterator[ObjectiveControlEvent]:
    """Apply legacy automatic control and periodic objective scoring."""

    for objective in sorted(
        world.objectives.values(), key=lambda item: item.objective_id
    ):
        occupying = {
            operator.side
            for operator in world.operators.values()
            if operator.alive
            and operator.deployed
            and distance(operator.position, (objective.x, objective.y))
            <= objective.radius + float_tolerance
        }
        controller = next(iter(occupying)) if len(occupying) == 1 else None
        if controller != objective.controlling_side:
            objective.controlling_side = controller
            yield ObjectiveControlEvent(
                controlling_side=controller,
                objective_id=objective.objective_id,
            )
        if controller is not None:
            world.sides[controller].objective_score += objective.value


def award_controlled_objective_scores(world: WorldState) -> None:
    """Apply explicit-policy periodic scoring without changing ownership."""

    for objective in sorted(
        world.objectives.values(), key=lambda item: item.objective_id
    ):
        controller = objective.controlling_side
        if controller is not None and controller in world.sides:
            world.sides[controller].objective_score += objective.value


__all__ = [
    "ObjectiveControlEvent",
    "SourceCaptureEvent",
    "SourceCaptureIntent",
    "apply_source_capture_intents",
    "award_controlled_objective_scores",
    "score_objectives",
    "stage_source_capture",
]
