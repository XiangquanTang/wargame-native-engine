from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.run_minimal_source_replay import _template
from wargame_native_engine.agents.protocol import AgentStepContext
from wargame_native_engine.agents.runner import LocalAgentMatchRunner
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.core.operator import MovementTaskPauseReason
from wargame_native_engine.core.action import ActionType
from wargame_native_engine.data import ScenarioBundle, ScenarioCatalog
from wargame_native_engine.gateway import (
    CommandGateway,
    CommandGatewayError,
    SessionManager,
)
from wargame_native_engine.gateway.controller_decision import (
    build_controller_decision_core,
)
from wargame_native_engine.gateway.models import ControllerView
from wargame_native_engine.io.replay import ReplayWriter, replay_actions
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.operations import ProductOperationService, WargameFacade
from wargame_native_engine.projection.decision_semantic import (
    CapabilityAvailabilityStatus,
    DecisionProjectionContractError,
    DecisionSide,
    DecisionSnapshotIdentity,
)
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import (
    SourceCompatibleEnvironment,
)
from wargame_native_engine.training.action_encoder import NativeActionEncoder
from wargame_native_engine.training.multi_seat import NativeDecisionSession
from wargame_native_engine.viewer.live_control import controller_view_record
from tests.test_realtime_agent_session import _environment as _realtime_environment


SCENARIO = Path("examples/source_movement_scenario.json")


class _Factory:
    def __init__(self) -> None:
        self.environments: list[SourceCompatibleEnvironment] = []

    def __call__(self) -> SourceCompatibleEnvironment:
        scenario = load_scenario(SCENARIO)
        value = SourceCompatibleEnvironment(
            GameSession(NativeEngineBackend(scenario)),
            template=_template(scenario),
        )
        self.environments.append(value)
        return value


@pytest.fixture
def integrated(tmp_path: Path):
    factory = _Factory()
    gateway = CommandGateway(
        SessionManager(factory, storage_root=tmp_path / "managed"),
        delta_time_seconds=20,
    )
    descriptor = gateway.create_session(
        red_controller_id="red-controller",
        blue_controller_id="blue-controller",
        seed=42,
    )
    return gateway, factory, descriptor.session_id


def _match(tmp_path: Path):
    bundle = ScenarioBundle.from_path(SCENARIO)
    return WargameFacade(
        ScenarioCatalog.from_bundles({
            bundle.scenario_identity.scenario_id: bundle,
        }),
        storage_root=tmp_path / "managed-match",
    ).create_match(
        scenario_id=bundle.scenario_identity.scenario_id,
        red_controller_id="seat-red",
        blue_controller_id="seat-blue",
        seed=42,
    )


def test_c1_c3_c4_controller_core_is_one_atomic_action_snapshot(integrated) -> None:
    gateway, _, session_id = integrated
    view = gateway.view(session_id, "red-controller")

    assert view.decision_semantic is not None
    assert view.action_catalog is not None
    snapshot = view.decision_semantic.snapshot
    assert (
        snapshot.side.value,
        snapshot.decision_window_index,
        snapshot.frame_index,
        snapshot.sim_time_ms,
    ) == ("red", view.step_index, view.frame_index, view.sim_time_ms)
    assert tuple(item["action_id"] for item in view.valid_actions) == (
        view.action_catalog.action_ids
    )
    assert view.decision_semantic.action_space == view.action_catalog.reference()
    for entry in view.action_catalog.entries:
        if entry.actor_reference is None:
            continue
        summary = next(
            item
            for item in view.decision_semantic.capabilities.operators
            if item.operator_reference == entry.actor_reference
        )
        family = next(
            item for item in summary.families
            if item.family is entry.capability_family
        )
        assert family.status is CapabilityAvailabilityStatus.AVAILABLE


def test_c2_controller_identity_or_action_drift_fails_closed(integrated) -> None:
    gateway, _, session_id = integrated
    view = gateway.view(session_id, "red-controller")
    with pytest.raises(DecisionProjectionContractError, match="window mismatch"):
        replace(view, step_index=view.step_index + 1)
    if len(view.valid_actions) > 1:
        with pytest.raises(
            DecisionProjectionContractError, match="action identity mismatch"
        ):
            replace(view, valid_actions=tuple(reversed(view.valid_actions)))


def test_c5_c6_controller_hash_is_stable_and_old_window_is_immutable(
    integrated,
) -> None:
    gateway, _, session_id = integrated
    old = gateway.view(session_id, "red-controller")
    decision_hash = old.decision_semantic.semantic_hash
    catalog_hash = old.action_catalog.catalog_hash
    old_actions = deepcopy(old.valid_actions)

    gateway.submit_batch(
        session_id, "red-controller", request_id="r0",
        expected_window=0, ordered_action_ids=(),
    )
    gateway.submit_batch(
        session_id, "blue-controller", request_id="b0",
        expected_window=0, ordered_action_ids=(),
    )
    new = gateway.view(session_id, "red-controller")

    assert old.decision_semantic.semantic_hash == decision_hash
    assert old.action_catalog.catalog_hash == catalog_hash
    assert old.valid_actions == old_actions
    assert new is not old
    assert new.step_index == 1
    with pytest.raises(FrozenInstanceError):
        old.step_index = 99  # type: ignore[misc]


def test_c7_snapshot_restore_rebuilds_equal_controller_core(integrated) -> None:
    gateway, factory, session_id = integrated
    before = gateway.view(session_id, "red-controller")
    environment = factory.environments[0]
    snapshot = deepcopy(environment.session.snapshot())
    environment.restore_views(snapshot)
    after = gateway.view(session_id, "red-controller")

    assert after.decision_semantic == before.decision_semantic
    assert after.action_catalog == before.action_catalog
    assert after.decision_semantic.semantic_hash == (
        before.decision_semantic.semantic_hash
    )
    assert after.action_catalog.catalog_hash == before.action_catalog.catalog_hash


def test_c8_strict_replay_rebuilds_equal_controller_core(tmp_path: Path) -> None:
    scenario = load_scenario(SCENARIO)
    original = GameSession(NativeEngineBackend(scenario))
    original.reset(42)
    action = next(
        item.action for item in original.valid_actions("red")
        if item.action.action_type is ActionType.MOVE
    )
    writer = ReplayWriter(
        tmp_path / "controller-decision-replay",
        run_id="controller-decision-replay",
        scenario=scenario,
        seed=42,
        agents={"red": "test", "blue": "test"},
        max_ticks=original.max_ticks,
        ruleset=original.backend.ruleset,
        project_commit=None,
    )
    writer.record_initial_state(original.snapshot())
    result = original.step_ms((action,), delta_time_ms=60_000)
    writer.record_step(result, original.snapshot())
    writer.finalize({"result": original.result().to_dict()}, status="complete")
    expected = _derived_controller_view(original, window=7, frame=1)

    verification = replay_actions(scenario, writer.root, strict=True)
    replayed = GameSession(NativeEngineBackend(load_scenario(SCENARIO)))
    replayed.reset(42)
    replayed.step_ms((action,), delta_time_ms=60_000)
    actual = _derived_controller_view(replayed, window=7, frame=1)

    assert verification.matches is True
    assert actual.decision_semantic == expected.decision_semantic
    assert actual.action_catalog == expected.action_catalog


def _derived_controller_view(
    session: GameSession, *, window: int, frame: int,
) -> ControllerView:
    observation = session.observe("red")
    world = session.snapshot()["world"]
    sim_time_ms = world.get("elapsed_time_ms", 0)
    result = session.result()
    identity = DecisionSnapshotIdentity(
        DecisionSide.RED,
        window,
        frame,
        sim_time_ms,
        observation.stage,
        result.terminal,
        result.truncated,
    )
    valid_actions = session.valid_actions("red")
    core = build_controller_decision_core(
        session=session,
        observation=observation,
        snapshot_identity=identity,
        frozen_valid_actions=valid_actions,
    )
    return ControllerView(
        session_id=session.session_id,
        perspective="RED",
        step_index=window,
        observation=observation.to_dict(),
        source_frame={},
        valid_actions=tuple(value.to_dict() for value in valid_actions),
        terminal=session.is_terminal(),
        winner=None,
        outcome=None,
        frame_index=frame,
        sim_time_ms=sim_time_ms,
        terminated=result.terminal,
        truncated=result.truncated,
        decision_semantic=core.decision_semantic,
        action_catalog=core.action_catalog,
    )


def test_joint_red_blue_views_share_the_same_pre_state_identity(integrated) -> None:
    gateway, _, session_id = integrated
    red = gateway.view(session_id, "red-controller")
    blue = gateway.view(session_id, "blue-controller")
    assert (
        red.step_index, red.frame_index, red.sim_time_ms,
    ) == (
        blue.step_index, blue.frame_index, blue.sim_time_ms,
    )


def test_controller_build_collects_each_explicit_capability_producer_once(
    integrated,
) -> None:
    gateway, factory, session_id = integrated
    session = factory.environments[0].session
    names = (
        "get_guided_fire_capabilities",
        "get_indirect_fire_capabilities",
        "get_board_capabilities",
        "get_unload_capabilities",
        "get_concealment_capabilities",
    )
    probes = {}
    for name in names:
        probe = Mock(wraps=getattr(session, name))
        setattr(session, name, probe)
        probes[name] = probe
    view = gateway.view(session_id, "red-controller")
    from wargame_native_engine.operations.controller import SideController

    service = ProductOperationService(
        SideController(gateway, session_id, "red-controller")
    )
    service.capabilities([1], view=view)
    assert all(value.call_count == 1 for value in probes.values())


def test_controller_decision_build_does_not_mutate_world_or_advance_time(
    integrated,
) -> None:
    gateway, factory, session_id = integrated
    session = factory.environments[0].session
    before = deepcopy(session.snapshot())
    gateway.view(session_id, "red-controller")
    assert session.snapshot() == before


def test_c9_c10_serialization_is_explicit_and_legacy_viewer_does_not_expand_core(
    integrated,
) -> None:
    gateway, _, session_id = integrated
    view = gateway.view(session_id, "red-controller")
    legacy = view.to_record()
    viewer = controller_view_record(view)
    machine = view.decision_record()

    assert "decision_semantic" not in legacy
    assert "action_catalog" not in legacy
    assert "decision_semantic" not in viewer
    assert set(machine) == {"decision_semantic", "action_catalog"}
    text = repr(machine).lower()
    assert "winner" not in text
    assert "reward" not in text
    assert "world_hash" not in text


def test_h1_h3_h6_h7_human_uses_core_and_catalog_but_keeps_preview_authority(
    integrated,
) -> None:
    gateway, _, session_id = integrated
    controller = WargameFacade  # keep the test independent of browser DTOs
    del controller
    from wargame_native_engine.operations.controller import SideController

    side = SideController(gateway, session_id, "red-controller")
    service = ProductOperationService(side)
    view = side.observe()
    machine = service.decision_core()
    status = service.status()

    assert machine["decision_semantic"]["semantic_hash"] == (
        view.decision_semantic.semantic_hash
    )
    assert machine["action_catalog"]["catalog_hash"] == (
        view.action_catalog.catalog_hash
    )
    assert status["decision_semantic_hash"] == view.decision_semantic.semantic_hash
    human_capabilities = service.capabilities([1], view=view)
    authoritative_machine = view.decision_semantic.capabilities.to_dict()
    assert human_capabilities["machine_capabilities"] == [
        value for value in authoritative_machine["operators"]
        if value["operator_reference"] == 1
    ]
    selected = next(
        entry for entry in view.action_catalog.entries
        if entry.actor_reference is not None
    )
    staged = side.stage_action(selected.action_id)
    assert staged[0].action_id == selected.action_id
    assert staged[0].actor_id == selected.actor_reference


def test_a1_a2_a6_product_agent_context_receives_same_detached_machine_core(
    tmp_path: Path,
) -> None:
    match = _match(tmp_path)
    try:
        session = NativeDecisionSession(
            match,
            episode_id="phase3-agent-context",
            run_namespace="phase3-test",
            seed=42,
        )
        session.reset_result()
        context = session.decision_context(
            "red", include_semantic_core=True,
        )
        view = match.bind_controller(
            "red", controller_id="seat-red"
        ).observe()
        assert context.decision_semantic == view.decision_semantic
        assert context.action_catalog == view.action_catalog
        assert context.operation_capabilities.decision_index == (
            context.decision_semantic.snapshot.decision_window_index
        )
        assert context.action_catalog is not view.action_catalog
    finally:
        match.close()


def test_a3_a4_a5_catalog_identity_is_window_local_and_stale_rejected(
    integrated,
) -> None:
    gateway, _, session_id = integrated
    old = gateway.view(session_id, "red-controller")
    action_id = old.action_catalog.action_ids[0]
    gateway.submit_batch(
        session_id, "red-controller", request_id="r-stale-0",
        expected_window=0, ordered_action_ids=(),
    )
    gateway.submit_batch(
        session_id, "blue-controller", request_id="b-stale-0",
        expected_window=0, ordered_action_ids=(),
    )
    with pytest.raises(CommandGatewayError, match="stale_expected_step"):
        gateway.submit_batch(
            session_id, "red-controller", request_id="r-stale-1",
            expected_window=0, ordered_action_ids=(action_id,),
        )


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
        lambda enemy: setattr(enemy, "movement_task_pause_reason", (
            MovementTaskPauseReason.SUPPRESSION
        )),
        lambda enemy: setattr(enemy, "passenger_id", 999),
    ),
)
def test_integration_hidden_enemy_state_is_noninterfering(
    integrated, mutation,
) -> None:
    gateway, factory, session_id = integrated
    before = gateway.view(session_id, "red-controller")
    world = factory.environments[0].session.backend.engine._require_world()  # noqa: SLF001
    mutation(world.operators[2])
    after = gateway.view(session_id, "red-controller")

    assert after.decision_semantic == before.decision_semantic
    assert after.action_catalog == before.action_catalog
    assert after.decision_semantic.semantic_hash == (
        before.decision_semantic.semantic_hash
    )


def test_t1_t5_training_legacy_input_is_unchanged_and_catalog_order_matches(
    integrated,
) -> None:
    gateway, _, session_id = integrated
    view = gateway.view(session_id, "red-controller")
    encoded = NativeActionEncoder(max_actions=64).encode(view.valid_actions)
    assert encoded.action_ids[: view.action_catalog.action_count] == (
        view.action_catalog.action_ids
    )
    assert "decision_semantic" not in view.to_record()


def test_t5_training_decision_context_does_not_migrate_to_core(tmp_path: Path) -> None:
    match = _match(tmp_path)
    try:
        session = NativeDecisionSession(
            match,
            episode_id="phase3-training-compatibility",
            run_namespace="phase3-test",
            seed=42,
        )
        session.reset_result()
        context = session.decision_context("red")
        assert context.decision_semantic is None
        assert context.action_catalog is None
    finally:
        match.close()


class _CoreOnlyAgent:
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.calls: list[tuple[object, object, AgentStepContext]] = []

    def reset(self, context) -> None:
        del context

    def act_decision(
        self, *, decision_semantic, action_catalog, context,
    ):
        self.calls.append((decision_semantic, action_catalog, context))
        return None

    def close(self) -> None:
        return None


def test_a10_classic_agent_has_versioned_decision_core_path(tmp_path: Path) -> None:
    scenario = load_scenario(SCENARIO)
    environment = SourceCompatibleEnvironment(
        GameSession(NativeEngineBackend(scenario)), template=_template(scenario)
    )
    red = _CoreOnlyAgent("core-red")
    blue = _CoreOnlyAgent("core-blue")
    runner = LocalAgentMatchRunner(
        environment=environment, red_agent=red, blue_agent=blue
    )
    try:
        environment.setup_views(42)
        runner._reset_agents(42)  # noqa: SLF001
        assert runner._joint_actions(step_index=0) == ()  # noqa: SLF001
        for agent in (red, blue):
            assert len(agent.calls) == 1
            decision, catalog, _ = agent.calls[0]
            assert decision.action_space == catalog.reference()
    finally:
        environment.session.close()


def test_a10_realtime_agent_receives_frozen_decision_core(tmp_path: Path) -> None:
    environment, _clock = _realtime_environment(tmp_path)
    try:
        reset = environment.reset(seed=42)
        assert reset.red.decision_semantic is not None
        assert reset.red.action_catalog is not None
        context = environment.decision_context(reset.red_binding)
        assert context.decision_semantic == reset.red.decision_semantic
        assert context.action_catalog == reset.red.action_catalog
        assert context.action_catalog is not reset.red.action_catalog
    finally:
        environment.close()
