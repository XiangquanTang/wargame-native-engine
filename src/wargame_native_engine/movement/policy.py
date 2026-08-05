from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import json
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.io.serialization import canonical_hash, json_clone
from wargame_native_engine.movement.models import MovementMode, RoadType, TerrainType


POLICY_SCHEMA = "native-source-movement-policy/1.0"


@dataclass(frozen=True, slots=True)
class SourceMovementPolicy:
    policy_id: str
    policy_version: str
    time_resolution_ms: int
    hex_size_m: int
    hex_size_status: str
    terrain_codes: tuple[tuple[int, TerrainType], ...]
    vehicle_terrain_multipliers: tuple[tuple[TerrainType, tuple[int, int]], ...]
    vehicle_slope_multipliers: tuple[tuple[int, tuple[int, int]], ...]
    road_codes: tuple[tuple[int, tuple[RoadType, int]], ...]
    mode_multipliers: tuple[tuple[MovementMode, tuple[int, int]], ...]
    personnel_height_threshold_m: float
    personnel_high_difference_multiplier: tuple[int, int]
    fatigue_recovery_ms: int
    transition_ms: int
    suppression_ms: int
    friendly_ground_stack_limit: int
    _document: Mapping[str, Any]
    _content_hash: str

    @property
    def content_hash(self) -> str:
        return self._content_hash

    def terrain(self, code: int | str) -> TerrainType:
        if isinstance(code, str) and not code.lstrip("-").isdigit():
            try:
                return TerrainType(code.upper())
            except ValueError:
                return TerrainType.UNKNOWN
        return dict(self.terrain_codes).get(int(code), TerrainType.UNKNOWN)

    def terrain_multiplier(self, terrain: TerrainType) -> tuple[int, int] | None:
        return dict(self.vehicle_terrain_multipliers).get(terrain)

    def slope_multiplier(self, level: int) -> tuple[int, int] | None:
        return dict(self.vehicle_slope_multipliers).get(int(level))

    def road(self, code: int) -> tuple[RoadType, int] | None:
        return dict(self.road_codes).get(int(code))

    def mode_multiplier(self, mode: MovementMode) -> tuple[int, int]:
        try:
            return dict(self.mode_multipliers)[mode]
        except KeyError as exc:
            raise ValueError(f"movement mode has no configured multiplier: {mode.value}") from exc

    def multiply_speed(self, base_speed_kph: float,
                       *multipliers: tuple[int, int]) -> int:
        value = Fraction(str(base_speed_kph))
        for numerator, denominator in multipliers:
            value *= Fraction(numerator, denominator)
        if value <= 0:
            raise ValueError("effective movement speed must be positive")
        return _round_fraction(value * 1000)

    def traversal_ms(self, speed_milli_kph: int) -> int:
        # distance(m) / speed(km/h), converted to integer milliseconds.
        numerator = self.hex_size_m * 3_600_000
        denominator = int(speed_milli_kph)
        return max(self.time_resolution_ms,
                   _ceil_fraction(Fraction(numerator, denominator)))

    def hex_rate_micro_per_second(self, speed_milli_kph: int) -> int:
        value = Fraction(speed_milli_kph, 1000) * Fraction(1000, 3600)
        value /= self.hex_size_m
        return _round_fraction(value * 1_000_000)

    def to_dict(self) -> dict[str, Any]:
        return json_clone(self._document)


def default_source_movement_policy_path() -> Path:
    return (Path(__file__).resolve().parents[3]
            / "config/source_compat/source_movement_policy_v1.json")


def load_source_movement_policy(
        path: str | Path | None = None) -> SourceMovementPolicy:
    source = default_source_movement_policy_path() if path is None else Path(path)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid source movement policy {source}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise ValueError("source movement policy root must be an object")
    required = {
        "schema_version", "movement_policy_id", "movement_policy_version",
        "time_resolution_ms", "hex_size_m", "terrain_codes",
        "vehicle_terrain_multipliers", "vehicle_slope_multipliers",
        "road_codes", "mode_multipliers", "personnel_height_threshold_m",
        "personnel_high_difference_multiplier", "fatigue_recovery_seconds",
        "transition_seconds", "suppression_seconds",
        "friendly_ground_stack_limit", "unknown_terrain_policy",
        "unknown_elevation_unit_policy", "path_completion_policy", "event_order",
    }
    missing = sorted(required - set(value))
    if missing:
        raise ValueError(f"source movement policy missing fields: {missing}")
    if value["schema_version"] != POLICY_SCHEMA:
        raise ValueError("unsupported source movement policy schema")
    if value["unknown_terrain_policy"] != "REJECT":
        raise ValueError("unknown terrain must be rejected")
    if value["unknown_elevation_unit_policy"] != "REJECT":
        raise ValueError("unknown elevation units must be rejected")
    if value["path_completion_policy"] != "CENTER_TO_CENTER":
        raise ValueError("movement must use center-to-center segments")
    if value["event_order"] != ["event_time", "action_sequence", "event_sequence"]:
        raise ValueError("movement event order must be deterministic")
    hex_size = _mapping(value["hex_size_m"], "hex_size_m")
    if hex_size.get("status") != "DERIVED_SOURCE_PARAMETER":
        raise ValueError("hex_size_m provenance must remain explicit")
    terrain_codes = tuple(sorted(
        (int(key), TerrainType(str(item)))
        for key, item in _mapping(value["terrain_codes"], "terrain_codes").items()))
    terrain_multipliers = tuple(sorted(
        ((TerrainType(str(key)), _fraction(item, f"terrain {key}"))
         for key, item in _mapping(value["vehicle_terrain_multipliers"],
                                   "vehicle terrain multipliers").items()),
        key=lambda item: item[0].value))
    slope_multipliers = tuple(sorted(
        (int(key), _fraction(item, f"slope {key}"))
        for key, item in _mapping(value["vehicle_slope_multipliers"],
                                  "vehicle slope multipliers").items()))
    road_codes = tuple(sorted(
        (int(key), (RoadType(str(_mapping(item, f"road {key}")["road_type"])),
                    int(_mapping(item, f"road {key}")["march_speed_kph"])))
        for key, item in _mapping(value["road_codes"], "road codes").items()))
    mode_multipliers = tuple(sorted(
        ((MovementMode(str(key)), _fraction(item, f"mode {key}"))
         for key, item in _mapping(value["mode_multipliers"],
                                   "mode multipliers").items()),
        key=lambda item: item[0].value))
    policy = SourceMovementPolicy(
        policy_id=str(value["movement_policy_id"]),
        policy_version=str(value["movement_policy_version"]),
        time_resolution_ms=_positive_int(value["time_resolution_ms"], "time_resolution_ms"),
        hex_size_m=_positive_int(hex_size["value"], "hex_size_m.value"),
        hex_size_status=str(hex_size["status"]),
        terrain_codes=terrain_codes,
        vehicle_terrain_multipliers=terrain_multipliers,
        vehicle_slope_multipliers=slope_multipliers,
        road_codes=road_codes, mode_multipliers=mode_multipliers,
        personnel_height_threshold_m=float(value["personnel_height_threshold_m"]),
        personnel_high_difference_multiplier=_fraction(
            value["personnel_high_difference_multiplier"],
            "personnel high-difference multiplier"),
        fatigue_recovery_ms=_positive_int(value["fatigue_recovery_seconds"],
                                          "fatigue recovery") * 1000,
        transition_ms=_positive_int(value["transition_seconds"], "transition") * 1000,
        suppression_ms=_positive_int(value["suppression_seconds"], "suppression") * 1000,
        friendly_ground_stack_limit=_positive_int(
            value["friendly_ground_stack_limit"], "stack limit"),
        _document=json_clone(value), _content_hash=canonical_hash(value),
    )
    if set(dict(policy.vehicle_slope_multipliers)) != set(range(6)):
        raise ValueError("vehicle slope table must explicitly cover levels 0..5")
    return policy


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _fraction(value: Any, name: str) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{name} must be [numerator, denominator]")
    numerator, denominator = int(value[0]), int(value[1])
    if numerator < 0 or denominator <= 0:
        raise ValueError(f"invalid fraction for {name}")
    return numerator, denominator


def _positive_int(value: Any, name: str) -> int:
    result = int(value)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _ceil_fraction(value: Fraction) -> int:
    return -(-value.numerator // value.denominator)


def _round_fraction(value: Fraction) -> int:
    return (value.numerator * 2 + value.denominator) // (2 * value.denominator)
