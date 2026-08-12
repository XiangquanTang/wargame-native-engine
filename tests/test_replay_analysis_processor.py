from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
from unittest.mock import Mock
from zipfile import BadZipFile, ZipFile

import pytest

from wargame_native_engine.analysis import (
    EightFactorAnalyzer,
    GreenFrameAnalysisInput,
    analyze_replay_to_jsonl,
    frame_analysis_to_record,
)
from wargame_native_engine.analysis.eight_factor_contract import FactorStatus
from wargame_native_engine.replay import SourceFrameRecorder


@pytest.fixture
def native_replay(tmp_path: Path) -> tuple[Path, tuple[dict[str, object], ...]]:
    frames = tuple(
        _source_green(index, red_value=100.0 + index * 50.0)
        for index in range(3)
    )
    recorder = SourceFrameRecorder(tmp_path / "recorded")
    recorder.start(
        {"episode_id": "processor", "scenario_hash": "scenario-exact"},
        started_at="2026-01-01T00:00:00Z",
    )
    recorder.record_initial(frames[0], initial_state_hash="a" * 64)
    for index, frame in enumerate(frames[1:], start=1):
        recorder.record_step(frame, {
            "delta_time_ms": 1,
            "ordered_actions": [],
            "expected_state_hash": f"state-{index}",
        })
    recorder.finalize(
        {"terminal": False, "truncated": True, "winner": None,
         "reason": "test"},
        final_state_hash="b" * 64,
    )
    return recorder.package_zip(tmp_path / "replay.zip"), frames


def test_single_frame_entry_matches_batch_and_is_stateless() -> None:
    frame = _source_green(0)
    incomplete = deepcopy(frame)
    del incomplete["operators"][0]["value"]  # type: ignore[index]
    frame_before = deepcopy(incomplete)
    item = GreenFrameAnalysisInput(
        frame_index=7,
        frame=incomplete,
        frame_hash="frame-seven",
        scenario_hash="scenario-seven",
    )
    analyzer = EightFactorAnalyzer(
        model_id="live-model",
        model_version="2.0",
        model_hash="model-hash",
    )

    first = analyzer.analyze_green_frame(item)
    repeated = analyzer.analyze_green_frame(item)
    batch = analyzer.analyze_green_frames((item,))

    assert first == repeated == batch.frame_analyses[0]
    assert incomplete == frame_before
    assert first.provenance.frame_index == 7
    assert first.provenance.frame_hash == "frame-seven"
    assert first.provenance.scenario_hash == "scenario-seven"
    assert first.provenance.model_id == "live-model"
    assert first.provenance.model_version == "2.0"
    assert first.provenance.model_hash == "model-hash"
    assert first.unit_analyses[0].factor_values[0].status is FactorStatus.UNKNOWN
    assert first.advantage.normalized_advantage is None
    assert first.advantage.label == "UNKNOWN"


def test_native_zip_offline_equals_live_and_batch_and_preserves_input(
    native_replay: tuple[Path, tuple[dict[str, object], ...]],
    tmp_path: Path,
) -> None:
    replay_path, _ = native_replay
    replay_before = replay_path.read_bytes()
    analyzer = EightFactorAnalyzer(
        model_id="shared-model",
        model_version="3.0",
        model_hash=None,
    )
    output = tmp_path / "analysis" / "frame_analysis.native.jsonl"

    result = analyze_replay_to_jsonl(
        replay_path,
        output,
        analyzer=analyzer,
    )
    first_payload = output.read_bytes()
    with ZipFile(replay_path) as archive:
        manifest = json.loads(
            archive.read("replay_manifest.native.jsonl").decode("utf-8")
        )
        inputs = tuple(
            _input_from_payload(
                index,
                archive.read(f"{index}.json"),
                scenario_hash=manifest["scenario_hash"],
            )
            for index in range(manifest["frame_count"])
        )

    live = tuple(analyzer.analyze_green_frame(item) for item in inputs)
    batch = analyzer.analyze_green_frames(inputs)
    actual_records = _records(first_payload)

    assert result.frame_count == 3
    assert result.source_format == "native"
    assert actual_records == [frame_analysis_to_record(frame) for frame in live]
    assert batch.frame_analyses == live
    assert [record["provenance"]["frame_index"]
            for record in actual_records] == [0, 1, 2]
    assert [record["provenance"]["frame_hash"]
            for record in actual_records] == [item.frame_hash for item in inputs]
    assert all(record["provenance"]["scenario_hash"] == "scenario-exact"
               for record in actual_records)
    assert first_payload.endswith(b"\n")
    assert replay_path.read_bytes() == replay_before

    analyze_replay_to_jsonl(
        replay_path,
        output,
        analyzer=analyzer,
        overwrite=True,
    )
    assert output.read_bytes() == first_payload
    assert replay_path.read_bytes() == replay_before


def test_native_directory_reads_only_integer_json_names_in_numeric_order(
    tmp_path: Path,
) -> None:
    replay_dir = tmp_path / "unpacked"
    replay_dir.mkdir()
    for index in (10, 0, 2):
        _write_frame(replay_dir / f"{index}.json", _source_green(index))
    _write_frame(replay_dir / "notes.json", {"not": "a frame"})
    _write_frame(replay_dir / "3.JSON", _source_green(3))
    (replay_dir / "other.jsonl").write_text("not a fact frame\n",
                                             encoding="utf-8")
    before = {item.name: item.read_bytes() for item in replay_dir.iterdir()}
    output = tmp_path / "derived" / "frame_analysis.native.jsonl"

    result = analyze_replay_to_jsonl(
        replay_dir,
        output,
    )

    records = _records(output.read_bytes())
    assert result.frame_count == 3
    assert [record["provenance"]["frame_index"] for record in records] == [0, 2, 10]
    assert {item.name: item.read_bytes() for item in replay_dir.iterdir()} == before


def test_reduced_historical_fixture_uses_importer_order_and_unknowns(
    tmp_path: Path,
) -> None:
    source = Path("examples/historical_replay_reduced_fixture/smoke-state-01")
    before = {
        item.relative_to(source).as_posix(): item.read_bytes()
        for item in source.rglob("*") if item.is_file()
    }
    output = tmp_path / "historical-analysis.jsonl"

    result = analyze_replay_to_jsonl(
        source,
        output,
    )

    records = _records(output.read_bytes())
    assert result.source_format == "historical"
    assert result.frame_count == len(records) == 3
    assert [record["provenance"]["frame_index"] for record in records] == [0, 1, 2]
    assert all(record["provenance"]["frame_hash"] is None for record in records)
    assert all(record["provenance"]["scenario_hash"] is None for record in records)
    expected_ids = [101, 102, 201, 202]
    expected_sides = ["red", "red", "blue", "blue"]
    for record in records:
        units = record["unit_analyses"]
        assert len(units) == 4
        assert [unit["operator_id"] for unit in units] == expected_ids
        assert [unit["side"] for unit in units] == expected_sides
        for unit in units:
            statuses = {
                value["factor_name"]: value["status"]
                for value in unit["factor_values"]
            }
            assert statuses["base_value"] == "UNKNOWN"
            assert statuses["health"] == "OBSERVED"
            assert statuses["weapon"] == "OBSERVED"
    assert {
        item.relative_to(source).as_posix(): item.read_bytes()
        for item in source.rglob("*") if item.is_file()
    } == before


def test_auto_routes_numbered_historical_zip_without_native_manifest(
    tmp_path: Path,
) -> None:
    source = tmp_path / "historical-source.zip"
    with ZipFile(source, "w") as archive:
        for name, index in (("96.json", 0), ("97.json", 1)):
            payload = (
                json.dumps(
                    _source_green(index),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ) + "\n"
            ).encode("utf-8")
            archive.writestr(name, payload)
    before = source.read_bytes()
    output = tmp_path / "historical-source-analysis.jsonl"

    result = analyze_replay_to_jsonl(source, output)

    records = _records(output.read_bytes())
    assert result.source_format == "historical"
    assert result.frame_count == 2
    assert [record["provenance"]["frame_index"] for record in records] == [0, 1]
    assert all(record["provenance"]["frame_hash"] is None for record in records)
    assert source.read_bytes() == before


def test_auto_rejects_mixed_historical_directory_and_numbered_frame(
    tmp_path: Path,
) -> None:
    source = tmp_path / "mixed-directory"
    shutil.copytree(
        Path("examples/historical_replay_reduced_fixture/smoke-state-01"),
        source,
    )
    _write_frame(source / "0.json", _source_green(0))
    before = {item.name: item.read_bytes() for item in source.iterdir()}

    with pytest.raises(ValueError, match="ambiguous replay format"):
        analyze_replay_to_jsonl(source, tmp_path / "auto.jsonl")

    native = analyze_replay_to_jsonl(
        source,
        tmp_path / "native.jsonl",
        source_format="native",
    )
    historical = analyze_replay_to_jsonl(
        source,
        tmp_path / "historical.jsonl",
        source_format="historical",
    )
    assert native.source_format == "native" and native.frame_count == 1
    assert historical.source_format == "historical"
    assert historical.frame_count == 3
    assert {item.name: item.read_bytes() for item in source.iterdir()} == before


def test_auto_rejects_native_zip_with_historical_manifest(
    native_replay: tuple[Path, tuple[dict[str, object], ...]],
    tmp_path: Path,
) -> None:
    original, _ = native_replay
    source = tmp_path / "mixed.zip"
    historical_manifest = Path(
        "examples/historical_replay_reduced_fixture/smoke-state-01/manifest.json"
    ).read_bytes()
    with ZipFile(original) as existing, ZipFile(source, "w") as mixed:
        for name in existing.namelist():
            mixed.writestr(name, existing.read(name))
        mixed.writestr("manifest.json", historical_manifest)
    before = source.read_bytes()

    with pytest.raises(ValueError, match="ambiguous replay format"):
        analyze_replay_to_jsonl(source, tmp_path / "auto-zip.jsonl")

    native = analyze_replay_to_jsonl(
        source,
        tmp_path / "native-zip.jsonl",
        source_format="native",
    )
    historical = analyze_replay_to_jsonl(
        source,
        tmp_path / "historical-zip.jsonl",
        source_format="historical",
    )
    assert native.source_format == "native" and native.frame_count == 3
    assert historical.source_format == "historical"
    assert historical.frame_count == 3
    assert source.read_bytes() == before


def test_auto_rejects_empty_directory_and_corrupt_zip(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="unable to detect replay format"):
        analyze_replay_to_jsonl(empty, tmp_path / "empty.jsonl")

    corrupt = tmp_path / "corrupt.zip"
    corrupt.write_bytes(b"not a zip")
    with pytest.raises(BadZipFile):
        analyze_replay_to_jsonl(corrupt, tmp_path / "corrupt.jsonl")


def test_existing_output_requires_overwrite_then_can_be_replaced(
    native_replay: tuple[Path, tuple[dict[str, object], ...]],
    tmp_path: Path,
) -> None:
    replay_path, _ = native_replay
    output = tmp_path / "existing.jsonl"
    output.write_bytes(b"old-output")
    analyzer = Mock(spec=EightFactorAnalyzer)

    with pytest.raises(FileExistsError, match="already exists"):
        analyze_replay_to_jsonl(replay_path, output, analyzer=analyzer)

    analyzer.analyze_green_frames.assert_not_called()
    assert output.read_bytes() == b"old-output"
    analyze_replay_to_jsonl(replay_path, output, overwrite=True)
    assert output.read_bytes() != b"old-output"
    assert output.read_bytes().endswith(b"\n")


@pytest.mark.parametrize("existing", [False, True])
def test_analysis_failure_is_atomic_and_cleans_temporary_files(
    native_replay: tuple[Path, tuple[dict[str, object], ...]],
    tmp_path: Path,
    existing: bool,
) -> None:
    replay_path, _ = native_replay
    output = tmp_path / "failed.jsonl"
    old = b"existing-output"
    if existing:
        output.write_bytes(old)
    real = EightFactorAnalyzer()
    analyzer = Mock(spec=EightFactorAnalyzer)

    def fail_after_one(inputs):
        real.analyze_green_frame(inputs[0])
        raise RuntimeError("injected analysis failure")

    analyzer.analyze_green_frames.side_effect = fail_after_one
    with pytest.raises(RuntimeError, match="injected analysis failure"):
        analyze_replay_to_jsonl(
            replay_path,
            output,
            analyzer=analyzer,
            overwrite=existing,
        )

    if existing:
        assert output.read_bytes() == old
    else:
        assert not output.exists()
    assert list(tmp_path.glob(f".{output.name}.*.tmp")) == []


def test_output_cannot_replace_input_replay_or_enter_input_directory(
    native_replay: tuple[Path, tuple[dict[str, object], ...]],
    tmp_path: Path,
) -> None:
    replay_path, _ = native_replay
    original = replay_path.read_bytes()
    with pytest.raises(ValueError, match="must not replace"):
        analyze_replay_to_jsonl(replay_path, replay_path, overwrite=True)
    assert replay_path.read_bytes() == original

    directory = tmp_path / "native-dir"
    directory.mkdir()
    _write_frame(directory / "0.json", _source_green(0))
    with pytest.raises(ValueError, match="outside"):
        analyze_replay_to_jsonl(
            directory,
            directory / "frame_analysis.native.jsonl",
        )


def _source_green(index: int, *, red_value: float = 100.0) -> dict[str, object]:
    return {
        "scenario_id": "processor-scenario",
        "terrain_id": 1,
        "time": {"cur_step": index, "tick": index, "max_time": 10,
                 "max_step": 10, "stage": 2},
        "operators": [
            _source_unit(1, 0, red_value),
            _source_unit(2, 1, 100.0),
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
        "extra": {"native": {"perspective": "GREEN"}},
    }


def _source_unit(
    obj_id: int,
    color: int,
    value: float,
) -> dict[str, object]:
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


def _input_from_payload(
    index: int,
    payload: bytes,
    *,
    scenario_hash: str | None,
) -> GreenFrameAnalysisInput:
    return GreenFrameAnalysisInput(
        frame_index=index,
        frame=json.loads(payload.decode("utf-8")),
        frame_hash=hashlib.sha256(payload).hexdigest(),
        scenario_hash=scenario_hash,
    )


def _write_frame(path: Path, frame: object) -> None:
    path.write_text(
        json.dumps(frame, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"), allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _records(payload: bytes) -> list[dict[str, object]]:
    return [json.loads(line) for line in payload.decode("utf-8").splitlines()]
