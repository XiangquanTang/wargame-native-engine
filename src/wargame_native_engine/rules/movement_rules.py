from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wargame_native_engine.core.operator import OperatorState


@dataclass(frozen=True, slots=True)
class MovementRules:
    default_move_range: float
    operator_type_ranges: tuple[tuple[str, float], ...]
    boundary_policy: str
    impassable_terrain: tuple[str, ...]
    terrain_multipliers: tuple[tuple[str, float], ...]
    allow_position_overlap: bool
    minimum_unit_spacing: float
    float_tolerance: float

    def for_operator(self, operator: "OperatorState") -> float:
        if operator.move_range > 0:
            return operator.move_range
        return dict(self.operator_type_ranges).get(
            operator.operator_type, self.default_move_range)
