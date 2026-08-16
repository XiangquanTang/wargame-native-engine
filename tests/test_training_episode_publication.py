from __future__ import annotations

import json
from pathlib import Path

import pytest

import wargame_native_engine.training.publication as publication_module
from wargame_native_engine.agents import SeatDecision
from wargame_native_engine.data import ScenarioBundle, ScenarioCatalog
from wargame_native_engine.operations import WargameFacade
from wargame_native_engine.operations.facade import MatchHandle
from wargame_native_engine.training import (
    NativeMultiSeatEnv,
    TrainingEpisodePublicationError,
)


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


def _completed_environment(root: Path) -> NativeMultiSeatEnv:
    environment = NativeMultiSeatEnv(
        MatchFactory(root),
        run_namespace="publication-worker-000",
        max_decision_windows=1,
    )
    environment.reset(42)
    environment.step(SeatDecision(), SeatDecision())
    return environment


def test_complete_training_package_appears_only_with_all_verified_members(
    tmp_path: Path,
) -> None:
    environment = _completed_environment(tmp_path / "managed")
    target = tmp_path / "episode"
    try:
        result = environment.publish_training_package(
            target,
            summary_schema="test-training-package/1.0",
            summary_extra={"agent_profile": "wait-v1"},
        )
        assert result.output_dir == target
        assert {path.name for path in target.iterdir()} == {
            "replay.zip",
            "frame_analysis.native.jsonl",
            "decision_transitions.native.jsonl",
            "match_summary.native.json",
            "control_audit.native.jsonl",
        }
        summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
        transitions = [
            json.loads(line)
            for line in result.transition_path.read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        assert summary["run_namespace"] == "publication-worker-000"
        assert summary["episode_id"] == result.episode_id
        assert summary["decision_window_count"] == 1
        assert summary["transition_count"] == 2
        assert summary["terminal"] is False
        assert summary["truncated"] is True
        assert summary["agent_profile"] == "wait-v1"
        assert [item["side"] for item in transitions] == ["red", "blue"]
        assert all(
            item["run_namespace"] == "publication-worker-000"
            and item["episode_id"] == result.episode_id
            for item in transitions
        )
        for name in (
            "frame_analysis.native.jsonl",
            "decision_transitions.native.jsonl",
            "control_audit.native.jsonl",
        ):
            payload = (target / name).read_bytes()
            assert payload.endswith(b"\n")
            assert not payload.endswith(b"\n\n")
            assert all(line.strip() for line in payload.splitlines())
        assert not list(tmp_path.glob(".episode.training.*"))
    finally:
        environment.close()


@pytest.mark.parametrize(
    ("phase", "patch_name"),
    [
        ("transitions", "write_decision_transitions_jsonl"),
        ("verification", "_verify_offline_analysis"),
        ("summary", "_write_summary"),
        ("integrity", "_verify_summary"),
        ("publish", "_publish_staging"),
    ],
)
def test_failure_after_fact_finalization_exposes_no_partial_new_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    patch_name: str,
) -> None:
    environment = _completed_environment(tmp_path / "managed")
    target = tmp_path / "episode"

    def fail(*args, **kwargs):
        del args, kwargs
        raise RuntimeError(f"injected-{phase}")

    monkeypatch.setattr(publication_module, patch_name, fail)
    try:
        with pytest.raises(TrainingEpisodePublicationError) as captured:
            environment.publish_training_package(
                target, summary_schema="test-training-package/1.0"
            )
        assert captured.value.phase == phase
        assert not target.exists()
        assert not list(tmp_path.glob(".episode.training.*"))
    finally:
        environment.close()


def test_replay_packaging_failure_exposes_no_partial_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _completed_environment(tmp_path / "managed")
    target = tmp_path / "episode"

    def fail_publish(self, *args, **kwargs):
        del self, args, kwargs
        raise OSError("injected replay packaging failure")

    monkeypatch.setattr(MatchHandle, "publish", fail_publish)
    try:
        with pytest.raises(
            TrainingEpisodePublicationError, match="publication_failed:facts"
        ):
            environment.publish_training_package(
                target, summary_schema="test-training-package/1.0"
            )
        assert not target.exists()
        assert not list(tmp_path.glob(".episode.training.*"))
    finally:
        environment.close()


def test_transition_target_conflict_is_a_failed_complete_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _completed_environment(tmp_path / "managed")
    target = tmp_path / "episode"

    def conflict(*args, **kwargs):
        del args, kwargs
        raise FileExistsError("injected transition target conflict")

    monkeypatch.setattr(
        publication_module, "write_decision_transitions_jsonl", conflict
    )
    try:
        with pytest.raises(TrainingEpisodePublicationError) as captured:
            environment.publish_training_package(
                target, summary_schema="test-training-package/1.0"
            )
        assert captured.value.phase == "transitions"
        assert not target.exists()
        assert not list(tmp_path.glob(".episode.training.*"))
    finally:
        environment.close()


@pytest.mark.parametrize("patch_name", ["_write_summary", "_publish_staging"])
def test_failed_overwrite_preserves_existing_episode_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patch_name: str,
) -> None:
    target = tmp_path / "episode"
    target.mkdir()
    marker = target / "old-sample.txt"
    marker.write_bytes(b"old-sample\n")
    environment = _completed_environment(tmp_path / "managed")

    def fail(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("injected overwrite failure")

    monkeypatch.setattr(publication_module, patch_name, fail)
    try:
        with pytest.raises(TrainingEpisodePublicationError):
            environment.publish_training_package(
                target,
                summary_schema="test-training-package/1.0",
                overwrite=True,
            )
        assert marker.read_bytes() == b"old-sample\n"
        assert {path.name for path in target.iterdir()} == {"old-sample.txt"}
        assert not list(tmp_path.glob(".episode.training.*"))
    finally:
        environment.close()


def test_existing_episode_is_rejected_before_session_finalization(
    tmp_path: Path,
) -> None:
    target = tmp_path / "episode"
    target.mkdir()
    (target / "old").write_text("old", encoding="utf-8")
    environment = _completed_environment(tmp_path / "managed")
    try:
        with pytest.raises(FileExistsError, match="already exists"):
            environment.publish_training_package(
                target, summary_schema="test-training-package/1.0"
            )
        assert (target / "old").read_text(encoding="utf-8") == "old"
        assert environment.match.status.finalized is False
    finally:
        environment.close()


def test_verified_overwrite_replaces_old_target_only_at_final_publish(
    tmp_path: Path,
) -> None:
    target = tmp_path / "episode"
    target.mkdir()
    (target / "old").write_text("old", encoding="utf-8")
    environment = _completed_environment(tmp_path / "managed")
    try:
        result = environment.publish_training_package(
            target,
            summary_schema="test-training-package/1.0",
            overwrite=True,
        )
        assert result.output_dir == target
        assert not (target / "old").exists()
        assert result.replay_path.is_file()
        assert result.transition_path.is_file()
        assert result.summary_path.is_file()
        assert not list(tmp_path.glob(".episode.previous.*"))
    finally:
        environment.close()
