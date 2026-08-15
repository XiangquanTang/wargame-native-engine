from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from wargame_native_engine.io.serialization import json_clone


class TerrainType(str, Enum):
    NORMAL = "NORMAL"
    FOREST = "FOREST"
    SETTLEMENT = "SETTLEMENT"
    SMALL_RIVER = "SMALL_RIVER"
    LARGE_RIVER = "LARGE_RIVER"
    SOFT_GROUND = "SOFT_GROUND"
    ROADBLOCK = "ROADBLOCK"
    UNKNOWN = "UNKNOWN"


class RoadType(str, Enum):
    RURAL = "RURAL"
    GENERAL = "GENERAL"
    GRADED = "GRADED"
    UNKNOWN = "UNKNOWN"


class MovementMode(str, Enum):
    NORMAL = "NORMAL"
    MARCHING = "MARCHING"
    CHARGE_1 = "CHARGE_1"
    CHARGE_2 = "CHARGE_2"


@dataclass(frozen=True, slots=True)
class MovementLegalityResult:
    legal: bool
    reason_code: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"legal": self.legal, "reason_code": self.reason_code,
                "evidence": json_clone(self.evidence)}


@dataclass(frozen=True, slots=True)
class SegmentPlan:
    from_hex: int
    to_hex: int
    base_speed_kph: float
    terrain_type: TerrainType
    terrain_multiplier: tuple[int, int]
    elevation_from: float
    elevation_to: float
    elevation_difference: float
    slope_level: int
    slope_multiplier: tuple[int, int]
    road_type: RoadType | None
    road_edge: tuple[int, int] | None
    road_override: bool
    movement_mode: MovementMode
    mode_multiplier: tuple[int, int]
    effective_speed_milli_kph: int
    effective_speed_micro_hex_per_second: int
    segment_distance_m: int
    traversal_ms: int
    map_cost_mode: int | None = None
    map_cost_factor: tuple[int, int] | None = None
    map_cost_source_sha256: str | None = None

    @property
    def effective_speed_kph(self) -> float:
        return self.effective_speed_milli_kph / 1000.0

    @property
    def effective_speed_hex_per_second(self) -> float:
        return self.effective_speed_micro_hex_per_second / 1_000_000.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_hex": self.from_hex, "to_hex": self.to_hex,
            "base_speed": self.base_speed_kph,
            "terrain_type": self.terrain_type.value,
            "terrain_multiplier": list(self.terrain_multiplier),
            "elevation_from": self.elevation_from,
            "elevation_to": self.elevation_to,
            "elevation_difference": self.elevation_difference,
            "slope_level": self.slope_level,
            "slope_multiplier": list(self.slope_multiplier),
            "road_type": None if self.road_type is None else self.road_type.value,
            "road_edge": None if self.road_edge is None else list(self.road_edge),
            "road_override": self.road_override,
            "movement_mode": self.movement_mode.value,
            "mode_multiplier": list(self.mode_multiplier),
            "effective_speed": self.effective_speed_kph,
            "effective_speed_hex_per_second": self.effective_speed_hex_per_second,
            "segment_distance_m": self.segment_distance_m,
            "traversal_seconds": self.traversal_ms / 1000.0,
            "map_cost_mode": self.map_cost_mode,
            "map_cost_factor": (
                None
                if self.map_cost_factor is None
                else list(self.map_cost_factor)
            ),
            "map_cost_source_sha256": self.map_cost_source_sha256,
        }


@dataclass(frozen=True, slots=True)
class RoutePlan:
    reachable: bool
    reason_code: str
    unit_id: int
    source_hex: int | None
    destination_hex: int
    movement_mode: MovementMode
    ordered_path: tuple[int, ...] = ()
    estimated_duration_ms: int | None = None
    segments: tuple[SegmentPlan, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "reason_code": self.reason_code,
            "unit_id": self.unit_id,
            "source_hex": self.source_hex,
            "destination_hex": self.destination_hex,
            "movement_mode": self.movement_mode.value,
            "ordered_path": list(self.ordered_path),
            "estimated_duration_ms": self.estimated_duration_ms,
            "segment_details": [item.to_dict() for item in self.segments],
        }
