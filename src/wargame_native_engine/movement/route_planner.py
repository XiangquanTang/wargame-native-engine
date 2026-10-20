"""Deterministic destination planning over detached movement inputs."""

from __future__ import annotations

from dataclasses import dataclass
from heapq import heappop, heappush
from typing import Protocol, runtime_checkable

from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.core.world import WorldState
from wargame_native_engine.movement.map_adapter import StaticMovementMapReader
from wargame_native_engine.movement.models import (
    MovementLegalityResult,
    MovementMode,
    RoutePlan,
    SegmentPlan,
)
from wargame_native_engine.movement.planner import (
    MovementPlanner,
    MovementUnitSnapshot,
)


@dataclass(frozen=True, slots=True)
class PathPlanningRequest:
    """Immutable request containing only movement-relevant own-side state."""

    unit: MovementUnitSnapshot
    start_hex: int
    destination_hex: int
    movement_mode: MovementMode
    full_friendly_ground_hexes: tuple[int, ...] = ()

    @classmethod
    def from_operator(
        cls,
        operator: OperatorState,
        destination_hex: int,
        movement_mode: MovementMode,
        full_friendly_ground_hexes: tuple[int, ...],
    ) -> "PathPlanningRequest":
        unit = MovementUnitSnapshot.from_operator(operator)
        return cls(
            unit=unit,
            start_hex=-1 if unit.current_hex is None else int(unit.current_hex),
            destination_hex=int(destination_hex),
            movement_mode=movement_mode,
            full_friendly_ground_hexes=tuple(
                sorted(int(value) for value in full_friendly_ground_hexes)
            ),
        )


@runtime_checkable
class MovementCostQuery(Protocol):
    """Read-only rule authority offered to a replaceable path algorithm."""

    def validate_operator(self) -> MovementLegalityResult: ...

    def plan_segment(
        self, from_hex: int, to_hex: int,
    ) -> tuple[MovementLegalityResult, SegmentPlan | None]: ...


@runtime_checkable
class PathPlanner(Protocol):
    planner_id: str
    planner_version: str

    def plan(
        self,
        request: PathPlanningRequest,
        map_reader: StaticMovementMapReader,
        movement_costs: MovementCostQuery,
    ) -> RoutePlan: ...


@dataclass(frozen=True, slots=True)
class DetachedMovementCostQuery:
    """Narrow cost view bound to one detached unit and movement mode."""

    _movement: MovementPlanner
    _unit: MovementUnitSnapshot
    _mode: MovementMode

    def validate_operator(self) -> MovementLegalityResult:
        return self._movement.validate_operator(self._unit, self._mode)

    def plan_segment(
        self, from_hex: int, to_hex: int,
    ) -> tuple[MovementLegalityResult, SegmentPlan | None]:
        return self._movement.plan_segment(
            self._unit, int(from_hex), int(to_hex), self._mode,
        )


def full_friendly_ground_hexes(
    world: WorldState,
    *,
    side: str,
    exclude: int,
    stack_limit: int,
) -> tuple[int, ...]:
    """Build the detached dynamic occupancy summary in one world traversal."""

    counts: dict[int, int] = {}
    for item in world.operators.values():
        if (
            item.operator_id == exclude
            or not item.alive
            or not item.deployed
            or item.side != side
            or item.operator_type.upper() not in {"PERSONNEL", "VEHICLE"}
            or item.current_hex is None
        ):
            continue
        location = int(item.current_hex)
        counts[location] = counts.get(location, 0) + 1
    return tuple(sorted(
        location
        for location, count in counts.items()
        if count >= int(stack_limit)
    ))


class DeterministicRoutePlanner:
    """Dijkstra search preserving the historical deterministic tie-break."""

    planner_id = "native-deterministic-dijkstra"
    planner_version = "1.0.0"

    def __init__(self, movement: MovementPlanner | None = None) -> None:
        # Optional only for the legacy direct-call adapter below.  Production
        # passes an explicit DetachedMovementCostQuery on every request.
        self._legacy_movement = movement

    def plan(
        self,
        request: PathPlanningRequest | OperatorState,
        map_reader: StaticMovementMapReader | int,
        movement_costs: MovementCostQuery | MovementMode,
        world: WorldState | None = None,
    ) -> RoutePlan:
        if isinstance(request, PathPlanningRequest):
            if not isinstance(map_reader, StaticMovementMapReader):
                raise TypeError("path planner requires StaticMovementMapReader")
            if not isinstance(movement_costs, MovementCostQuery):
                raise TypeError("path planner requires MovementCostQuery")
            return self._search(request, map_reader, movement_costs)

        # Compatibility for the former plan(operator, destination, mode,
        # world) entry point.  It delegates to the same search implementation.
        if (
            self._legacy_movement is None
            or type(map_reader) is not int
            or not isinstance(movement_costs, MovementMode)
            or world is None
        ):
            raise TypeError("invalid legacy route-planner call")
        occupied = full_friendly_ground_hexes(
            world,
            side=request.side,
            exclude=request.operator_id,
            stack_limit=self._legacy_movement.policy.friendly_ground_stack_limit,
        )
        legacy_request = PathPlanningRequest.from_operator(
            request, map_reader, movement_costs, occupied,
        )
        query = DetachedMovementCostQuery(
            self._legacy_movement,
            legacy_request.unit,
            legacy_request.movement_mode,
        )
        return self._search(
            legacy_request,
            self._legacy_movement.map_reader,
            query,
        )

    def _search(
        self,
        request: PathPlanningRequest,
        map_reader: StaticMovementMapReader,
        movement_costs: MovementCostQuery,
    ) -> RoutePlan:
        destination = request.destination_hex
        source = request.unit.current_hex
        initial = movement_costs.validate_operator()
        if not initial.legal:
            return _unreachable(request, initial.reason_code)
        if source is None:
            return _unreachable(request, "CURRENT_HEX_REQUIRED")
        if not map_reader.contains(destination):
            return _unreachable(request, "UNKNOWN_DESTINATION")
        if destination == source:
            return _unreachable(request, "ALREADY_AT_DESTINATION")

        blocked = frozenset(request.full_friendly_ground_hexes)
        # The full path is part of the heap key and remains the stable
        # equal-cost tie-break across dict/set implementations.
        frontier: list[
            tuple[int, tuple[int, ...], int, tuple[SegmentPlan, ...]]
        ] = [(0, (), source, ())]
        best: dict[int, tuple[int, tuple[int, ...]]] = {source: (0, ())}
        while frontier:
            cost, path, current, segments = heappop(frontier)
            if best.get(current) != (cost, path):
                continue
            if current == destination:
                return RoutePlan(
                    True, "LEGAL", request.unit.operator_id, source,
                    destination, request.movement_mode, path, cost, segments,
                )
            for neighbor in sorted(
                value for value in map_reader.neighbors(current) if value >= 0
            ):
                if neighbor in blocked:
                    continue
                legality, segment = movement_costs.plan_segment(current, neighbor)
                if not legality.legal or segment is None:
                    continue
                candidate_path = path + (neighbor,)
                candidate_cost = cost + segment.traversal_ms
                candidate_key = (candidate_cost, candidate_path)
                previous = best.get(neighbor)
                if previous is not None and previous <= candidate_key:
                    continue
                best[neighbor] = candidate_key
                heappush(
                    frontier,
                    (candidate_cost, candidate_path, neighbor, segments + (segment,)),
                )
        return _unreachable(request, "DESTINATION_UNREACHABLE")


def _unreachable(request: PathPlanningRequest, reason: str) -> RoutePlan:
    return RoutePlan(
        False, str(reason), request.unit.operator_id,
        request.unit.current_hex, request.destination_hex, request.movement_mode,
    )
