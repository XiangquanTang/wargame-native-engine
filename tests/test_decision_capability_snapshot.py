from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import FrozenInstanceError, dataclass, fields, replace
from pathlib import Path

import pytest

from tests.test_authoritative_timer_repair import (
    _create_indirect_mission,
    _guided_action,
    _guided_engine,
    _indirect_engine,
)
from tests.test_concealment_action import _movement_engine
from tests.test_decision_semantic_projection import (
    SCENARIO_PATH,
    _engine_with_contact,
    _safe_bundle,
)
from tests.test_source_movement import _scenario_document
from wargame_native_engine.core.action import (
    ACTION_PRIORITY,
    ActionType,
    EngineAction,
    ValidAction,
)
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.gateway.decision_window import DecisionWindow
from wargame_native_engine.gateway.models import CommandGatewayError
from wargame_native_engine.io.replay import ReplayWriter, replay_actions
from wargame_native_engine.io.scenario import load_scenario, scenario_from_dict
from wargame_native_engine.projection.decision_capability import (
    ActionCatalogEntry,
    ActionCatalogSnapshot,
    ExplicitCapabilityEvidence,
    board_capability_evidence,
    build_action_catalog_snapshot,
    build_decision_capability_snapshot,
    build_decision_from_snapshots,
    capability_family_for_action,
    concealment_capability_evidence,
    guided_capability_evidence,
    indirect_capability_evidence,
    unload_capability_evidence,
)
from wargame_native_engine.projection.decision_semantic import (
    CapabilityAvailabilityStatus,
    DecisionCapabilityFamily,
    DecisionCapabilitySnapshot,
    DecisionProjectionContractError,
    DecisionSide,
    DecisionSnapshotIdentity,
    SideDecisionContext,
)
from wargame_native_engine.training.action_encoder import NativeActionEncoder


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT / "src/wargame_native_engine/projection/decision_capability.py"
)


def _adapter_outputs(
    engine: NativeEngine,
    *,
    window: int = 7,
    frame: int = 11,
    explicit: tuple[ExplicitCapabilityEvidence, ...] = (),
):
    safe = _safe_bundle(engine, window=window, frame=frame)
    snapshot = safe["snapshot"]
    context = safe["side_context"]
    assert isinstance(snapshot, DecisionSnapshotIdentity)
    assert isinstance(context, SideDecisionContext)
    frozen_actions = engine.valid_actions(snapshot.side.value)
    catalog = build_action_catalog_snapshot(
        snapshot_identity=snapshot,
        frozen_valid_actions=frozen_actions,
    )
    capability = build_decision_capability_snapshot(
        snapshot_identity=snapshot,
        own_operator_references=context.controllable_operator_references,
        action_catalog=catalog,
        explicit_capability_records=explicit,
    )
    decision = build_decision_from_snapshots(
        authority_metadata=safe["authority_metadata"],  # type: ignore[arg-type]
        snapshot=snapshot,
        side_context=context,
        own_operators=safe["own_operators"],  # type: ignore[arg-type]
        observed_enemies=safe["observed_enemies"],  # type: ignore[arg-type]
        capability_snapshot=capability,
        action_catalog_snapshot=catalog,
    )
    return safe, frozen_actions, catalog, capability, decision


def _family_status(
    snapshot: DecisionCapabilitySnapshot,
    actor: int,
    family: DecisionCapabilityFamily,
):
    operator = next(
        item for item in snapshot.operators
        if item.operator_reference == actor
    )
    return next(item for item in operator.families if item.family is family)


def _explicit(
    snapshot: DecisionSnapshotIdentity,
    actor: int,
    family: DecisionCapabilityFamily,
    status: CapabilityAvailabilityStatus,
    *,
    producer: str = "engine.test_capability",
    blockers: tuple[str, ...] = (),
) -> ExplicitCapabilityEvidence:
    return ExplicitCapabilityEvidence(
        side=snapshot.side,
        decision_window_index=snapshot.decision_window_index,
        frame_index=snapshot.frame_index,
        sim_time_ms=snapshot.sim_time_ms,
        operator_reference=actor,
        family=family,
        status=status,
        producer_namespace=producer,
        blocker_codes=blockers,
    )


def test_c1_c2_c4_catalog_freezes_engine_order_and_hash_deterministically() -> None:
    engine = _engine_with_contact()
    safe = _safe_bundle(engine)
    snapshot = safe["snapshot"]
    assert isinstance(snapshot, DecisionSnapshotIdentity)
    actions = engine.valid_actions("red")

    first = build_action_catalog_snapshot(
        snapshot_identity=snapshot,
        frozen_valid_actions=actions,
    )
    second = build_action_catalog_snapshot(
        snapshot_identity=snapshot,
        frozen_valid_actions=tuple(actions),
    )

    assert first == second
    assert first.catalog_hash == second.catalog_hash
    assert first.action_ids == tuple(item.action_id for item in actions)
    assert [item.action_id for item in first.entries] == list(first.action_ids)
    with pytest.raises(FrozenInstanceError):
        first.catalog_hash = "changed"  # type: ignore[misc]


def test_c1_catalog_detaches_mutable_action_parameter_mapping() -> None:
    engine = _engine_with_contact()
    safe = _safe_bundle(engine)
    snapshot = safe["snapshot"]
    assert isinstance(snapshot, DecisionSnapshotIdentity)
    parameters = {"path": [102], "movement_mode": "NORMAL"}
    source = ValidAction(
        EngineAction("red", 1, ActionType.MOVE, parameters=parameters),
        ACTION_PRIORITY[ActionType.MOVE],
        "engine.test_valid_actions",
    )
    original_action_id = source.action_id
    catalog = build_action_catalog_snapshot(
        snapshot_identity=snapshot,
        frozen_valid_actions=(source,),
    )

    parameters["path"].append(103)  # type: ignore[union-attr]

    assert catalog.entries[0].parameters()["path"] == [102]
    assert catalog.entries[0].action_id == original_action_id
    assert source.action_id != original_action_id


def test_c3_duplicate_action_id_and_semantic_action_fail_closed() -> None:
    engine = _engine_with_contact()
    safe = _safe_bundle(engine)
    snapshot = safe["snapshot"]
    assert isinstance(snapshot, DecisionSnapshotIdentity)
    action = engine.valid_actions("red")[0]

    with pytest.raises(
        DecisionProjectionContractError,
        match="duplicate action_id",
    ):
        build_action_catalog_snapshot(
            snapshot_identity=snapshot,
            frozen_valid_actions=(action, action),
        )


def test_c5_catalog_identity_is_window_local() -> None:
    engine = _engine_with_contact()
    first_safe = _safe_bundle(engine, window=7, frame=11)
    second_safe = _safe_bundle(engine, window=8, frame=11)
    first_snapshot = first_safe["snapshot"]
    second_snapshot = second_safe["snapshot"]
    assert isinstance(first_snapshot, DecisionSnapshotIdentity)
    assert isinstance(second_snapshot, DecisionSnapshotIdentity)
    actions = engine.valid_actions("red")

    first = build_action_catalog_snapshot(
        snapshot_identity=first_snapshot,
        frozen_valid_actions=actions,
    )
    second = build_action_catalog_snapshot(
        snapshot_identity=second_snapshot,
        frozen_valid_actions=actions,
    )

    assert first.action_ids == second.action_ids
    assert first.catalog_hash != second.catalog_hash


def test_c6_previous_window_action_id_remains_rejected_by_gateway() -> None:
    engine = NativeEngine(load_scenario(SCENARIO_PATH))
    engine.reset(42)
    before = engine.valid_actions("red")
    stale = next(
        item for item in before
        if item.action.action_type is ActionType.MOVE
    )
    engine.step([stale.action], delta_time_ms=1_000)
    after = engine.valid_actions("red")
    assert stale.action_id not in {item.action_id for item in after}
    window = DecisionWindow.open(
        session_id="stale-catalog-test",
        decision_window_index=2,
        opened_at_sim_time_ms=1_000,
        red_valid_actions=after,
        blue_valid_actions=engine.valid_actions("blue"),
    )

    with pytest.raises(CommandGatewayError, match="not a current RED action"):
        window.submit(
            side="red",
            request_id="stale",
            ordered_action_ids=(stale.action_id,),
        )


def test_c7_native_action_encoder_order_and_mask_match_catalog() -> None:
    engine = _engine_with_contact()
    _, actions, catalog, _, _ = _adapter_outputs(engine)
    encoded = NativeActionEncoder(max_actions=16).encode(
        tuple(item.to_dict() for item in actions)
    )

    assert encoded.action_ids == catalog.action_ids
    assert encoded.mask[:catalog.action_count] == (True,) * catalog.action_count
    assert not any(encoded.mask[catalog.action_count:])


def test_c8_c9_valid_actions_make_family_available_with_exact_count() -> None:
    engine = NativeEngine(load_scenario(SCENARIO_PATH))
    engine.reset(42)
    _, actions, catalog, capabilities, _ = _adapter_outputs(engine)
    counts: dict[DecisionCapabilityFamily, int] = {}
    for entry in catalog.entries:
        if entry.actor_reference == 1:
            counts[entry.capability_family] = (
                counts.get(entry.capability_family, 0) + 1
            )

    for family, count in counts.items():
        status = _family_status(capabilities, 1, family)
        assert status.status is CapabilityAvailabilityStatus.AVAILABLE
        assert status.available_instance_count == count
    assert tuple(item.action_id for item in actions) == catalog.action_ids


def test_c10_explicit_available_can_have_zero_catalog_instances() -> None:
    engine = _indirect_engine()
    safe = _safe_bundle(engine)
    snapshot = safe["snapshot"]
    context = safe["side_context"]
    assert isinstance(snapshot, DecisionSnapshotIdentity)
    assert isinstance(context, SideDecisionContext)
    catalog = build_action_catalog_snapshot(
        snapshot_identity=snapshot,
        frozen_valid_actions=engine.valid_actions("red"),
    )
    evidence = indirect_capability_evidence(
        snapshot,
        engine.get_indirect_fire_capabilities("red"),
    )
    capabilities = build_decision_capability_snapshot(
        snapshot_identity=snapshot,
        own_operator_references=context.controllable_operator_references,
        action_catalog=catalog,
        explicit_capability_records=evidence,
    )

    status = _family_status(
        capabilities,
        1,
        DecisionCapabilityFamily.INDIRECT_FIRE,
    )
    assert status.status is CapabilityAvailabilityStatus.AVAILABLE
    assert status.available_instance_count == 0
    assert status.producer_namespace == "engine.indirect_fire_capabilities"


def test_c11_explicit_indirect_blocker_is_machine_blocked_evidence() -> None:
    engine = _indirect_engine()
    _create_indirect_mission(engine)
    safe = _safe_bundle(engine)
    snapshot = safe["snapshot"]
    context = safe["side_context"]
    assert isinstance(snapshot, DecisionSnapshotIdentity)
    assert isinstance(context, SideDecisionContext)
    catalog = build_action_catalog_snapshot(
        snapshot_identity=snapshot,
        frozen_valid_actions=engine.valid_actions("red"),
    )
    evidence = indirect_capability_evidence(
        snapshot,
        engine.get_indirect_fire_capabilities("red"),
    )
    capabilities = build_decision_capability_snapshot(
        snapshot_identity=snapshot,
        own_operator_references=context.controllable_operator_references,
        action_catalog=catalog,
        explicit_capability_records=evidence,
    )

    status = _family_status(
        capabilities,
        1,
        DecisionCapabilityFamily.INDIRECT_FIRE,
    )
    assert status.status is CapabilityAvailabilityStatus.BLOCKED
    assert status.blocker_codes[0].producer_namespace == (
        "engine.indirect_fire_capabilities"
    )
    assert status.blocker_codes[0].code == (
        "indirect_fire_planned_cooldown_remaining"
    )


def test_c12_action_absence_without_explicit_evidence_is_unknown() -> None:
    engine = _engine_with_contact()
    _, _, _, capabilities, _ = _adapter_outputs(engine)

    status = _family_status(
        capabilities,
        1,
        DecisionCapabilityFamily.GUIDED_FIRE,
    )
    assert status.status is CapabilityAvailabilityStatus.UNKNOWN
    assert status.available_instance_count is None


def test_c13_real_concealment_applicability_can_be_not_applicable() -> None:
    engine = _movement_engine(mover_domain="AIR")
    safe = _safe_bundle(engine)
    snapshot = safe["snapshot"]
    context = safe["side_context"]
    assert isinstance(snapshot, DecisionSnapshotIdentity)
    assert isinstance(context, SideDecisionContext)
    catalog = build_action_catalog_snapshot(
        snapshot_identity=snapshot,
        frozen_valid_actions=engine.valid_actions("red"),
    )
    evidence = concealment_capability_evidence(
        snapshot,
        engine.get_concealment_capabilities("red"),
    )
    capabilities = build_decision_capability_snapshot(
        snapshot_identity=snapshot,
        own_operator_references=context.controllable_operator_references,
        action_catalog=catalog,
        explicit_capability_records=evidence,
    )

    status = _family_status(
        capabilities,
        1,
        DecisionCapabilityFamily.ENTER_CONCEALMENT,
    )
    assert status.status is CapabilityAvailabilityStatus.NOT_APPLICABLE
    assert status.blocker_codes == ()


def test_c14_explicit_missing_producer_evidence_is_missing() -> None:
    engine = _engine_with_contact()
    safe = _safe_bundle(engine)
    snapshot = safe["snapshot"]
    assert isinstance(snapshot, DecisionSnapshotIdentity)
    missing = _explicit(
        snapshot,
        1,
        DecisionCapabilityFamily.GUIDED_FIRE,
        CapabilityAvailabilityStatus.MISSING,
        producer="engine.guided_fire_required_record",
    )
    _, _, _, capabilities, _ = _adapter_outputs(
        engine,
        explicit=(missing,),
    )

    assert _family_status(
        capabilities,
        1,
        DecisionCapabilityFamily.GUIDED_FIRE,
    ).status is CapabilityAvailabilityStatus.MISSING


def test_c15_valid_action_and_explicit_blocker_conflict_fails_closed() -> None:
    engine = _engine_with_contact()
    safe = _safe_bundle(engine)
    snapshot = safe["snapshot"]
    context = safe["side_context"]
    assert isinstance(snapshot, DecisionSnapshotIdentity)
    assert isinstance(context, SideDecisionContext)
    catalog = build_action_catalog_snapshot(
        snapshot_identity=snapshot,
        frozen_valid_actions=engine.valid_actions("red"),
    )
    blocked = _explicit(
        snapshot,
        1,
        DecisionCapabilityFamily.WAIT,
        CapabilityAvailabilityStatus.BLOCKED,
        blockers=("CONTRADICTORY_TEST_BLOCKER",),
    )

    with pytest.raises(DecisionProjectionContractError, match="conflicts"):
        build_decision_capability_snapshot(
            snapshot_identity=snapshot,
            own_operator_references=context.controllable_operator_references,
            action_catalog=catalog,
            explicit_capability_records=(blocked,),
        )


def test_c16_c17_presentation_text_is_excluded_and_namespaces_are_stable() -> None:
    engine = _indirect_engine()
    _create_indirect_mission(engine)
    safe = _safe_bundle(engine)
    snapshot = safe["snapshot"]
    assert isinstance(snapshot, DecisionSnapshotIdentity)
    evidence = indirect_capability_evidence(
        snapshot,
        engine.get_indirect_fire_capabilities("red"),
    )
    before = tuple(evidence)
    ui_message = "This product operation is currently unavailable."
    after = tuple(evidence)

    assert ui_message not in repr(before)
    assert before == after
    assert all(item.producer_namespace.startswith("engine.") for item in before)
    assert "reason_text" not in {
        item.name for item in fields(ExplicitCapabilityEvidence)
    }
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert "wargame_native_engine.operations.product" not in imports


@pytest.mark.parametrize("field_name", ["side", "window", "frame", "time"])
def test_c18_evidence_side_window_frame_time_mismatch_fails_closed(
    field_name: str,
) -> None:
    engine = _engine_with_contact()
    safe = _safe_bundle(engine)
    snapshot = safe["snapshot"]
    context = safe["side_context"]
    assert isinstance(snapshot, DecisionSnapshotIdentity)
    assert isinstance(context, SideDecisionContext)
    catalog = build_action_catalog_snapshot(
        snapshot_identity=snapshot,
        frozen_valid_actions=engine.valid_actions("red"),
    )
    evidence = _explicit(
        snapshot,
        1,
        DecisionCapabilityFamily.GUIDED_FIRE,
        CapabilityAvailabilityStatus.AVAILABLE,
    )
    changes = {
        "side": {"side": DecisionSide.BLUE},
        "window": {"decision_window_index": snapshot.decision_window_index + 1},
        "frame": {"frame_index": snapshot.frame_index + 1},
        "time": {"sim_time_ms": snapshot.sim_time_ms + 1},
    }[field_name]

    with pytest.raises(DecisionProjectionContractError, match="mismatch"):
        build_decision_capability_snapshot(
            snapshot_identity=snapshot,
            own_operator_references=context.controllable_operator_references,
            action_catalog=catalog,
            explicit_capability_records=(replace(evidence, **changes),),
        )


def test_c19_c20_capabilities_are_own_only_and_enemy_actor_fails_closed() -> None:
    engine = _engine_with_contact()
    safe = _safe_bundle(engine)
    snapshot = safe["snapshot"]
    context = safe["side_context"]
    assert isinstance(snapshot, DecisionSnapshotIdentity)
    assert isinstance(context, SideDecisionContext)
    catalog = build_action_catalog_snapshot(
        snapshot_identity=snapshot,
        frozen_valid_actions=engine.valid_actions("red"),
    )
    enemy = context.observed_contact_references[0]
    evidence = _explicit(
        snapshot,
        enemy,
        DecisionCapabilityFamily.WAIT,
        CapabilityAvailabilityStatus.AVAILABLE,
    )

    with pytest.raises(DecisionProjectionContractError, match="not an OWN"):
        build_decision_capability_snapshot(
            snapshot_identity=snapshot,
            own_operator_references=context.controllable_operator_references,
            action_catalog=catalog,
            explicit_capability_records=(evidence,),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: setattr(value, "health", 45.0),
        lambda value: setattr(value, "suppression_remaining_ms", 10_000),
        lambda value: setattr(value, "weapon_cooldown_remaining_ms", 30_000),
        lambda value: (
            setattr(value, "lifecycle_profile_id", "hidden-profile"),
            setattr(value, "lifecycle_remaining_ms", 500_000),
        ),
        lambda value: setattr(value, "passenger_id", 999),
    ],
)
def test_c21_hidden_enemy_state_does_not_change_catalog_capability_or_decision(
    mutation,
) -> None:
    engine = _engine_with_contact()
    _, _, baseline_catalog, baseline_capability, baseline_decision = (
        _adapter_outputs(engine)
    )
    enemy = engine._require_world().operators[2]  # noqa: SLF001
    mutation(enemy)

    _, _, catalog, capability, decision = _adapter_outputs(engine)

    assert catalog == baseline_catalog
    assert capability == baseline_capability
    assert decision == baseline_decision
    assert catalog.catalog_hash == baseline_catalog.catalog_hash
    assert capability.semantic_hash == baseline_capability.semantic_hash
    assert decision.semantic_hash == baseline_decision.semantic_hash


def test_c22_own_suppression_guided_and_indirect_evidence_is_sensitive() -> None:
    record = _scenario_document(mover_domain="PERSONNEL")
    record["operators"][0]["suppression_remaining_ms"] = 1_000
    record["operators"][0]["state"] = "SUPPRESSED"
    suppressed = NativeEngine(scenario_from_dict(record))
    suppressed.reset(42)
    _, _, before_catalog, before_capability, before_decision = _adapter_outputs(
        suppressed,
        window=1,
        frame=1,
    )
    suppressed.step([], delta_time_ms=1_000)
    _, _, after_catalog, after_capability, after_decision = _adapter_outputs(
        suppressed,
        window=2,
        frame=2,
    )
    assert before_catalog.catalog_hash != after_catalog.catalog_hash
    assert before_capability.semantic_hash != after_capability.semantic_hash
    assert before_decision.semantic_hash != after_decision.semantic_hash

    guided = _guided_engine()
    guided_safe = _safe_bundle(guided, window=1, frame=1)
    guided_snapshot = guided_safe["snapshot"]
    assert isinstance(guided_snapshot, DecisionSnapshotIdentity)
    assert guided_capability_evidence(
        guided_snapshot,
        guided.get_guided_fire_capabilities("red"),
    )
    guided.step([_guided_action(guided)], delta_time_ms=1)
    guided_after = _safe_bundle(guided, window=2, frame=2)["snapshot"]
    assert isinstance(guided_after, DecisionSnapshotIdentity)
    assert guided_capability_evidence(
        guided_after,
        guided.get_guided_fire_capabilities("red"),
    ) == ()

    indirect = _indirect_engine()
    indirect_before = _safe_bundle(indirect, window=1, frame=1)["snapshot"]
    assert isinstance(indirect_before, DecisionSnapshotIdentity)
    available = indirect_capability_evidence(
        indirect_before,
        indirect.get_indirect_fire_capabilities("red"),
    )[0]
    _create_indirect_mission(indirect)
    indirect_after = _safe_bundle(indirect, window=2, frame=2)["snapshot"]
    assert isinstance(indirect_after, DecisionSnapshotIdentity)
    blocked = indirect_capability_evidence(
        indirect_after,
        indirect.get_indirect_fire_capabilities("red"),
    )[0]
    assert available.status is CapabilityAvailabilityStatus.AVAILABLE
    assert blocked.status is CapabilityAvailabilityStatus.BLOCKED


def test_c23_snapshot_restore_rebuilds_capability_catalog_and_decision() -> None:
    engine = _engine_with_contact()
    _, _, expected_catalog, expected_capability, expected_decision = (
        _adapter_outputs(engine)
    )
    snapshot = deepcopy(engine.snapshot())
    restored = NativeEngine(load_scenario(SCENARIO_PATH))
    restored.reset(7)
    restored.restore(snapshot)

    _, _, catalog, capability, decision = _adapter_outputs(restored)

    assert catalog == expected_catalog
    assert capability == expected_capability
    assert decision == expected_decision


def test_c24_strict_replay_rebuilds_all_derived_snapshots(tmp_path: Path) -> None:
    engine = NativeEngine(load_scenario(SCENARIO_PATH))
    engine.reset(42)
    action = next(
        item.action for item in engine.valid_actions("red")
        if item.action.action_type is ActionType.MOVE
    )
    writer = ReplayWriter(
        tmp_path / "decision-capability-replay",
        run_id="decision-capability-replay",
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
    _, _, expected_catalog, expected_capability, expected_decision = (
        _adapter_outputs(engine)
    )

    verification = replay_actions(engine.scenario, writer.root, strict=True)
    replayed = NativeEngine(load_scenario(SCENARIO_PATH))
    replayed.reset(42)
    replayed.step([action], delta_time_ms=60_000)
    _, _, catalog, capability, decision = _adapter_outputs(replayed)

    assert verification.matches is True
    assert catalog == expected_catalog
    assert capability == expected_capability
    assert decision == expected_decision


def test_c25_c26_decision_binds_real_reference_and_forgery_is_rejected() -> None:
    engine = _engine_with_contact()
    _, _, catalog, _, decision = _adapter_outputs(engine)

    assert decision.action_space == catalog.reference()
    assert decision.action_space.catalog_hash == catalog.catalog_hash
    forged = deepcopy(catalog)
    object.__setattr__(forged, "catalog_hash", "0" * 64)
    with pytest.raises(DecisionProjectionContractError, match="hash mismatch"):
        forged.reference()


def test_c27_future_action_schema_or_subclass_fails_closed() -> None:
    engine = _engine_with_contact()
    safe = _safe_bundle(engine)
    snapshot = safe["snapshot"]
    assert isinstance(snapshot, DecisionSnapshotIdentity)
    value = engine.valid_actions("red")[0]

    @dataclass(frozen=True, slots=True)
    class FutureValidAction(ValidAction):
        future_internal_state: str = "hidden"

    future = FutureValidAction(
        action=value.action,
        priority=value.priority,
        source=value.source,
    )
    with pytest.raises(TypeError, match="exact ValidAction"):
        build_action_catalog_snapshot(
            snapshot_identity=snapshot,
            frozen_valid_actions=(future,),
        )


def test_c28_future_capability_record_subclass_fails_closed() -> None:
    engine = _engine_with_contact()
    safe = _safe_bundle(engine)
    snapshot = safe["snapshot"]
    context = safe["side_context"]
    assert isinstance(snapshot, DecisionSnapshotIdentity)
    assert isinstance(context, SideDecisionContext)
    catalog = build_action_catalog_snapshot(
        snapshot_identity=snapshot,
        frozen_valid_actions=engine.valid_actions("red"),
    )

    @dataclass(frozen=True, slots=True)
    class FutureEvidence(ExplicitCapabilityEvidence):
        future_internal_state: str = "hidden"

    future = FutureEvidence(
        side=snapshot.side,
        decision_window_index=snapshot.decision_window_index,
        frame_index=snapshot.frame_index,
        sim_time_ms=snapshot.sim_time_ms,
        operator_reference=1,
        family=DecisionCapabilityFamily.GUIDED_FIRE,
        status=CapabilityAvailabilityStatus.AVAILABLE,
        producer_namespace="engine.future_capability",
    )
    with pytest.raises(TypeError, match="exact evidence"):
        build_decision_capability_snapshot(
            snapshot_identity=snapshot,
            own_operator_references=context.controllable_operator_references,
            action_catalog=catalog,
            explicit_capability_records=(future,),
        )


def test_all_current_engine_action_types_have_explicit_family_mapping() -> None:
    actions = [
        EngineAction("red", 1, action_type)
        for action_type in ActionType
        if action_type is not ActionType.FIRE
    ]
    actions.extend((
        EngineAction(
            "red",
            1,
            ActionType.FIRE,
            parameters={"fire_mode": "GUIDED"},
        ),
        EngineAction(
            "red",
            1,
            ActionType.FIRE,
            parameters={"fire_mode": "INDIRECT"},
        ),
    ))

    assert all(capability_family_for_action(value) for value in actions)
    with pytest.raises(DecisionProjectionContractError, match="fire_mode"):
        capability_family_for_action(
            EngineAction("red", 1, ActionType.FIRE)
        )


def test_real_guided_board_unload_and_concealment_producer_adapters() -> None:
    guided = _guided_engine()
    guided_snapshot = _safe_bundle(guided)["snapshot"]
    assert isinstance(guided_snapshot, DecisionSnapshotIdentity)
    assert guided_capability_evidence(
        guided_snapshot,
        guided.get_guided_fire_capabilities("red"),
    )[0].status is CapabilityAvailabilityStatus.AVAILABLE

    from tests.test_boarding_contract import _engine as boarding_engine

    boarding = boarding_engine()
    boarding_snapshot = _safe_bundle(boarding)["snapshot"]
    assert isinstance(boarding_snapshot, DecisionSnapshotIdentity)
    board = board_capability_evidence(
        boarding_snapshot,
        boarding.get_board_capabilities("red"),
    )
    assert board[0].family is DecisionCapabilityFamily.BOARD
    boarding.step(
        [boarding.board_action(boarding.get_board_capabilities("red")[0])],
        delta_time_ms=75_000,
    )
    unload_snapshot = _safe_bundle(boarding, window=8, frame=12)["snapshot"]
    assert isinstance(unload_snapshot, DecisionSnapshotIdentity)
    unload = unload_capability_evidence(
        unload_snapshot,
        boarding.get_unload_capabilities("red"),
    )
    assert unload[0].family is DecisionCapabilityFamily.UNLOAD

    concealment = _movement_engine(mover_domain="PERSONNEL")
    concealment_snapshot = _safe_bundle(concealment)["snapshot"]
    assert isinstance(concealment_snapshot, DecisionSnapshotIdentity)
    record = concealment_capability_evidence(
        concealment_snapshot,
        concealment.get_concealment_capabilities("red"),
    )[0]
    assert record.status is CapabilityAvailabilityStatus.AVAILABLE


def test_adapter_does_not_import_world_engine_lifecycle_or_consumers() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    forbidden = {
        "wargame_native_engine.core.engine",
        "wargame_native_engine.core.operator",
        "wargame_native_engine.core.world",
        "wargame_native_engine.operations.controller",
        "wargame_native_engine.operations.product",
        "wargame_native_engine.training",
        "wargame_native_engine.viewer",
    }
    assert imports.isdisjoint(forbidden)
    assert not hasattr(ActionCatalogSnapshot, "world")
    assert not hasattr(ActionCatalogEntry, "engine")
