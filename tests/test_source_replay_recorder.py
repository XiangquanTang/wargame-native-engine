from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.run_minimal_source_replay import run_acceptance
from wargame_native_engine.io.importers.historical_replay import (
    EvidenceLevel, HistoricalReplayImporter,
)
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.replay import SourceFrameRecorder


@pytest.fixture(scope="module")
def acceptance(tmp_path_factory):
    root = tmp_path_factory.mktemp("source-replay-acceptance")
    report = run_acceptance(root, seed=42)
    return root, report


def test_setup_writes_green_zero_frame_with_empty_action_timing(acceptance) -> None:
    root, _ = acceptance
    frame = json.loads((root / "green_frames/0.json").read_text(encoding="utf-8"))
    assert frame["extra"]["native"]["perspective"] == "GREEN"
    assert frame["actions"] == [] and frame["judge_info"] == []


def test_each_step_adds_one_continuous_numbered_frame(acceptance) -> None:
    root, report = acceptance
    frames = sorted((root / "green_frames").glob("*.json"),
                    key=lambda path: int(path.stem))
    assert [int(path.stem) for path in frames] == list(range(report["frame_count"]))
    assert report["frame_count"] == report["step_count"] + 1


def test_only_numbered_green_frames_are_json_zip_members(acceptance) -> None:
    root, report = acceptance
    with ZipFile(root / "replay.zip") as archive:
        names = archive.namelist()
        json_names = [name for name in names if name.endswith(".json")]
        assert json_names == [f"{index}.json"
                              for index in range(report["frame_count"])]
        assert "steps.jsonl" in names
        assert "replay_manifest.native.jsonl" in names
        assert not any("RED" in name or "BLUE" in name for name in names)


def test_actions_and_judge_info_follow_current_frame_timing(acceptance) -> None:
    root, _ = acceptance
    frames = [json.loads(path.read_text(encoding="utf-8"))
              for path in sorted((root / "green_frames").glob("*.json"),
                                 key=lambda path: int(path.stem))]
    steps = [json.loads(line) for line in
             (root / "green_frames/steps.jsonl").read_text(
                 encoding="utf-8").splitlines()]
    for frame, step in zip(frames[1:], steps):
        assert frame["actions"] == step["ordered_actions"]
    assert any(frame["judge_info"] for frame in frames)
    assert any(frame["actions"] and all(
        action["action_type"] != "SHOOT" for action in frame["actions"])
               and frame["judge_info"] == [] for frame in frames)


def test_manifest_jsonl_contains_provenance_hashes_and_result(acceptance) -> None:
    root, report = acceptance
    path = root / "green_frames/replay_manifest.native.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    manifest = json.loads(lines[0])
    assert manifest["perspective"] == "GREEN"
    assert manifest["frame_count"] == report["frame_count"]
    assert manifest["step_count"] == report["step_count"]
    assert manifest["winner"] == "red"
    for key in ("ruleset_hash", "adjudication_table_set_hash",
                "movement_policy_hash", "perspective_policy_hash",
                "steps_sha256", "frames_merkle_root",
                "deterministic_content_hash"):
        assert len(manifest[key]) == 64


def test_native_zip_imports_without_warnings_and_stable_sorting(acceptance) -> None:
    root, report = acceptance
    archive = SourceReplayArchiveImporter().import_path(root / "replay.zip")
    assert archive.warnings == ()
    assert len(archive.frames) == report["frame_count"]
    assert archive.frame_names == tuple(
        f"{index}.json" for index in range(report["frame_count"]))


def test_native_zip_rejects_tampered_numbered_frame(tmp_path: Path,
                                                    acceptance) -> None:
    root, _ = acceptance
    tampered = tmp_path / "tampered.zip"
    with ZipFile(root / "replay.zip") as source, ZipFile(tampered, "w") as target:
        for name in source.namelist():
            payload = source.read(name)
            if name == "1.json":
                value = json.loads(payload)
                value["time"]["cur_step"] += 1
                payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            target.writestr(name, payload)
    with pytest.raises(ValueError, match="frame hash"):
        SourceReplayArchiveImporter().import_path(tampered)


def test_historical_importer_reads_native_numbered_zip_without_warnings(
        acceptance) -> None:
    root, report = acceptance
    result = HistoricalReplayImporter().import_path(root / "replay.zip")
    assert result.replay.warnings == ()
    assert len(result.replay.frames) == report["frame_count"]
    assert result.replay.evidence_level is EvidenceLevel.ACTION_LEVEL


def test_historical_numbered_zip_without_native_sidecars_still_imports(
        tmp_path: Path, acceptance) -> None:
    root, _ = acceptance
    historical = tmp_path / "historical-small.zip"
    first = json.loads((root / "green_frames/0.json").read_text(encoding="utf-8"))
    second = json.loads((root / "green_frames/1.json").read_text(encoding="utf-8"))
    with ZipFile(historical, "w") as archive:
        archive.writestr("96.json", json.dumps(first, ensure_ascii=False))
        archive.writestr("97.json", json.dumps(second, ensure_ascii=False))
    imported = SourceReplayArchiveImporter().import_path(historical)
    assert imported.frame_names == ("96.json", "97.json")
    assert imported.steps == () and imported.warnings == ()
    historical_result = HistoricalReplayImporter().import_path(historical)
    assert historical_result.replay.evidence_level is EvidenceLevel.STATE_TRAJECTORY_LEVEL
    assert historical_result.replay.warnings == ()


def test_native_source_steps_strictly_replay_final_hash(acceptance) -> None:
    root, report = acceptance
    verification = replay_source_archive(
        load_scenario("examples/source_movement_scenario.json"),
        root / "replay.zip", strict=True)
    assert verification.matches
    assert verification.expected_world_hash == report["expected_final_hash"]
    assert verification.actual_world_hash == report["actual_final_hash"]


def test_recorder_is_observer_and_does_not_change_engine_trace(acceptance) -> None:
    _, report = acceptance
    assert report["recorder_is_observer"] is True
    assert report["actual_final_hash"] == report["recorder_off_final_hash"]


def test_minimal_acceptance_contains_move_observation_shoot_and_terminal(
        acceptance) -> None:
    _, report = acceptance
    assert report["move_actions"] > 0 and report["arrived_hex_changes"] > 0
    assert report["observation_changes"] > 0
    assert report["shoot_actions"] > 0 and report["judge_frames"] > 0
    assert report["terminal"] and not report["truncated"]
    assert report["termination_reason"] == "side_eliminated"
    assert report["winner"] == "red"


@pytest.mark.parametrize(("terminal", "truncated", "winner"), [
    (True, False, "red"), (True, False, "blue"), (True, False, None),
    (False, True, None),
])
def test_recorder_manifest_supports_terminal_truncated_and_nullable_winner(
        tmp_path: Path, terminal: bool, truncated: bool,
        winner: str | None) -> None:
    root = tmp_path / f"case-{terminal}-{truncated}-{winner}"
    recorder = SourceFrameRecorder(root)
    recorder.start({"episode_id": "case"}, started_at="2026-01-01T00:00:00Z")
    frame = _minimal_green()
    recorder.record_initial(frame, initial_state_hash="a" * 64)
    manifest = recorder.finalize(
        {"terminal": terminal, "truncated": truncated, "winner": winner,
         "reason": "test"}, final_state_hash="b" * 64)
    assert manifest["termination"] is terminal
    assert manifest["truncated"] is truncated and manifest["winner"] == winner


def test_recorder_rejects_non_green_frame(tmp_path: Path) -> None:
    recorder = SourceFrameRecorder(tmp_path / "red")
    recorder.start({}, started_at="2026-01-01T00:00:00Z")
    frame = _minimal_green()
    frame["extra"]["native"]["perspective"] = "RED"
    with pytest.raises(ValueError, match="GREEN"):
        recorder.record_initial(frame, initial_state_hash="a" * 64)


def test_repeat_package_for_same_run_is_byte_stable(acceptance, tmp_path: Path) -> None:
    recorder = SourceFrameRecorder(tmp_path / "stable")
    recorder.start({"episode_id": "stable"}, started_at="2026-01-01T00:00:00Z")
    recorder.record_initial(_minimal_green(), initial_state_hash="a" * 64)
    recorder.finalize({"terminal": False, "truncated": True, "winner": None,
                       "reason": "max_ticks"}, final_state_hash="b" * 64)
    first = recorder.package_zip(tmp_path / "first.zip").read_bytes()
    second = recorder.package_zip(tmp_path / "second.zip").read_bytes()
    assert first == second


def _minimal_green() -> dict:
    return {
        "scenario_id": "minimal", "terrain_id": 1,
        "time": {"cur_step": 0, "tick": 1, "max_time": 1,
                 "max_step": 1, "stage": 2},
        "operators": [], "passengers": [], "cities": [], "landmarks": {},
        "jm_points": [], "scores": {}, "valid_actions": {},
        "role_and_grouping_info": {}, "communication": [],
        "actions": [], "judge_info": [],
        "extra": {"native": {"perspective": "GREEN"}},
    }
