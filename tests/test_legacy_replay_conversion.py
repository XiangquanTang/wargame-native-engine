from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from scripts.convert_legacy_replay import main as convert_main
from wargame_native_engine.analysis import EightFactorAnalyzer
from wargame_native_engine.analysis.legacy_replay_adapter import (
    load_legacy_serialized_replay,
)
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)
from wargame_native_engine.replay import convert_legacy_replay
import wargame_native_engine.replay.legacy_conversion as conversion_module


def test_conversion_publishes_continuous_playback_and_aligned_analysis(
    tmp_path: Path,
) -> None:
    source = _legacy_archive(tmp_path / "legacy.zip", [
        ("session/4.json", _frame(3, "last")),
        ("session/1.json", _frame(0, "initial", padding="x" * 20_000)),
        ("session/3.json", _frame(1, "middle")),
        ("session/2.json", _frame(0, "deployed")),
    ])
    source_before = source.read_bytes()
    selected = load_legacy_serialized_replay(source)
    output = tmp_path / "converted" / "game"
    analyzer = EightFactorAnalyzer(
        model_id="conversion-model",
        model_version="7.0",
        model_hash="model-hash",
    )

    result = convert_legacy_replay(source, output, analyzer=analyzer)

    assert {item.name for item in output.iterdir()} == {
        "replay.zip",
        "frame_analysis.native.jsonl",
    }
    assert result.input_path == source
    assert result.output_dir == output
    assert result.raw_record_count == 4
    assert result.selected_frame_count == 3
    assert result.collapsed_record_count == 1
    assert result.selected_member_names == (
        "session/2.json",
        "session/3.json",
        "session/4.json",
    )
    assert result.strict_replay_supported is False
    assert result.playback_compatibility == "source-numbered-frame-playback/1.0"
    assert result.source_archive_sha256 == hashlib.sha256(source_before).hexdigest()
    assert result.replay_zip_sha256 == _sha256(result.replay_zip_path)

    with ZipFile(result.replay_zip_path) as archive:
        assert archive.namelist() == ["0.json", "1.json", "2.json"]
        assert "steps.jsonl" not in archive.namelist()
        assert "replay_manifest.native.jsonl" not in archive.namelist()
        assert "frame_analysis.native.jsonl" not in archive.namelist()
        payloads = [archive.read(f"{index}.json") for index in range(3)]
        converted_frames = [json.loads(payload) for payload in payloads]
        for info in archive.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.compress_type == ZIP_DEFLATED
            assert info.external_attr >> 16 == 0o100644

    assert converted_frames == [item.frame for item in selected.inputs]
    assert [frame["marker"] for frame in converted_frames] == [
        "deployed",
        "middle",
        "last",
    ]
    assert [frame["time"]["cur_step"] for frame in converted_frames] == [0, 1, 3]
    assert converted_frames[0]["facts"] == {"nested": [True, None, 0.0]}

    records = _records(result.analysis_jsonl_path)
    assert len(records) == len(payloads) == 3
    for frame_index, (record, payload) in enumerate(zip(records, payloads)):
        provenance = record["provenance"]
        assert provenance["frame_index"] == frame_index
        assert provenance["frame_hash"] == hashlib.sha256(payload).hexdigest()
        assert provenance["scenario_hash"] is None
        assert provenance["model_id"] == analyzer.model_id
        assert provenance["model_version"] == analyzer.model_version
        assert provenance["model_hash"] == analyzer.model_hash
    assert result.analysis_jsonl_path.read_bytes().endswith(b"\n")

    imported = SourceReplayArchiveImporter().import_path(result.replay_zip_path)
    assert imported.frame_names == ("0.json", "1.json", "2.json")
    assert [frame.time.cur_step for frame in imported.frames] == [0, 1, 3]
    assert imported.steps == ()
    assert imported.manifest == {}
    analysis_by_index = {
        record["provenance"]["frame_index"]: record for record in records
    }
    assert [analysis_by_index[index]["provenance"]["frame_hash"]
            for index in range(3)] == [
        hashlib.sha256(payload).hexdigest() for payload in payloads
    ]
    assert source.read_bytes() == source_before


def test_conversion_is_deterministic_protects_existing_and_overwrites_as_bundle(
    tmp_path: Path,
) -> None:
    source = _legacy_archive(tmp_path / "legacy.zip", [
        ("session/1", _frame(0, "initial")),
        ("session/2", _frame(0, "final")),
        ("session/3", _frame(2, "gap")),
    ])
    first = tmp_path / "first"
    second = tmp_path / "second"

    first_result = convert_legacy_replay(source, first)
    second_result = convert_legacy_replay(source, second)
    replay_bytes = first_result.replay_zip_path.read_bytes()
    analysis_bytes = first_result.analysis_jsonl_path.read_bytes()
    assert second_result.replay_zip_path.read_bytes() == replay_bytes
    assert second_result.analysis_jsonl_path.read_bytes() == analysis_bytes

    with pytest.raises(FileExistsError, match="already exists"):
        convert_legacy_replay(source, first)
    assert first_result.replay_zip_path.read_bytes() == replay_bytes
    assert first_result.analysis_jsonl_path.read_bytes() == analysis_bytes

    (first / "stale.txt").write_bytes(b"stale")
    repeated = convert_legacy_replay(source, first, overwrite=True)
    assert not (first / "stale.txt").exists()
    assert repeated.replay_zip_path.read_bytes() == replay_bytes
    assert repeated.analysis_jsonl_path.read_bytes() == analysis_bytes
    assert list(tmp_path.glob(".first.*.tmp")) == []
    assert list(tmp_path.glob(".first.*.backup")) == []


def test_conversion_preserves_missing_legacy_time_metadata_as_unknown(
    tmp_path: Path,
) -> None:
    first = _frame(0, "initial")
    second = _frame(1, "next")
    for frame in (first, second):
        del frame["time"]["max_time"]
        del frame["time"]["max_step"]
        del frame["time"]["stage"]
    source = _legacy_archive(tmp_path / "legacy-minimal-time.zip", [
        ("session/1.json", first),
        ("session/2.json", second),
    ])

    result = convert_legacy_replay(source, tmp_path / "converted")

    with ZipFile(result.replay_zip_path) as archive:
        values = [json.loads(archive.read(f"{index}.json")) for index in range(2)]
    assert [value["time"] for value in values] == [
        {"cur_step": 0, "tick": 0},
        {"cur_step": 1, "tick": 1},
    ]
    imported = SourceReplayArchiveImporter().import_path(result.replay_zip_path)
    assert imported.frame_names == ("0.json", "1.json")
    assert all(frame.time.max_time is None for frame in imported.frames)
    assert all(frame.time.max_step is None for frame in imported.frames)
    assert all(frame.time.stage is None for frame in imported.frames)


@pytest.mark.parametrize("existing", [False, True])
def test_analysis_failure_never_publishes_partial_bundle(
    tmp_path: Path,
    existing: bool,
) -> None:
    source = _legacy_archive(tmp_path / "legacy.zip", [
        ("session/1.json", _frame(0, "frame")),
    ])
    target = tmp_path / "converted"
    old_tree = {"old.txt": b"old", "replay.zip": b"old-replay"}
    if existing:
        target.mkdir()
        for name, payload in old_tree.items():
            (target / name).write_bytes(payload)

    class FailingAnalyzer:
        def analyze_green_frames(self, inputs):
            raise RuntimeError("injected analysis failure")

    with pytest.raises(RuntimeError, match="injected analysis failure"):
        convert_legacy_replay(
            source,
            target,
            analyzer=FailingAnalyzer(),  # type: ignore[arg-type]
            overwrite=existing,
        )

    if existing:
        assert _flat_tree(target) == old_tree
    else:
        assert not target.exists()
    assert list(tmp_path.glob(".converted.*.tmp")) == []
    assert list(tmp_path.glob(".converted.*.backup")) == []


def test_post_zip_validation_failure_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _legacy_archive(tmp_path / "legacy.zip", [
        ("session/1.json", _frame(0, "frame")),
    ])
    target = tmp_path / "converted"

    def fail_import(self, path):
        raise RuntimeError("injected playback validation failure")

    monkeypatch.setattr(
        conversion_module.SourceReplayArchiveImporter,
        "import_path",
        fail_import,
    )
    with pytest.raises(RuntimeError, match="playback validation failure"):
        convert_legacy_replay(source, target)

    assert not target.exists()
    assert list(tmp_path.glob(".converted.*.tmp")) == []


def test_failed_overwrite_publish_restores_complete_old_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _legacy_archive(tmp_path / "legacy.zip", [
        ("session/1.json", _frame(0, "frame")),
    ])
    target = tmp_path / "converted"
    target.mkdir()
    (target / "replay.zip").write_bytes(b"old-replay")
    (target / "frame_analysis.native.jsonl").write_bytes(b"old-analysis")
    before = _flat_tree(target)
    real_replace = conversion_module.os.replace
    call_count = 0

    def fail_new_publish(source_path, destination_path):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise OSError("injected publish failure")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(conversion_module.os, "replace", fail_new_publish)
    with pytest.raises(OSError, match="injected publish failure"):
        convert_legacy_replay(source, target, overwrite=True)

    assert _flat_tree(target) == before
    assert list(tmp_path.glob(".converted.*.tmp")) == []
    assert list(tmp_path.glob(".converted.*.backup")) == []


def test_conversion_output_cannot_replace_or_contain_input(tmp_path: Path) -> None:
    source = _legacy_archive(tmp_path / "inputs" / "legacy.zip", [
        ("session/1.json", _frame(0, "frame")),
    ])
    original = source.read_bytes()

    with pytest.raises(ValueError, match="input replay"):
        convert_legacy_replay(source, source, overwrite=True)
    with pytest.raises(ValueError, match="input replay"):
        convert_legacy_replay(source, source.parent, overwrite=True)

    assert source.read_bytes() == original


def test_cli_reports_playback_scope_counts_and_model_provenance(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _legacy_archive(tmp_path / "legacy.zip", [
        ("session/1.json", _frame(0, "early")),
        ("session/2.json", _frame(0, "final")),
        ("session/3.json", _frame(1, "next")),
    ])
    output = tmp_path / "converted"
    arguments = [
        "--input", str(source),
        "--output", str(output),
        "--model-id", "cli-model",
        "--model-version", "9.0",
        "--model-hash", "cli-hash",
    ]

    assert convert_main(arguments) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["raw_record_count"] == 3
    assert report["selected_frame_count"] == 2
    assert report["collapsed_record_count"] == 1
    assert report["playback_compatibility"] == (
        "source-numbered-frame-playback/1.0"
    )
    assert report["strict_replay_supported"] is False
    assert report["model_id"] == "cli-model"
    assert report["model_version"] == "9.0"
    assert report["model_hash"] == "cli-hash"
    assert Path(report["replay_zip"]) == output / "replay.zip"
    assert Path(report["analysis_jsonl"]) == (
        output / "frame_analysis.native.jsonl"
    )

    assert convert_main(arguments) == 1
    assert "FileExistsError" in capsys.readouterr().err


def _legacy_archive(
    path: Path,
    entries: list[tuple[str, dict[str, Any]]],
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w") as archive:
        for name, frame in entries:
            archive.writestr(
                name,
                (json.dumps(
                    frame,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ) + "\n").encode("utf-8"),
            )
        archive.writestr("session/notes.txt", b"ignored")
    return path


def _frame(
    cur_step: int,
    marker: str,
    *,
    padding: str = "",
) -> dict[str, Any]:
    return {
        "scenario_id": "legacy-conversion",
        "terrain_id": 7,
        "time": {
            "cur_step": cur_step,
            "tick": cur_step,
            "max_time": 10,
            "max_step": 10,
            "stage": 2,
        },
        "operators": [
            _unit(1, 0, 120.0),
            _unit(2, 1, 100.0),
        ],
        "passengers": [],
        "jm_points": [],
        "cities": [],
        "scores": {},
        "landmarks": {},
        "judge_info": [],
        "role_and_grouping_info": {},
        "communication": [],
        "valid_actions": {},
        "actions": [],
        "marker": marker,
        "padding": padding,
        "facts": {"nested": [True, None, 0.0]},
    }


def _unit(obj_id: int, color: int, value: float) -> dict[str, Any]:
    return {
        "obj_id": obj_id,
        "color": color,
        "type": 2,
        "sub_type": 0,
        "name": f"unit-{obj_id}",
        "armor": 0,
        "cur_hex": 100 + obj_id,
        "blood": 100,
        "max_blood": 100,
        "value": value,
        "carry_weapon_ids": [36],
        "remain_bullet_nums": {"36": 10},
        "weapon_cool_time": 0,
        "weapon_unfold_state": 2,
        "move_state": 0,
        "stop": False,
        "can_to_move": True,
        "on_board": False,
        "see_enemy_bop_ids": [],
        "keep": False,
        "tire": 0,
        "lose_control": False,
        "close_combat": False,
    }


def _records(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _flat_tree(root: Path) -> dict[str, bytes]:
    return {
        item.relative_to(root).as_posix(): item.read_bytes()
        for item in sorted(root.rglob("*"))
        if item.is_file()
    }
