from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from wargame_native_engine.movement.models import RoadType, TerrainType
from wargame_native_engine.movement.policy import SourceMovementPolicy

if TYPE_CHECKING:
    from wargame_native_engine.io.scenario import NativeHex, NativeMap


@dataclass(frozen=True, slots=True)
class RoadEdge:
    from_hex: int
    to_hex: int
    road_type: RoadType
    source_code: int


class SourceHexMap:
    """Named adapter over documented pos/cond/elev/edge arrays."""

    def __init__(self, native_map: "NativeMap",
                 policy: SourceMovementPolicy) -> None:
        if not native_map.hexes:
            raise ValueError("source-compatible movement requires a hex grid")
        self.native_map = native_map
        self.policy = policy
        self._cells = {item.hex_id: item for item in native_map.hexes}
        if len(self._cells) != len(native_map.hexes):
            raise ValueError("hex ids must be unique")
        for cell in native_map.hexes:
            if len(cell.neighbors) != 6 or len(cell.roads) != 6 or len(cell.rivers) != 6:
                raise ValueError(f"hex {cell.hex_id} requires six directional entries")

    def cell(self, hex_id: int) -> "NativeHex":
        try:
            return self._cells[int(hex_id)]
        except KeyError as exc:
            raise ValueError(f"unknown hex: {hex_id}") from exc

    def terrain(self, hex_id: int) -> TerrainType:
        cell = self.cell(hex_id)
        return self.policy.terrain(cell.terrain)

    def direction(self, from_hex: int, to_hex: int) -> int | None:
        current = self.cell(from_hex)
        try:
            return current.neighbors.index(int(to_hex))
        except ValueError:
            return None

    def adjacent(self, from_hex: int, to_hex: int) -> bool:
        return self.direction(from_hex, to_hex) is not None

    def road_edge(self, from_hex: int, to_hex: int) -> RoadEdge | None:
        forward = self.direction(from_hex, to_hex)
        reverse = self.direction(to_hex, from_hex)
        if forward is None or reverse is None:
            return None
        left = int(self.cell(from_hex).roads[forward])
        right = int(self.cell(to_hex).roads[reverse])
        if left <= 0 or right <= 0 or left != right:
            return None
        mapped = self.policy.road(left)
        if mapped is None:
            return RoadEdge(from_hex, to_hex, RoadType.UNKNOWN, left)
        return RoadEdge(from_hex, to_hex, mapped[0], left)

    def crosses_small_river(self, from_hex: int, to_hex: int) -> bool:
        forward = self.direction(from_hex, to_hex)
        reverse = self.direction(to_hex, from_hex)
        if forward is None or reverse is None:
            return False
        return (int(self.cell(from_hex).rivers[forward]) > 0
                or int(self.cell(to_hex).rivers[reverse]) > 0)

    def terrain_for_segment(self, from_hex: int, to_hex: int) -> TerrainType:
        target = self.terrain(to_hex)
        if target is TerrainType.ROADBLOCK:
            return target
        if self.crosses_small_river(from_hex, to_hex):
            return TerrainType.SMALL_RIVER
        return target

    def elevation_difference_m(self, from_hex: int, to_hex: int) -> float | None:
        left, right = self.cell(from_hex), self.cell(to_hex)
        if left.elevation is None or right.elevation is None:
            return None
        if self.native_map.elevation_unit_m is None:
            return None
        if self.native_map.elevation_kind == "LEVEL":
            return abs(right.elevation - left.elevation) * self.native_map.elevation_unit_m
        if self.native_map.elevation_kind == "METERS":
            return abs(right.elevation - left.elevation)
        return None

    def slope_level(self, from_hex: int, to_hex: int) -> int | None:
        left, right = self.cell(from_hex), self.cell(to_hex)
        unit = self.native_map.elevation_unit_m
        if left.elevation is None or right.elevation is None or unit is None or unit <= 0:
            return None
        difference = abs(right.elevation - left.elevation)
        if self.native_map.elevation_kind == "LEVEL":
            return int(round(difference))
        if self.native_map.elevation_kind == "METERS":
            value = difference / unit
            if abs(value - round(value)) > 1e-9:
                return None
            return int(round(value))
        return None
