"""Canonical semantic identity for the Native eight-factor analyzer.

This module is the single typed authority for analyzer constants, tables,
formula identifiers and their canonical semantic specification.  The hash is
over explicit JSON semantics, never Python source bytes, paths or comments.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from wargame_native_engine.io.serialization import canonical_hash, json_clone


ANALYZER_SEMANTIC_SCHEMA_VERSION = (
    "native-eight-factor-analyzer-semantics/1.0"
)
FRAME_ANALYSIS_SCHEMA_VERSION = "native-eight-factor-frame-analysis/1.0"
ANALYZER_MODEL_ID = "native-eight-factor"
ANALYZER_MODEL_VERSION = "0.1.0-contract"

FACTOR_ORDER = (
    "base_value",
    "health",
    "weapon",
    "ammo",
    "readiness",
    "mobility",
    "information",
    "status",
)

FACTOR_SOURCE_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "base_value": ("value",),
    "health": ("blood", "max_blood"),
    "weapon": ("carry_weapon_ids",),
    "ammo": ("remain_bullet_nums",),
    "readiness": ("weapon_cool_time", "weapon_unfold_state"),
    "mobility": ("move_state", "stop", "can_to_move", "on_board"),
    "information": ("see_enemy_bop_ids",),
    "status": ("keep", "tire", "lose_control", "close_combat"),
})

FACTOR_FORMULA_IDS: Mapping[str, str] = MappingProxyType({
    "base_value": "demo-compatible.base_value.v1",
    "health": "demo-compatible.health.v1",
    "weapon": "demo-compatible.weapon.v1",
    "ammo": "demo-compatible.ammo.v1",
    "readiness": "demo-compatible.readiness.v1",
    "mobility": "demo-compatible.mobility.v1",
    "information": "demo-compatible.information.v1",
    "status": "demo-compatible.status.v1",
})

EFFECTIVE_POWER_FORMULA_ID = "demo-compatible.unit-effective-power.v1"
SIDE_AGGREGATION_FORMULA_ID = "demo-compatible.sum-valid-units.v1"
ADVANTAGE_FORMULA_ID = "demo-compatible.green-relative-advantage.v1"

BASE_VALUE_FLOOR = 1.0
HEALTH_DESTROYED_THRESHOLD = 0.0
HEALTH_ALIVE_FLOOR = 0.25
HEALTH_ALIVE_SPAN = 0.75
HEALTH_RATIO_MIN = 0.0
HEALTH_RATIO_MAX = 1.0
HEALTH_NONPOSITIVE_MAX_ALIVE_VALUE = 1.0

WEAPON_EMPTY_WEIGHT = 0.6
WEAPON_UNKNOWN_WEIGHT = 1.0
WEAPON_WEIGHT: Mapping[int, float] = MappingProxyType({
    1: 1.8, 2: 1.9, 3: 2.1, 4: 1.5, 5: 1.7, 29: 0.8,
    35: 1.2, 36: 2.5, 37: 2.0, 43: 0.9, 54: 1.4, 56: 1.6,
    69: 2.0, 71: 1.6, 72: 2.3, 73: 2.7, 74: 2.1, 75: 1.7,
    76: 2.2, 83: 2.1, 84: 2.2, 88: 1.8, 89: 1.2,
})

AMMO_NON_MAPPING_VALUE = 0.7
AMMO_ZERO_TOTAL_VALUE = 0.3
AMMO_LOG_INTERCEPT = 0.55
AMMO_LOG_DIVISOR = 5.0
AMMO_CAP = 1.25

READINESS_BASE = 1.0
READINESS_COOLDOWN_FLOOR = 0.55
READINESS_COOLDOWN_CAP = 100.0
READINESS_COOLDOWN_DIVISOR = 180.0
READINESS_FOLDED_MULTIPLIER = 0.9
READINESS_UNFOLDED_MULTIPLIER = 1.05

MOBILITY_DEFAULT = 1.0
MOBILITY_STATE: Mapping[int, float] = MappingProxyType({
    0: 1.0, 1: 1.05, 2: 1.1, 3: 1.15, 4: 0.9, 5: 0.95,
})
MOBILITY_STOP_MULTIPLIER = 0.95
MOBILITY_CANNOT_MOVE_MULTIPLIER = 0.85
MOBILITY_ON_BOARD_MULTIPLIER = 0.9

INFORMATION_BASE = 1.0
INFORMATION_PER_CONTACT = 0.05
INFORMATION_CAP = 1.25

STATUS_BASE = 1.0
STATUS_SUPPRESSED_MULTIPLIER = 0.65
STATUS_FATIGUE_LEVEL_ONE_MULTIPLIER = 0.9
STATUS_FATIGUE_LEVEL_TWO_PLUS_MULTIPLIER = 0.75
STATUS_LOSE_CONTROL_MULTIPLIER = 0.2
STATUS_CLOSE_COMBAT_MULTIPLIER = 1.05


def analyzer_semantic_spec() -> dict[str, object]:
    """Return a detached canonical specification of every analyzer semantic."""

    return json_clone({
        "schema_version": ANALYZER_SEMANTIC_SCHEMA_VERSION,
        "analysis_schema_version": FRAME_ANALYSIS_SCHEMA_VERSION,
        "model_id": ANALYZER_MODEL_ID,
        "model_version": ANALYZER_MODEL_VERSION,
        "factor_formula": {
            "aggregation": "LEFT_ASSOCIATIVE_MULTIPLICATIVE",
            "factor_order": list(FACTOR_ORDER),
            "missing_factor_semantics": "UNIT_EFFECTIVE_POWER_MISSING",
        },
        "factors": {
            "base_value": {
                "formula_id": FACTOR_FORMULA_IDS["base_value"],
                "source_fields": list(FACTOR_SOURCE_FIELDS["base_value"]),
                "mapping": "MAX_VALUE_AND_FLOOR",
                "floor": BASE_VALUE_FLOOR,
            },
            "health": {
                "formula_id": FACTOR_FORMULA_IDS["health"],
                "source_fields": list(FACTOR_SOURCE_FIELDS["health"]),
                "destroyed_threshold": HEALTH_DESTROYED_THRESHOLD,
                "alive_floor": HEALTH_ALIVE_FLOOR,
                "alive_span": HEALTH_ALIVE_SPAN,
                "ratio_min": HEALTH_RATIO_MIN,
                "ratio_max": HEALTH_RATIO_MAX,
                "nonpositive_max_alive_value": (
                    HEALTH_NONPOSITIVE_MAX_ALIVE_VALUE
                ),
            },
            "weapon": {
                "formula_id": FACTOR_FORMULA_IDS["weapon"],
                "source_fields": list(FACTOR_SOURCE_FIELDS["weapon"]),
                "aggregation": "MAX_CARRIED_WEAPON_WEIGHT",
                "empty_weight": WEAPON_EMPTY_WEIGHT,
                "unknown_weapon_weight": WEAPON_UNKNOWN_WEIGHT,
                "weapon_weights": {
                    str(key): value for key, value in WEAPON_WEIGHT.items()
                },
            },
            "ammo": {
                "formula_id": FACTOR_FORMULA_IDS["ammo"],
                "source_fields": list(FACTOR_SOURCE_FIELDS["ammo"]),
                "aggregation": "SUM_NONNEGATIVE_AMMUNITION",
                "non_mapping_value": AMMO_NON_MAPPING_VALUE,
                "zero_total_value": AMMO_ZERO_TOTAL_VALUE,
                "positive_mapping": "MIN_CAP_INTERCEPT_PLUS_LOG1P_OVER_DIVISOR",
                "log_intercept": AMMO_LOG_INTERCEPT,
                "log_divisor": AMMO_LOG_DIVISOR,
                "cap": AMMO_CAP,
            },
            "readiness": {
                "formula_id": FACTOR_FORMULA_IDS["readiness"],
                "source_fields": list(FACTOR_SOURCE_FIELDS["readiness"]),
                "base": READINESS_BASE,
                "cooldown_floor": READINESS_COOLDOWN_FLOOR,
                "cooldown_cap": READINESS_COOLDOWN_CAP,
                "cooldown_divisor": READINESS_COOLDOWN_DIVISOR,
                "folded_state": 0,
                "folded_multiplier": READINESS_FOLDED_MULTIPLIER,
                "unfolded_state": 1,
                "unfolded_multiplier": READINESS_UNFOLDED_MULTIPLIER,
            },
            "mobility": {
                "formula_id": FACTOR_FORMULA_IDS["mobility"],
                "source_fields": list(FACTOR_SOURCE_FIELDS["mobility"]),
                "default": MOBILITY_DEFAULT,
                "state_weights": {
                    str(key): value for key, value in MOBILITY_STATE.items()
                },
                "stop_code": 1,
                "stop_multiplier": MOBILITY_STOP_MULTIPLIER,
                "cannot_move_value": False,
                "cannot_move_multiplier": MOBILITY_CANNOT_MOVE_MULTIPLIER,
                "on_board_multiplier": MOBILITY_ON_BOARD_MULTIPLIER,
            },
            "information": {
                "formula_id": FACTOR_FORMULA_IDS["information"],
                "source_fields": list(FACTOR_SOURCE_FIELDS["information"]),
                "mapping": "MIN_CAP_BASE_PLUS_PER_VISIBLE_CONTACT",
                "base": INFORMATION_BASE,
                "per_contact": INFORMATION_PER_CONTACT,
                "cap": INFORMATION_CAP,
            },
            "status": {
                "formula_id": FACTOR_FORMULA_IDS["status"],
                "source_fields": list(FACTOR_SOURCE_FIELDS["status"]),
                "base": STATUS_BASE,
                "suppressed_multiplier": STATUS_SUPPRESSED_MULTIPLIER,
                "fatigue_level_one_multiplier": (
                    STATUS_FATIGUE_LEVEL_ONE_MULTIPLIER
                ),
                "fatigue_level_two_plus_multiplier": (
                    STATUS_FATIGUE_LEVEL_TWO_PLUS_MULTIPLIER
                ),
                "lose_control_multiplier": STATUS_LOSE_CONTROL_MULTIPLIER,
                "close_combat_multiplier": STATUS_CLOSE_COMBAT_MULTIPLIER,
            },
        },
        "unit_effective_force": {
            "formula_id": EFFECTIVE_POWER_FORMULA_ID,
            "formula": "V*H*W*A*R*M*I*S",
            "order": list(FACTOR_ORDER),
            "zero_is_valid": True,
            "any_missing_factor": "MISSING",
        },
        "side_aggregation": {
            "formula_id": SIDE_AGGREGATION_FORMULA_ID,
            "operator_order": "OPERATOR_ID_ASCENDING",
            "formula": "SUM_VALID_UNITS",
            "empty_side_effective_power": 0.0,
            "nonempty_no_valid_units": "MISSING",
            "partial_units": "SUM_VALID_AND_WARN",
        },
        "coverage": {
            "formula": "VALID_UNIT_COUNT_DIVIDED_BY_TOTAL_UNIT_COUNT",
            "empty_side": "MISSING",
        },
        "relative_advantage": {
            "formula_id": ADVANTAGE_FORMULA_ID,
            "canonical_side": "RED",
            "missing_side_power": "MISSING",
            "nonpositive_total": 0.0,
            "positive_total_formula": (
                "(E_RED-E_BLUE)/(E_RED+E_BLUE)"
            ),
            "range": [-1.0, 1.0],
            "zero_force": {
                "red_zero_blue_positive": -1.0,
                "red_positive_blue_zero": 1.0,
                "both_zero": 0.0,
            },
        },
        "confidence": "UNIMPLEMENTED_NULL",
        "future_information": "FORBIDDEN",
    })


def analyzer_semantic_hash(
    spec: Mapping[str, Any] | None = None,
) -> str:
    """Hash a semantic spec using canonical JSON/UTF-8/SHA-256."""

    source = analyzer_semantic_spec() if spec is None else spec
    if not isinstance(source, Mapping):
        raise TypeError("analyzer semantic spec must be a Mapping")
    return canonical_hash(source)


ANALYZER_MODEL_HASH = analyzer_semantic_hash()


@dataclass(frozen=True, slots=True)
class AnalyzerAuthorityMetadata:
    analysis_schema_version: str
    model_id: str
    model_version: str
    model_hash: str

    def __post_init__(self) -> None:
        if self.analysis_schema_version != FRAME_ANALYSIS_SCHEMA_VERSION:
            raise ValueError("unsupported frame analysis schema version")
        if self.model_id != ANALYZER_MODEL_ID:
            raise ValueError("unsupported analyzer model id")
        if self.model_version != ANALYZER_MODEL_VERSION:
            raise ValueError("unsupported analyzer model version")
        _require_sha256(self.model_hash)

    def to_dict(self) -> dict[str, str]:
        return {
            "analysis_schema_version": self.analysis_schema_version,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "model_hash": self.model_hash,
        }


def _require_sha256(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError("model_hash must be lowercase SHA-256")
    return value


DEFAULT_ANALYZER_AUTHORITY = AnalyzerAuthorityMetadata(
    FRAME_ANALYSIS_SCHEMA_VERSION,
    ANALYZER_MODEL_ID,
    ANALYZER_MODEL_VERSION,
    ANALYZER_MODEL_HASH,
)


def require_analyzer_authority(
    actual: AnalyzerAuthorityMetadata,
    expected: AnalyzerAuthorityMetadata = DEFAULT_ANALYZER_AUTHORITY,
) -> None:
    """Fail closed when replay/runtime analyzer identities do not match."""

    if not isinstance(actual, AnalyzerAuthorityMetadata):
        raise TypeError("actual must be AnalyzerAuthorityMetadata")
    if not isinstance(expected, AnalyzerAuthorityMetadata):
        raise TypeError("expected must be AnalyzerAuthorityMetadata")
    if actual != expected:
        raise ValueError("EightFactor analyzer authority identity mismatch")


__all__ = [
    "ADVANTAGE_FORMULA_ID",
    "ANALYZER_MODEL_HASH",
    "ANALYZER_MODEL_ID",
    "ANALYZER_MODEL_VERSION",
    "ANALYZER_SEMANTIC_SCHEMA_VERSION",
    "AnalyzerAuthorityMetadata",
    "DEFAULT_ANALYZER_AUTHORITY",
    "EFFECTIVE_POWER_FORMULA_ID",
    "FACTOR_FORMULA_IDS",
    "FACTOR_ORDER",
    "FACTOR_SOURCE_FIELDS",
    "FRAME_ANALYSIS_SCHEMA_VERSION",
    "MOBILITY_STATE",
    "SIDE_AGGREGATION_FORMULA_ID",
    "WEAPON_WEIGHT",
    "analyzer_semantic_hash",
    "analyzer_semantic_spec",
    "require_analyzer_authority",
]
