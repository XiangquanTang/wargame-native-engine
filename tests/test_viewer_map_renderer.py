from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess

import pytest

from wargame_native_engine.compat.source_frame.importer import SourceFrameImporter
from wargame_native_engine.viewer.map_config import (
    ViewerMapConfigError,
    coordinate_fallback_record,
    load_viewer_map_config,
)
from wargame_native_engine.viewer.replay_view import _frame_record


ROOT = Path(__file__).resolve().parents[1]
MAP_RENDERER = (
    ROOT / "src/wargame_native_engine/viewer/static/map_renderer.js"
)


def test_explicit_map_config_loads_old_renderer_odd_r_subset(
    tmp_path: Path,
) -> None:
    path = tmp_path / "basic.json"
    value = _map_config()
    payload = json.dumps(value, sort_keys=True).encode("utf-8")
    path.write_bytes(payload)

    loaded = load_viewer_map_config(path)
    record = loaded.to_record()

    assert record["mode"] == "configured-hex-map"
    assert record["coordinate_system"] == "odd-r"
    assert record["orientation"] == "pointy-top"
    assert record["position_encoding"] == "row_times_100_plus_col"
    assert record["rows"] == record["columns"] == 2
    assert record["bounds"] == {
        "min_row": 0, "max_row": 1,
        "min_column": 0, "max_column": 1,
    }
    assert [cell["terrain_semantic"] for cell in record["cells"]] == [
        "open", "forest", "settlement", "water",
    ]
    assert record["cells"][0]["roads"] == [1, 0, 0, 0, 0, 0]
    assert record["cells"][1]["rivers"] == [0, 1, 0, 0, 0, 0]
    assert record["source"]["sha256"] == hashlib.sha256(payload).hexdigest()


def test_map_config_rejects_wrong_geometry_and_bad_edges(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    value = _map_config()
    value["coordinate_system"] = "square"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ViewerMapConfigError, match="odd-r"):
        load_viewer_map_config(path)

    value["coordinate_system"] = "odd-r"
    value["map_data"][0][0]["roads"] = [1, 2]
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ViewerMapConfigError, match="six"):
        load_viewer_map_config(path)


def test_coordinate_fallback_uses_fact_extent_without_terrain() -> None:
    first = SourceFrameImporter().from_dict(
        _frame([_unit(1, 0, 101), _unit(2, 1, 304)]),
        source_hash="first",
    )
    second = SourceFrameImporter().from_dict(
        _frame([_unit(1, 0, 205), _unit(2, 1, 402)]),
        source_hash="second",
    )

    record = coordinate_fallback_record((first, second))

    assert record["mode"] == "coordinate-fallback"
    assert record["bounds"] == {
        "min_row": 1, "max_row": 4,
        "min_column": 1, "max_column": 5,
    }
    assert record["cells"] == []
    assert record["source"] is None


def test_viewer_unit_projection_preserves_side_category_and_fact_status() -> None:
    operator = _unit(1, 0, 101)
    operator.update({"blood": 0, "lose_control": True, "can_to_move": False})
    passenger = _unit(2, 1, 101)
    passenger.update({"on_board": 1, "car": 1})
    frame = SourceFrameImporter().from_dict(
        _frame([operator], passengers=[passenger]),
        source_hash="frame",
    )

    units = _frame_record(0, "0.json", frame)["viewer_units"]
    by_id = {unit["operator_id"]: unit for unit in units}

    assert by_id[1]["side"] == "red"
    assert by_id[1]["category"] == "operator"
    assert by_id[1]["destroyed"] is True
    assert by_id[1]["lose_control"] is True
    assert by_id[1]["can_to_move"] is False
    assert by_id[2]["side"] == "blue"
    assert by_id[2]["category"] == "passenger"
    assert by_id[2]["on_board"] is True
    assert by_id[2]["carrier_id"] == 1
    assert by_id[1]["row"] == by_id[2]["row"] == 1
    assert by_id[1]["column"] == by_id[2]["column"] == 1


def test_web_geometry_matches_mature_pyqt_reference_points() -> None:
    """Pin the audited PyQt transform used verbatim by map_renderer.js."""

    size = 36.0
    width = math.sqrt(3.0) * size
    reference = {
        (0, 0): (width / 2, 36),
        (1, 0): (width, 90),
        (2, 1): (width * 1.5, 144),
        (8, 9): (width * 9.5, 468),
        (9, 9): (width * 10, 522),
    }
    for (row, column), expected in reference.items():
        actual = _web_reference_hex_to_pixel(row, column, size)
        assert actual == pytest.approx(expected)
    source = MAP_RENDERER.read_text(encoding="utf-8")
    assert "column * width + (row % 2 ? width / 2 : 0) + width / 2" in source
    assert "row * hexSize * 1.5 + hexSize" in source
    assert "const DIRECTION_ANGLES = [0, -60, -120, 180, 120, 60]" in source


def test_map_renderer_has_stack_selection_zoom_and_pan_contracts() -> None:
    source = MAP_RENDERER.read_text(encoding="utf-8")

    assert "function stackUnits(units)" in source
    assert "this.hitTargets.push" in source
    assert 'addEventListener("wheel"' in source
    assert 'addEventListener("pointermove"' in source
    assert "this.offsetX = this.drag.offsetX + dx" in source
    assert "unit.operator_id === this.selectedUnitId" in source
    assert "unit.category === \"passenger\"" in source
    assert "unit.destroyed" in source
    assert "unit.lose_control || unit.can_to_move === false" in source


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_map_renderer_executes_in_node_when_available() -> None:
    script = (
        "const r=require(process.argv[1]);"
        "const p=r.hexToPixel(1,0,36);"
        "if(Math.abs(p.x-Math.sqrt(3)*36)>1e-9||p.y!==90)process.exit(2);"
        "const s=r.stackUnits([{row:1,column:1,operator_id:2,category:'passenger'},"
        "{row:1,column:1,operator_id:1,category:'operator'}]);"
        "if(s.length!==1||s[0][0].operator_id!==1)process.exit(3);"
    )
    subprocess.run(
        ["node", "-e", script, str(MAP_RENDERER)],
        check=True,
        capture_output=True,
        text=True,
    )


def _web_reference_hex_to_pixel(
    row: int,
    column: int,
    hex_size: float,
) -> tuple[float, float]:
    width = math.sqrt(3.0) * hex_size
    return (
        column * width + (width / 2 if row % 2 else 0) + width / 2,
        row * hex_size * 1.5 + hex_size,
    )


def _map_config() -> dict:
    return {
        "map_id": "7",
        "coordinate_system": "odd-r",
        "max_row": 2,
        "max_column": 2,
        "map_data": [
            [
                {"r": 0, "c": 0, "elev": 0, "cond": 0,
                 "roads": [1, 0, 0, 0, 0, 0], "rivers": [0] * 6},
                {"r": 0, "c": 1, "elev": 10, "cond": 1,
                 "roads": [0] * 6, "rivers": [0, 1, 0, 0, 0, 0]},
            ],
            [
                {"r": 1, "c": 0, "elev": 20, "cond": 2,
                 "roads": [0] * 6, "rivers": [0] * 6},
                {"r": 1, "c": 1, "elev": 30, "cond": 4,
                 "roads": [0] * 6, "rivers": [0] * 6},
            ],
        ],
    }


def _frame(operators: list[dict], *, passengers: list[dict] | None = None) -> dict:
    return {
        "scenario_id": "viewer-map",
        "terrain_id": 7,
        "time": {"cur_step": 0, "tick": 0},
        "operators": operators,
        "passengers": passengers or [],
        "jm_points": [], "cities": [], "scores": {}, "landmarks": {},
        "judge_info": [], "role_and_grouping_info": {}, "communication": [],
        "valid_actions": {}, "actions": [],
    }


def _unit(obj_id: int, color: int, current_hex: int) -> dict:
    return {
        "obj_id": obj_id,
        "color": color,
        "type": 2,
        "sub_type": 0,
        "name": f"unit-{obj_id}",
        "armor": 0,
        "cur_hex": current_hex,
        "blood": 100,
        "max_blood": 100,
    }
