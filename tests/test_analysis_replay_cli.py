from __future__ import annotations

import json
from pathlib import Path

from scripts.analyze_source_replay import main as analyze_main
from scripts.batch_analyze_replays import main as batch_main
from wargame_native_engine.replay import SourceFrameRecorder


def test_single_cli_reports_provenance_and_honors_overwrite(
    tmp_path: Path,
    capsys,
) -> None:
    replay = _write_replay_zip(tmp_path / "game.zip", frame_count=2)
    output = tmp_path / "frame_analysis.native.jsonl"
    arguments = [
        "--input", str(replay),
        "--output", str(output),
        "--format", "auto",
        "--model-id", "cli-model",
        "--model-version", "9.1",
        "--model-hash", "model-hash",
    ]

    assert analyze_main(arguments) == 0
    report = json.loads(capsys.readouterr().out)
    first = output.read_bytes()
    assert report == {
        "input": str(replay),
        "source_format": "native",
        "frame_count": 2,
        "output": str(output),
        "model_id": "cli-model",
        "model_version": "9.1",
        "model_hash": "model-hash",
    }
    records = _records(output)
    assert len(records) == 2
    assert all(record["provenance"]["model_id"] == "cli-model"
               for record in records)

    assert analyze_main(arguments) == 1
    captured = capsys.readouterr()
    assert "FileExistsError" in captured.err
    assert output.read_bytes() == first

    assert analyze_main([*arguments, "--overwrite"]) == 0
    capsys.readouterr()
    assert output.read_bytes() == first


def test_batch_processes_sorted_replays_mirrors_paths_and_resumes(
    tmp_path: Path,
    capsys,
) -> None:
    input_root = tmp_path / "inputs"
    first = _write_replay_zip(input_root / "day2" / "game002.zip")
    second = _write_replay_zip(input_root / "day1" / "game001.zip")
    output_root = tmp_path / "outputs"
    errors = tmp_path / "logs" / "errors.jsonl"
    arguments = [
        "--input-root", str(input_root),
        "--output-root", str(output_root),
        "--errors", str(errors),
        "--model-id", "batch-model",
    ]

    assert batch_main(arguments) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["discovered"] == 2
    assert report["success"] == 2
    assert report["skipped"] == report["failed"] == 0
    first_output = (
        output_root / "day2" / "game002" / "frame_analysis.native.jsonl"
    )
    second_output = (
        output_root / "day1" / "game001" / "frame_analysis.native.jsonl"
    )
    assert first_output.is_file() and second_output.is_file()
    assert errors.read_bytes() == b""
    assert all(
        record["provenance"]["model_id"] == "batch-model"
        for output in (first_output, second_output)
        for record in _records(output)
    )
    replay_bytes = (first.read_bytes(), second.read_bytes())
    output_bytes = (first_output.read_bytes(), second_output.read_bytes())

    assert batch_main([*arguments, "--resume"]) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["discovered"] == 2
    assert resumed["success"] == resumed["failed"] == 0
    assert resumed["skipped"] == 2
    assert (first.read_bytes(), second.read_bytes()) == replay_bytes
    assert (first_output.read_bytes(), second_output.read_bytes()) == output_bytes


def test_batch_continues_after_corrupt_replay_and_writes_strict_error_jsonl(
    tmp_path: Path,
    capsys,
) -> None:
    input_root = tmp_path / "inputs"
    valid = _write_replay_zip(input_root / "z" / "valid.zip")
    corrupt = input_root / "a" / "corrupt.zip"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"not a zip archive")
    output_root = tmp_path / "outputs"
    errors = tmp_path / "errors.jsonl"

    exit_code = batch_main([
        "--input-root", str(input_root),
        "--output-root", str(output_root),
        "--errors", str(errors),
    ])
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert report["discovered"] == 2
    assert report["success"] == 1
    assert report["failed"] == 1
    assert report["skipped"] == 0
    assert (
        output_root / "z" / "valid" / "frame_analysis.native.jsonl"
    ).is_file()
    error_lines = errors.read_text(encoding="utf-8").splitlines()
    assert len(error_lines) == 1
    error = json.loads(error_lines[0])
    assert error["input_path"] == str(corrupt)
    assert error["error_type"] == "BadZipFile"
    assert error["error_message"]
    assert valid.is_file() and corrupt.read_bytes() == b"not a zip archive"


def test_batch_invalid_root_returns_nonzero_with_specific_error(
    tmp_path: Path,
    capsys,
) -> None:
    exit_code = batch_main([
        "--input-root", str(tmp_path / "missing"),
        "--output-root", str(tmp_path / "output"),
        "--errors", str(tmp_path / "errors.jsonl"),
    ])

    assert exit_code == 2
    assert "NotADirectoryError" in capsys.readouterr().err


def _write_replay_zip(path: Path, *, frame_count: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    recorder = SourceFrameRecorder(path.parent / f".{path.stem}-facts")
    recorder.start(
        {"episode_id": path.stem, "scenario_hash": "cli-scenario-hash"},
        started_at="2026-01-01T00:00:00Z",
    )
    recorder.record_initial(
        _minimal_source_frame(0),
        initial_state_hash="a" * 64,
    )
    for index in range(1, frame_count):
        recorder.record_step(_minimal_source_frame(index), {
            "delta_time_ms": 1,
            "ordered_actions": [],
            "expected_state_hash": f"state-{index}",
        })
    recorder.finalize(
        {"terminal": False, "truncated": True, "winner": None,
         "reason": "test"},
        final_state_hash="b" * 64,
    )
    return recorder.package_zip(path)


def _minimal_source_frame(index: int) -> dict[str, object]:
    return {
        "scenario_id": "cli-scenario",
        "terrain_id": 1,
        "time": {"cur_step": index, "tick": index, "max_time": 10,
                 "max_step": 10, "stage": 2},
        "operators": [],
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
        "extra": {"native": {"perspective": "GREEN"}},
    }


def _records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(
        encoding="utf-8").splitlines()]
