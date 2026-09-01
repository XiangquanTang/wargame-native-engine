from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import FrozenInstanceError, dataclass, fields, replace
from pathlib import Path
from typing import Any

import pytest

from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.io.replay import ReplayWriter, replay_actions
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.projection.decision_semantic import (
    DECISION_ACTION_CATALOG_SCHEMA_VERSION,
    DECISION_CAPABILITY_SNAPSHOT_SCHEMA_VERSION,
    ActionCatalogReference,
    CapabilityAvailabilityStatus,
    CapabilityBlockerCode,
    CapabilityEvidenceKind,
    CapabilityFamilyStatus,
    DecisionCapabilityFamily,
    DecisionCapabilitySnapshot,
    DecisionMissionContext,
    DecisionObjective,
    DecisionProjectionContractError,
    DecisionSide,
    DecisionSnapshotIdentity,
    DecisionTerminalRules,
    OperatorCapabilitySummary,
    SideDecisionContext,
    action_catalog_reference_hash,
    build_decision_semantic_projection,
    current_decision_authority_metadata,
)
from wargame_native_engine.projection.operator_lifecycle import (
    Applicability,
    RecoveryView,
    SuppressionView,
    project_operator_lifecycle,
)
from wargame_native_engine.projection.operator_perspective import (
    ObservedEnemyOperatorLifecyclePayload,
    OwnOperatorLifecyclePayload,
    project_observed_enemy_operator_lifecycle,
    project_own_operator_lifecycle,
)


ROOT = Path(__file__).resolve().parents[1]
SCENARIO_PATH = ROOT / "examples/source_movement_scenario.json"
MODULE_PATH = (
    ROOT / "src/wargame_native_engine/projection/decision_semantic.py"
)


def _engine_with_contact() -> NativeEngine:
    engine = NativeEngine(load_scenario(SCENARIO_PATH))
    engine.reset(42)
    result = engine.step(
        [
            EngineAction(
                "red",
                1,
                ActionType.MOVE,
                parameters={
                    "path": [102, 103, 104],
                    "movement_mode": "NORMAL",
                },
            )
        ],
        delta_time_ms=60_000,
    )
    assert result.decisions[0].accepted is True
    assert engine.observe("red").visible_enemies
    return engine


def _available(
    family: DecisionCapabilityFamily = DecisionCapabilityFamily.WAIT,
) -> CapabilityFamilyStatus:
    return CapabilityFamilyStatus(
        family=family,
        status=CapabilityAvailabilityStatus.AVAILABLE,
        producer_namespace="engine.valid_actions",
        evidence_kind=CapabilityEvidenceKind.ENGINE_VALID_ACTION_INSTANCE,
        available_instance_count=1,
    )


def _unknown(
    family: DecisionCapabilityFamily,
) -> CapabilityFamilyStatus:
    return CapabilityFamilyStatus(
        family=family,
        status=CapabilityAvailabilityStatus.UNKNOWN,
        producer_namespace="decision.phase1.synthetic_evidence",
        evidence_kind=CapabilityEvidenceKind.NO_CONCLUSIVE_ENGINE_EVIDENCE,
    )


def _blocked(
    family: DecisionCapabilityFamily,
    code: str,
) -> CapabilityFamilyStatus:
    return CapabilityFamilyStatus(
        family=family,
        status=CapabilityAvailabilityStatus.BLOCKED,
        producer_namespace="engine.capability",
        evidence_kind=CapabilityEvidenceKind.ENGINE_CAPABILITY_RECORD,
        blocker_codes=(
            CapabilityBlockerCode("engine.capability", code),
        ),
    )


def _objective(value: Any) -> DecisionObjective:
    return DecisionObjective(
        objective_id=int(value["objective_id"]),
        x=float(value["x"]),
        y=float(value["y"]),
        radius=float(value["radius"]),
        value=int(value["value"]),
        controlling_side=(
            None
            if value.get("controlling_side") is None
            else str(value["controlling_side"])
        ),
    )


def _safe_bundle(
    engine: NativeEngine,
    *,
    window: int = 7,
    frame: int = 11,
    own_families: tuple[CapabilityFamilyStatus, ...] | None = None,
) -> dict[str, object]:
    world = engine._require_world()  # noqa: SLF001
    observation = engine.observe("red")
    own = tuple(
        project_own_operator_lifecycle(
            project_operator_lifecycle(world, item.operator_id)
        ).payload
        for item in observation.friendly_operators
    )
    assert all(isinstance(item, OwnOperatorLifecyclePayload) for item in own)
    enemies = tuple(
        project_observed_enemy_operator_lifecycle(item).payload
        for item in observation.visible_enemies
    )
    assert all(
        isinstance(item, ObservedEnemyOperatorLifecyclePayload)
        for item in enemies
    )
    terminal = observation.mission["terminal_rules"]
    snapshot = DecisionSnapshotIdentity(
        side=DecisionSide.RED,
        decision_window_index=window,
        frame_index=frame,
        sim_time_ms=world.elapsed_time_ms,
        stage=observation.stage,
        terminated=False,
        truncated=False,
    )
    context = SideDecisionContext(
        side=DecisionSide.RED,
        objectives=tuple(_objective(item) for item in observation.objectives),
        mission=DecisionMissionContext(
            DecisionTerminalRules(
                elimination=bool(terminal["elimination"]),
                objective_score_to_win=terminal["objective_score_to_win"],
                max_ticks=terminal["max_ticks"],
            )
        ),
        controllable_operator_references=tuple(
            item.operator_reference for item in own
        ),
        observed_contact_references=tuple(
            item.operator_reference for item in enemies
        ),
    )
    families = (_available(),) if own_families is None else own_families
    capabilities = DecisionCapabilitySnapshot(
        schema_version=DECISION_CAPABILITY_SNAPSHOT_SCHEMA_VERSION,
        side=DecisionSide.RED,
        decision_window_index=window,
        frame_index=frame,
        sim_time_ms=world.elapsed_time_ms,
        operators=tuple(
            OperatorCapabilitySummary(item.operator_reference, families)
            for item in own
        ),
    )
    valid_actions = engine.valid_actions("red")
    action_ids = tuple(item.action_id for item in valid_actions)
    action_entry_hashes = tuple(
        canonical_hash(item.to_dict()) for item in valid_actions
    )
    actor_references = tuple(sorted({
        item.action.actor_id
        for item in valid_actions
        if item.action.actor_id is not None
    }))
    catalog_hash = action_catalog_reference_hash(
        schema_version=DECISION_ACTION_CATALOG_SCHEMA_VERSION,
        side=DecisionSide.RED,
        decision_window_index=window,
        frame_index=frame,
        sim_time_ms=world.elapsed_time_ms,
        action_ids=action_ids,
        action_entry_hashes=action_entry_hashes,
        actor_references=actor_references,
    )
    catalog = ActionCatalogReference(
        schema_version=DECISION_ACTION_CATALOG_SCHEMA_VERSION,
        side=DecisionSide.RED,
        decision_window_index=window,
        frame_index=frame,
        sim_time_ms=world.elapsed_time_ms,
        action_count=len(action_ids),
        action_ids=action_ids,
        action_entry_hashes=action_entry_hashes,
        actor_references=actor_references,
        catalog_hash=catalog_hash,
    )
    metadata = current_decision_authority_metadata(
        ruleset_id=engine.ruleset.ruleset_id,
        ruleset_version=engine.ruleset.ruleset_version,
        ruleset_hash=engine.ruleset.content_hash,
    )
    return {
        "authority_metadata": metadata,
        "snapshot": snapshot,
        "side_context": context,
        "own_operators": own,
        "observed_enemies": enemies,
        "capability_snapshot": capabilities,
        "action_catalog_reference": catalog,
    }


def _build(bundle: dict[str, object]):
    return build_decision_semantic_projection(**bundle)  # type: ignore[arg-type]


def _replace_capability(
    bundle: dict[str, object],
    families: tuple[CapabilityFamilyStatus, ...],
) -> dict[str, object]:
    capabilities = bundle["capability_snapshot"]
    assert isinstance(capabilities, DecisionCapabilitySnapshot)
    return {
        **bundle,
        "capability_snapshot": replace(
            capabilities,
            operators=tuple(
                replace(item, families=families)
                for item in capabilities.operators
            ),
        ),
    }


def test_d1_same_safe_inputs_are_equal_hash_stable_and_immutable() -> None:
    engine = _engine_with_contact()
    bundle = _safe_bundle(engine)

    first = _build(bundle)
    second = _build(bundle)

    assert first == second
    assert first.semantic_hash == second.semantic_hash
    assert canonical_hash(first.to_dict()) == canonical_hash(second.to_dict())
    with pytest.raises(FrozenInstanceError):
        first.semantic_hash = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "hidden_difference",
    [
        {"enemy_health": 1},
        {"enemy_cooldown_ms": 50_000, "enemy_suppression_ms": 10_000},
        {"enemy_lifecycle_remaining_ms": 700_000},
        {"enemy_boarding_relation": 123},
    ],
)
def test_d2_d5_hidden_world_differences_cannot_enter_safe_input_builder(
    hidden_difference: dict[str, int],
) -> None:
    engine = _engine_with_contact()
    bundle = _safe_bundle(engine)
    baseline = _build(bundle)

    # The hidden evidence is deliberately not an input to this API.
    isolated_copy = deepcopy(hidden_difference)
    actual = _build(bundle)

    assert isolated_copy == hidden_difference
    assert actual == baseline
    assert actual.semantic_hash == baseline.semantic_hash


def test_d6_own_suppression_expiry_legitimately_changes_projection() -> None:
    engine = _engine_with_contact()
    bundle = _safe_bundle(engine)
    own = bundle["own_operators"]
    assert isinstance(own, tuple)
    active = replace(
        own[0],
        suppression=SuppressionView(active=True, remaining_ms=1),
    )
    clear = replace(
        own[0],
        suppression=SuppressionView(active=False, remaining_ms=0),
    )

    active_projection = _build({**bundle, "own_operators": (active,)})
    clear_projection = _build({**bundle, "own_operators": (clear,)})

    assert active_projection != clear_projection
    assert active_projection.semantic_hash != clear_projection.semantic_hash


def test_d7_guided_recovery_and_supplied_capability_evidence_change() -> None:
    engine = _engine_with_contact()
    bundle = _safe_bundle(engine)
    own = bundle["own_operators"]
    assert isinstance(own, tuple)
    recovering = replace(
        own[0],
        guided_recovery=RecoveryView(
            applicability=Applicability.APPLICABLE,
            active=True,
            remaining_ms=1,
            ready=False,
        ),
    )
    ready = replace(
        own[0],
        guided_recovery=RecoveryView(
            applicability=Applicability.APPLICABLE,
            active=False,
            remaining_ms=0,
            ready=True,
        ),
    )
    blocked_bundle = _replace_capability(
        {**bundle, "own_operators": (recovering,)},
        (_blocked(DecisionCapabilityFamily.GUIDED_FIRE, "RECOVERY_ACTIVE"),),
    )
    ready_bundle = _replace_capability(
        {**bundle, "own_operators": (ready,)},
        (_available(DecisionCapabilityFamily.GUIDED_FIRE),),
    )

    assert _build(blocked_bundle).semantic_hash != _build(
        ready_bundle
    ).semantic_hash


def test_d8_indirect_recovery_and_supplied_capability_evidence_change() -> None:
    engine = _engine_with_contact()
    bundle = _safe_bundle(engine)
    own = bundle["own_operators"]
    assert isinstance(own, tuple)
    cooling = replace(
        own[0],
        indirect_planning=RecoveryView(
            applicability=Applicability.APPLICABLE,
            active=True,
            remaining_ms=1,
            ready=False,
        ),
    )
    ready = replace(
        own[0],
        indirect_planning=RecoveryView(
            applicability=Applicability.APPLICABLE,
            active=False,
            remaining_ms=0,
            ready=True,
        ),
    )
    blocked_bundle = _replace_capability(
        {**bundle, "own_operators": (cooling,)},
        (_blocked(DecisionCapabilityFamily.INDIRECT_FIRE, "COOLDOWN_ACTIVE"),),
    )
    ready_bundle = _replace_capability(
        {**bundle, "own_operators": (ready,)},
        (_available(DecisionCapabilityFamily.INDIRECT_FIRE),),
    )

    assert _build(blocked_bundle).semantic_hash != _build(
        ready_bundle
    ).semantic_hash


def test_d9_contact_collection_is_visible_decision_state() -> None:
    engine = _engine_with_contact()
    bundle = _safe_bundle(engine)
    context = bundle["side_context"]
    assert isinstance(context, SideDecisionContext)
    present = _build(bundle)
    absent = _build({
        **bundle,
        "side_context": replace(context, observed_contact_references=()),
        "observed_enemies": (),
    })

    assert present.observed_enemies
    assert absent.observed_enemies == ()
    assert present.semantic_hash != absent.semantic_hash


def test_d13_snapshot_restore_rebuilds_equal_projection() -> None:
    engine = _engine_with_contact()
    expected = _build(_safe_bundle(engine))
    snapshot = deepcopy(engine.snapshot())
    restored = NativeEngine(load_scenario(SCENARIO_PATH))
    restored.reset(7)
    restored.restore(snapshot)

    actual = _build(_safe_bundle(restored))

    assert actual == expected
    assert actual.semantic_hash == expected.semantic_hash


def test_d14_strict_replay_rebuilds_equal_projection(tmp_path: Path) -> None:
    engine = NativeEngine(load_scenario(SCENARIO_PATH))
    engine.reset(42)
    action = EngineAction(
        "red",
        1,
        ActionType.MOVE,
        parameters={"path": [102, 103, 104], "movement_mode": "NORMAL"},
    )
    writer = ReplayWriter(
        tmp_path / "decision-replay",
        run_id="decision-replay",
        scenario=engine.scenario,
        seed=42,
        agents={"red": "test", "blue": "test"},
        max_ticks=engine.terminal_rules.max_ticks,
        ruleset=engine.ruleset,
        project_commit=None,
    )
    writer.record_initial_state(engine.snapshot())
    result = engine.step([action], delta_time_ms=60_000)
    writer.record_step(result, engine.snapshot())
    writer.finalize({"result": engine.result().to_dict()}, status="complete")
    expected = _build(_safe_bundle(engine))

    verification = replay_actions(engine.scenario, writer.root, strict=True)
    replayed = NativeEngine(load_scenario(SCENARIO_PATH))
    replayed.reset(42)
    replayed.step([action], delta_time_ms=60_000)
    actual = _build(_safe_bundle(replayed))

    assert verification.matches is True
    assert actual == expected
    assert actual.semantic_hash == expected.semantic_hash


def test_d15_all_set_like_input_collections_have_stable_order() -> None:
    engine = _engine_with_contact()
    bundle = _safe_bundle(
        engine,
        own_families=(
            _unknown(DecisionCapabilityFamily.MOVE),
            _available(DecisionCapabilityFamily.WAIT),
        ),
    )
    context = bundle["side_context"]
    capabilities = bundle["capability_snapshot"]
    own = bundle["own_operators"]
    enemies = bundle["observed_enemies"]
    assert isinstance(context, SideDecisionContext)
    assert isinstance(capabilities, DecisionCapabilitySnapshot)
    assert isinstance(own, tuple)
    assert isinstance(enemies, tuple)
    reversed_capabilities = replace(
        capabilities,
        operators=tuple(
            replace(item, families=tuple(reversed(item.families)))
            for item in reversed(capabilities.operators)
        ),
    )
    reversed_bundle = {
        **bundle,
        "side_context": replace(
            context,
            objectives=tuple(reversed(context.objectives)),
            controllable_operator_references=tuple(
                reversed(context.controllable_operator_references)
            ),
            observed_contact_references=tuple(
                reversed(context.observed_contact_references)
            ),
        ),
        "own_operators": tuple(reversed(own)),
        "observed_enemies": tuple(reversed(enemies)),
        "capability_snapshot": reversed_capabilities,
    }

    assert _build(reversed_bundle) == _build(bundle)


def test_d16_builder_is_pure_and_has_no_high_level_runtime_dependencies() -> None:
    engine = _engine_with_contact()
    before = canonical_hash(engine.snapshot())
    bundle = _safe_bundle(engine)
    bundle_before = deepcopy(bundle)

    _build(bundle)

    assert canonical_hash(engine.snapshot()) == before
    assert bundle == bundle_before
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    forbidden = {
        "wargame_native_engine.core.engine",
        "wargame_native_engine.core.operator",
        "wargame_native_engine.gateway",
        "wargame_native_engine.training",
        "wargame_native_engine.viewer",
    }
    assert imported.isdisjoint(forbidden)


def test_d17_absence_without_authority_is_unknown_not_blocked() -> None:
    unknown = _unknown(DecisionCapabilityFamily.GUIDED_FIRE)
    assert unknown.status is CapabilityAvailabilityStatus.UNKNOWN
    with pytest.raises(
        DecisionProjectionContractError,
        match="BLOCKED requires explicit Engine blocker evidence",
    ):
        CapabilityFamilyStatus(
            family=DecisionCapabilityFamily.GUIDED_FIRE,
            status=CapabilityAvailabilityStatus.BLOCKED,
            producer_namespace="decision.phase1.synthetic_evidence",
            evidence_kind=CapabilityEvidenceKind.NO_CONCLUSIVE_ENGINE_EVIDENCE,
        )


def test_d18_machine_capability_contract_has_no_presentation_text() -> None:
    names = {item.name for item in fields(CapabilityFamilyStatus)}
    assert "reason" not in names
    assert "reason_text" not in names
    assert "message" not in names


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        result = set(value)
        for item in value.values():
            result.update(_all_keys(item))
        return result
    if isinstance(value, list):
        result: set[str] = set()
        for item in value:
            result.update(_all_keys(item))
        return result
    return set()


def test_d19_authoritative_hashes_are_not_in_decision_schema() -> None:
    projection = _build(_safe_bundle(_engine_with_contact()))
    keys = _all_keys(projection.to_dict())
    assert {
        "world_hash",
        "authoritative_semantic_hash",
        "green_semantic_hash",
        "enemy_hidden_hash",
    }.isdisjoint(keys)


def test_d20_training_supervision_is_structurally_excluded() -> None:
    projection = _build(_safe_bundle(_engine_with_contact()))
    keys = _all_keys(projection.to_dict())
    assert {
        "reward",
        "winner",
        "outcome",
        "advantage",
        "eight_factor",
        "value_target",
        "episode_return",
    }.isdisjoint(keys)


def _rehash_catalog(value: ActionCatalogReference, **changes: object):
    values = {
        item.name: changes.get(item.name, getattr(value, item.name))
        for item in fields(ActionCatalogReference)
        if item.name != "catalog_hash"
    }
    values["catalog_hash"] = action_catalog_reference_hash(
        schema_version=values["schema_version"],
        side=values["side"],
        decision_window_index=values["decision_window_index"],
        frame_index=values["frame_index"],
        sim_time_ms=values["sim_time_ms"],
        action_ids=values["action_ids"],
        action_entry_hashes=values["action_entry_hashes"],
        actor_references=values["actor_references"],
    )
    return ActionCatalogReference(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", ["window", "frame", "operator", "side"])
def test_d21_window_frame_reference_and_side_mismatch_fail_closed(
    kind: str,
) -> None:
    bundle = _safe_bundle(_engine_with_contact())
    capabilities = bundle["capability_snapshot"]
    catalog = bundle["action_catalog_reference"]
    context = bundle["side_context"]
    assert isinstance(capabilities, DecisionCapabilitySnapshot)
    assert isinstance(catalog, ActionCatalogReference)
    assert isinstance(context, SideDecisionContext)
    if kind == "window":
        bundle["capability_snapshot"] = replace(
            capabilities,
            decision_window_index=capabilities.decision_window_index + 1,
        )
    elif kind == "frame":
        bundle["action_catalog_reference"] = _rehash_catalog(
            catalog,
            frame_index=catalog.frame_index + 1,
        )
    elif kind == "operator":
        bundle["side_context"] = replace(
            context,
            controllable_operator_references=(999,),
        )
    else:
        bundle["capability_snapshot"] = replace(
            capabilities,
            side=DecisionSide.BLUE,
        )

    with pytest.raises(DecisionProjectionContractError, match="mismatch"):
        _build(bundle)


def test_d22_future_payload_subclass_cannot_auto_leak_new_field() -> None:
    bundle = _safe_bundle(_engine_with_contact())
    own = bundle["own_operators"]
    assert isinstance(own, tuple)

    @dataclass(frozen=True, slots=True)
    class FutureOwnPayload(OwnOperatorLifecyclePayload):
        future_green_secret: str = "hidden"

    future = FutureOwnPayload(
        **{
            item.name: getattr(own[0], item.name)
            for item in fields(OwnOperatorLifecyclePayload)
        }
    )
    with pytest.raises(TypeError, match="exact OWN lifecycle payloads"):
        _build({**bundle, "own_operators": (future,)})


def test_action_catalog_reference_binds_window_local_indices_to_stable_ids() -> None:
    bundle = _safe_bundle(_engine_with_contact())
    catalog = bundle["action_catalog_reference"]
    assert isinstance(catalog, ActionCatalogReference)
    assert catalog.action_count == len(catalog.action_ids)
    assert catalog.catalog_hash == action_catalog_reference_hash(
        schema_version=catalog.schema_version,
        side=catalog.side,
        decision_window_index=catalog.decision_window_index,
        frame_index=catalog.frame_index,
        sim_time_ms=catalog.sim_time_ms,
        action_ids=catalog.action_ids,
        action_entry_hashes=catalog.action_entry_hashes,
        actor_references=catalog.actor_references,
    )
    with pytest.raises(DecisionProjectionContractError, match="hash mismatch"):
        replace(catalog, action_ids=("future-action-id",))


def test_public_decision_types_are_frozen_slotted_value_objects() -> None:
    projection = _build(_safe_bundle(_engine_with_contact()))
    assert not hasattr(projection, "__dict__")
    assert not hasattr(projection, "world")
    assert not hasattr(projection, "engine")
    assert not hasattr(projection, "observation")
    assert not hasattr(projection, "rng")
