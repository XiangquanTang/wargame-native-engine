from __future__ import annotations

from typing import Iterable

from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.core.world import WorldState
from wargame_native_engine.movement.map_adapter import SourceHexMap
from wargame_native_engine.movement.models import (
    MovementLegalityResult, MovementMode, RoadType, SegmentPlan, TerrainType,
)
from wargame_native_engine.movement.policy import SourceMovementPolicy


class MovementPlanner:
    def __init__(self, hex_map: SourceHexMap,
                 policy: SourceMovementPolicy) -> None:
        self.hex_map = hex_map
        self.policy = policy
        if hex_map.native_map.hex_size_m != policy.hex_size_m:
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
        operator: OperatorState,
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
        canonical = str(operator.raw_attributes.get(
            "canonical_type", operator.operator_type)).upper()
        if canonical == "ARTILLERY":
            return _illegal("ARTILLERY_CANNOT_MOVE", evidence)
        domain = operator.operator_type.upper()
        if domain == "AIR":
            return _illegal("AIR_MOVEMENT_UNSUPPORTED", evidence)
        if domain not in {"PERSONNEL", "VEHICLE"}:
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

    def plan_segment(self, operator: OperatorState, from_hex: int, to_hex: int,
                     mode: MovementMode
                     ) -> tuple[MovementLegalityResult, SegmentPlan | None]:
        evidence: dict[str, object] = {"from_hex": from_hex, "to_hex": to_hex,
                                      "movement_mode": mode.value}
        if not self.hex_map.adjacent(from_hex, to_hex):
            return _illegal("PATH_NOT_ADJACENT", evidence), None
        terrain = self.hex_map.terrain_for_segment(from_hex, to_hex)
        evidence["terrain_type"] = terrain.value
        if terrain is TerrainType.UNKNOWN:
            return _illegal("UNKNOWN_TERRAIN", evidence), None
        if (terrain is TerrainType.ROADBLOCK
                or to_hex in self.hex_map.native_map.roadblocks):
            return _illegal("ROADBLOCK_IMPASSABLE", evidence), None
        slope_level = self.hex_map.slope_level(from_hex, to_hex)
        difference_m = self.hex_map.elevation_difference_m(from_hex, to_hex)
        if slope_level is None or difference_m is None:
            return _illegal("UNKNOWN_ELEVATION_UNIT", evidence), None
        left, right = self.hex_map.cell(from_hex), self.hex_map.cell(to_hex)
        road = self.hex_map.road_edge(from_hex, to_hex)
        if road is not None and road.road_type is RoadType.UNKNOWN:
            return _illegal("UNKNOWN_ROAD_TYPE", evidence), None
        base_speed = float(operator.basic_speed or 0.0)
        terrain_multiplier = (1, 1)
        slope_multiplier = (1, 1)
        mode_multiplier = (1, 1)
        road_override = road is not None
        domain = operator.operator_type.upper()
        if mode is MovementMode.MARCHING:
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
            elevation_from=float(left.elevation), elevation_to=float(right.elevation),
            elevation_difference=float(right.elevation) - float(left.elevation),
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
