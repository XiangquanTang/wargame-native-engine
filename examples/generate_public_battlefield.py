#!/usr/bin/env python3
"""Generate the project-authored public river-valley demo fixtures."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROWS = 18
COLUMNS = 24
BRIDGE_ROWS = frozenset({5, 9, 13})
MAP_ID = "public-river-valley-24x18"
SCENARIO_ID = "public-river-valley-contact"


def build_records() -> tuple[dict[str, Any], dict[str, Any]]:
    """Return deterministic canonical scenario and presentation map records."""

    cells = _cells()
    scenario = {
        "schema_version": "native-scenario/1.0",
        "scenario_id": SCENARIO_ID,
        "initial_stage": "combat",
        "map": {
            "map_id": MAP_ID,
            "width": COLUMNS,
            "height": ROWS,
            "distance_unit": "hex",
            "blocked_positions": [],
            "hex_size_m": 200,
            "elevation_unit_m": 20,
            "elevation_kind": "LEVEL",
            "roadblocks": [],
            "hexes": [
                {
                    key: value
                    for key, value in cell.items()
                    if key not in {"r", "c"}
                }
                for cell in cells
            ],
        },
        "sides": ["red", "blue"],
        "operators": _operators(),
        "objectives": [
            {
                "objective_id": index,
                "x": 12,
                "y": row,
                "radius": 1,
                "value": 10,
                "controlling_side": None,
            }
            for index, row in enumerate(sorted(BRIDGE_ROWS), start=1)
        ],
        "deployment": {
            "positions": {
                side: {
                    str(item["operator_id"]): [item["x"], item["y"]]
                    for item in _operators()
                    if item["side"] == side
                }
                for side in ("red", "blue")
            }
        },
        "terminal_rules": {
            "max_ticks": 600,
            "elimination": True,
            "objective_score_to_win": None,
        },
        "metadata": {
            "fixture_type": "project_authored_public_river_valley_demo",
            "description": (
                "Synthetic 24x18 terrain-rich map for the public realtime Web demo."
            ),
            "source_compatible": {
                "weapons": {
                    "36": {
                        "weapon_class": "large_direct_fire_gun",
                        "maximum_range": 10,
                        "target_domains": ["PERSONNEL", "VEHICLE"],
                        "ammunition_key": "0",
                        "requires_unfolded": True,
                        "can_fire_while_moving": False,
                    },
                    "29": {
                        "weapon_class": "infantry_light_weapon",
                        "maximum_range": 4,
                        "target_domains": ["PERSONNEL", "VEHICLE"],
                        "ammunition_key": "0",
                        "requires_unfolded": False,
                        "can_fire_while_moving": False,
                    },
                }
            },
        },
    }
    map_record = {
        "map_id": MAP_ID,
        "coordinate_system": "odd-r",
        "max_row": ROWS + 1,
        "max_column": COLUMNS + 1,
        "hex_size": 23,
        "map_data": [
            [],
            *[
                [None, *[cell for cell in cells if cell["r"] == row]]
                for row in range(1, ROWS + 1)
            ],
        ],
    }
    return scenario, map_record


def write_records(output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    scenario, map_record = build_records()
    scenario_path = output_dir / "public_river_valley_scenario.json"
    map_path = output_dir / "public_river_valley_map.json"
    for path, value in ((scenario_path, scenario), (map_path, map_record)):
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return scenario_path, map_path


def _cells() -> list[dict[str, Any]]:
    cells: dict[int, dict[str, Any]] = {}
    for row in range(1, ROWS + 1):
        for column in range(1, COLUMNS + 1):
            position = row * 100 + column
            cells[position] = {
                "r": row,
                "c": column,
                "pos": position,
                "elev": _elevation(row, column),
                "cond": _terrain(row, column),
                "neighbors": _neighbors(row, column),
                "roads": [0, 0, 0, 0, 0, 0],
                "rivers": [0, 0, 0, 0, 0, 0],
            }

    road_positions = {
        position
        for position, cell in cells.items()
        if cell["r"] in BRIDGE_ROWS or cell["c"] in {6, 19}
    }
    for position, cell in cells.items():
        for direction, neighbor in enumerate(cell["neighbors"]):
            if neighbor in road_positions and position in road_positions:
                cell["roads"][direction] = 2 if cell["r"] == 9 else 1
        if cell["c"] == 12 and cell["r"] not in BRIDGE_ROWS:
            east = cell["neighbors"][0]
            if east != -1:
                cell["rivers"][0] = 2
                cells[east]["rivers"][3] = 2
    return [cells[position] for position in sorted(cells)]


def _neighbors(row: int, column: int) -> list[int]:
    if row % 2:
        offsets = ((0, 1), (1, 1), (1, 0), (0, -1), (-1, 0), (-1, 1))
    else:
        offsets = ((0, 1), (1, 0), (1, -1), (0, -1), (-1, -1), (-1, 0))
    values = []
    for row_delta, column_delta in offsets:
        target_row = row + row_delta
        target_column = column + column_delta
        values.append(
            target_row * 100 + target_column
            if 1 <= target_row <= ROWS and 1 <= target_column <= COLUMNS
            else -1
        )
    return values


def _terrain(row: int, column: int) -> int:
    if column in {12, 13} and row not in BRIDGE_ROWS:
        return 4
    if (7 <= row <= 11 and 8 <= column <= 10) or (
        8 <= row <= 12 and 15 <= column <= 17
    ):
        return 2
    if (row <= 6 and 7 <= column <= 11) or (
        row >= 13 and 14 <= column <= 19
    ):
        return 1
    if (row >= 12 and column <= 8) or (row <= 5 and column >= 18):
        return 3
    return 0


def _elevation(row: int, column: int) -> int:
    if column in {12, 13}:
        return 0
    western_ridge = max(0, 5 - (abs(row - 6) + abs(column - 6)) // 2)
    eastern_ridge = max(0, 6 - (abs(row - 13) + abs(column - 19)) // 2)
    return max(western_ridge, eastern_ridge)


def _operators() -> list[dict[str, Any]]:
    positions = {
        "red": ((101, 4, 4), (102, 6, 5), (103, 11, 7),
                (104, 6, 10), (105, 4, 13), (106, 6, 15)),
        "blue": ((201, 21, 4), (202, 19, 6), (203, 17, 7),
                 (204, 19, 11), (205, 21, 14), (206, 19, 16)),
    }
    # Keep one symmetric vehicle contact in the open central corridor.  Hexes
    # 711 and 717 have deterministic clear LOS and are six hexes apart, so the
    # public Web demo exposes a real SHOOT action immediately without changing
    # observation, weapon-range, or direct-fire rules.
    operators = []
    for side, records in positions.items():
        for index, (operator_id, column, row) in enumerate(records):
            vehicle = index % 2 == 0
            operators.append({
                "operator_id": operator_id,
                "side": side,
                "operator_type": "VEHICLE" if vehicle else "PERSONNEL",
                "subtype": "public-demo-vehicle" if vehicle else "public-demo-infantry",
                "x": column,
                "y": row,
                "altitude": 0,
                "heading": 0 if side == "red" else 180,
                "health": 100,
                "max_health": 100,
                "blood": 3,
                "max_blood": 3,
                "speed": 0,
                "max_speed": 54 if vehicle else 6,
                "basic_speed": 54 if vehicle else 6,
                "move_range": 0,
                "sensor_range": 25 if vehicle else 12,
                "state": "STOPPED",
                "alive": True,
                "deployed": True,
                "weapon_ids": [36 if vehicle else 29],
                "attack_range": 0,
                "attack_damage": 0,
                "current_hex": row * 100 + column,
                "cur_pos_millionths": 0,
                "move_path": [],
                "movement_mode": "NORMAL",
                "stopped": True,
                "weapon_unfolded": True,
                "raw_attributes": {
                    "armor_class": "MEDIUM" if vehicle else "NONE",
                    "ammunition": {"0": 20},
                },
            })
    return operators


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("examples"))
    args = parser.parse_args()
    scenario, map_path = write_records(args.output_dir)
    print(scenario)
    print(map_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
