from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wargame_native_engine.core.operator import OperatorState


@dataclass(frozen=True, slots=True)
class VisibilityRules:
    default_sensor_range: float
    operator_type_ranges: tuple[tuple[str, float], ...]
    friendly_always_visible: bool
    enemy_condition: str
    distance_method: str
    inclusive_boundary: bool
    occlusion_enabled: bool
    elevation_enabled: bool
    concealment_enabled: bool

    def sensor_range(self, operator: "OperatorState") -> float:
        if operator.sensor_range > 0:
            return operator.sensor_range
        return dict(self.operator_type_ranges).get(
            operator.operator_type, self.default_sensor_range)
