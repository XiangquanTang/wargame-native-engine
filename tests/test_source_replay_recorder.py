from __future__ import annotations

import json
import hashlib
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
from wargame_native_engine.io.replay import (
    UnsupportedReplaySemanticsError,
    replay_source_archive,
)
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.compat.source_frame.serialization import (
    canonical_source_json,
)
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
    assert manifest["adjudication_semantics_version"] == (
        "native-joint-prestate/1.1")
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


def test_source_strict_replay_rejects_incompatible_semantics_before_step(
    tmp_path: Path, acceptance, monkeypatch,
) -> None:
    root, _ = acceptance
    output = tmp_path / "incompatible-semantics.zip"

    def mutate(values):
        manifest = _manifest(values)
        manifest["adjudication_semantics_version"] = "ordered-sequential/0"
        values["replay_manifest.native.jsonl"] = (
            canonical_source_json(manifest) + "\n").encode()

    _rewrite_native_zip(root / "replay.zip", output, mutate)

    def stepped(*args, **kwargs):
        raise AssertionError("source environment must not step")

    monkeypatch.setattr(
        "wargame_native_engine.session.source_environment."
        "SourceCompatibleEnvironment.step_views_ms",
        stepped,
    )
    with pytest.raises(UnsupportedReplaySemanticsError, match="ordered-sequential"):
        replay_source_archive(
            load_scenario("examples/source_movement_scenario.json"),
            output,
            strict=True,
        )


def test_strict_replay_rejects_self_consistent_tampered_intermediate_frame(
    tmp_path: Path,
    acceptance,
) -> None:
    root, _ = acceptance
    output = tmp_path / "self-consistent-frame-tamper.zip"

    def mutate(values):
        frame = json.loads(values["1.json"])
        frame["operators"][0]["blood"] = max(
            0, int(frame["operators"][0]["blood"]) - 1
        )
        values["1.json"] = (canonical_source_json(frame) + "\n").encode()

    _rewrite_native_zip(root / "replay.zip", output, mutate)
    SourceReplayArchiveImporter().import_path(output)
    with pytest.raises(ValueError, match="GREEN frame hash mismatch"):
        replay_source_archive(
            load_scenario("examples/source_movement_scenario.json"),
            output,
            strict=True,
        )


def test_strict_replay_rejects_terminal_step_and_frame_suffix(
    tmp_path: Path,
    acceptance,
) -> None:
    root, _ = acceptance
    output = tmp_path / "terminal-suffix.zip"

    def mutate(values):
        manifest = _manifest(values)
        final_index = int(manifest["frame_count"]) - 1
        suffix_index = final_index + 1
        values[f"{suffix_index}.json"] = values[f"{final_index}.json"]
        steps = values["steps.jsonl"].decode().splitlines()
        previous = json.loads(steps[-1])
        previous.update({
            "step_index": suffix_index,
            "tick_before": previous["tick_after"],
            "ordered_actions": [],
            "action_sequences": [],
        })
        steps.append(canonical_source_json(previous))
        values["steps.jsonl"] = ("\n".join(steps) + "\n").encode()

    _rewrite_native_zip(root / "replay.zip", output, mutate)
    SourceReplayArchiveImporter().import_path(output)
    with pytest.raises(ValueError, match="step after terminal"):
        replay_source_archive(
            load_scenario("examples/source_movement_scenario.json"),
            output,
            strict=True,
        )


@pytest.mark.parametrize("member", [
    "0.json",
    "steps.jsonl",
    "replay_manifest.native.jsonl",
])
def test_source_archive_rejects_duplicate_zip_members(
    tmp_path: Path,
    acceptance,
    member: str,
) -> None:
    root, _ = acceptance
    output = tmp_path / f"duplicate-{member.replace('.', '-')}.zip"
    with ZipFile(root / "replay.zip") as source:
        values = [(info.filename, source.read(info)) for info in source.infolist()]
        duplicate_payload = source.read(member)
    with ZipFile(output, "w") as target:
        for name, payload in values:
            target.writestr(name, payload)
        with pytest.warns(UserWarning, match="Duplicate name"):
            target.writestr(member, duplicate_payload)
    with pytest.raises(ValueError, match="duplicate ZIP member"):
        SourceReplayArchiveImporter().import_path(output)


@pytest.mark.parametrize("member", ["../0.json", "/0.json", "nested/0.json"])
def test_source_archive_rejects_unsafe_or_nested_member_paths(
    tmp_path: Path,
    member: str,
) -> None:
    output = tmp_path / "unsafe.zip"
    with ZipFile(output, "w") as archive:
        archive.writestr(member, b"{}")
    with pytest.raises(ValueError, match="ZIP member|top-level"):
        SourceReplayArchiveImporter().import_path(output)


def test_source_archive_rejects_file_directory_conflict(tmp_path: Path) -> None:
    output = tmp_path / "conflict.zip"
    with ZipFile(output, "w") as archive:
        archive.writestr("0.json/", b"")
        archive.writestr("0.json", b"{}")
    with pytest.raises(ValueError, match="file/directory conflict"):
        SourceReplayArchiveImporter().import_path(output)


def test_source_archive_rejects_multiple_manifest_records(
    tmp_path: Path,
    acceptance,
) -> None:
    root, _ = acceptance
    output = tmp_path / "two-manifests.zip"
    with ZipFile(root / "replay.zip") as source, ZipFile(output, "w") as target:
        for info in source.infolist():
            payload = source.read(info)
            if info.filename == "replay_manifest.native.jsonl":
                payload += payload
            target.writestr(info, payload)
    with pytest.raises(ValueError, match="exactly one"):
        SourceReplayArchiveImporter().import_path(output)


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


def test_strict_replay_rejects_self_consistent_sim_time_tampering(
    acceptance,
    tmp_path: Path,
) -> None:
    root, _ = acceptance
    output = tmp_path / "tampered-sim-time.zip"

    def mutate(values):
        steps = [
            json.loads(line)
            for line in values["steps.jsonl"].decode().splitlines()
        ]
        assert "sim_time_ms" in steps[0]
        steps[0]["sim_time_ms"] += 1
        values["steps.jsonl"] = (
            "\n".join(canonical_source_json(step) for step in steps) + "\n"
        ).encode()

    _rewrite_native_zip(root / "replay.zip", output, mutate)
    SourceReplayArchiveImporter().import_path(output)
    with pytest.raises(ValueError, match="sim_time_ms mismatch at step 1"):
        replay_source_archive(
            load_scenario("examples/source_movement_scenario.json"),
            output,
            strict=True,
        )


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


def _manifest(values: dict[str, bytes]) -> dict:
    return json.loads(values["replay_manifest.native.jsonl"].decode())


def _rewrite_native_zip(source: Path, output: Path, mutate) -> None:
    with ZipFile(source) as archive:
        values = {info.filename: archive.read(info) for info in archive.infolist()}
    mutate(values)
    manifest = _manifest(values)
    frame_names = sorted(
        (name for name in values if name.endswith(".json") and name[:-5].isdigit()),
        key=lambda name: int(name[:-5]),
    )
    hashes = [hashlib.sha256(values[name]).hexdigest() for name in frame_names]
    manifest.update({
        "frame_count": len(frame_names),
        "step_count": len(values["steps.jsonl"].decode().splitlines()),
        "frame_hashes": hashes,
        "frames_merkle_root": canonical_hash(hashes),
        "steps_sha256": hashlib.sha256(values["steps.jsonl"]).hexdigest(),
    })
    deterministic = {
        key: value for key, value in manifest.items()
        if key not in {"started_at", "deterministic_content_hash"}
    }
    manifest["deterministic_content_hash"] = canonical_hash(deterministic)
    values["replay_manifest.native.jsonl"] = (
        canonical_source_json(manifest) + "\n"
    ).encode()
    with ZipFile(output, "w") as archive:
        for name in sorted(values):
            archive.writestr(name, values[name])
