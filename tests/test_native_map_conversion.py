from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from wargame_native_engine.io.native_map_converter import (
    NativeMapConversionError,
    convert_benchmark_basic_map,
    publish_native_map_conversion,
)
from wargame_native_engine.movement.map_adapter import SourceHexMap
from wargame_native_engine.movement.policy import load_source_movement_policy


EXTERNAL_SNAPSHOT = Path(os.environ.get(
    "WARGAME_SOURCE_SNAPSHOT_ROOT",
    "local_data/source_snapshot",
))


def _cell(
    row: int,
    column: int,
    neighbors: list[int],
    *,
    position: int | str | None = None,
    elevation: int = 100,
    terrain: int = 0,
    roads: list[int] | None = None,
    rivers: list[int] | None = None,
) -> dict[str, object]:
    return {
        "r": row,
        "c": column,
        "pos": row * 100 + column if position is None else position,
        "elev": elevation,
        "cond": terrain,
        "neighbors": neighbors,
        "roads": roads or [0] * 6,
        "rivers": rivers or [0] * 6,
    }


def _map_document() -> dict[str, object]:
    return {
        "map_id": 7,
        "coordinate_system": "oddr",
        "scale": 200,
        "ele_grade": 20,
        "max_row": 2,
        "max_column": 2,
        "map_data": [
            [
                _cell(
                    0,
                    0,
                    [1, -1, -1, -1, -1, 100],
                    position="0000",
                    roads=[1, 0, 0, 0, 0, 0],
                ),
                _cell(
                    0,
                    1,
                    [-1, -1, -1, 0, 100, 101],
                    roads=[0, 0, 0, 1, 0, 0],
                    terrain=1,
                ),
            ],
            [
                _cell(1, 0, [101, 1, 0, -1, -1, -1], elevation=120),
                _cell(1, 1, [-1, -1, 1, 100, -1, -1], terrain=2),
            ],
        ],
    }


def _write_map(path: Path, value: dict[str, object] | None = None) -> Path:
    path.write_text(
        json.dumps(value or _map_document(), ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_conversion_is_deterministic_and_preserves_position_identity(
    tmp_path: Path,
) -> None:
    source = _write_map(tmp_path / "basic.json")
    before = source.read_bytes()
    first = convert_benchmark_basic_map(source)
    second = convert_benchmark_basic_map(source)

    assert first.native_map_payload == second.native_map_payload
    assert first.provenance_payload == second.provenance_payload
    assert first.native_map_payload.endswith(b"\n")
    assert first.native_map_payload.count(b"\n") == 1
    assert first.provenance_payload.endswith(b"\n")
    assert first.native_map.map_id == "7"
    assert first.native_map.hex_size_m == 200
    assert first.native_map.elevation_kind == "METERS"
    assert first.native_map.elevation_unit_m == 20
    assert first.native_map.hexes[0].hex_id == 0
    identities = first.provenance["position_identities"]
    assert identities[0] == {
        "row": 0,
        "column": 0,
        "source_position_id": "0000",
        "native_hex_id": 0,
    }
    assert first.provenance["output"]["sha256"] == hashlib.sha256(
        first.native_map_payload
    ).hexdigest()
    assert source.read_bytes() == before


def test_conversion_produces_native_adjudication_map(tmp_path: Path) -> None:
    result = convert_benchmark_basic_map(
        _write_map(tmp_path / "basic.json")
    )
    native_hex_map = SourceHexMap(
        result.native_map,
        load_source_movement_policy(),
    )

    assert native_hex_map.adjacent(0, 1)
    assert native_hex_map.road_edge(0, 1) is not None
    assert native_hex_map.elevation_difference_m(0, 100) == 20
    assert native_hex_map.slope_level(0, 100) == 1


def test_conversion_publication_is_complete_and_protects_existing_output(
    tmp_path: Path,
) -> None:
    result = convert_benchmark_basic_map(
        _write_map(tmp_path / "basic.json")
    )
    published = publish_native_map_conversion(result, tmp_path / "converted")

    assert published.native_map_path.read_bytes() == result.native_map_payload
    assert published.provenance_path.read_bytes() == result.provenance_payload
    with pytest.raises(FileExistsError):
        publish_native_map_conversion(result, tmp_path / "converted")


def test_missing_source_metadata_requires_explicit_options(
    tmp_path: Path,
) -> None:
    value = _map_document()
    value.pop("coordinate_system")
    value.pop("scale")
    value.pop("ele_grade")
    source = _write_map(tmp_path / "basic.json", value)

    with pytest.raises(NativeMapConversionError, match="coordinate_system"):
        convert_benchmark_basic_map(source)
    with pytest.raises(NativeMapConversionError, match="hex_size_m_override"):
        convert_benchmark_basic_map(
            source,
            coordinate_system_override="odd-r",
        )
    result = convert_benchmark_basic_map(
        source,
        coordinate_system_override="odd-r",
        hex_size_m_override=200,
        elevation_unit_m_override=20,
    )
    options = result.provenance["conversion_options"]
    assert options["hex_size_source"] == "explicit_override"
    assert options["elevation_unit_source"] == "explicit_override"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("cond", 99, "terrain"),
        ("roads", [9, 0, 0, 0, 0, 0], "road"),
        ("rivers", [2, 0, 0, 0, 0, 0], "river"),
        ("neighbors", [999, -1, -1, -1, -1, 100], "unknown neighbor"),
    ],
)
def test_unknown_adjudication_facts_are_rejected(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    document = _map_document()
    document["map_data"][0][0][field] = value  # type: ignore[index]
    source = _write_map(tmp_path / "basic.json", document)
    with pytest.raises(NativeMapConversionError, match=message):
        convert_benchmark_basic_map(source)


def test_conflicting_neighbor_alias_is_rejected(tmp_path: Path) -> None:
    document = _map_document()
    document["map_data"][0][0]["nb"] = [-1] * 6  # type: ignore[index]
    source = _write_map(tmp_path / "basic.json", document)
    with pytest.raises(NativeMapConversionError, match="conflicting"):
        convert_benchmark_basic_map(source)


@pytest.mark.skipif(
    not EXTERNAL_SNAPSHOT.is_dir(),
    reason="five-sample read-only snapshot is unavailable",
)
def test_four_unique_five_sample_maps_convert_without_modification() -> None:
    expected = {
        "123": (10_000, 10.0, ()),
        "221": (4_200, 10.0, ()),
        "96": (
            7_084,
            20.0,
            (
                "asymmetric_road_edges_preserved:43",
                "asymmetric_river_edges_preserved:241",
            ),
        ),
        "9601": (
            7_084,
            20.0,
            (
                "asymmetric_road_edges_preserved:43",
                "asymmetric_river_edges_preserved:241",
            ),
        ),
    }
    for map_id, (cell_count, elevation_unit, warnings) in expected.items():
        source = EXTERNAL_SNAPSHOT / "maps" / map_id / "basic.json"
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        result = convert_benchmark_basic_map(
            source,
            coordinate_system_override=(
                "odd-r" if map_id in {"96", "9601"} else None
            ),
            hex_size_m_override=(
                200 if map_id in {"96", "9601"} else None
            ),
        )
        assert len(result.native_map.hexes) == cell_count
        assert result.native_map.elevation_unit_m == elevation_unit
        assert tuple(result.provenance["warnings"]) == warnings
        assert result.provenance["source"]["sha256"] == before
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before
