from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable

from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.core.world import WorldState
from wargame_native_engine.movement.map_adapter import (
    MovementCostLookup,
    SourceHexMap,
    SourceMovementMapReader,
    StaticMovementMapReader,
)
from wargame_native_engine.movement.models import (
    MovementLegalityResult, MovementMode, RoadType, SegmentPlan, TerrainType,
)
from wargame_native_engine.movement.policy import SourceMovementPolicy


@dataclass(frozen=True, slots=True)
class MovementUnitSnapshot:
    """Detached movement-only projection of an authoritative operator."""

    operator_id: int
    side: str
    operator_type: str
    canonical_type: str
    basic_speed: float | None
    current_hex: int | None
    state: str
    moving: bool
    move_path: tuple[int, ...]
    transition_kind: str | None
    move_to_stop_remaining_ms: int
    fatigue_level: int
    weapon_locked: bool
    suppression_remaining_ms: int

    @classmethod
    def from_operator(cls, operator: OperatorState) -> "MovementUnitSnapshot":
        return cls(
            operator_id=int(operator.operator_id),
            side=str(operator.side),
            operator_type=str(operator.operator_type),
            canonical_type=str(operator.raw_attributes.get(
                "canonical_type", operator.operator_type,
            )),
            basic_speed=(
                None if operator.basic_speed is None else float(operator.basic_speed)
            ),
            current_hex=(
                None if operator.current_hex is None else int(operator.current_hex)
            ),
            state=str(operator.state),
            moving=bool(operator.moving),
            move_path=tuple(int(value) for value in operator.move_path),
            transition_kind=operator.transition_kind,
            move_to_stop_remaining_ms=int(operator.move_to_stop_remaining_ms),
            fatigue_level=int(operator.fatigue_level),
            weapon_locked=bool(operator.weapon_locked),
            suppression_remaining_ms=int(operator.suppression_remaining_ms),
        )


MovementOperator = OperatorState | MovementUnitSnapshot


class MovementPlanner:
    def __init__(self, hex_map: SourceHexMap | StaticMovementMapReader,
                 policy: SourceMovementPolicy,
                 movement_costs: MovementCostLookup | None = None) -> None:
        self.map_reader: StaticMovementMapReader = (
            SourceMovementMapReader(hex_map, movement_costs)
            if isinstance(hex_map, SourceHexMap)
            else hex_map
        )
        # Kept as a compatibility attribute for direct consumers.  It now
        # exposes only the narrow read-only reader instead of NativeMap cells.
        self.hex_map = self.map_reader
        self.policy = policy
        if self.map_reader.hex_size_m != policy.hex_size_m:
            raise ValueError("map hex_size_m does not match source movement policy")

    def validate_path(self, operator: OperatorState, path: Iterable[int],
                      mode: MovementMode,
                      world: WorldState) -> MovementLegalityResult:
        values = tuple(int(item) for item in path)
        evidence = {"unit_id": operator.operator_id,
                    "current_hex": operator.current_hex,
                    "path": list(values), "movement_mode": mode.value}
        if not values:
            return _illegal("PATH_REQUIRED", evidence)
        operator_legality = self.validate_operator(operator, mode)
        if not operator_legality.legal:
            return MovementLegalityResult(
                False,
                operator_legality.reason_code,
                {**evidence, **dict(operator_legality.evidence)},
            )

        current = operator.current_hex
        if current is None:  # kept explicit for static type narrowing
            return _illegal("CURRENT_HEX_REQUIRED", evidence)
        segment_evidence: list[dict[str, object]] = []
        for target in values:
            legality, plan = self.plan_segment(operator, current, target, mode)
            segment_evidence.append(legality.to_dict())
            if not legality.legal:
                return MovementLegalityResult(False, legality.reason_code,
                                              {**evidence,
                                               "segments": segment_evidence})
            if self.friendly_ground_count(world, target,
                                          side=operator.side,
                                          exclude=operator.operator_id) >= (
                                              self.policy.friendly_ground_stack_limit):
                return _illegal("FRIENDLY_STACK_LIMIT",
                                {**evidence, "target_hex": target,
                                 "segments": segment_evidence})
            if plan is None:
                raise RuntimeError("legal segment did not produce a plan")
            current = target
        return MovementLegalityResult(True, "LEGAL",
                                      {**evidence, "segments": segment_evidence})

    def validate_operator(
        self,
        operator: MovementOperator,
        mode: MovementMode,
    ) -> MovementLegalityResult:
        """Validate movement-task state without duplicating segment costs."""

        evidence = {
            "unit_id": operator.operator_id,
            "current_hex": operator.current_hex,
            "movement_mode": mode.value,
        }
        if operator.current_hex is None:
            return _illegal("CURRENT_HEX_REQUIRED", evidence)
        if operator.moving or operator.move_path:
            return _illegal("MOVE_TASK_ALREADY_ACTIVE", evidence)
        if operator.transition_kind is not None or operator.move_to_stop_remaining_ms > 0:
            return _illegal("TRANSITION_IN_PROGRESS", evidence)
        canonical = (
            operator.canonical_type
            if isinstance(operator, MovementUnitSnapshot)
            else operator.raw_attributes.get(
                "canonical_type", operator.operator_type,
            )
        )
        canonical = str(canonical).upper()
        if canonical == "ARTILLERY":
            return _illegal("ARTILLERY_CANNOT_MOVE", evidence)
        domain = operator.operator_type.upper()
        if domain == "AIR":
            if (
                self.map_reader.movement_cost_source_sha256 is not None
                and not self.map_reader.supports_movement_mode(3)
            ):
                return _illegal("AIR_MOVEMENT_UNSUPPORTED", evidence)
            if mode is not MovementMode.NORMAL:
                return _illegal("AIR_MOVEMENT_MODE_UNSUPPORTED", evidence)
        elif domain not in {"PERSONNEL", "VEHICLE"}:
            return _illegal("UNKNOWN_UNIT_DOMAIN", evidence)
        if operator.basic_speed is None or operator.basic_speed <= 0:
            return _illegal("BASIC_SPEED_UNAVAILABLE", evidence)
        if operator.suppression_remaining_ms > 0 or operator.state == "SUPPRESSED":
            return _illegal("ACTOR_SUPPRESSED", evidence)
        if domain == "PERSONNEL":
            if operator.fatigue_level >= 2:
                return _illegal("FATIGUE_PREVENTS_MOVEMENT", evidence)
            if mode is MovementMode.CHARGE_2 and operator.fatigue_level >= 1:
                return _illegal("FATIGUE_PREVENTS_CHARGE_2", evidence)
            if mode is MovementMode.MARCHING:
                return _illegal("PERSONNEL_CANNOT_MARCH", evidence)
        elif mode in {MovementMode.CHARGE_1, MovementMode.CHARGE_2}:
            return _illegal("VEHICLE_CANNOT_CHARGE", evidence)
        if mode is MovementMode.MARCHING and not operator.weapon_locked:
            return _illegal("WEAPON_NOT_LOCKED", evidence)
        return MovementLegalityResult(True, "LEGAL", evidence)

    def plan_segment(self, operator: MovementOperator, from_hex: int, to_hex: int,
                     mode: MovementMode
                     ) -> tuple[MovementLegalityResult, SegmentPlan | None]:
        evidence: dict[str, object] = {"from_hex": from_hex, "to_hex": to_hex,
                                      "movement_mode": mode.value}
        if not self.map_reader.adjacent(from_hex, to_hex):
            return _illegal("PATH_NOT_ADJACENT", evidence), None
        terrain = self.map_reader.terrain_for_segment(from_hex, to_hex)
        evidence["terrain_type"] = terrain.value
        if terrain is TerrainType.UNKNOWN:
            return _illegal("UNKNOWN_TERRAIN", evidence), None
        if (terrain is TerrainType.ROADBLOCK
                or self.map_reader.is_roadblock(to_hex)):
            return _illegal("ROADBLOCK_IMPASSABLE", evidence), None
        slope_level = self.map_reader.slope_level(from_hex, to_hex)
        difference_m = self.map_reader.elevation_difference_m(from_hex, to_hex)
        if slope_level is None or difference_m is None:
            return _illegal("UNKNOWN_ELEVATION_UNIT", evidence), None
        left_elevation = self.map_reader.elevation(from_hex)
        right_elevation = self.map_reader.elevation(to_hex)
        if left_elevation is None or right_elevation is None:
            return _illegal("UNKNOWN_ELEVATION_UNIT", evidence), None
        road = self.map_reader.road_edge(from_hex, to_hex)
        if road is not None and road.road_type is RoadType.UNKNOWN:
            return _illegal("UNKNOWN_ROAD_TYPE", evidence), None
        base_speed = float(operator.basic_speed or 0.0)
        terrain_multiplier = (1, 1)
        slope_multiplier = (1, 1)
        mode_multiplier = (1, 1)
        road_override = road is not None
        domain = operator.operator_type.upper()
        map_cost_mode: int | None = None
        map_cost_factor: Fraction | None = None
        movement_cost_source = self.map_reader.movement_cost_source_sha256
        if movement_cost_source is not None:
            map_cost_mode = _movement_cost_mode(domain, mode)
            if map_cost_mode is None:
                return _illegal("MOVEMENT_MODE_COST_UNAVAILABLE", evidence), None
            map_cost_factor = self.map_reader.movement_factor(
                map_cost_mode,
                from_hex,
                to_hex,
            )
            if map_cost_factor is None:
                reason = (
                    "MARCHING_REQUIRES_CONTINUOUS_ROAD"
                    if mode is MovementMode.MARCHING
                    else "MAP_COST_EDGE_UNAVAILABLE"
                )
                return _illegal(reason, {
                    **evidence,
                    "map_cost_mode": map_cost_mode,
                    "map_cost_source_sha256": movement_cost_source,
                }), None
            if mode is MovementMode.MARCHING and road is None:
                return _illegal("MARCHING_REQUIRES_CONTINUOUS_ROAD", evidence), None
            if domain == "PERSONNEL" and mode in {
                MovementMode.CHARGE_1,
                MovementMode.CHARGE_2,
            }:
                mode_multiplier = self.policy.mode_multiplier(mode)
            effective_milli = self.policy.multiply_speed(
                base_speed,
                mode_multiplier,
                (map_cost_factor.denominator, map_cost_factor.numerator),
            )
        elif mode is MovementMode.MARCHING:
            if road is None:
                return _illegal("MARCHING_REQUIRES_CONTINUOUS_ROAD", evidence), None
            configured = self.policy.road(road.source_code)
            if configured is None:
                return _illegal("UNKNOWN_ROAD_TYPE", evidence), None
            effective_milli = configured[1] * 1000
        elif domain == "VEHICLE":
            if slope_level > 5 and not road_override:
                return _illegal("SLOPE_IMPASSABLE",
                                {**evidence, "slope_level": slope_level}), None
            if not road_override:
                configured_terrain = self.policy.terrain_multiplier(terrain)
                configured_slope = self.policy.slope_multiplier(slope_level)
                if configured_terrain is None:
                    return _illegal("UNKNOWN_TERRAIN", evidence), None
                if configured_slope is None:
                    return _illegal("SLOPE_IMPASSABLE", evidence), None
                terrain_multiplier = configured_terrain
                slope_multiplier = configured_slope
            effective_milli = self.policy.multiply_speed(
                base_speed, terrain_multiplier, slope_multiplier)
        elif domain == "AIR":
            # AIR traversal uses the explicitly configured platform speed and
            # is not modified by ground terrain or ground slope.  Versioned
            # MapRuntime mode 3 remains authoritative when it is available.
            effective_milli = self.policy.multiply_speed(base_speed, (1, 1))
        else:
            if mode in {MovementMode.CHARGE_1, MovementMode.CHARGE_2}:
                mode_multiplier = self.policy.mode_multiplier(mode)
            height_multiplier = ((1, 1) if difference_m <=
                                 self.policy.personnel_height_threshold_m else
                                 self.policy.personnel_high_difference_multiplier)
            effective_milli = self.policy.multiply_speed(
                base_speed, mode_multiplier, height_multiplier)
            slope_multiplier = height_multiplier
        traversal_ms = self.policy.traversal_ms(effective_milli)
        plan = SegmentPlan(
            from_hex=from_hex, to_hex=to_hex,
            base_speed_kph=base_speed, terrain_type=terrain,
            terrain_multiplier=terrain_multiplier,
            elevation_from=left_elevation, elevation_to=right_elevation,
            elevation_difference=right_elevation - left_elevation,
            slope_level=slope_level, slope_multiplier=slope_multiplier,
            road_type=None if road is None else road.road_type,
            road_edge=None if road is None else (from_hex, to_hex),
            road_override=road_override, movement_mode=mode,
            mode_multiplier=mode_multiplier,
            effective_speed_milli_kph=effective_milli,
            effective_speed_micro_hex_per_second=(
                self.policy.hex_rate_micro_per_second(effective_milli)),
            segment_distance_m=self.policy.hex_size_m,
            traversal_ms=traversal_ms,
            map_cost_mode=map_cost_mode,
            map_cost_factor=(
                None
                if map_cost_factor is None
                else (map_cost_factor.numerator, map_cost_factor.denominator)
            ),
            map_cost_source_sha256=movement_cost_source,
        )
        return MovementLegalityResult(True, "LEGAL", plan.to_dict()), plan

    @staticmethod
    def friendly_ground_count(world: WorldState, hex_id: int, *,
                              side: str | None = None,
                              exclude: int | None = None) -> int:
        return sum(1 for item in world.operators.values()
                   if item.operator_id != exclude and item.alive and item.deployed
                   and item.operator_type.upper() in {"PERSONNEL", "VEHICLE"}
                   and item.current_hex == hex_id
                   and (side is None or item.side == side))


def _illegal(reason: str, evidence: dict[str, object]) -> MovementLegalityResult:
    return MovementLegalityResult(False, reason, evidence)


def _movement_cost_mode(domain: str, mode: MovementMode) -> int | None:
    if domain == "VEHICLE":
        return 1 if mode is MovementMode.MARCHING else 0
    if domain == "PERSONNEL":
        return 2
    if domain == "AIR" and mode is MovementMode.NORMAL:
        return 3
    return None
