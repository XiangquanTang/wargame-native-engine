from __future__ import annotations

import json
from pathlib import Path

import pytest

from wargame_native_engine.compat.source_frame import SourceFrameExporter, SourceFrameImporter
from wargame_native_engine.io.serialization import file_sha256


ROOT = Path(__file__).resolve().parents[1]
SOURCE_96 = ROOT / "local_data/source_frames/96.json"


@pytest.fixture
def source_frame():
    if not SOURCE_96.exists():
        pytest.skip("local source frame 96.json is intentionally not committed")
    return SourceFrameImporter().load(SOURCE_96)


def test_source_frame_96_loads(source_frame) -> None:
    assert len(source_frame.operators) == 34
    assert len(source_frame.passengers) == 23


def test_source_frame_hash_is_stable(source_frame) -> None:
    assert source_frame.source_hash == file_sha256(SOURCE_96)
    assert source_frame.source_hash == "10022073edb19596387cf4d75ecde9c60dc06fb140555fb2ff07c2db32f60c62"


def test_source_file_is_not_modified(source_frame) -> None:
    before = file_sha256(SOURCE_96)
    SourceFrameImporter().load(SOURCE_96)
    assert file_sha256(SOURCE_96) == before


def test_operator_and_passenger_schema(source_frame) -> None:
    assert all(item.max_blood > 0 for item in source_frame.all_units)
    assert all(item.raw["obj_id"] == item.obj_id for item in source_frame.all_units)


def test_relationship_ids_resolve(source_frame) -> None:
    ids = {item.obj_id for item in source_frame.all_units}
    assert all(item.car_id is None or item.car_id in ids for item in source_frame.passengers)


def test_source_frame_to_native_world(source_frame) -> None:
    world, report = SourceFrameImporter().to_world(source_frame)
    assert len(world.operators) == 57
    assert world.operators[1].blood == 3
    assert report["valid_actions_rule_truth"] is False
    assert report["records"][0]["armor"]["native_effective_value"] == "UNKNOWN"
    assert "keep/keep_remain_time state semantics (U01)" in report[
        "unresolved_source_semantics"]


def test_native_world_to_compatible_frame(source_frame) -> None:
    world, _ = SourceFrameImporter().to_world(source_frame)
    exported = SourceFrameExporter().export(world, template=source_frame)
    by_id = {item["obj_id"]: item for item in exported["operators"] + exported["passengers"]}
    assert by_id[1]["cur_hex"] == source_frame.operators[0].current_hex
    assert by_id[1]["blood"] == source_frame.operators[0].blood


def test_unknown_source_fields_preserved(tmp_path: Path) -> None:
    value = json.loads(SOURCE_96.read_text(encoding="utf-8"))
    value["operators"][0]["future_unknown"] = {"opaque": [1, 2]}
    path = tmp_path / "frame.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    frame = SourceFrameImporter().load(path)
    world, _ = SourceFrameImporter().to_world(frame)
    exported = SourceFrameExporter().export(world, template=frame)
    assert exported["operators"][0]["future_unknown"] == {"opaque": [1, 2]}


def test_valid_actions_not_used_as_rule_truth(source_frame) -> None:
    world, report = SourceFrameImporter().to_world(source_frame)
    assert report["valid_actions_rule_truth"] is False
    assert all("valid_actions" not in item.raw_attributes
               for item in world.operators.values())
