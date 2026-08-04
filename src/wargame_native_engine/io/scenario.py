from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.core.world import (
    ObjectiveState, SideState, Stage, WorldState,
)
from wargame_native_engine.io.serialization import canonical_hash, json_clone
from wargame_native_engine.rules.terminal_rules import TerminalRules


SCENARIO_SCHEMA_VERSION = "native-scenario/1.0"


@dataclass(frozen=True, slots=True)
class NativeMap:
    map_id: str
    width: float
    height: float
    distance_unit: str = "m"
    blocked_positions: tuple[tuple[float, float], ...] = ()

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("map width and height must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {"map_id": self.map_id, "width": self.width,
                "height": self.height, "distance_unit": self.distance_unit,
                "blocked_positions": [list(item) for item in self.blocked_positions]}


@dataclass(frozen=True, slots=True)
class NativeScenario:
    scenario_id: str
    map: NativeMap
    side_order: tuple[str, ...]
    operators: tuple[OperatorState, ...]
    objectives: tuple[ObjectiveState, ...]
    deployment_positions: Mapping[str, Mapping[int, tuple[float, float]]]
    terminal_rules: TerminalRules
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = SCENARIO_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCENARIO_SCHEMA_VERSION:
            raise ValueError("unsupported native scenario schema")
        if len(self.side_order) < 2 or len(set(self.side_order)) != len(self.side_order):
            raise ValueError("scenario requires at least two unique sides")
        ids = [item.operator_id for item in self.operators]
        if len(ids) != len(set(ids)):
            raise ValueError("operator ids must be unique")
        for operator in self.operators:
            if operator.side not in self.side_order:
                raise ValueError(f"unknown operator side: {operator.side}")
            if operator.operator_id not in self.deployment_positions.get(operator.side, {}):
                raise ValueError(f"missing deployment position: {operator.operator_id}")

    @property
    def scenario_hash(self) -> str:
        return canonical_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scenario_id": self.scenario_id,
            "map": self.map.to_dict(),
            "sides": list(self.side_order),
            "operators": [item.to_dict() for item in self.operators],
            "objectives": [item.to_dict() for item in self.objectives],
            "deployment": {"positions": {
                side: {str(key): list(position)
                       for key, position in sorted(values.items())}
                for side, values in sorted(self.deployment_positions.items())
            }},
            "terminal_rules": {
                "max_ticks": self.terminal_rules.max_ticks,
                "elimination": self.terminal_rules.elimination,
                "objective_score_to_win": self.terminal_rules.objective_score_to_win,
            },
            "metadata": json_clone(self.metadata),
        }

    def initial_world(self) -> WorldState:
        operators = {item.operator_id: OperatorState.from_dict(item.to_dict())
                     for item in self.operators}
        for item in operators.values():
            item.deployed = False
        objectives = {item.objective_id: ObjectiveState(**item.to_dict())
                      for item in self.objectives}
        sides = {side: SideState(side) for side in self.side_order}
        return WorldState(0, Stage.DEPLOYMENT, operators, sides, objectives,
                          False, None, None, False)


def load_scenario(path: str | Path) -> NativeScenario:
    source = Path(path)
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("scenario root must be an object")
    return scenario_from_dict(value)


def scenario_from_dict(value: Mapping[str, Any]) -> NativeScenario:
    if str(value.get("schema_version")) != SCENARIO_SCHEMA_VERSION:
        raise ValueError("unsupported native scenario schema")
    map_value = _mapping(value.get("map"), "map")
    blocked = tuple(_position(item, "map.blocked_positions[]")
                    for item in map_value.get("blocked_positions", []))
    native_map = NativeMap(
        map_id=str(map_value["map_id"]), width=float(map_value["width"]),
        height=float(map_value["height"]),
        distance_unit=str(map_value.get("distance_unit", "m")),
        blocked_positions=blocked,
    )
    sides = tuple(str(item) for item in value.get("sides", []))
    operators = tuple(_operator(_mapping(item, "operators[]"))
                      for item in value.get("operators", []))
    objectives = tuple(_objective(_mapping(item, "objectives[]"))
                       for item in value.get("objectives", []))
    deployment = _mapping(value.get("deployment", {}), "deployment")
    raw_positions = _mapping(deployment.get("positions", {}),
                             "deployment.positions")
    positions = {
        str(side): {int(key): _position(position, "deployment position")
                    for key, position in _mapping(values, "side positions").items()}
        for side, values in raw_positions.items()
    }
    terminal = _mapping(value.get("terminal_rules", {}), "terminal_rules")
    return NativeScenario(
        scenario_id=str(value["scenario_id"]), map=native_map,
        side_order=sides, operators=operators, objectives=objectives,
        deployment_positions=positions,
        terminal_rules=TerminalRules(
            max_ticks=int(terminal.get("max_ticks", 500)),
            elimination=bool(terminal.get("elimination", True)),
            objective_score_to_win=(None if terminal.get("objective_score_to_win") is None
                                    else int(terminal["objective_score_to_win"])),
        ),
        metadata=json_clone(value.get("metadata", {})),
    )


_OPERATOR_FIELDS = {
    "operator_id", "side", "operator_type", "subtype", "x", "y",
    "altitude", "heading", "health", "max_health", "speed", "max_speed",
    "move_range", "sensor_range", "state", "alive", "weapon_ids",
    "raw_attributes", "attack_range", "attack_damage", "deployed",
}


def _operator(value: Mapping[str, Any]) -> OperatorState:
    raw = dict(value.get("raw_attributes", {}))
    raw.update({key: json_clone(item) for key, item in value.items()
                if key not in _OPERATOR_FIELDS})
    normalized = dict(value)
    normalized["raw_attributes"] = raw
    return OperatorState.from_dict(normalized)


def _objective(value: Mapping[str, Any]) -> ObjectiveState:
    return ObjectiveState(
        objective_id=int(value["objective_id"]), x=float(value["x"]),
        y=float(value["y"]), radius=float(value.get("radius", 1.0)),
        value=int(value.get("value", 1)),
        controlling_side=(None if value.get("controlling_side") is None
                          else str(value["controlling_side"])),
    )


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _position(value: Any, name: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{name} must be an XY pair")
    return (float(value[0]), float(value[1]))
