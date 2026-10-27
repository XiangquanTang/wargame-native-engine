from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import math
from typing import Mapping


@dataclass(frozen=True, slots=True)
class HexCell:
    hex_id: int
    elevation: float
    terrain: str = "OPEN"

    @property
    def row_col(self) -> tuple[int, int]:
        return divmod(self.hex_id, 100)

    @property
    def intervening_height_bonus(self) -> float:
        return 1.0 if self.terrain.upper() in {"SETTLEMENT", "FOREST"} else 0.0


@dataclass(frozen=True, slots=True)
class HexMapProfile:
    """Terrain heights and their declared units for Native fallback LOS.

    Direct profile callers historically supply elevation levels. Native maps
    must pass their metadata explicitly; meter heights need a known grade size
    before the rule's one-level bonuses can be applied. Cached source LOS does
    not use this conversion and remains a separate authority.
    """

    cells: Mapping[int, HexCell]
    float_tolerance: float = 1e-9
    elevation_kind: str | None = "LEVEL"
    elevation_unit_m: float | None = None

    def cell(self, hex_id: int) -> HexCell:
        try:
            return self.cells[int(hex_id)]
        except KeyError as exc:
            raise ValueError(f"missing terrain/elevation data for hex {hex_id}") from exc

    def elevation_units_per_level(self) -> float:
        """Return the scale for grade-based LOS, never guess unknown units."""

        if self.elevation_kind == "LEVEL":
            return 1.0
        if self.elevation_kind != "METERS":
            raise ValueError("fallback LOS requires elevation_kind LEVEL or METERS")
        unit = self.elevation_unit_m
        if (isinstance(unit, bool) or not isinstance(unit, (int, float))
                or not math.isfinite(unit) or unit <= 0):
            raise ValueError(
                "fallback LOS for METERS requires positive finite elevation_unit_m"
            )
        return float(unit)


    def elevation_level_difference(self, shooter_hex: int, target_hex: int) -> int:
        """Signed target-minus-shooter grades for the discrete firing table.

        Keep raw map heights unchanged. Only declared METERS scales are divided;
        LEVEL values already are grades. Fractional grades are unsupported, not
        rounded, truncated, clamped or interpolated into a different rule cell.
        """
        scale = Fraction(str(self.elevation_units_per_level()))
        shooter = self.cell(shooter_hex).elevation
        target = self.cell(target_hex).elevation
        for value in (shooter, target):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value)):
                raise ValueError("fire elevation requires finite numeric map heights")
        difference = (Fraction(str(target)) - Fraction(str(shooter))) / scale
        if difference.denominator != 1:
            raise ValueError(
                "fire elevation difference is not an integral elevation level: "
                f"difference={difference}"
            )
        return int(difference)


@dataclass(frozen=True, slots=True)
class LineOfSightEvidence:
    visible: bool
    distance: int
    blocking_hexes: tuple[int, ...]
    samples: tuple[dict[str, float | int | str], ...]
    modifiers: tuple[dict[str, float | int | str], ...]


def evaluate_line_of_sight(profile: HexMapProfile, observer_hex: int,
                           target_hex: int) -> LineOfSightEvidence:
    start = profile.cell(observer_hex)
    end = profile.cell(target_hex)
    line = _hex_line(start.row_col, end.row_col)
    distance = len(line) - 1
    if distance <= 1:
        return LineOfSightEvidence(True, distance, (), (),
                                   ({"rule": "adjacent_hex_always_visible"},))
    level_size = profile.elevation_units_per_level()
    # Perform every LOS height comparison in levels, including the evidence.
    # A +1 bonus is one elevation grade, not one meter on a METERS map.
    start_height = start.elevation / level_size
    end_height = end.elevation / level_size
    modifiers: list[dict[str, float | int | str]] = []
    if start.elevation > end.elevation:
        start_height += 1.0
        modifiers.append({"rule": "high_endpoint_bonus", "hex_id": start.hex_id,
                          "value": 1.0})
    elif end.elevation > start.elevation:
        end_height += 1.0
        modifiers.append({"rule": "high_endpoint_bonus", "hex_id": end.hex_id,
                          "value": 1.0})
    blocking: list[int] = []
    samples: list[dict[str, float | int | str]] = []
    for index, (row, col) in enumerate(line[1:-1], 1):
        hex_id = row * 100 + col
        cell = profile.cell(hex_id)
        ratio = index / distance
        line_height = start_height + (end_height - start_height) * ratio
        terrain_height = cell.elevation / level_size
        modified_height = terrain_height + cell.intervening_height_bonus
        sample = {"hex_id": hex_id, "ratio": ratio,
                  "line_height": line_height,
                  "terrain": cell.terrain,
                  "terrain_elevation": terrain_height,
                  "modified_elevation": modified_height}
        samples.append(sample)
        if cell.intervening_height_bonus:
            modifiers.append({"rule": "intermediate_terrain_height_bonus",
                              "hex_id": hex_id, "value": 1.0,
                              "terrain": cell.terrain})
        if modified_height > line_height + profile.float_tolerance:
            blocking.append(hex_id)
    return LineOfSightEvidence(not blocking, distance, tuple(blocking),
                               tuple(samples), tuple(modifiers))


def hex_distance(left_hex: int, right_hex: int) -> int:
    left = _odd_r_to_cube(divmod(left_hex, 100))
    right = _odd_r_to_cube(divmod(right_hex, 100))
    return int(max(abs(left[0] - right[0]), abs(left[1] - right[1]),
                   abs(left[2] - right[2])))


def _hex_line(start: tuple[int, int], end: tuple[int, int]) -> tuple[tuple[int, int], ...]:
    a, b = _odd_r_to_cube(start), _odd_r_to_cube(end)
    count = int(max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2])))
    if count == 0:
        return (start,)
    values = []
    for index in range(count + 1):
        ratio = index / count
        cube = _cube_round(tuple(a[axis] + (b[axis] - a[axis]) * ratio
                                 for axis in range(3)))
        values.append(_cube_to_odd_r(cube))
    return tuple(values)


def _odd_r_to_cube(value: tuple[int, int]) -> tuple[int, int, int]:
    row, col = value
    x = col - (row - (row & 1)) // 2
    z = row
    return (x, -x - z, z)


def _cube_to_odd_r(value: tuple[int, int, int]) -> tuple[int, int]:
    x, _, z = value
    return (z, x + (z - (z & 1)) // 2)


def _cube_round(value: tuple[float, float, float]) -> tuple[int, int, int]:
    rx, ry, rz = (round(item) for item in value)
    dx, dy, dz = abs(rx - value[0]), abs(ry - value[1]), abs(rz - value[2])
    if dx > dy and dx > dz:
        rx = -ry - rz
    elif dy > dz:
        ry = -rx - rz
    else:
        rz = -rx - ry
    return int(rx), int(ry), int(rz)
