from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import ast
import inspect
import math
from pathlib import Path

import pytest

from scripts.run_minimal_source_replay import _template
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.core.operator import MovementTaskPauseReason
from wargame_native_engine.data import ScenarioBundle, ScenarioCatalog
from wargame_native_engine.gateway import CommandGateway, CommandGatewayError, SessionManager
from wargame_native_engine.gateway.controller_decision import build_controller_decision_core
from wargame_native_engine.io.replay import ReplayWriter, replay_actions
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.operations import WargameFacade
from wargame_native_engine.projection.decision_capability import ActionCatalogSnapshot
from wargame_native_engine.projection.decision_semantic import (
    CapabilityAvailabilityStatus,
    CapabilityEvidenceKind,
    CapabilityFamilyStatus,
    DecisionCapabilityFamily,
    DecisionCapabilitySnapshot,
    DecisionObjective,
    DecisionProjectionContractError,
    DecisionSide,
    DecisionSnapshotIdentity,
    OperatorCapabilitySummary,
    build_decision_semantic_projection,
)
from wargame_native_engine.projection.operator_lifecycle import SuppressionView
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment
from wargame_native_engine.training import (
    CAPABILITY_FAMILY_ORDER,
    DECISION_ACTION_ENCODER_SCHEMA_VERSION,
    DECISION_OBSERVATION_ENCODER_SCHEMA_VERSION,
    DecisionActionEncoder,
    DecisionEncoderConfig,
    DecisionEncodingError,
    DecisionObservationEncoder,
    NativeMultiSeatEnv,
    NativeActionEncoder,
    SideObservationEncoder,
)
import wargame_native_engine.training.decision_encoder as encoder_module


SCENARIO = Path("examples/source_movement_scenario.json")


class _Factory:
    def __init__(self) -> None:
        self.environments: list[SourceCompatibleEnvironment] = []

    def __call__(self) -> SourceCompatibleEnvironment:
        scenario = load_scenario(SCENARIO)
        environment = SourceCompatibleEnvironment(
            GameSession(NativeEngineBackend(scenario)), template=_template(scenario)
        )
        self.environments.append(environment)
        return environment


@pytest.fixture
def integrated(tmp_path: Path):
    factory = _Factory()
    gateway = CommandGateway(
        SessionManager(factory, storage_root=tmp_path / "managed"),
        delta_time_seconds=20,
    )
    descriptor = gateway.create_session(
        red_controller_id="encoder-red",
        blue_controller_id="encoder-blue",
        seed=42,
    )
    try:
        yield gateway, factory, descriptor.session_id
    finally:
        gateway.close_session(descriptor.session_id)


def _encoders(max_actions: int = 64):
    config = DecisionEncoderConfig(
        max_own_operators=8,
        max_observed_enemies=8,
        max_objectives=8,
        max_actions=max_actions,
    )
    return DecisionObservationEncoder(config), DecisionActionEncoder(max_actions)


def _view(integrated, side: str = "red"):
    gateway, _, session_id = integrated
    controller = "encoder-red" if side == "red" else "encoder-blue"
    return gateway.view(session_id, controller)


def _rebuild(decision, **changes):
    return build_decision_semantic_projection(
        authority_metadata=changes.get("authority_metadata", decision.authority_metadata),
        snapshot=changes.get("snapshot", decision.snapshot),
        side_context=changes.get("side_context", decision.side_context),
        own_operators=changes.get("own_operators", decision.own_operators),
        observed_enemies=changes.get("observed_enemies", decision.observed_enemies),
        capability_snapshot=changes.get("capabilities", decision.capabilities),
        action_catalog_reference=changes.get("action_space", decision.action_space),
    )


def _derived_core(session: GameSession, *, side: str, window: int, frame: int):
    observation = session.observe(side)
    result = session.result()
    sim_time_ms = int(session.snapshot()["world"].get("elapsed_time_ms", 0))
    identity = DecisionSnapshotIdentity(
        side=DecisionSide(side),
        decision_window_index=window,
        frame_index=frame,
        sim_time_ms=sim_time_ms,
        stage=observation.stage,
        terminated=result.terminal,
        truncated=result.truncated,
    )
    return build_controller_decision_core(
        session=session,
        observation=observation,
        snapshot_identity=identity,
        frozen_valid_actions=session.valid_actions(side),
    )


def test_e1_e2_same_safe_input_has_identical_tensor_and_catalog_mapping(integrated) -> None:
    view = _view(integrated)
    observation_encoder, action_encoder = _encoders()
    first = observation_encoder.encode(view.decision_semantic)
    second = observation_encoder.encode(deepcopy(view.decision_semantic))
    actions_one = action_encoder.encode(view.action_catalog)
    actions_two = action_encoder.encode(deepcopy(view.action_catalog))

    assert first == second
    assert first.tensor == second.tensor
    assert actions_one == actions_two
    assert actions_one.action_ids == view.action_catalog.action_ids
    assert actions_one.mask[: view.action_catalog.action_count] == (
        True,
    ) * view.action_catalog.action_count
    assert not any(actions_one.mask[view.action_catalog.action_count :])


@pytest.mark.parametrize(
    "mutation",
    (
        lambda enemy: setattr(enemy, "health", enemy.health - 1),
        lambda enemy: setattr(enemy, "suppression_remaining_ms", 777),
        lambda enemy: setattr(enemy, "weapon_cooldown_remaining_ms", 888),
        lambda enemy: setattr(enemy, "guided_fire_recovery_remaining_ms", 999),
        lambda enemy: setattr(
            enemy, "indirect_fire_planned_cooldown_remaining_ms", 111
        ),
        lambda enemy: setattr(enemy, "lifecycle_remaining_ms", 222),
        lambda enemy: setattr(
            enemy,
            "movement_task_pause_reason",
            MovementTaskPauseReason.SUPPRESSION,
        ),
        lambda enemy: setattr(enemy, "passenger_id", 999),
    ),
)
def test_e3_e5_hidden_enemy_state_is_tensor_and_mask_noninterfering(
    integrated, mutation
) -> None:
    gateway, factory, session_id = integrated
    before = gateway.view(session_id, "encoder-red")
    observation_encoder, action_encoder = _encoders()
    before_tensor = observation_encoder.encode(before.decision_semantic)
    before_actions = action_encoder.encode(before.action_catalog)

    world = factory.environments[0].session.backend.engine._require_world()  # noqa: SLF001
    mutation(world.operators[2])
    after = gateway.view(session_id, "encoder-red")

    assert after.decision_semantic == before.decision_semantic
    assert observation_encoder.encode(after.decision_semantic).tensor == before_tensor.tensor
    assert action_encoder.encode(after.action_catalog) == before_actions


def test_e6_authority_metadata_and_hashes_are_out_of_band(integrated) -> None:
    decision = _view(integrated).decision_semantic
    encoder, _ = _encoders()
    changed = _rebuild(
        decision,
        authority_metadata=replace(
            decision.authority_metadata, engine_version="metadata-only-change"
        ),
    )

    assert changed.semantic_hash != decision.semantic_hash
    assert encoder.encode(changed).tensor == encoder.encode(decision).tensor
    policy = encoder.encode(decision).policy_record()
    rendered = repr(policy).lower()
    assert "hash" not in rendered
    assert "version" not in rendered


def test_e7_e10_visible_semantic_changes_change_intended_features(integrated) -> None:
    decision = _view(integrated).decision_semantic
    encoder, _ = _encoders()
    base = encoder.encode(decision)
    own = decision.own_operators[0]
    suppression = own.suppression
    suppressed = replace(
        own,
        suppression=SuppressionView(
            active=not suppression.active,
            remaining_ms=0 if suppression.active else 1000,
        ),
    )
    own_change = _rebuild(decision, own_operators=(suppressed, *decision.own_operators[1:]))
    assert encoder.encode(own_change).own_operator_features != base.own_operator_features

    if decision.observed_enemies:
        enemies = decision.observed_enemies[:-1]
        context = replace(
            decision.side_context,
            observed_contact_references=tuple(
                value.operator_reference for value in enemies
            ),
        )
        contact_change = _rebuild(
            decision, side_context=context, observed_enemies=enemies
        )
        assert encoder.encode(contact_change).observed_enemy_mask != base.observed_enemy_mask

    changed_objective = DecisionObjective(
        objective_id=999,
        x=1.0,
        y=2.0,
        radius=1.0,
        value=1,
        controlling_side=decision.snapshot.side.value,
    )
    objective_change = _rebuild(
        decision,
        side_context=replace(
            decision.side_context,
            objectives=(*decision.side_context.objectives, changed_objective),
        ),
    )
    assert encoder.encode(objective_change).objective_features != base.objective_features


@pytest.mark.parametrize(
    "family",
    (DecisionCapabilityFamily.GUIDED_FIRE, DecisionCapabilityFamily.INDIRECT_FIRE),
)
def test_e8_e9_capability_evidence_changes_capability_tensor(
    integrated, family
) -> None:
    decision = _view(integrated).decision_semantic
    encoder, _ = _encoders()
    summary = decision.capabilities.operators[0]
    families = tuple(
        CapabilityFamilyStatus(
            family=item.family,
            status=CapabilityAvailabilityStatus.AVAILABLE,
            producer_namespace=f"test.{family.value.lower()}",
            evidence_kind=CapabilityEvidenceKind.ENGINE_CAPABILITY_RECORD,
            available_instance_count=0,
        )
        if item.family is family
        else item
        for item in summary.families
    )
    changed_capabilities = DecisionCapabilitySnapshot(
        schema_version=decision.capabilities.schema_version,
        side=decision.capabilities.side,
        decision_window_index=decision.capabilities.decision_window_index,
        frame_index=decision.capabilities.frame_index,
        sim_time_ms=decision.capabilities.sim_time_ms,
        operators=(
            OperatorCapabilitySummary(summary.operator_reference, families),
            *decision.capabilities.operators[1:],
        ),
    )
    changed = _rebuild(decision, capabilities=changed_capabilities)
    assert encoder.encode(changed).capability_features != (
        encoder.encode(decision).capability_features
    )


def test_e11_e15_stable_order_explicit_mappings_numeric_safety_and_padding(integrated) -> None:
    decision = _view(integrated).decision_semantic
    encoder, _ = _encoders()
    encoded = encoder.encode(decision)

    own_refs = tuple(
        int(row[0])
        for row, present in zip(
            encoded.own_operator_features, encoded.own_operator_mask
        )
        if present
    )
    enemy_refs = tuple(
        int(row[0])
        for row, present in zip(
            encoded.observed_enemy_features, encoded.observed_enemy_mask
        )
        if present
    )
    assert own_refs == tuple(sorted(own_refs))
    assert enemy_refs == tuple(sorted(enemy_refs))
    assert all(math.isfinite(value) for value in encoded.tensor)
    assert not any(encoded.own_operator_mask[len(decision.own_operators) :])
    assert all(
        row == (0.0,) * len(row)
        for row in encoded.own_operator_features[len(decision.own_operators) :]
    )
    assert encoder.observation_space.contains(encoded)
    tree = ast.parse(inspect.getsource(encoder_module))
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "hash"
        for node in ast.walk(tree)
    )


def test_e16_e20_catalog_order_padding_overflow_and_window_binding(integrated) -> None:
    view = _view(integrated)
    encoder = DecisionActionEncoder(64)
    encoded = encoder.encode(view.action_catalog)
    assert encoded.action_ids == view.action_catalog.action_ids
    assert tuple(value["action_id"] for value in encoded.actions) == encoded.action_ids
    assert not any(encoded.mask[view.action_catalog.action_count :])
    assert encoder.resolve(encoded, view.action_catalog, 0) == encoded.action_ids[0]

    with pytest.raises(DecisionEncodingError, match="exceeds"):
        DecisionActionEncoder(max(1, view.action_catalog.action_count - 1)).encode(
            view.action_catalog
        )
    stale_catalog = ActionCatalogSnapshot(
        schema_version=view.action_catalog.schema_version,
        side=view.action_catalog.side,
        decision_window_index=view.action_catalog.decision_window_index + 1,
        frame_index=view.action_catalog.frame_index + 1,
        sim_time_ms=view.action_catalog.sim_time_ms + 1,
        entries=(),
    )
    with pytest.raises(DecisionProjectionContractError, match="mismatch"):
        encoder.resolve(encoded, stale_catalog, 0)


def test_e20_gateway_still_rejects_previous_window_action(integrated) -> None:
    gateway, _, session_id = integrated
    old = gateway.view(session_id, "encoder-red")
    action_id = old.action_catalog.action_ids[0]
    gateway.submit_batch(
        session_id, "encoder-red", request_id="encoder-r0",
        expected_window=0, ordered_action_ids=(),
    )
    gateway.submit_batch(
        session_id, "encoder-blue", request_id="encoder-b0",
        expected_window=0, ordered_action_ids=(),
    )
    with pytest.raises(CommandGatewayError, match="stale_expected_step"):
        gateway.submit_batch(
            session_id, "encoder-red", request_id="encoder-r-stale",
            expected_window=0, ordered_action_ids=(action_id,),
        )


def test_e21_snapshot_restore_rebuilds_same_tensor_mask_and_mapping(integrated) -> None:
    gateway, factory, session_id = integrated
    before = gateway.view(session_id, "encoder-red")
    observation_encoder, action_encoder = _encoders()
    expected_observation = observation_encoder.encode(before.decision_semantic)
    expected_actions = action_encoder.encode(before.action_catalog)
    environment = factory.environments[0]
    snapshot = deepcopy(environment.session.snapshot())
    environment.restore_views(snapshot)
    after = gateway.view(session_id, "encoder-red")

    assert observation_encoder.encode(after.decision_semantic) == expected_observation
    assert action_encoder.encode(after.action_catalog) == expected_actions


def test_e22_strict_replay_rebuilds_same_tensor_mask_and_mapping(tmp_path: Path) -> None:
    scenario = load_scenario(SCENARIO)
    original = GameSession(NativeEngineBackend(scenario))
    original.reset(42)
    action = original.valid_actions("red")[0].action
    writer = ReplayWriter(
        tmp_path / "decision-encoder-replay",
        run_id="decision-encoder-replay",
        scenario=scenario,
        seed=42,
        agents={"red": "test", "blue": "test"},
        max_ticks=original.max_ticks,
        ruleset=original.backend.ruleset,
        project_commit=None,
    )
    writer.record_initial_state(original.snapshot())
    result = original.step_ms((action,), delta_time_ms=20_000)
    writer.record_step(result, original.snapshot())
    writer.finalize({"result": original.result().to_dict()}, status="complete")
    assert replay_actions(scenario, writer.root, strict=True).matches
    expected = _derived_core(original, side="red", window=1, frame=1)

    replayed = GameSession(NativeEngineBackend(load_scenario(SCENARIO)))
    replayed.reset(42)
    replayed.step_ms((action,), delta_time_ms=20_000)
    actual = _derived_core(replayed, side="red", window=1, frame=1)
    observation_encoder, action_encoder = _encoders()
    assert observation_encoder.encode(actual.decision_semantic) == (
        observation_encoder.encode(expected.decision_semantic)
    )
    assert action_encoder.encode(actual.action_catalog) == (
        action_encoder.encode(expected.action_catalog)
    )


def test_e23_red_blue_are_side_isolated_and_share_pre_state(integrated) -> None:
    red = _view(integrated, "red")
    blue = _view(integrated, "blue")
    encoder, action_encoder = _encoders()
    red_value = encoder.encode(red.decision_semantic)
    blue_value = encoder.encode(blue.decision_semantic)

    assert red_value.snapshot_features[0] == 1.0
    assert blue_value.snapshot_features[0] == 2.0
    assert red_value.frame_index == blue_value.frame_index
    assert red_value.sim_time_ms == blue_value.sim_time_ms
    assert action_encoder.encode(red.action_catalog).side is DecisionSide.RED
    assert action_encoder.encode(blue.action_catalog).side is DecisionSide.BLUE


def test_e24_multi_seat_catalog_indices_are_joint_same_pre_state(tmp_path: Path) -> None:
    bundle = ScenarioBundle.from_path(SCENARIO)

    def factory(seed: int, sequence: int):
        app = WargameFacade(
            ScenarioCatalog.from_bundles({
                bundle.scenario_identity.scenario_id: bundle,
            }),
            storage_root=tmp_path / f"multi-{sequence}",
        )
        return app.create_match(
            scenario_id=bundle.scenario_identity.scenario_id,
            red_controller_id="multi-red",
            blue_controller_id="multi-blue",
            seed=seed,
            decision_interval_ms=1000,
            frame_interval_ms=1000,
        )

    environment = NativeMultiSeatEnv(factory, run_namespace="encoder-multi")
    try:
        initial = environment.reset(42)
        red_indices = (0,) if initial.action_spaces["red"].action_ids else ()
        blue_indices = (0,) if initial.action_spaces["blue"].action_ids else ()
        result = environment.step_action_indices(red_indices, blue_indices)
        assert result.decision_index == initial.decision_index == 0
        assert result.fact_frame_start == result.fact_frame_end == 1
        assert tuple(
            transition.decision.action_ids for transition in result.transitions
        ) == tuple(
            (() if not values else (initial.action_spaces[side].action_ids[0],))
            for side, values in (("red", red_indices), ("blue", blue_indices))
        )
    finally:
        environment.close()


def test_e25_e27_supervision_and_rules_are_excluded_from_policy_record(integrated) -> None:
    encoded = DecisionObservationEncoder(
        DecisionEncoderConfig(8, 8, 8, 64)
    ).encode(_view(integrated).decision_semantic)
    record = encoded.policy_record()
    text = repr(record).lower()
    for forbidden in (
        "reward", "winner", "outcome", "eightfactor", "green", "world"
    ):
        assert forbidden not in text
    source = inspect.getsource(encoder_module)
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "WorldState" not in imported
    assert "NativeEngine" not in imported
    assert "valid_actions(" not in source
    assert "suppression >" not in source


def test_e28_legacy_decision_v1_differential_has_explicit_reference_joins(
    integrated,
) -> None:
    view = _view(integrated)
    legacy_observation = SideObservationEncoder().encode(view)
    legacy_actions = NativeActionEncoder(64).encode(view.valid_actions)
    observation_encoder, action_encoder = _encoders()
    decision = observation_encoder.encode(view.decision_semantic)
    catalog = action_encoder.encode(view.action_catalog)

    legacy_own = tuple(sorted(
        value["operator_id"] for value in legacy_observation["friendly_operators"]
    ))
    decision_own = tuple(
        int(row[0]) for row, present in zip(
            decision.own_operator_features, decision.own_operator_mask
        ) if present
    )
    legacy_enemy = tuple(sorted(
        value["operator_id"] for value in legacy_observation["visible_enemies"]
    ))
    decision_enemy = tuple(
        int(row[0]) for row, present in zip(
            decision.observed_enemy_features, decision.observed_enemy_mask
        ) if present
    )
    assert decision_own == legacy_own
    assert decision_enemy == legacy_enemy
    assert catalog.action_ids == legacy_actions.action_ids


def test_e29_e32_spaces_terminal_zero_action_and_capacity(integrated) -> None:
    view = _view(integrated)
    observation_encoder, action_encoder = _encoders()
    observation = observation_encoder.encode(view.decision_semantic)
    actions = action_encoder.encode(view.action_catalog)
    assert observation_encoder.observation_space.contains(observation.policy_record())
    assert action_encoder.action_space.n == len(actions.mask) == 64
    empty = ActionCatalogSnapshot(
        schema_version=view.action_catalog.schema_version,
        side=view.action_catalog.side,
        decision_window_index=view.action_catalog.decision_window_index,
        frame_index=view.action_catalog.frame_index,
        sim_time_ms=view.action_catalog.sim_time_ms,
        entries=(),
    )
    empty_encoded = action_encoder.encode(empty)
    assert not any(empty_encoded.mask)
    with pytest.raises(ValueError, match="not currently valid"):
        action_encoder.resolve(empty_encoded, empty, 0)


def test_e33_encoder_instances_do_not_share_mutable_state(integrated) -> None:
    decision = _view(integrated).decision_semantic
    first, _ = _encoders()
    second, _ = _encoders()
    one = first.encode(decision)
    two = second.encode(decision)
    assert one == two
    assert first is not second
    assert first.config is not second.config


def test_e34_e35_future_fields_and_capability_families_fail_closed(integrated) -> None:
    view = _view(integrated)
    decision = view.decision_semantic
    encoder, action_encoder = _encoders()
    assert set(encoder.encode(decision).policy_record()) == {
        "snapshot_features",
        "objective_features",
        "objective_mask",
        "own_operator_features",
        "own_operator_mask",
        "observed_enemy_features",
        "observed_enemy_mask",
        "capability_features",
        "capability_mask",
    }
    action_record = action_encoder.encode(view.action_catalog).actions[0]
    assert set(action_record) == {"action_id", "priority", "source", "action"}
    assert set(action_record["action"]) == {
        "side", "actor_id", "action_type", "target_id",
        "target_position", "parameters",
    }
    summary = decision.capabilities.operators[0]
    missing = tuple(
        value for value in summary.families
        if value.family is not CAPABILITY_FAMILY_ORDER[-1]
    )
    capabilities = DecisionCapabilitySnapshot(
        schema_version=decision.capabilities.schema_version,
        side=decision.capabilities.side,
        decision_window_index=decision.capabilities.decision_window_index,
        frame_index=decision.capabilities.frame_index,
        sim_time_ms=decision.capabilities.sim_time_ms,
        operators=(
            OperatorCapabilitySummary(summary.operator_reference, missing),
            *decision.capabilities.operators[1:],
        ),
    )
    changed = _rebuild(decision, capabilities=capabilities)
    with pytest.raises(DecisionEncodingError, match="version update"):
        encoder.encode(changed)


def test_encoder_versions_and_contract_hashes_are_stable() -> None:
    first, first_action = _encoders()
    second, second_action = _encoders()
    assert DECISION_OBSERVATION_ENCODER_SCHEMA_VERSION == (
        "native-decision-observation-encoder/1.0"
    )
    assert DECISION_ACTION_ENCODER_SCHEMA_VERSION == (
        "native-decision-action-encoder/1.0"
    )
    assert first.contract_hash == second.contract_hash
    assert first_action.contract_hash == second_action.contract_hash
    assert len(first.contract_hash) == len(first_action.contract_hash) == 64
