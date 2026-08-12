from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest

from scripts.analyze_source_replay import main as analyze_main
from wargame_native_engine.analysis import EightFactorAnalyzer
from wargame_native_engine.analysis.legacy_replay_adapter import (
    LEGACY_SERIALIZED_SOURCE_FORMAT,
    load_legacy_serialized_replay,
)
from wargame_native_engine.analysis.replay_processor import (
    analyze_replay_to_jsonl,
)
from wargame_native_engine.analysis.timeline import build_analysis_timeline


def test_nested_json_members_select_last_source_record_per_cur_step(
    tmp_path: Path,
) -> None:
    archive = _write_archive(tmp_path / "legacy.zip", [
        ("session/", b""),
        ("session/notes.txt", b"ignored non-numeric member"),
        ("session/1.json", _frame(0, marker="initial")),
        ("session/2.json", _frame(0, marker="deployed")),
        ("session/3.json", _frame(1, marker="step-one")),
    ])

    selection = load_legacy_serialized_replay(archive)

    assert selection.session_prefix == "session"
    assert selection.raw_record_count == 3
    assert selection.selected_frame_count == 2
    assert selection.collapsed_record_count == 1
    assert selection.selected_member_names == (
        "session/2.json",
        "session/3.json",
    )
    assert [item.frame_index for item in selection.inputs] == [0, 1]
    assert [item.frame["marker"] for item in selection.inputs] == [
        "deployed",
        "step-one",
    ]


def test_extensionless_members_are_supported(tmp_path: Path) -> None:
    archive = _write_archive(tmp_path / "extensionless.zip", [
        ("session/0", _frame(0, marker="early")),
        ("session/1", _frame(0, marker="final")),
        ("session/2", _frame(1, marker="next")),
    ])

    selection = load_legacy_serialized_replay(archive)

    assert selection.selected_member_names == ("session/1", "session/2")
    assert [item.frame["marker"] for item in selection.inputs] == [
        "final",
        "next",
    ]


def test_selection_uses_numeric_source_order_not_payload_size(
    tmp_path: Path,
) -> None:
    archive = _write_archive(tmp_path / "sizes.zip", [
        ("session/1.json", _frame(0, marker="early", padding="x" * 20_000)),
        ("session/2.json", _frame(0, marker="final")),
        ("session/3.json", _frame(1, marker="next")),
    ])

    selection = load_legacy_serialized_replay(archive)

    assert selection.selected_member_names[0] == "session/2.json"
    assert selection.inputs[0].frame["marker"] == "final"


def test_numeric_source_order_does_not_use_zip_or_string_order(
    tmp_path: Path,
) -> None:
    archive = _write_archive(tmp_path / "numeric.zip", [
        ("session/10.json", _frame(2, marker="ten")),
        ("session/2.json", _frame(0, marker="two")),
    ])

    selection = load_legacy_serialized_replay(archive)

    assert selection.selected_member_names == (
        "session/2.json",
        "session/10.json",
    )
    assert [item.frame_index for item in selection.inputs] == [0, 2]


def test_processor_counts_hashes_and_analyzes_only_selected_records(
    tmp_path: Path,
) -> None:
    entries = [
        ("session/1.json", _frame(0, marker="discard-zero")),
        ("session/2.json", _frame(0, marker="keep-zero")),
        ("session/3.json", _frame(1, marker="discard-one")),
        ("session/4.json", _frame(1, marker="keep-one")),
        ("session/5.json", _frame(2, marker="keep-two")),
    ]
    archive, payloads = _write_archive_with_payloads(
        tmp_path / "counts.zip", entries
    )
    archive_before = archive.read_bytes()
    output = tmp_path / "frame_analysis.native.jsonl"
    analyzer = _RecordingAnalyzer()

    result = analyze_replay_to_jsonl(archive, output, analyzer=analyzer)

    records = _records(output)
    assert result.source_format == LEGACY_SERIALIZED_SOURCE_FORMAT
    assert result.frame_count == result.selected_frame_count == 3
    assert result.raw_record_count == 5
    assert result.collapsed_record_count == 2
    assert len(records) == len(analyzer.inputs) == analyzer.frame_call_count == 3
    assert [item.frame["marker"] for item in analyzer.inputs] == [
        "keep-zero",
        "keep-one",
        "keep-two",
    ]
    expected_hashes = [
        hashlib.sha256(payloads[name]).hexdigest()
        for name in ("session/2.json", "session/4.json", "session/5.json")
    ]
    discarded_hashes = {
        hashlib.sha256(payloads[name]).hexdigest()
        for name in ("session/1.json", "session/3.json")
    }
    actual_hashes = [record["provenance"]["frame_hash"] for record in records]
    assert actual_hashes == expected_hashes
    assert discarded_hashes.isdisjoint(actual_hashes)
    assert [record["provenance"]["frame_index"] for record in records] == [0, 1, 2]
    assert all(record["provenance"]["scenario_hash"] is None for record in records)
    assert output.read_bytes().endswith(b"\n")
    assert archive.read_bytes() == archive_before


def test_explicit_historical_routes_legacy_archive_to_adapter(
    tmp_path: Path,
) -> None:
    archive = _write_archive(tmp_path / "explicit.zip", [
        ("session/1.json", _frame(0)),
        ("session/2.json", _frame(1)),
    ])

    result = analyze_replay_to_jsonl(
        archive,
        tmp_path / "analysis.jsonl",
        source_format="historical",
    )

    assert result.source_format == LEGACY_SERIALIZED_SOURCE_FORMAT
    assert result.raw_record_count == result.selected_frame_count == 2
    assert result.collapsed_record_count == 0


def test_single_cli_reports_legacy_selection_counts(
    tmp_path: Path,
    capsys,
) -> None:
    archive = _write_archive(tmp_path / "cli.zip", [
        ("session/1.json", _frame(0, marker="early")),
        ("session/2.json", _frame(0, marker="final")),
        ("session/3.json", _frame(1, marker="next")),
    ])
    output = tmp_path / "analysis.jsonl"

    exit_code = analyze_main([
        "--input", str(archive),
        "--output", str(output),
        "--format", "auto",
    ])

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["source_format"] == LEGACY_SERIALIZED_SOURCE_FORMAT
    assert report["raw_record_count"] == 3
    assert report["selected_frame_count"] == report["frame_count"] == 2
    assert report["collapsed_record_count"] == 1


_MISSING = object()


@pytest.mark.parametrize(
    "cur_step",
    [_MISSING, None, True, 1.0, "1", -1],
    ids=["missing", "none", "bool", "float", "string", "negative"],
)
def test_invalid_cur_step_is_rejected(
    tmp_path: Path,
    cur_step: object,
) -> None:
    frame = _frame(0)
    if cur_step is _MISSING:
        del frame["time"]["cur_step"]
    else:
        frame["time"]["cur_step"] = cur_step
    archive = _write_archive(tmp_path / "bad-step.zip", [
        ("session/1.json", frame),
    ])

    with pytest.raises(ValueError, match="time.cur_step"):
        load_legacy_serialized_replay(archive)


def test_cur_step_regression_is_rejected(tmp_path: Path) -> None:
    archive = _write_archive(tmp_path / "regression.zip", [
        ("session/1.json", _frame(1)),
        ("session/2.json", _frame(0)),
    ])

    with pytest.raises(ValueError, match="must not regress"):
        load_legacy_serialized_replay(archive)


def test_source_sequence_alias_is_rejected(tmp_path: Path) -> None:
    archive = _write_archive(tmp_path / "duplicate.zip", [
        ("session/1", _frame(0)),
        ("session/1.json", _frame(1)),
    ])

    with pytest.raises(ValueError, match="source sequence conflict"):
        load_legacy_serialized_replay(archive)


def test_multiple_session_prefixes_are_rejected(tmp_path: Path) -> None:
    archive = _write_archive(tmp_path / "sessions.zip", [
        ("session-a/1.json", _frame(0)),
        ("session-b/2.json", _frame(1)),
    ])

    with pytest.raises(ValueError, match="exactly one session prefix"):
        load_legacy_serialized_replay(archive)


def test_numeric_member_outside_session_directory_is_rejected(
    tmp_path: Path,
) -> None:
    archive = _write_archive(tmp_path / "top-level.zip", [
        ("1.json", _frame(0)),
    ])

    with pytest.raises(ValueError, match="session directory"):
        load_legacy_serialized_replay(archive)


def test_archive_without_numeric_members_is_rejected(tmp_path: Path) -> None:
    archive = _write_archive(tmp_path / "empty.zip", [
        ("session/notes.txt", b"not a frame"),
    ])

    with pytest.raises(ValueError, match="no numeric data members"):
        load_legacy_serialized_replay(archive)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"\xff", "UTF-8"),
        (b"{", "valid JSON"),
        (b"[]", "root must be an object"),
        (json.dumps({"time": []}).encode("utf-8"), "time must be an object"),
    ],
)
def test_invalid_member_payload_is_rejected(
    tmp_path: Path,
    payload: bytes,
    message: str,
) -> None:
    archive = _write_archive(tmp_path / "invalid.zip", [
        ("session/1.json", payload),
    ])

    with pytest.raises(ValueError, match=message):
        load_legacy_serialized_replay(archive)


@pytest.mark.parametrize(
    ("marker_name", "marker_payload"),
    [
        (
            "replay_manifest.native.jsonl",
            b'{"replay_format":"native-source-frame-replay/1.0"}\n',
        ),
        (
            "manifest.json",
            b'{"schema_version":"historical-replay-source/1.0"}\n',
        ),
    ],
)
def test_auto_rejects_legacy_members_mixed_with_format_marker(
    tmp_path: Path,
    marker_name: str,
    marker_payload: bytes,
) -> None:
    archive = _write_archive(tmp_path / "mixed.zip", [
        (marker_name, marker_payload),
        ("session/1.json", _frame(0)),
    ])

    with pytest.raises(ValueError, match="ambiguous replay format"):
        analyze_replay_to_jsonl(archive, tmp_path / "analysis.jsonl")


def test_cur_step_gap_is_allowed_and_used_as_frame_index(tmp_path: Path) -> None:
    archive = _write_archive(tmp_path / "gap.zip", [
        ("session/1.json", _frame(0)),
        ("session/2.json", _frame(2)),
    ])
    output = tmp_path / "gap.jsonl"

    result = analyze_replay_to_jsonl(archive, output)

    assert result.frame_count == 2
    assert [record["provenance"]["frame_index"]
            for record in _records(output)] == [0, 2]


class _RecordingAnalyzer:
    def __init__(self) -> None:
        self.real = EightFactorAnalyzer()
        self.inputs = ()
        self.frame_call_count = 0

    def analyze_green_frames(self, inputs):
        self.inputs = tuple(inputs)
        frames = []
        for item in self.inputs:
            self.frame_call_count += 1
            frames.append(self.real.analyze_green_frame(item))
        return build_analysis_timeline(tuple(frames))


def _write_archive(
    path: Path,
    entries: list[tuple[str, object]],
) -> Path:
    return _write_archive_with_payloads(path, entries)[0]


def _write_archive_with_payloads(
    path: Path,
    entries: list[tuple[str, object]],
) -> tuple[Path, dict[str, bytes]]:
    payloads: dict[str, bytes] = {}
    with ZipFile(path, "w") as archive:
        for name, value in entries:
            payload = value if isinstance(value, bytes) else (
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ) + "\n"
            ).encode("utf-8")
            payloads[name] = payload
            archive.writestr(name, payload)
    return path, payloads


def _frame(
    cur_step: int,
    *,
    marker: str = "frame",
    padding: str = "",
) -> dict[str, Any]:
    return {
        "time": {"cur_step": cur_step},
        "operators": [
            _unit(1, 0, 100.0),
            _unit(2, 1, 100.0),
        ],
        "passengers": [],
        "marker": marker,
        "padding": padding,
    }


def _unit(obj_id: int, color: int, value: float) -> dict[str, Any]:
    return {
        "obj_id": obj_id,
        "color": color,
        "value": value,
        "blood": 100,
        "max_blood": 100,
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
