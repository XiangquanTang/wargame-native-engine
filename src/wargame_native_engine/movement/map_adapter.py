from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping, Protocol, runtime_checkable

from wargame_native_engine.io.scenario import native_map_identity_hash
from wargame_native_engine.movement.models import RoadType, TerrainType
from wargame_native_engine.movement.policy import SourceMovementPolicy

if TYPE_CHECKING:
    from wargame_native_engine.io.scenario import NativeHex, NativeMap


class MovementCostLookup(Protocol):
    """Existing immutable MapRuntime movement lookup contract."""

    source_sha256: str
    payload_sha256: str

    def factor(self, mode: int, from_hex: int, to_hex: int) -> Fraction | None: ...

    def supports_mode(self, mode: int) -> bool: ...


@dataclass(frozen=True, slots=True)
class RoadEdge:
    from_hex: int
    to_hex: int
    road_type: RoadType
    source_code: int


@runtime_checkable
class StaticMovementMapReader(Protocol):
    """Read-only static map surface required by movement planning."""

    @property
    def identity(self) -> Mapping[str, object]: ...

    @property
    def hex_size_m(self) -> int: ...

    @property
    def movement_cost_source_sha256(self) -> str | None: ...

    def contains(self, hex_id: int) -> bool: ...

    def neighbors(self, hex_id: int) -> tuple[int, ...]: ...

    def adjacent(self, from_hex: int, to_hex: int) -> bool: ...

    def terrain(self, hex_id: int) -> TerrainType: ...

    def terrain_for_segment(self, from_hex: int, to_hex: int) -> TerrainType: ...

    def road_edge(self, from_hex: int, to_hex: int) -> RoadEdge | None: ...

    def is_roadblock(self, hex_id: int) -> bool: ...

    def elevation(self, hex_id: int) -> float | None: ...

    def elevation_difference_m(self, from_hex: int, to_hex: int) -> float | None: ...

    def slope_level(self, from_hex: int, to_hex: int) -> int | None: ...

    def movement_factor(
        self, mode: int, from_hex: int, to_hex: int,
    ) -> Fraction | None: ...

    def supports_movement_mode(self, mode: int) -> bool: ...


class SourceHexMap:
    """Named adapter over documented pos/cond/elev/edge arrays."""

    __slots__ = ("native_map", "policy", "_cells", "_sealed")

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("SourceHexMap is immutable")
        object.__setattr__(self, name, value)

    def __init__(self, native_map: "NativeMap",
                 policy: SourceMovementPolicy) -> None:
        if not native_map.hexes:
            raise ValueError("source-compatible movement requires a hex grid")
        self.native_map = native_map
        self.policy = policy
        self._cells = MappingProxyType({
            item.hex_id: item for item in native_map.hexes
        })
        if len(self._cells) != len(native_map.hexes):
            raise ValueError("hex ids must be unique")
        for cell in native_map.hexes:
            if len(cell.neighbors) != 6 or len(cell.roads) != 6 or len(cell.rivers) != 6:
                raise ValueError(f"hex {cell.hex_id} requires six directional entries")
        self._sealed = True

    def cell(self, hex_id: int) -> "NativeHex":
        try:
            return self._cells[int(hex_id)]
        except KeyError as exc:
            raise ValueError(f"unknown hex: {hex_id}") from exc

    def contains(self, hex_id: int) -> bool:
        return int(hex_id) in self._cells

    def neighbors(self, hex_id: int) -> tuple[int, ...]:
        return tuple(int(value) for value in self.cell(hex_id).neighbors)

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


class SourceMovementMapReader:
    """Immutable movement facade borrowing an existing Source map/runtime.

    Construction validates and records identity once.  Per-edge reads return
    scalars, enums, tuples, or Fractions and never expose the NativeMap, its
    cell index, or the borrowed MapRuntimeData lookup for mutation/closure.
    """

    __slots__ = (
        "_cells",
        "_terrain",
        "_road_types",
        "_roadblocks",
        "_hex_size_m",
        "_elevation_unit_m",
        "_elevation_kind",
        "_movement_costs",
        "_movement_cost_source_sha256",
        "_identity",
        "_sealed",
    )

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("SourceMovementMapReader is immutable")
        object.__setattr__(self, name, value)

    def __init__(
        self,
        source: SourceHexMap,
        movement_costs: MovementCostLookup | None = None,
    ) -> None:
        native_map = source.native_map
        if native_map.hex_size_m is None:
            raise ValueError("source-compatible movement requires hex_size_m")
        self._cells = source._cells
        self._terrain = MappingProxyType({
            hex_id: source.terrain(hex_id) for hex_id in self._cells
        })
        self._road_types = MappingProxyType({
            int(code): configured[0]
            for code, configured in source.policy.road_codes
        })
        self._roadblocks = frozenset(int(value) for value in native_map.roadblocks)
        self._hex_size_m = int(native_map.hex_size_m)
        self._elevation_unit_m = native_map.elevation_unit_m
        self._elevation_kind = native_map.elevation_kind
        self._movement_costs = movement_costs
        self._movement_cost_source_sha256 = (
            None if movement_costs is None else movement_costs.source_sha256
        )
        map_sha256 = native_map_identity_hash(native_map)
        identity: dict[str, object] = {
            "map_id": native_map.map_id,
            "map_sha256": map_sha256,
        }
        if movement_costs is not None:
            identity.update({
                "movement_source_sha256": movement_costs.source_sha256,
                "movement_payload_sha256": movement_costs.payload_sha256,
            })
        self._identity = MappingProxyType(identity)
        self._sealed = True

    @property
    def identity(self) -> Mapping[str, object]:
        return self._identity

    @property
    def hex_size_m(self) -> int:
        return self._hex_size_m

    @property
    def movement_cost_source_sha256(self) -> str | None:
        return self._movement_cost_source_sha256

    def _cell(self, hex_id: int) -> "NativeHex":
        try:
            return self._cells[int(hex_id)]
        except KeyError as exc:
            raise ValueError(f"unknown hex: {hex_id}") from exc

    def _direction(self, from_hex: int, to_hex: int) -> int | None:
        try:
            return self._cell(from_hex).neighbors.index(int(to_hex))
        except ValueError:
            return None

    def contains(self, hex_id: int) -> bool:
        return int(hex_id) in self._cells

    def neighbors(self, hex_id: int) -> tuple[int, ...]:
        return tuple(int(value) for value in self._cell(hex_id).neighbors)

    def adjacent(self, from_hex: int, to_hex: int) -> bool:
        return self._direction(from_hex, to_hex) is not None

    def terrain(self, hex_id: int) -> TerrainType:
        try:
            return self._terrain[int(hex_id)]
        except KeyError as exc:
            raise ValueError(f"unknown hex: {hex_id}") from exc

    def terrain_for_segment(self, from_hex: int, to_hex: int) -> TerrainType:
        target = self.terrain(to_hex)
        if target is TerrainType.ROADBLOCK:
            return target
        forward = self._direction(from_hex, to_hex)
        reverse = self._direction(to_hex, from_hex)
        if forward is not None and reverse is not None and (
            int(self._cell(from_hex).rivers[forward]) > 0
            or int(self._cell(to_hex).rivers[reverse]) > 0
        ):
            return TerrainType.SMALL_RIVER
        return target

    def road_edge(self, from_hex: int, to_hex: int) -> RoadEdge | None:
        forward = self._direction(from_hex, to_hex)
        reverse = self._direction(to_hex, from_hex)
        if forward is None or reverse is None:
            return None
        left = int(self._cell(from_hex).roads[forward])
        right = int(self._cell(to_hex).roads[reverse])
        if left <= 0 or right <= 0 or left != right:
            return None
        return RoadEdge(
            int(from_hex),
            int(to_hex),
            self._road_types.get(left, RoadType.UNKNOWN),
            left,
        )

    def is_roadblock(self, hex_id: int) -> bool:
        return int(hex_id) in self._roadblocks

    def elevation(self, hex_id: int) -> float | None:
        value = self._cell(hex_id).elevation
        return None if value is None else float(value)

    def elevation_difference_m(self, from_hex: int, to_hex: int) -> float | None:
        left, right = self._cell(from_hex), self._cell(to_hex)
        if left.elevation is None or right.elevation is None:
            return None
        if self._elevation_unit_m is None:
            return None
        if self._elevation_kind == "LEVEL":
            return abs(right.elevation - left.elevation) * self._elevation_unit_m
        if self._elevation_kind == "METERS":
            return abs(right.elevation - left.elevation)
        return None

    def slope_level(self, from_hex: int, to_hex: int) -> int | None:
        left, right = self._cell(from_hex), self._cell(to_hex)
        unit = self._elevation_unit_m
        if left.elevation is None or right.elevation is None or unit is None or unit <= 0:
            return None
        difference = abs(right.elevation - left.elevation)
        if self._elevation_kind == "LEVEL":
            return int(round(difference))
        if self._elevation_kind == "METERS":
            value = difference / unit
            if abs(value - round(value)) > 1e-9:
                return None
            return int(round(value))
        return None

    def movement_factor(
        self, mode: int, from_hex: int, to_hex: int,
    ) -> Fraction | None:
        if self._movement_costs is None:
            return None
        return self._movement_costs.factor(mode, from_hex, to_hex)

    def supports_movement_mode(self, mode: int) -> bool:
        return (
            self._movement_costs is not None
            and self._movement_costs.supports_mode(mode)
        )
