"""Source-compatible, path-segment movement for the unified engine step."""

from wargame_native_engine.movement.map_adapter import SourceHexMap
from wargame_native_engine.movement.models import (
    MovementLegalityResult, MovementMode, RoadType, SegmentPlan, TerrainType,
)
from wargame_native_engine.movement.policy import (
    SourceMovementPolicy, load_source_movement_policy,
)

__all__ = [
    "MovementLegalityResult", "MovementMode", "RoadType", "SegmentPlan",
    "SourceHexMap", "SourceMovementPolicy", "TerrainType",
    "load_source_movement_policy",
]
