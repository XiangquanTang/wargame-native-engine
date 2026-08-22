#!/usr/bin/env python3
"""Generate the project-authored public close-combat demo fixtures."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from examples.generate_public_battlefield import (
        BRIDGE_ROWS,
        COLUMNS,
        ROWS,
        _cells,
    )
except ModuleNotFoundError:  # Direct ``python examples/...`` execution.
    from generate_public_battlefield import (
        BRIDGE_ROWS,
        COLUMNS,
        ROWS,
        _cells,
    )


MAP_ID = "public-close-combat-24x18"
SCENARIO_ID = "public-close-combat-contact"
POSITIONS = {
    "red": (
        (101, 11, 4),
        (102, 10, 5),
        (103, 11, 6),
        (104, 11, 7),
        (105, 11, 8),
        (106, 8, 9),
    ),
    "blue": (
        (201, 17, 4),
        (202, 16, 5),
        (203, 17, 6),
        (204, 17, 7),
        (205, 19, 8),
        (206, 14, 9),
    ),
}


def build_records() -> tuple[dict[str, Any], dict[str, Any]]:
    """Return one deterministic close-contact scenario and viewer map."""

    cells = _cells()
    operators = _operators()
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
        "operators": operators,
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
                    for item in operators
                    if item["side"] == side
                }
                for side in ("red", "blue")
            }
        },
        "terminal_rules": {
            "max_ticks": 120,
            "elimination": True,
            "objective_score_to_win": None,
        },
        "metadata": {
            "fixture_type": "project_authored_public_close_combat_demo",
            "description": (
                "Synthetic 24x18 close-contact layout for Human and Rule Agent demos."
            ),
            "deployment_contract": (
                "paired starting positions have clear LOS at six to eight hexes"
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
    scenario_path = output_dir / "public_close_combat_scenario.json"
    map_path = output_dir / "public_close_combat_map.json"
    for path, value in ((scenario_path, scenario), (map_path, map_record)):
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return scenario_path, map_path


def _operators() -> list[dict[str, Any]]:
    operators = []
    for side, records in POSITIONS.items():
        for index, (operator_id, column, row) in enumerate(records):
            vehicle = index % 2 == 0
            operators.append({
                "operator_id": operator_id,
                "side": side,
                "operator_type": "VEHICLE" if vehicle else "PERSONNEL",
                "subtype": (
                    "close-combat-vehicle" if vehicle
                    else "close-combat-infantry"
                ),
                "x": column,
                "y": row,
                "altitude": 0,
                "heading": 0 if side == "red" else 180,
                "health": 100,
                "max_health": 100,
                "blood": 5,
                "max_blood": 5,
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
                "weapon_locked": False,
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
