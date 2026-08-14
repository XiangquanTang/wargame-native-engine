"""Deterministic destination planning over authoritative segment costs."""

from __future__ import annotations

from heapq import heappop, heappush

from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.core.world import WorldState
from wargame_native_engine.movement.models import (
    MovementMode,
    RoutePlan,
    SegmentPlan,
)
from wargame_native_engine.movement.planner import MovementPlanner


class DeterministicRoutePlanner:
    """Dijkstra search whose edge weights come from ``MovementPlanner``.

    The lexicographic full-path tuple is the stable equal-cost tie-break.  The
    returned route is only a proposal; execution still calls
    ``MovementPlanner.validate_path`` segment by segment.
    """

    def __init__(self, movement: MovementPlanner) -> None:
        self.movement = movement

    def plan(
        self,
        operator: OperatorState,
        destination_hex: int,
        mode: MovementMode,
        world: WorldState,
    ) -> RoutePlan:
        destination = int(destination_hex)
        source = operator.current_hex
        initial = self.movement.validate_operator(operator, mode)
        if not initial.legal:
            return self._unreachable(
                operator, destination, mode, initial.reason_code
            )
        if source is None:
            return self._unreachable(
                operator, destination, mode, "CURRENT_HEX_REQUIRED"
            )
        try:
            self.movement.hex_map.cell(destination)
        except ValueError:
            return self._unreachable(
                operator, destination, mode, "UNKNOWN_DESTINATION"
            )
        if destination == source:
            return self._unreachable(
                operator, destination, mode, "ALREADY_AT_DESTINATION"
            )

        # cost, full path, node, segment tuple.  Path is part of the heap key,
        # so equal-cost choices remain stable across dict/set implementations.
        frontier: list[
            tuple[int, tuple[int, ...], int, tuple[SegmentPlan, ...]]
        ] = [(0, (), source, ())]
        best: dict[int, tuple[int, tuple[int, ...]]] = {source: (0, ())}
        while frontier:
            cost, path, current, segments = heappop(frontier)
            if best.get(current) != (cost, path):
                continue
            if current == destination:
                legality = self.movement.validate_path(
                    operator, path, mode, world
                )
                if not legality.legal:
                    return self._unreachable(
                        operator, destination, mode, legality.reason_code
                    )
                return RoutePlan(
                    True,
                    "LEGAL",
                    operator.operator_id,
                    source,
                    destination,
                    mode,
                    path,
                    cost,
                    segments,
                )
            cell = self.movement.hex_map.cell(current)
            for neighbor in sorted(value for value in cell.neighbors if value >= 0):
                if self.movement.friendly_ground_count(
                    world,
                    neighbor,
                    side=operator.side,
                    exclude=operator.operator_id,
                ) >= self.movement.policy.friendly_ground_stack_limit:
                    continue
                legality, segment = self.movement.plan_segment(
                    operator, current, neighbor, mode
                )
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
                    (
                        candidate_cost,
                        candidate_path,
                        neighbor,
                        segments + (segment,),
                    ),
                )
        return self._unreachable(operator, destination, mode, "DESTINATION_UNREACHABLE")

    @staticmethod
    def _unreachable(
        operator: OperatorState,
        destination: int,
        mode: MovementMode,
        reason: str,
    ) -> RoutePlan:
        return RoutePlan(
            False,
            reason,
            operator.operator_id,
            operator.current_hex,
            destination,
            mode,
        )
