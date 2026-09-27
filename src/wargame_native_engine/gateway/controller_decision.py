"""Shared construction boundary for one frozen controller decision snapshot.

The builder consumes the already-created side observation, a frozen sequence
of Engine valid actions, and detached Engine capability/lifecycle evidence.  It
does not run visibility, mutate the Engine, or expose WorldState to consumers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from wargame_native_engine.core.action import ValidAction
from wargame_native_engine.core.boarding import (
    BoardingCapabilityStatus,
    BoardingTransitionType,
)
from wargame_native_engine.core.observation import SideObservation
from wargame_native_engine.projection.decision_capability import (
    ActionCatalogSnapshot,
    boarding_capability_assessment_evidence,
    build_action_catalog_snapshot,
    build_decision_capability_snapshot,
    build_decision_from_snapshots,
    concealment_capability_evidence,
    guided_capability_evidence,
    indirect_capability_evidence,
)
from wargame_native_engine.projection.decision_semantic import (
    DecisionMissionContext,
    DecisionObjective,
    DecisionProjectionContractError,
    DecisionSide,
    DecisionSnapshotIdentity,
    DecisionTerminalRules,
    SideDecisionContext,
    SideDecisionSemanticProjection,
    current_decision_authority_metadata,
)
from wargame_native_engine.projection.operator_perspective import (
    ObservedEnemyOperatorLifecyclePayload,
    OperatorLifecyclePerspectiveKind,
    OwnOperatorLifecyclePayload,
    project_side_operator_lifecycles,
)
from wargame_native_engine.session.game_session import GameSession


@dataclass(frozen=True, slots=True)
class ControllerDecisionCore:
    """The two immutable machine contracts attached to ControllerView."""

    decision_semantic: SideDecisionSemanticProjection
    action_catalog: ActionCatalogSnapshot


def build_controller_decision_core(
    *,
    session: GameSession,
    observation: SideObservation,
    snapshot_identity: DecisionSnapshotIdentity,
    frozen_valid_actions: Sequence[ValidAction],
) -> ControllerDecisionCore:
    """Build one side/window/frame/time-bound semantic core, fail closed."""

    if not isinstance(session, GameSession):
        raise TypeError("session must be GameSession")
    if not isinstance(observation, SideObservation):
        raise TypeError("observation must be SideObservation")
    if type(snapshot_identity) is not DecisionSnapshotIdentity:
        raise TypeError("snapshot_identity must be DecisionSnapshotIdentity")
    if observation.side != snapshot_identity.side.value:
        raise DecisionProjectionContractError("observation/decision side mismatch")
    # SideObservation.tick is the Engine tick, not the Source fact-frame
    # identity.  The latter is bound by SessionManager and must not be inferred
    # from this legacy field.
    if observation.stage is not snapshot_identity.stage:
        raise DecisionProjectionContractError("observation/decision stage mismatch")

    authoritative = {
        value.operator_id: value
        for value in session.authoritative_operator_lifecycle_views(
            observation.side
        )
    }
    envelopes = project_side_operator_lifecycles(observation, authoritative)
    own = tuple(
        value.payload
        for value in envelopes
        if value.perspective_kind is OperatorLifecyclePerspectiveKind.OWN
    )
    enemies = tuple(
        value.payload
        for value in envelopes
        if value.perspective_kind
        is OperatorLifecyclePerspectiveKind.OBSERVED_ENEMY
    )
    if any(type(value) is not OwnOperatorLifecyclePayload for value in own):
        raise RuntimeError("OWN lifecycle projection changed payload type")
    if any(
        type(value) is not ObservedEnemyOperatorLifecyclePayload
        for value in enemies
    ):
        raise RuntimeError("enemy lifecycle projection changed payload type")

    catalog = build_action_catalog_snapshot(
        snapshot_identity=snapshot_identity,
        frozen_valid_actions=tuple(frozen_valid_actions),
    )
    board_capabilities = session.get_board_capabilities(observation.side)
    unload_capabilities = session.get_unload_capabilities(observation.side)
    boarding_assessments = session.get_boarding_capability_assessments(
        observation.side
    )
    available_board_actors = {
        value.actor_id
        for value in boarding_assessments
        if value.operation_type is BoardingTransitionType.BOARD
        and value.status is BoardingCapabilityStatus.AVAILABLE
    }
    available_unload_actors = {
        value.actor_id
        for value in boarding_assessments
        if value.operation_type is BoardingTransitionType.UNLOAD
        and value.status is BoardingCapabilityStatus.AVAILABLE
    }
    if available_board_actors != {
        value.passenger_id for value in board_capabilities
    }:
        raise RuntimeError("BOARD capability assessment mismatch")
    if available_unload_actors != {
        value.carrier_id for value in unload_capabilities
    }:
        raise RuntimeError("UNLOAD capability assessment mismatch")

    explicit = (
        *guided_capability_evidence(
            snapshot_identity,
            session.get_guided_fire_capabilities(observation.side),
        ),
        *indirect_capability_evidence(
            snapshot_identity,
            session.get_indirect_fire_capabilities(observation.side),
        ),
        *boarding_capability_assessment_evidence(
            snapshot_identity,
            boarding_assessments,
        ),
        *concealment_capability_evidence(
            snapshot_identity,
            session.get_concealment_capabilities(observation.side),
        ),
    )
    own_references = tuple(value.operator_reference for value in own)
    capabilities = build_decision_capability_snapshot(
        snapshot_identity=snapshot_identity,
        own_operator_references=own_references,
        action_catalog=catalog,
        explicit_capability_records=explicit,
    )
    ruleset = session.backend.ruleset
    decision = build_decision_from_snapshots(
        authority_metadata=current_decision_authority_metadata(
            ruleset_id=ruleset.ruleset_id,
            ruleset_version=ruleset.ruleset_version,
            ruleset_hash=ruleset.content_hash,
        ),
        snapshot=snapshot_identity,
        side_context=_side_context(observation, own, enemies),
        own_operators=own,
        observed_enemies=enemies,
        capability_snapshot=capabilities,
        action_catalog_snapshot=catalog,
    )
    return ControllerDecisionCore(decision, catalog)


def _side_context(
    observation: SideObservation,
    own: tuple[OwnOperatorLifecyclePayload, ...],
    enemies: tuple[ObservedEnemyOperatorLifecyclePayload, ...],
) -> SideDecisionContext:
    terminal = observation.mission.get("terminal_rules")
    if not isinstance(terminal, Mapping):
        raise DecisionProjectionContractError(
            "side observation mission terminal_rules are missing"
        )
    elimination = terminal.get("elimination")
    if type(elimination) is not bool:
        raise DecisionProjectionContractError(
            "mission elimination must be an exact bool"
        )
    return SideDecisionContext(
        side=DecisionSide(observation.side),
        objectives=tuple(_objective(value) for value in observation.objectives),
        mission=DecisionMissionContext(DecisionTerminalRules(
            elimination=elimination,
            objective_score_to_win=_optional_nonnegative_int(
                terminal.get("objective_score_to_win"),
                "objective_score_to_win",
            ),
            max_ticks=_optional_nonnegative_int(
                terminal.get("max_ticks"), "max_ticks"
            ),
        )),
        controllable_operator_references=tuple(
            value.operator_reference for value in own
        ),
        observed_contact_references=tuple(
            value.operator_reference for value in enemies
        ),
    )


def _objective(value: object) -> DecisionObjective:
    if not isinstance(value, Mapping):
        raise DecisionProjectionContractError("objective evidence must be an object")
    try:
        controlling = value.get("controlling_side")
        return DecisionObjective(
            objective_id=value["objective_id"],
            x=value["x"],
            y=value["y"],
            radius=value["radius"],
            value=value["value"],
            controlling_side=(None if controlling is None else str(controlling)),
        )
    except KeyError as exc:
        raise DecisionProjectionContractError(
            f"objective evidence is missing {exc.args[0]}"
        ) from exc


def _optional_nonnegative_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise DecisionProjectionContractError(
            f"{field} must be a non-negative exact int or None"
        )
    return value


__all__ = ["ControllerDecisionCore", "build_controller_decision_core"]
