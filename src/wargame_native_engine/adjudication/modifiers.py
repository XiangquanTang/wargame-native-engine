from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from wargame_native_engine.adjudication.request import DirectFireRequest


@dataclass(frozen=True, slots=True)
class RollModifier:
    name: str
    value: int

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "value": self.value}


def roll_modifiers(request: DirectFireRequest,
                   config: Mapping[str, Any]) -> tuple[RollModifier, ...]:
    """Select only modifiers declared by the final machine table."""
    values: list[RollModifier] = []
    shooter = _mapping(config.get("shooter_state"), "shooter_state")
    target_state = _mapping(config.get("target_state"), "target_state")
    terrain = _mapping(config.get("target_terrain"), "target_terrain")
    if request.attacker_suppressed:
        _append(values, shooter, "suppressed", "shooter_suppressed")
    if request.attacker_moving:
        _append(values, shooter, "moving", "shooter_moving")
    if request.attacker_tank_on_water:
        _append(values, shooter, "tank_on_water", "shooter_tank_on_water")
    target_terrain = request.target_terrain.upper()
    if target_terrain in {"SETTLEMENT", "RESIDENTIAL"}:
        key = "residential" if "residential" in terrain else "residential_or_fort"
        _append(values, terrain, key, "target_residential")
    elif target_terrain == "FORTIFICATION":
        key = "fort" if "fort" in terrain else "residential_or_fort"
        _append(values, terrain, key, "target_fortification")
    elif target_terrain == "FOREST":
        _append(values, terrain, "forest", "target_forest")
    elif target_terrain == "WATER":
        _append(values, terrain, "water", "target_water")
    if request.target_concealed:
        _append(values, target_state, "concealed", "target_concealed")
    if request.target_moving:
        _append(values, target_state, "moving", "target_moving")
    if request.target_stacked:
        _append(values, target_state, "stacked", "target_stacked")
    if request.target_marching:
        _append(values, target_state, "marching", "target_marching")
    return tuple(values)


def _append(values: list[RollModifier], config: Mapping[str, Any],
            key: str, evidence_name: str) -> None:
    if key not in config:
        raise ValueError(f"modifier is not defined by final table: {key}")
    values.append(RollModifier(evidence_name, int(config[key])))


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"modifier group must be an object: {name}")
    return value
