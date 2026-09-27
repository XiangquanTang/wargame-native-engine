from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.core.world import (
    ObjectiveState, SideState, Stage, WorldState,
)
from wargame_native_engine.io.serialization import (
    canonical_hash,
    canonical_json,
    json_clone,
)
from wargame_native_engine.rules.terminal_rules import TerminalRules


SCENARIO_SCHEMA_VERSION = "native-scenario/1.0"


@dataclass(frozen=True, slots=True)
class NativeHex:
    hex_id: int
    elevation: float | None
    terrain: int | str
    neighbors: tuple[int, ...]
    roads: tuple[int, ...] = (0, 0, 0, 0, 0, 0)
    rivers: tuple[int, ...] = (0, 0, 0, 0, 0, 0)

    def __post_init__(self) -> None:
        if any(len(value) != 6 for value in
               (self.neighbors, self.roads, self.rivers)):
            raise ValueError("native hex directional arrays must have six entries")

    def to_dict(self) -> dict[str, Any]:
        return {"pos": self.hex_id, "elev": self.elevation,
                "cond": self.terrain, "neighbors": list(self.neighbors),
                "roads": list(self.roads), "rivers": list(self.rivers)}


@dataclass(frozen=True, slots=True)
class NativeMap:
    map_id: str
    width: float
    height: float
    distance_unit: str = "m"
    blocked_positions: tuple[tuple[float, float], ...] = ()
    hexes: tuple[NativeHex, ...] = ()
    hex_size_m: int | None = None
    elevation_unit_m: float | None = None
    elevation_kind: str | None = None
    roadblocks: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("map width and height must be positive")
        if self.hexes:
            if self.hex_size_m is None or self.hex_size_m <= 0:
                raise ValueError("hex map requires positive hex_size_m")
            if self.elevation_kind not in {None, "LEVEL", "METERS"}:
                raise ValueError("hex map elevation_kind must be LEVEL, METERS or unknown")
            if self.elevation_unit_m is not None and self.elevation_unit_m <= 0:
                raise ValueError("hex map elevation_unit_m must be positive when known")

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "map_id": self.map_id, "width": self.width,
            "height": self.height, "distance_unit": self.distance_unit,
            "blocked_positions": [list(item) for item in self.blocked_positions]}
        if self.hexes:
            value.update({
                "hexes": [item.to_dict() for item in self.hexes],
                "hex_size_m": self.hex_size_m,
                "elevation_unit_m": self.elevation_unit_m,
                "elevation_kind": self.elevation_kind,
                "roadblocks": list(self.roadblocks)})
        return value


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
    initial_stage: Stage = Stage.DEPLOYMENT
    schema_version: str = SCENARIO_SCHEMA_VERSION
    _canonical_map_json: str = field(init=False, repr=False, compare=False)

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
        # NativeMap is deeply immutable: it contains only frozen scalar/tuple
        # values and frozen NativeHex records.  Cache only that large static
        # canonical fragment.  The rest of NativeScenario intentionally stays
        # live because operators, objectives, deployment and metadata contain
        # mutable nested values.
        object.__setattr__(
            self,
            "_canonical_map_json",
            canonical_json(self.map.to_dict()),
        )

    @property
    def scenario_hash(self) -> str:
        record = self._to_dict_without_map()
        encoded_items = []
        for key in sorted((*record, "map")):
            encoded_value = (
                self._canonical_map_json
                if key == "map"
                else canonical_json(record[key])
            )
            encoded_items.append(f"{canonical_json(key)}:{encoded_value}")
        canonical = "{" + ",".join(encoded_items) + "}"
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        remainder = self._to_dict_without_map()
        value = {
            "schema_version": remainder.pop("schema_version"),
            "scenario_id": remainder.pop("scenario_id"),
            "map": self.map.to_dict(),
        }
        value.update(remainder)
        return value

    def _to_dict_without_map(self) -> dict[str, Any]:
        value = {
            "schema_version": self.schema_version,
            "scenario_id": self.scenario_id,
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
        if self.initial_stage is not Stage.DEPLOYMENT:
            value["initial_stage"] = self.initial_stage.value
        return value

    def initial_world(self) -> WorldState:
        operators = {item.operator_id: OperatorState.from_dict(item.to_dict())
                     for item in self.operators}
        for item in operators.values():
            item.deployed = (
                self.initial_stage is Stage.COMBAT and item.mounted_on is None
            )
        objectives = {item.objective_id: ObjectiveState(**item.to_dict())
                      for item in self.objectives}
        sides = {side: SideState(side, self.initial_stage is Stage.COMBAT)
                 for side in self.side_order}
        return WorldState(0, self.initial_stage, operators, sides, objectives,
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
    native_map = native_map_from_dict(map_value)

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
            max_ticks=int(terminal["max_ticks"]),
            elimination=bool(terminal.get("elimination", True)),
            objective_score_to_win=(None if terminal.get("objective_score_to_win") is None
                                    else int(terminal["objective_score_to_win"])),
        ),
        metadata=json_clone(value.get("metadata", {})),
        initial_stage=Stage(str(value.get("initial_stage", Stage.DEPLOYMENT.value))),
    )


def native_map_from_dict(value: Mapping[str, Any]) -> NativeMap:
    """Parse and normalize one canonical NativeMap record."""

    map_value = _mapping(value, "map")
    blocked = tuple(_position(item, "map.blocked_positions[]")
                    for item in map_value.get("blocked_positions", []))
    return NativeMap(
        map_id=str(map_value["map_id"]), width=float(map_value["width"]),
        height=float(map_value["height"]),
        distance_unit=str(map_value.get("distance_unit", "m")),
        blocked_positions=blocked,
        hexes=tuple(_hex(_mapping(item, "map.hexes[]"))
                    for item in map_value.get("hexes", [])),
        hex_size_m=(None if map_value.get("hex_size_m") is None
                    else int(map_value["hex_size_m"])),
        elevation_unit_m=(None if map_value.get("elevation_unit_m") is None
                          else float(map_value["elevation_unit_m"])),
        elevation_kind=(None if map_value.get("elevation_kind") is None
                        else str(map_value["elevation_kind"]).upper()),
        roadblocks=tuple(int(item) for item in map_value.get("roadblocks", [])),
    )


def native_map_identity_hash(native_map: NativeMap) -> str:
    """Hash the normalized map representation used after Scenario loading."""

    normalized = native_map_from_dict(native_map.to_dict())
    return canonical_hash(normalized.to_dict())


_OPERATOR_FIELDS = {
    "operator_id", "side", "operator_type", "subtype", "x", "y",
    "altitude", "heading", "health", "max_health", "speed", "max_speed",
    "move_range", "sensor_range", "state", "alive", "weapon_ids",
    "raw_attributes", "attack_range", "attack_damage", "deployed",
    "blood", "max_blood",
    "basic_speed", "current_hex", "cur_pos_millionths", "move_path",
    "movement_mode", "stopped", "move_to_stop_remaining_ms",
    "fatigue_level", "fatigue_recovery_elapsed_ms", "movement_action_id",
    "movement_action_sequence", "movement_stop_requested",
    "movement_blocked_reason", "movement_task_pause_reason",
    "segment_from_hex", "segment_to_hex",
    "segment_elapsed_ms", "segment_traversal_ms", "segment_event_id",
    "transition_kind", "transition_remaining_ms", "weapon_locked",
    "weapon_unfolded", "weapon_cooldown_remaining_ms",
    "suppression_remaining_ms",
    "guidance_profile_id", "guidance_affiliation_id",
    "guided_fire_recovery_remaining_ms", "guidance_affiliation_evidence",
    "indirect_fire_planned_cooldown_remaining_ms",
    "boarding_profile_id", "mounted_on", "passenger_id", "movement_locked",
    "lifecycle_profile_id", "lifecycle_remaining_ms",
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


def _hex(value: Mapping[str, Any]) -> NativeHex:
    return NativeHex(
        hex_id=int(value["pos"]),
        elevation=(None if value.get("elev") is None else float(value["elev"])),
        terrain=value.get("cond", "UNKNOWN"),
        neighbors=tuple(int(item) for item in value["neighbors"]),
        roads=tuple(int(item) for item in value.get("roads", [0] * 6)),
        rivers=tuple(int(item) for item in value.get("rivers", [0] * 6)),
    )


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _position(value: Any, name: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{name} must be an XY pair")
    return (float(value[0]), float(value[1]))
