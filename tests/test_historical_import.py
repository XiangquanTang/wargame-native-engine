from __future__ import annotations

import hashlib
import json
from pathlib import Path

from wargame_native_engine.io.importers.historical_replay import (
    EvidenceLevel, HistoricalReplayImporter, load_historical_replay,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples" / "historical_replay_reduced_fixture"


def _tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_file():
            digest.update(str(item.relative_to(path)).encode())
            digest.update(item.read_bytes())
    return digest.hexdigest()


def test_historical_replay_import_does_not_modify_source(tmp_path: Path) -> None:
    source = FIXTURES / "smoke-state-01"
    before = _tree_digest(source)
    HistoricalReplayImporter().import_path(source, output_dir=tmp_path / "out")
    assert _tree_digest(source) == before


def test_historical_replay_source_hash() -> None:
    result = HistoricalReplayImporter().import_path(
        FIXTURES / "calibration-action-01")
    assert result.replay.source_hash == (
        "0d0a040a51d8d86f6bc6c1b8a1cb281a845a35487cd7f7d0434dd6eea0ba6970")
    assert result.import_manifest["source_hash"] == result.replay.source_hash


def test_evidence_level_detection() -> None:
    importer = HistoricalReplayImporter()
    assert importer.import_path(FIXTURES / "calibration-action-01").replay.evidence_level is EvidenceLevel.ACTION_LEVEL
    assert importer.import_path(FIXTURES / "smoke-state-01").replay.evidence_level is EvidenceLevel.STATE_TRAJECTORY_LEVEL


def test_unknown_fields_are_preserved() -> None:
    result = HistoricalReplayImporter().import_path(FIXTURES / "smoke-state-01")
    source = result.replay.frames[0].operators[0].source_raw
    assert source["armor"] == 4
    assert source["sdk_cur_hex"] == 21


def test_unmapped_fields_are_reported() -> None:
    result = HistoricalReplayImporter().import_path(FIXTURES / "smoke-state-01")
    names = {item["field"] for item in result.unmapped_fields["operator_fields"]}
    assert {"armor", "sdk_cur_hex"} <= names
    assert result.unmapped_fields["handling"].startswith("preserved")


def test_action_echo_not_assumed_executed() -> None:
    replay = HistoricalReplayImporter().import_path(
        FIXTURES / "smoke-state-01").replay
    assert len(replay.actions) == 1
    assert replay.actions[0].source_kind == "ACTION_ECHO"
    assert replay.actions[0].execution_status == "UNKNOWN"
    assert replay.actions[0].executable is False


def test_time_unit_is_explicit() -> None:
    replay = HistoricalReplayImporter().import_path(
        FIXTURES / "smoke-state-01").replay
    assert replay.time_unit == "sdk_step"
    assert replay.coordinate_unit == "native_grid_unit"


def test_operator_id_mapping() -> None:
    replay = HistoricalReplayImporter().import_path(
        FIXTURES / "smoke-state-01").replay
    assert [item.operator_id for item in replay.frames[0].operators] == [101, 102, 201, 202]
    assert all(item.field_mapping["operator_id"] == "DIRECT"
               for item in replay.frames[0].operators)


def test_small_fixture_import(tmp_path: Path) -> None:
    result = HistoricalReplayImporter().import_path(
        FIXTURES / "smoke-state-01", output_dir=tmp_path / "imported")
    expected = {"import_manifest.json", "historical_replay.json", "frames.jsonl",
                "actions.jsonl", "events.jsonl", "summary.json",
                "unmapped_fields.json", "warnings.json"}
    assert {item.name for item in (tmp_path / "imported").iterdir()} == expected
    loaded = load_historical_replay(tmp_path / "imported" / "historical_replay.json")
    assert loaded.to_dict() == result.replay.to_dict()
    json.loads((tmp_path / "imported" / "import_manifest.json").read_text())


def test_fixture_collection_selects_declared_smoke() -> None:
    replay = HistoricalReplayImporter().import_path(FIXTURES).replay
    assert replay.replay_id == "smoke-state-01"
