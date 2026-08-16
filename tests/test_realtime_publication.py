from __future__ import annotations

import json
from pathlib import Path

import pytest

from wargame_native_engine.agents import (
    ManualRealtimeClock,
    RealtimeAgentSession,
    RealtimeEpisodePublicationError,
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
        with pytest.raises(
            RealtimeEpisodePublicationError,
            match=f"publication_failed:{phase}",
        ):
            environment.publish(output, truncated=True)
        assert not output.exists()
        assert not tuple(tmp_path.glob(f".{output.name}.realtime.*.tmp"))
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
