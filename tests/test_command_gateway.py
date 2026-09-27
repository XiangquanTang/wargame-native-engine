from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.run_minimal_source_replay import _template
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.gateway import (
    CommandGateway,
    CommandGatewayError,
    SessionManager,
)
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import (
    SourceCompatibleEnvironment,
)


SCENARIO_PATH = Path("examples/source_movement_scenario.json")


class EnvironmentFactory:
    def __init__(self) -> None:
        self.environments: list[SourceCompatibleEnvironment] = []

    def __call__(self) -> SourceCompatibleEnvironment:
        scenario = load_scenario(SCENARIO_PATH)
        environment = SourceCompatibleEnvironment(
            GameSession(NativeEngineBackend(scenario)),
            template=_template(scenario),
        )
        original = environment.session.step
        environment.session.step = Mock(  # type: ignore[method-assign]
            wraps=original
        )
        self.environments.append(environment)
        return environment


@pytest.fixture
def gateway(tmp_path: Path):
    factory = EnvironmentFactory()
    manager = SessionManager(factory, storage_root=tmp_path / "managed")
    value = CommandGateway(manager, delta_time_seconds=20)
    return value, factory


def _create(gateway: CommandGateway):
    return gateway.create_session(
        red_controller_id="red-controller",
        blue_controller_id="blue-controller",
        seed=42,
    )


def test_managed_sim_time_does_not_construct_snapshot_or_hash_scenario(
    gateway,
    monkeypatch,
) -> None:
    value, factory = gateway
    descriptor = _create(value)
    session = factory.environments[0].session
    snapshot_spy = Mock(wraps=session.snapshot)
    session.snapshot = snapshot_spy  # type: ignore[method-assign]

    scenario_hash_reads = 0
    original = type(session.scenario).scenario_hash.fget
    assert original is not None

    def counted_scenario_hash(scenario):
        nonlocal scenario_hash_reads
        scenario_hash_reads += 1
        return original(scenario)

    monkeypatch.setattr(
        type(session.scenario),
        "scenario_hash",
        property(counted_scenario_hash),
    )

    assert value.manager.sim_time_ms(descriptor.session_id) == 0
    assert value.manager.descriptor(descriptor.session_id).sim_time_ms == 0
    assert snapshot_spy.call_count == 0
    assert scenario_hash_reads == 0


def test_controller_views_are_identity_bound_detached_and_side_only(
    gateway,
) -> None:
    value, factory = gateway
    descriptor = _create(value)

    red = value.view(descriptor.session_id, "red-controller")
    blue = value.view(descriptor.session_id, "blue-controller")

    assert red.perspective == "RED"
    assert blue.perspective == "BLUE"
    assert red.observation["side"] == "red"
    assert blue.observation["side"] == "blue"
    assert "green" not in red.to_record()
    assert "frame_analysis" not in red.to_record()
    assert red.source_frame["extra"]["native"]["perspective"] == "RED"
    assert blue.source_frame["extra"]["native"]["perspective"] == "BLUE"
    assert "judge_info" not in red.observation
    assert [item["operator_id"] for item in red.observation["friendly_operators"]] == [1]
    assert [item["operator_id"] for item in blue.observation["friendly_operators"]] == [2]
    red.observation["mission"]["mutated"] = True
    assert "mutated" not in value.view(
        descriptor.session_id, "red-controller"
    ).observation["mission"]
    assert factory.environments[0].session.observe("blue").side == "blue"
    with pytest.raises(CommandGatewayError, match="controller_not_bound"):
        value.view(descriptor.session_id, "intruder")


def _controller_view_hash(value) -> str:
    return canonical_hash({
        "legacy": value.to_record(),
        "decision": value.decision_record(),
    })


def test_controller_view_cache_matches_reference_and_is_deeply_detached(
    gateway,
) -> None:
    value, factory = gateway
    session_id = _create(value).session_id
    first = value.view(session_id, "red-controller")
    expected_hash = _controller_view_hash(first)
    authoritative_world = deepcopy(factory.environments[0].session.snapshot()["world"])

    # Omitting a revision for caller-supplied actions deliberately exercises
    # the uncached reference path.
    window = value._gateway_session(session_id).window  # noqa: SLF001
    descriptor = value.manager.descriptor(session_id)
    reference = value.manager.controller_view(
        session_id,
        "red",
        decision_window_index=window.decision_window_index,
        frozen_valid_actions=window.frozen_valid_actions("red"),
        frame_index=descriptor.frame_index,
        sim_time_ms=descriptor.sim_time_ms,
    )
    assert _controller_view_hash(reference) == expected_hash

    first.observation["mission"]["cache_poison"] = True
    first.source_frame["extra"]["native"]["cache_poison"] = True
    first.valid_actions[0]["action"]["parameters"]["cache_poison"] = True
    object.__setattr__(
        first.decision_semantic.snapshot, "frame_index", 999
    )
    object.__setattr__(first.action_catalog.entries[0], "action_id", "poison")

    repeated = value.view(session_id, "red-controller")
    blue = value.view(session_id, "blue-controller")
    stats = value.manager.controller_view_cache_stats(session_id)

    assert _controller_view_hash(repeated) == expected_hash
    assert "cache_poison" not in repeated.observation["mission"]
    assert "cache_poison" not in repeated.source_frame["extra"]["native"]
    assert "cache_poison" not in repeated.valid_actions[0]["action"]["parameters"]
    assert repeated.decision_semantic.snapshot.frame_index == 0
    assert repeated.action_catalog.entries[0].action_id != "poison"
    assert blue.perspective == "BLUE"
    assert factory.environments[0].session.snapshot()["world"] == authoritative_world
    assert stats == {
        "requests": 4,
        "hits": 1,
        "misses": 2,
        "bypasses": 1,
        "builds": 3,
        "entries": 2,
    }


def test_controller_view_cache_tracks_submission_action_revision_and_advance(
    gateway,
) -> None:
    value, _ = gateway
    session_id = _create(value).session_id
    before = value.view(session_id, "red-controller")
    before_hash = _controller_view_hash(before)

    pending = value.submit_batch(
        session_id,
        "red-controller",
        request_id="red-empty",
        expected_window=0,
        ordered_action_ids=(),
    )
    after_submit = value.manager.controller_view_cache_stats(session_id)
    assert pending.decision_window.submitted is True
    assert _controller_view_hash(pending.view) == before_hash
    assert after_submit["hits"] == 1
    assert after_submit["builds"] == 1

    advanced = value.submit_batch(
        session_id,
        "blue-controller",
        request_id="blue-empty",
        expected_window=0,
        ordered_action_ids=(),
    )
    after_advance = value.view(session_id, "red-controller")
    value.view(session_id, "red-controller")
    advanced_stats = value.manager.controller_view_cache_stats(session_id)
    assert advanced.view.frame_index == 1
    assert after_advance.frame_index == 1
    assert after_advance.sim_time_ms == 20_000
    assert advanced_stats["builds"] == 3
    assert advanced_stats["hits"] == 2
    assert advanced_stats["entries"] == 2


def test_controller_view_cache_invalidates_for_new_proposal_and_restore(
    gateway,
) -> None:
    value, factory = gateway
    session_id = _create(value).session_id
    original = value.view(session_id, "red-controller")
    snapshot = deepcopy(factory.environments[0].session.snapshot())

    proposal = value.plan_move(
        session_id,
        "red-controller",
        request_id="cache-proposal",
        expected_window=0,
        unit_id=1,
        destination_hex=105,
    )
    assert proposal.action_id is not None
    with_proposal = value.view(session_id, "red-controller")
    assert proposal.action_id not in {
        item["action_id"] for item in original.valid_actions
    }
    assert proposal.action_id in {
        item["action_id"] for item in with_proposal.valid_actions
    }
    assert value.manager.controller_view_cache_stats(session_id)["builds"] == 2

    # restore_views may install the same tick/time, so frame identity alone is
    # insufficient.  GameSession's monotonic state revision forces a rebuild.
    factory.environments[0].restore_views(snapshot)
    restored = value.view(session_id, "red-controller")
    stats = value.manager.controller_view_cache_stats(session_id)
    assert restored.frame_index == original.frame_index == 0
    assert restored.sim_time_ms == original.sim_time_ms == 0
    assert stats["builds"] == 3
    assert stats["entries"] == 1

    value.close_session(session_id)
    assert session_id not in value.manager._sessions  # noqa: SLF001
    with pytest.raises(CommandGatewayError, match="session_not_found"):
        value.manager.controller_view_cache_stats(session_id)


def test_joint_submission_advances_once_in_red_blue_order_and_is_idempotent(
    gateway,
) -> None:
    value, factory = gateway
    descriptor = _create(value)
    session_id = descriptor.session_id

    red_pending = value.submit(
        session_id,
        "red-controller",
        request_id="red-0",
        expected_step=0,
        valid_action_id=None,
    )
    repeated_red = value.submit(
        session_id,
        "red-controller",
        request_id="red-0",
        expected_step=0,
        valid_action_id=None,
    )
    blue_advance = value.submit(
        session_id,
        "blue-controller",
        request_id="blue-0",
        expected_step=0,
        valid_action_id=None,
    )
    repeated_blue = value.submit(
        session_id,
        "blue-controller",
        request_id="blue-0",
        expected_step=0,
        valid_action_id=None,
    )

    assert red_pending == repeated_red
    assert red_pending.advanced is False
    assert red_pending.pending_perspectives == ("BLUE",)
    assert blue_advance == repeated_blue
    assert blue_advance.advanced is True
    assert blue_advance.view.step_index == 1
    assert factory.environments[0].session.step.call_count == 1  # type: ignore[attr-defined]
    assert factory.environments[0].last_step_record["ordered_actions"] == []
    repeated_red.view.observation["mission"]["caller_mutation"] = True
    detached_retry = value.submit(
        session_id,
        "red-controller",
        request_id="red-0",
        expected_step=0,
        valid_action_id=None,
    )
    assert "caller_mutation" not in detached_retry.view.observation["mission"]


def test_stale_step_request_conflict_and_side_reuse_are_rejected(
    gateway,
) -> None:
    value, _ = gateway
    session_id = _create(value).session_id
    value.submit(
        session_id, "red-controller",
        request_id="red", expected_step=0, valid_action_id=None,
    )
    with pytest.raises(CommandGatewayError, match="request_id_conflict"):
        value.submit(
            session_id, "red-controller",
            request_id="red", expected_step=0, valid_action_id="different",
        )
    with pytest.raises(CommandGatewayError, match="side_already_submitted"):
        value.submit(
            session_id, "red-controller",
            request_id="red-second", expected_step=0, valid_action_id=None,
        )
    value.submit(
        session_id, "blue-controller",
        request_id="blue", expected_step=0, valid_action_id=None,
    )
    with pytest.raises(CommandGatewayError, match="stale_expected_step"):
        value.submit(
            session_id, "red-controller",
            request_id="stale", expected_step=0, valid_action_id=None,
        )


def test_valid_action_identity_is_resolved_only_for_bound_side(gateway) -> None:
    value, factory = gateway
    session_id = _create(value).session_id
    blue_action = value.view(
        session_id, "blue-controller"
    ).valid_actions[0]["action_id"]

    with pytest.raises(CommandGatewayError, match="not a current RED action"):
        value.submit(
            session_id,
            "red-controller",
            request_id="wrong-side",
            expected_step=0,
            valid_action_id=blue_action,
        )
    assert factory.environments[0].session.step.call_count == 0  # type: ignore[attr-defined]


def test_concurrent_duplicate_side_submission_cannot_double_write(gateway) -> None:
    value, factory = gateway
    session_id = _create(value).session_id

    def submit(request_id: str):
        try:
            return value.submit(
                session_id,
                "red-controller",
                request_id=request_id,
                expected_step=0,
                valid_action_id=None,
            )
        except CommandGatewayError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(submit, ("one", "two")))

    assert sum(getattr(item, "accepted", False) for item in results) == 1
    assert "side_already_submitted" in results
    assert factory.environments[0].session.step.call_count == 0  # type: ignore[attr-defined]


def test_sessions_isolate_state_seed_recorder_and_step_index(gateway) -> None:
    value, factory = gateway
    first = _create(value)
    second = value.create_session(
        red_controller_id="red-two",
        blue_controller_id="blue-two",
        seed=43,
    )

    value.submit(
        first.session_id, "red-controller",
        request_id="r", expected_step=0, valid_action_id=None,
    )
    value.submit(
        first.session_id, "blue-controller",
        request_id="b", expected_step=0, valid_action_id=None,
    )

    assert value.view(first.session_id, "red-controller").step_index == 1
    assert value.view(second.session_id, "red-two").step_index == 0
    assert factory.environments[0].session.seed == 42
    assert factory.environments[1].session.seed == 43
    assert factory.environments[1].session.step.call_count == 0  # type: ignore[attr-defined]


def test_truncated_publication_uses_recorder_and_live_analysis(gateway, tmp_path: Path) -> None:
    value, _ = gateway
    session_id = _create(value).session_id
    value.submit(
        session_id, "red-controller",
        request_id="r", expected_step=0, valid_action_id=None,
    )
    value.submit(
        session_id, "blue-controller",
        request_id="b", expected_step=0, valid_action_id=None,
    )
    output = tmp_path / "published"

    publication = value.publish_session(
        session_id,
        "red-controller",
        str(output),
        truncated=True,
    )
    archive = SourceReplayArchiveImporter().import_path(publication.replay_path)

    assert publication.frame_count == 2
    assert publication.step_count == 1
    assert publication.terminal is False
    assert publication.truncated is True
    assert archive.frame_names == ("0.json", "1.json")
    assert len(publication.analysis_path.read_text(encoding="utf-8").splitlines()) == 2
    assert archive.manifest["truncated"] is True
    assert "GREEN" not in value.view(session_id, "red-controller").to_record()
    assert value.manager.descriptor(session_id).finalized is True
    with pytest.raises(CommandGatewayError, match="session_finalized"):
        value.submit(
            session_id, "red-controller",
            request_id="after-publication", expected_step=1,
            valid_action_id=None,
        )
