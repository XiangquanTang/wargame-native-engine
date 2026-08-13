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
from wargame_native_engine.viewer.scenario_config import (
    ViewerScenarioConfigError,
    load_viewer_scenario_config,
)
from wargame_native_engine.viewer.replay_view import _frame_record


ROOT = Path(__file__).resolve().parents[1]
MAP_RENDERER = (
    ROOT / "src/wargame_native_engine/viewer/static/map_renderer.js"
)
EXTERNAL_SNAPSHOT = Path(
    "/home/txq/wargame/wargame_benchmark_lab/local_data/"
    "sdk_data_snapshot/phase9a_multireplay_pilot"
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
    assert record["elevation"] == {
        "mode": "episode", "minimum": 0.0, "maximum": 30.0, "levels": 7,
    }
    assert record["cells"][0]["position_id"] == "0"


def test_missing_coordinate_system_requires_explicit_odd_r_override(
    tmp_path: Path,
) -> None:
    path = tmp_path / "old-basic.json"
    value = _map_config()
    del value["coordinate_system"]
    for row in value["map_data"]:
        for cell in row:
            cell.pop("r")
            cell.pop("c")
            cell["pos"] = cell.get("pos", 0)
    for row_index, row in enumerate(value["map_data"]):
        for column_index, cell in enumerate(row):
            cell["pos"] = row_index * 100 + column_index
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ViewerMapConfigError, match="coordinate_system"):
        load_viewer_map_config(path)
    loaded = load_viewer_map_config(
        path,
        coordinate_system_override="odd-r",
    )
    assert loaded.to_record()["source"]["coordinate_system"] == (
        "explicit_override"
    )


def test_explicit_scenario_overlay_keeps_only_static_display_facts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "scenario.json"
    payload = {
        "scenario_id": "s1",
        "operators": [{"obj_id": 7, "cur_hex": 101}],
        "cities": [{"coord": 102, "name": "objective", "flag": 0, "value": 80}],
        "landmarks": {"roadblocks": [103], "minefields": [{"pos": 104}]},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_viewer_scenario_config(
        path,
        expected_scenario_id="s1",
        expected_map_id="7",
    ).to_record()

    assert loaded["bound_map_id"] == "7"
    assert loaded["dynamic_units_source"] == "replay_frames"
    assert loaded["initial_unit_count"] == 1
    assert [item["kind"] for item in loaded["features"]] == [
        "city", "roadblocks", "minefields",
    ]
    assert {item["position_id"] for item in loaded["features"]} == {
        "102", "103", "104",
    }
    with pytest.raises(ViewerScenarioConfigError, match="does not match"):
        load_viewer_scenario_config(
            path,
            expected_scenario_id="different",
            expected_map_id="7",
        )


@pytest.mark.skipif(
    not EXTERNAL_SNAPSHOT.is_dir(),
    reason="five-sample read-only snapshot is unavailable",
)
def test_all_five_sample_map_scenario_bindings_are_display_compatible() -> None:
    bindings = (
        ("123", "2201010105", False),
        ("221", "2201010101", False),
        ("96", "2020331196", True),
        ("9601", "201033029601", True),
    )
    for map_id, scenario_id, needs_override in bindings:
        map_path = EXTERNAL_SNAPSHOT / "maps" / map_id / "basic.json"
        scenario_path = EXTERNAL_SNAPSHOT / "scenarios" / f"{scenario_id}.json"
        loaded_map = load_viewer_map_config(
            map_path,
            coordinate_system_override="odd-r" if needs_override else None,
        )
        loaded_scenario = load_viewer_scenario_config(
            scenario_path,
            expected_scenario_id=scenario_id,
            expected_map_id=map_id,
        )
        assert loaded_map.map_id == map_id
        assert loaded_map.cells
        assert loaded_scenario.features
        assert loaded_scenario.source_sha256


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
    assert "function computeWorldBounds(mapData, units = [])" in source
    assert "const representativeRows = new Set([minRow, maxRow])" in source
    assert "return computeWorldBounds(this.map, this.frame?.viewer_units || [])" in source
    assert "function pixelToHex(x, y, hexSize, bounds = null)" in source
    assert "this.onHexSelected" in source
    assert "this._drawScenarioFeatures" in source


@pytest.mark.parametrize(("min_row", "max_row"), [
    (0, 0),
    (1, 1),
    (0, 1),
    (0, 2),
    (1, 3),
])
def test_odd_r_complete_bounds_cover_every_row_parity(
    min_row: int,
    max_row: int,
) -> None:
    bounds = _expected_odd_r_bounds(
        min_row=min_row,
        max_row=max_row,
        min_column=0,
        max_column=5,
        hex_size=18.0,
    )

    assert all(math.isfinite(value) for value in bounds.values())
    assert bounds["maxX"] > bounds["minX"]
    assert bounds["maxY"] > bounds["minY"]
    if min_row <= 1 <= max_row:
        odd_right = _web_reference_hex_to_pixel(1, 5, 18.0)[0]
        assert bounds["maxX"] >= odd_right + math.sqrt(3.0) * 18.0 / 2


def test_middle_odd_row_stack_reserves_fit_padding_beyond_hex() -> None:
    hex_size = 18.0
    map_bounds = _expected_odd_r_bounds(
        min_row=0,
        max_row=2,
        min_column=0,
        max_column=5,
        hex_size=hex_size,
    )
    center_x, center_y = _web_reference_hex_to_pixel(1, 5, hex_size)
    stack_extent = 30 + 19
    assert center_x + math.sqrt(3.0) * hex_size / 2 == pytest.approx(
        map_bounds["maxX"]
    )
    assert center_y < map_bounds["maxY"]
    assert stack_extent == 49
    source = MAP_RENDERER.read_text(encoding="utf-8")
    assert "let screenPadding = SELECTED_UNIT_SCREEN_RADIUS + MAX_STACK_SCREEN_SPREAD" in source
    assert "const margin = 28 + Math.max(0, bounds.screenPadding || 0)" in source


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_map_renderer_executes_in_node_when_available() -> None:
    script = (
        "const r=require(process.argv[1]);"
        "const p=r.hexToPixel(1,0,36);"
        "if(Math.abs(p.x-Math.sqrt(3)*36)>1e-9||p.y!==90)process.exit(2);"
        "const s=r.stackUnits([{row:1,column:1,operator_id:2,category:'passenger'},"
        "{row:1,column:1,operator_id:1,category:'operator'}]);"
        "if(s.length!==1||s[0][0].operator_id!==1)process.exit(3);"
        "const m={hex_size:18,bounds:{min_row:0,max_row:2,min_column:0,max_column:5},cells:[]};"
        "const u=[1,2,3].map(i=>({row:1,column:5,operator_id:i,category:'operator'}));"
        "const b=r.computeWorldBounds(m,u);"
        "const expected=r.hexToPixel(1,5,18).x+Math.sqrt(3)*18/2;"
        "if(Math.abs(b.maxX-expected)>1e-9||b.screenPadding!==49)process.exit(4);"
        "const h=r.pixelToHex(p.x,p.y,36,{min_row:0,max_row:2,min_column:0,max_column:2});"
        "if(!h||h.row!==1||h.column!==0)process.exit(5);"
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


def _expected_odd_r_bounds(
    *,
    min_row: int,
    max_row: int,
    min_column: int,
    max_column: int,
    hex_size: float,
) -> dict[str, float]:
    points = []
    for row in range(min_row, max_row + 1):
        for column in (min_column, max_column):
            center_x, center_y = _web_reference_hex_to_pixel(
                row,
                column,
                hex_size,
            )
            points.extend((
                (center_x + hex_size * math.cos(math.radians(60 * index - 90)),
                 center_y + hex_size * math.sin(math.radians(60 * index - 90)))
                for index in range(6)
            ))
    return {
        "minX": min(point[0] for point in points),
        "minY": min(point[1] for point in points),
        "maxX": max(point[0] for point in points),
        "maxY": max(point[1] for point in points),
    }


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
