from __future__ import annotations

from dataclasses import dataclass

from wargame_native_engine.compat.source_frame.models import UnitDomain


@dataclass(frozen=True, slots=True)
class ObservationProfiles:
    """Named observation limits transcribed from the provided rule prose."""

    ground_to_personnel: float = 10.0
    ground_to_vehicle: float = 25.0
    ground_to_air: float = 25.0
    air_to_personnel: float = 10.0
    air_to_vehicle: float = 25.0
    air_to_air: float = 25.0
    drone_to_ground: float = 2.0
    ground_to_drone: float = 1.0

    def limit(self, observer: UnitDomain, target: UnitDomain, *,
              observer_is_drone: bool = False,
              target_is_drone: bool = False) -> float:
        if observer_is_drone:
            if target is UnitDomain.AIR:
                return 0.0
            return self.drone_to_ground
        if target_is_drone:
            return self.ground_to_drone
        if observer is UnitDomain.AIR:
            return {UnitDomain.PERSONNEL: self.air_to_personnel,
                    UnitDomain.VEHICLE: self.air_to_vehicle,
                    UnitDomain.AIR: self.air_to_air}.get(target, 0.0)
        return {UnitDomain.PERSONNEL: self.ground_to_personnel,
                UnitDomain.VEHICLE: self.ground_to_vehicle,
                UnitDomain.AIR: self.ground_to_air}.get(target, 0.0)
