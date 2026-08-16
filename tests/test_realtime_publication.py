from __future__ import annotations

import json
from pathlib import Path
from threading import Event, Thread

import pytest

from wargame_native_engine.agents import (
    ManualRealtimeClock,
    RealtimeCancellationReason,
    RealtimeAgentSession,
    RealtimeEpisodePublicationError,
    RealtimeLifecycleState,
    RealtimeShutdownTimeoutError,
    SeatDecision,
)
from wargame_native_engine.data import ScenarioBundle, ScenarioCatalog
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.operations import WargameFacade


SCENARIO = Path("examples/source_movement_scenario.json")


class MatchFactory:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.bundle = ScenarioBundle.from_path(SCENARIO)

    def __call__(self, seed: int, episode_sequence: int):
        app = WargameFacade(
            ScenarioCatalog.from_bundles({
                self.bundle.scenario_identity.scenario_id: self.bundle,
            }),
            storage_root=self.root / f"managed-{episode_sequence}",
        )
        return app.create_match(
            scenario_id=self.bundle.scenario_identity.scenario_id,
            red_controller_id="publication-red",
            blue_controller_id="publication-blue",
            seed=seed,
            decision_interval_ms=1_000,
            frame_interval_ms=1_000,
        )


def _ready_session(tmp_path: Path) -> RealtimeAgentSession:
    value = RealtimeAgentSession(
        MatchFactory(tmp_path / "factory"),
        run_namespace="publication-worker-000",
        tick_interval_ms=1_000,
        clock=ManualRealtimeClock(),
    )
    reset = value.reset(seed=42)
    value.submit(
        reset.red_binding,
        observation_version=reset.red.observation_version,
        decision=SeatDecision(),
    )
    value.advance_tick()
    value.advance_tick()
    return value


def test_publish_and_submit_have_one_lossless_linearization_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wargame_native_engine.agents import realtime_publication as module

    environment = _ready_session(tmp_path)
    output = tmp_path / "episode"
    entered = Event()
    release = Event()
    failure: list[BaseException] = []
    real_publish = module.publish_realtime_episode

    def blocked_publish(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(module, "publish_realtime_episode", blocked_publish)
    binding = environment._bindings["red"]  # noqa: SLF001
    view = environment.latest_view(binding)

    def publish() -> None:
        try:
            environment.publish(output, truncated=True)
        except BaseException as exc:  # pragma: no cover - asserted below
            failure.append(exc)

    thread = Thread(target=publish)
    try:
        thread.start()
        assert entered.wait(2)
        assert environment.lifecycle is RealtimeLifecycleState.PUBLISHING
        with pytest.raises(RealtimeShutdownTimeoutError) as captured:
            environment.stop_realtime(timeout_seconds=0.01)
        assert captured.value.phase == "publication"
        receipt = environment.submit(
            binding,
            observation_version=view.observation_version,
            decision=SeatDecision(),
        )
        assert receipt.accepted is False
        assert receipt.command_sequence is None
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        assert failure == []
        commands = output.joinpath("seat_commands.native.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        assert len(commands) == len(environment.command_events)
        assert environment.pending_command_count == 0
        assert environment.lifecycle is RealtimeLifecycleState.PUBLISHED
    finally:
        release.set()
        thread.join(5)
        environment.close()


def test_submit_that_linearizes_before_publish_is_cancelled_and_persisted(
    tmp_path: Path,
) -> None:
    environment = _ready_session(tmp_path)
    output = tmp_path / "episode"
    try:
        binding = environment._bindings["blue"]  # noqa: SLF001
        view = environment.latest_view(binding)
        receipt = environment.submit(
            binding,
            observation_version=view.observation_version,
            decision=SeatDecision(),
        )
        assert receipt.accepted is True
        publication = environment.publish(output, truncated=True)
        records = [
            json.loads(line)
            for line in publication.command_path.read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        persisted = next(
            value for value in records
            if value["command_sequence"] == receipt.command_sequence
        )
        assert persisted["status"] == "CANCELLED_ON_TRUNCATION"
        assert publication.cancelled_truncation_command_count == 1
    finally:
        environment.close()


def test_publication_summary_counts_runner_limit_cancellation(
    tmp_path: Path,
) -> None:
    environment = _ready_session(tmp_path)
    output = tmp_path / "episode"
    try:
        binding = environment._bindings["red"]  # noqa: SLF001
        view = environment.latest_view(binding)
        receipt = environment.submit(
            binding,
            observation_version=view.observation_version,
            decision=SeatDecision(),
        )
        publication = environment.publish(
            output,
            truncated=True,
            cancellation_reason=RealtimeCancellationReason.RUNNER_LIMIT,
        )
        event = environment.command_events[receipt.command_sequence]
        assert event.status.name == "CANCELLED_ON_RUNNER_LIMIT"
        assert publication.cancelled_runner_limit_command_count == 1
        summary = json.loads(publication.summary_path.read_text(encoding="utf-8"))
        assert summary["cancelled_runner_limit_command_count"] == 1
        assert summary["cancelled_truncation_command_count"] == 0
    finally:
        environment.close()


def test_realtime_package_is_complete_aligned_and_strictly_replayable(
    tmp_path: Path,
) -> None:
    environment = _ready_session(tmp_path)
    output = tmp_path / "episode"
    try:
        publication = environment.publish(output, truncated=True)
        assert sorted(value.name for value in output.iterdir()) == [
            "control_audit.native.jsonl",
            "frame_analysis.native.jsonl",
            "match_summary.native.json",
            "realtime_ticks.native.jsonl",
            "replay.zip",
            "seat_commands.native.jsonl",
        ]
        summary = json.loads(publication.summary_path.read_text(encoding="utf-8"))
        assert summary["tick_count"] == 2
        assert summary["fact_frame_count"] == 3
        assert summary["accepted_command_count"] == 1
        assert summary["applied_command_count"] == 1
        assert summary["superseded_command_count"] == 0
        assert summary["rejected_command_count"] == 0
        assert summary["terminal"] is False
        assert summary["truncated"] is True
        archive = SourceReplayArchiveImporter().import_path(publication.replay_path)
        assert len(archive.frames) == 3
        assert len(publication.analysis_path.read_text(
            encoding="utf-8"
        ).splitlines()) == 3
        match = environment._require_match()  # noqa: SLF001
        assert replay_source_archive(
            match.bundle.scenario,
            publication.replay_path,
            strict=True,
            map_runtime_data=match.bundle.map_runtime_data,
        ).matches is True
    finally:
        environment.close()


@pytest.mark.parametrize(
    ("phase", "attribute"),
    [
        ("command_jsonl", "seat_commands_to_jsonl"),
        ("tick_jsonl", "realtime_ticks_to_jsonl"),
        ("summary", "_write_summary"),
        ("publish", "_publish_staging"),
    ],
)
def test_realtime_publication_failure_exposes_no_partial_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    attribute: str,
) -> None:
    import wargame_native_engine.agents.realtime_publication as module

    environment = _ready_session(tmp_path)
    output = tmp_path / f"failed-{phase}"

    def explode(*args, **kwargs):
        del args, kwargs
        raise RuntimeError(f"injected-{phase}")

    monkeypatch.setattr(module, attribute, explode)
    try:
        binding = environment._bindings["red"]  # noqa: SLF001
        view = environment.latest_view(binding)
        with pytest.raises(
            RealtimeEpisodePublicationError,
            match=f"publication_failed:{phase}",
        ):
            environment.publish(output, truncated=True)
        assert not output.exists()
        assert not tuple(tmp_path.glob(f".{output.name}.realtime.*.tmp"))
        assert environment.lifecycle is RealtimeLifecycleState.FAILED
        receipt = environment.submit(
            binding,
            observation_version=view.observation_version,
            decision=SeatDecision(),
        )
        assert receipt.accepted is False
        assert receipt.command_sequence is None
    finally:
        environment.close()


def test_existing_target_is_rejected_before_match_finalization(
    tmp_path: Path,
) -> None:
    environment = _ready_session(tmp_path)
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "old.txt"
    marker.write_text("old", encoding="utf-8")
    try:
        with pytest.raises(FileExistsError, match="already exists"):
            environment.publish(output, truncated=True)
        assert marker.read_text(encoding="utf-8") == "old"
        assert environment._require_match().status.finalized is False  # noqa: SLF001
    finally:
        environment.close()


def test_replay_packaging_failure_exposes_no_target(tmp_path: Path, monkeypatch) -> None:
    environment = _ready_session(tmp_path)
    output = tmp_path / "failed-replay"
    monkeypatch.setattr(
        "wargame_native_engine.operations.facade.MatchHandle.publish",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("replay-packaging-failed")
        ),
    )
    try:
        with pytest.raises(RealtimeEpisodePublicationError, match="failed:facts"):
            environment.publish(output, truncated=True)
        assert not output.exists()
    finally:
        environment.close()


@pytest.mark.parametrize(
    ("attribute", "phase"),
    [
        ("_verify_offline_analysis", "analysis"),
        ("_verify_ticks", "integrity"),
    ],
)
def test_analysis_or_integrity_failure_exposes_no_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attribute: str,
    phase: str,
) -> None:
    import wargame_native_engine.agents.realtime_publication as module

    environment = _ready_session(tmp_path)
    output = tmp_path / f"failed-{phase}"
    monkeypatch.setattr(
        module,
        attribute,
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError(f"{phase}-failed")
        ),
    )
    try:
        with pytest.raises(
            RealtimeEpisodePublicationError,
            match=f"failed:{phase}",
        ):
            environment.publish(output, truncated=True)
        assert not output.exists()
    finally:
        environment.close()


def test_failed_overwrite_restores_old_target(tmp_path: Path, monkeypatch) -> None:
    import wargame_native_engine.agents.realtime_publication as module

    environment = _ready_session(tmp_path)
    output = tmp_path / "overwrite"
    output.mkdir()
    marker = output / "old.txt"
    marker.write_text("old", encoding="utf-8")
    monkeypatch.setattr(
        module,
        "_publish_staging",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("rename-failure")
        ),
    )
    try:
        with pytest.raises(RealtimeEpisodePublicationError, match="publish"):
            environment.publish(output, truncated=True, overwrite=True)
        assert marker.read_text(encoding="utf-8") == "old"
        assert not tuple(tmp_path.glob(".overwrite.realtime.*.tmp"))
    finally:
        environment.close()
