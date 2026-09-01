"""Deterministic, offline Source Scenario to NativeScenario conversion.

The source files are data evidence only.  This module never imports the old
benchmark runtime and every executable Native rule value absent from Source
must come from an explicit, hashed supplement.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Mapping, Sequence

from wargame_native_engine.compat.source_frame.mapping import SourceMappings
from wargame_native_engine.compat.source_frame.models import ArmorClass, UnitDomain
from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.core.world import ObjectiveState, Stage
from wargame_native_engine.io.native_map_converter import (
    NATIVE_MAP_CONVERTER_VERSION,
    convert_benchmark_basic_map,
)
from wargame_native_engine.io.scenario import NativeScenario, scenario_from_dict
from wargame_native_engine.io.serialization import canonical_hash, canonical_json, json_clone
from wargame_native_engine.rules.terminal_rules import TerminalRules
from wargame_native_engine.rules.operator_lifecycle import (
    load_operator_lifecycle_authority,
)


SOURCE_SCENARIO_CONVERSION_SCHEMA = "source-scenario-conversion/1.0"
SOURCE_SCENARIO_SUPPLEMENT_SCHEMA = "source-scenario-native-supplement/1.0"
SOURCE_SCENARIO_CONVERTER_VERSION = "1.0.0"
STRICT_SOURCE = "strict-source"
EXPLICIT_NATIVE_SUPPLEMENT = "explicit-native-supplement"
_MODES = frozenset({STRICT_SOURCE, EXPLICIT_NATIVE_SUPPLEMENT})
_SIDES = {"0": "red", "1": "blue"}


class SourceScenarioConversionError(ValueError):
    """Stable conversion-boundary rejection with structured details."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = str(code)
        self.message = str(message)
        self.details = json_clone(details or {})
        super().__init__(f"{self.code}:{self.message}")


@dataclass(frozen=True, slots=True)
class SourceScenarioConversion:
    scenario_payload: bytes
    manifest: Mapping[str, Any]
    manifest_payload: bytes

    @property
    def scenario(self) -> NativeScenario:
        value = json.loads(self.scenario_payload.decode("utf-8"))
        return scenario_from_dict(value)

    @property
    def scenario_payload_sha256(self) -> str:
        return hashlib.sha256(self.scenario_payload).hexdigest()

    @property
    def manifest_sha256(self) -> str:
        return hashlib.sha256(self.manifest_payload).hexdigest()


@dataclass(frozen=True, slots=True)
class PublishedSourceScenarioConversion:
    output_dir: Path
    scenario_path: Path
    manifest_path: Path
    scenario_sha256: str
    manifest_sha256: str


def convert_source_scenario(
    source_scenario_path: str | Path,
    source_map_path: str | Path,
    *,
    conversion_mode: str,
    source_scenario_identity: str,
    source_map_identity: str,
    expected_scenario_id: str,
    expected_map_id: str,
    supplement_path: str | Path | None = None,
    coordinate_system_override: str | None = None,
    hex_size_m_override: int | None = None,
    elevation_unit_m_override: float | None = None,
) -> SourceScenarioConversion:
    """Convert one explicitly bound Source Scenario and map.

    Absolute paths never enter canonical output.  The caller supplies stable
    source identities while exact input byte hashes bind those identities to
    the files actually read.
    """

    mode = str(conversion_mode)
    if mode not in _MODES:
        raise SourceScenarioConversionError(
            "unsupported_conversion_mode", f"unsupported mode {mode!r}"
        )
    scenario_identity = _identity(source_scenario_identity, "source_scenario_identity")
    map_identity = _identity(source_map_identity, "source_map_identity")
    expected_scenario = _identity(expected_scenario_id, "expected_scenario_id")
    expected_map = _identity(expected_map_id, "expected_map_id")

    scenario_path = Path(source_scenario_path)
    map_path = Path(source_map_path)
    scenario_raw, source = _read_object(scenario_path, "source scenario")
    map_raw = _read_bytes(map_path, "source map")
    source_id = _source_identity(source.get("scenario_id"), "scenario_id")
    if source_id != expected_scenario:
        raise SourceScenarioConversionError(
            "scenario_id_mismatch", f"expected {expected_scenario}, got {source_id}"
        )

    supplement_raw: bytes | None = None
    if mode == STRICT_SOURCE:
        if supplement_path is not None:
            raise SourceScenarioConversionError(
                "strict_source_rejects_supplement",
                "strict-source mode cannot consume a Native supplement",
            )
        embedded = source.get("native_conversion_profile")
        if not isinstance(embedded, Mapping):
            raise SourceScenarioConversionError(
                "strict_source_missing_fields",
                "Source does not contain the executable Native rule contract",
                details={"missing_fields": _strict_missing_fields(source)},
            )
        profile = json_clone(embedded)
        profile_source = "SOURCE_EXACT"
        supplement_hash = None
        supplement_content_hash = None
    else:
        if supplement_path is None:
            raise SourceScenarioConversionError(
                "supplement_required",
                "explicit-native-supplement mode requires a supplement file",
            )
        supplement_raw, profile = _read_object(Path(supplement_path), "supplement")
        profile_source = "NATIVE_SUPPLEMENT"
        supplement_hash = hashlib.sha256(supplement_raw).hexdigest()
        supplement_content_hash = canonical_hash(profile)

    _validate_profile_identity(profile, expected_scenario, expected_map)
    map_conversion = convert_benchmark_basic_map(
        map_path,
        coordinate_system_override=coordinate_system_override,
        hex_size_m_override=hex_size_m_override,
        elevation_unit_m_override=elevation_unit_m_override,
    )
    native_map = map_conversion.native_map
    if native_map.map_id != expected_map:
        raise SourceScenarioConversionError(
            "map_id_mismatch", f"expected {expected_map}, got {native_map.map_id}"
        )
    _validate_map_profile(profile, native_map)
    native_map = _apply_source_roadblocks(native_map, source)

    mappings = SourceMappings()
    operators, unit_records, warnings = _convert_operators(
        source,
        native_map_ids={item.hex_id for item in native_map.hexes},
        profile=profile,
        mappings=mappings,
        profile_source=profile_source,
    )
    objectives, objective_records = _convert_objectives(source, native_map, profile)
    termination = _mapping(profile.get("termination"), "termination")
    max_ticks = _exact_positive_int(termination.get("max_ticks"), "termination.max_ticks")
    elimination = _exact_bool(termination.get("elimination"), "termination.elimination")
    objective_score = termination.get("objective_score_to_win")
    if objective_score is not None:
        objective_score = _exact_positive_int(
            objective_score, "termination.objective_score_to_win"
        )
    default_seed = _exact_int(
        _mapping(profile.get("seed_policy"), "seed_policy").get("default_seed"),
        "seed_policy.default_seed",
    )

    positions: dict[str, dict[int, tuple[float, float]]] = {"red": {}, "blue": {}}
    for operator in operators:
        if operator.current_hex is None:
            raise RuntimeError("converted source operator lost current_hex")
        row, column = divmod(operator.current_hex, 100)
        positions[operator.side][operator.operator_id] = (float(column), float(row))

    source_sha = hashlib.sha256(scenario_raw).hexdigest()
    source_map_sha = hashlib.sha256(map_raw).hexdigest()
    conversion_record = {
        "schema": SOURCE_SCENARIO_CONVERSION_SCHEMA,
        "converter_version": SOURCE_SCENARIO_CONVERTER_VERSION,
        "conversion_mode": mode,
        "native_rules_equivalence": False,
        "source_scenario": {"identity": scenario_identity, "sha256": source_sha},
        "source_map": {"identity": map_identity, "sha256": source_map_sha},
        "native_map": {
            "map_id": native_map.map_id,
            "sha256": canonical_hash(native_map.to_dict()),
            "payload_sha256": hashlib.sha256(
                _payload(native_map.to_dict())
            ).hexdigest(),
            "converter_version": NATIVE_MAP_CONVERTER_VERSION,
            "coordinate_system": "odd-r",
        },
        "profile": {
            "source": profile_source,
            "supplement_id": profile.get("supplement_id"),
            "supplement_version": profile.get("supplement_version"),
            "file_sha256": supplement_hash,
            "content_sha256": supplement_content_hash,
        },
        "deployment_mode": "SOURCE_DEPLOYMENT",
        "termination_mode": profile_source,
        "default_seed_policy": {"default_seed": default_seed, "source": profile_source},
        "source_exact_fields": [
            "scenario_id", "operators.obj_id", "operators.color",
            "operators.type", "operators.sub_type", "operators.name",
            "operators.cur_hex", "operators.blood", "operators.max_blood",
            "operators.basic_speed", "operators.carry_weapon_ids",
            "operators.remain_bullet_nums", "operators.weapon_unfold_state",
            "cities", "landmarks", "map facts",
        ],
        "source_derived_fields": [
            "side from documented color mapping", "row/column from row*100+column",
            "unit domain from versioned Source type mapping", "source deployment",
        ],
        "native_supplemented_fields": (
            [] if mode == STRICT_SOURCE else [
                "platform profiles", "armor classes", "sensor profiles",
                "weapon execution profiles", "initial runtime defaults",
                "objective policy", "termination profile", "default seed policy",
            ]
        ),
        "missing_fields": [],
        "warnings": list(warnings),
    }
    source_metadata = {
        **conversion_record,
        "unit_records": unit_records,
        "objective_records": objective_records,
    }
    scenario = NativeScenario(
        scenario_id=f"source-{source_id}-native-v1",
        map=native_map,
        side_order=("red", "blue"),
        operators=tuple(sorted(operators, key=lambda item: item.operator_id)),
        objectives=tuple(objectives),
        deployment_positions=positions,
        terminal_rules=TerminalRules(
            max_ticks=max_ticks,
            elimination=elimination,
            objective_score_to_win=objective_score,
        ),
        metadata={
            "source_scenario_conversion": source_metadata,
            "source_compatible": {
                "weapons": json_clone(profile["weapon_profiles"]),
            },
            "source_cities": json_clone(source.get("cities", [])),
            "source_landmarks": json_clone(source.get("landmarks", {})),
            "source_time": json_clone(source.get("time", {})),
        },
        initial_stage=Stage.COMBAT,
    )
    scenario_payload = _payload(scenario.to_dict())
    # Parse the final bytes before returning; this is the same path used by the
    # catalog and catches schema drift at the conversion boundary.
    scenario_from_dict(json.loads(scenario_payload.decode("utf-8")))
    manifest = {
        **conversion_record,
        "output": {
            "scenario_id": scenario.scenario_id,
            "scenario_hash": scenario.scenario_hash,
            "scenario_payload_sha256": hashlib.sha256(scenario_payload).hexdigest(),
            "operator_count": len(operators),
            "red_count": sum(item.side == "red" for item in operators),
            "blue_count": sum(item.side == "blue" for item in operators),
            "objective_count": len(objectives),
            "map_cell_count": len(native_map.hexes),
        },
        "coverage": {
            "unit_mapping": {"mapped": len(operators), "total": len(operators)},
            "initial_position": {"mapped": len(operators), "total": len(operators)},
            "platform_profile": {"mapped": len(operators), "total": len(operators)},
            "weapon_id": {
                "mapped": len({weapon for item in operators for weapon in item.weapon_ids}),
                "total": len({weapon for item in operators for weapon in item.weapon_ids}),
            },
        },
    }
    manifest_payload = _payload(manifest)
    return SourceScenarioConversion(
        scenario_payload,
        json_clone(manifest),
        manifest_payload,
    )


def publish_source_scenario_conversion(
    conversion: SourceScenarioConversion,
    output_dir: str | Path,
) -> PublishedSourceScenarioConversion:
    """Atomically publish a canonical scenario and its external manifest."""

    target = Path(output_dir)
    if target.exists():
        raise FileExistsError(f"source scenario output already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=target.parent))
    scenario_name = "native_scenario.json"
    manifest_name = "source_scenario_conversion.native.json"
    try:
        (staging / scenario_name).write_bytes(conversion.scenario_payload)
        (staging / manifest_name).write_bytes(conversion.manifest_payload)
        os.replace(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return PublishedSourceScenarioConversion(
        target,
        target / scenario_name,
        target / manifest_name,
        conversion.scenario_payload_sha256,
        conversion.manifest_sha256,
    )


def inspect_source_scenario(
    source_scenario_path: str | Path,
    source_map_path: str | Path,
    *,
    expected_scenario_id: str,
    expected_map_id: str,
    coordinate_system_override: str | None = None,
    hex_size_m_override: int | None = None,
) -> Mapping[str, Any]:
    """Return a read-only structural inventory without applying supplements."""

    scenario_raw, source = _read_object(Path(source_scenario_path), "source scenario")
    map_raw = _read_bytes(Path(source_map_path), "source map")
    if _source_identity(source.get("scenario_id"), "scenario_id") != str(
        expected_scenario_id
    ):
        raise SourceScenarioConversionError("scenario_id_mismatch", "unexpected scenario")
    converted_map = convert_benchmark_basic_map(
        source_map_path,
        coordinate_system_override=coordinate_system_override,
        hex_size_m_override=hex_size_m_override,
    )
    if converted_map.native_map.map_id != str(expected_map_id):
        raise SourceScenarioConversionError("map_id_mismatch", "unexpected map")
    operators = _sequence(source.get("operators"), "operators")
    ids = [_exact_int(_mapping(item, "operator").get("obj_id"), "obj_id") for item in operators]
    weapon_ids = sorted({
        _exact_int(weapon, "weapon_id")
        for raw in operators
        for weapon in _sequence(_mapping(raw, "operator").get("carry_weapon_ids", []), "carry_weapon_ids")
    })
    return {
        "scenario_id": str(expected_scenario_id),
        "map_id": str(expected_map_id),
        "source_scenario_sha256": hashlib.sha256(scenario_raw).hexdigest(),
        "source_map_sha256": hashlib.sha256(map_raw).hexdigest(),
        "native_map_sha256": converted_map.native_map_sha256,
        "operator_count": len(operators),
        "red_count": sum(_mapping(item, "operator").get("color") == 0 for item in operators),
        "blue_count": sum(_mapping(item, "operator").get("color") == 1 for item in operators),
        "duplicate_operator_ids": len(ids) - len(set(ids)),
        "weapon_ids": weapon_ids,
        "map_cell_count": len(converted_map.native_map.hexes),
        "strict_source_missing_fields": _strict_missing_fields(source),
    }


def _convert_operators(
    source: Mapping[str, Any],
    *,
    native_map_ids: set[int],
    profile: Mapping[str, Any],
    mappings: SourceMappings,
    profile_source: str,
) -> tuple[list[OperatorState], list[Mapping[str, Any]], tuple[str, ...]]:
    raw_operators = _sequence(source.get("operators"), "operators")
    if not raw_operators:
        raise SourceScenarioConversionError("operators_required", "operators must not be empty")
    platform_profiles = _mapping(profile.get("platform_profiles"), "platform_profiles")
    lifecycle_authority = load_operator_lifecycle_authority()
    armor_profiles = _mapping(profile.get("armor_profiles"), "armor_profiles")
    weapon_profiles = _mapping(profile.get("weapon_profiles"), "weapon_profiles")
    defaults = _mapping(profile.get("initial_state_defaults"), "initial_state_defaults")
    default_altitude = _finite_number(defaults.get("altitude"), "initial_state_defaults.altitude")
    default_heading = _finite_number(defaults.get("heading"), "initial_state_defaults.heading")
    default_speed = _finite_nonnegative(defaults.get("speed"), "initial_state_defaults.speed")
    default_state = _nonempty_string(defaults.get("state"), "initial_state_defaults.state")
    default_stopped = _exact_bool(defaults.get("stopped"), "initial_state_defaults.stopped")
    default_weapon_locked = _exact_bool(
        defaults.get("weapon_locked"), "initial_state_defaults.weapon_locked"
    )

    operators: list[OperatorState] = []
    records: list[Mapping[str, Any]] = []
    seen: set[int] = set()
    warnings: set[str] = set()
    for index, raw in enumerate(raw_operators):
        item = _mapping(raw, f"operators[{index}]")
        operator_id = _exact_nonnegative_int(item.get("obj_id"), f"operators[{index}].obj_id")
        if operator_id in seen:
            raise SourceScenarioConversionError(
                "duplicate_operator_id", f"duplicate operator {operator_id}"
            )
        seen.add(operator_id)
        color = _exact_int(item.get("color"), f"operators[{index}].color")
        side = _SIDES.get(str(color))
        if side is None:
            raise SourceScenarioConversionError("unknown_side", f"unmapped color {color}")
        type_code = _exact_int(item.get("type"), f"operators[{index}].type")
        subtype_code = _exact_int(item.get("sub_type"), f"operators[{index}].sub_type")
        profile_key = f"{type_code}:{subtype_code}"
        platform = _mapping(
            platform_profiles.get(profile_key), f"platform_profiles.{profile_key}"
        )
        native_domain = _nonempty_string(platform.get("native_domain"), "native_domain").upper()
        if native_domain not in {item.value for item in UnitDomain if item is not UnitDomain.UNKNOWN}:
            raise SourceScenarioConversionError(
                "invalid_native_domain", f"invalid domain {native_domain!r}"
            )
        documented_domain = mappings.domain(type_code)
        if documented_domain.status == "UNMAPPED" or documented_domain.native_effective_value != native_domain:
            raise SourceScenarioConversionError(
                "platform_domain_conflict",
                f"profile {profile_key} conflicts with versioned Source type mapping",
            )
        expected_names = _sequence(platform.get("source_names"), "source_names")
        source_name = _nonempty_string(item.get("name"), f"operators[{index}].name")
        if source_name not in expected_names:
            raise SourceScenarioConversionError(
                "platform_name_mismatch",
                f"operator {operator_id} name does not match exact profile {profile_key}",
            )
        canonical_type = _nonempty_string(platform.get("canonical_type"), "canonical_type")
        mapped_identity = mappings.canonical_identity(type_code, subtype_code)
        if (
            mapped_identity.status != "UNMAPPED"
            and mapped_identity.native_effective_value != canonical_type.upper()
        ):
            raise SourceScenarioConversionError(
                "platform_identity_conflict",
                f"profile {profile_key} conflicts with versioned Source identity mapping",
            )
        lifecycle_profile = lifecycle_authority.profile_for_source_codes(
            type_code,
            subtype_code,
        )
        if (
            lifecycle_profile is not None
            and lifecycle_profile.canonical_identity != canonical_type.upper()
        ):
            raise SourceScenarioConversionError(
                "lifecycle_identity_conflict",
                f"profile {profile_key} conflicts with lifecycle authority",
            )
        sensor_range = _finite_nonnegative(platform.get("sensor_range"), "sensor_range")
        current_hex = _exact_nonnegative_int(item.get("cur_hex"), "cur_hex")
        if current_hex not in native_map_ids:
            raise SourceScenarioConversionError(
                "initial_hex_not_in_map",
                f"operator {operator_id} uses unknown hex {current_hex}",
            )
        blood = _exact_nonnegative_int(item.get("blood"), "blood")
        max_blood = _exact_positive_int(item.get("max_blood"), "max_blood")
        if blood > max_blood:
            raise SourceScenarioConversionError("invalid_health", f"operator {operator_id}")
        basic_speed = _finite_nonnegative(item.get("basic_speed"), "basic_speed")
        armor_code = _exact_nonnegative_int(item.get("armor"), "armor")
        armor_class = _nonempty_string(
            armor_profiles.get(str(armor_code)), f"armor_profiles.{armor_code}"
        ).upper()
        if armor_class not in {item.value for item in ArmorClass if item is not ArmorClass.UNKNOWN}:
            raise SourceScenarioConversionError(
                "invalid_armor_profile", f"unsupported armor class {armor_class!r}"
            )
        source_weapons = tuple(
            _exact_nonnegative_int(value, "carry_weapon_ids[]")
            for value in _sequence(item.get("carry_weapon_ids", []), "carry_weapon_ids")
        )
        for weapon_id in source_weapons:
            configured = _mapping(
                weapon_profiles.get(str(weapon_id)), f"weapon_profiles.{weapon_id}"
            )
            mapped = mappings.weapon(weapon_id)
            if mapped.status == "UNMAPPED":
                raise SourceScenarioConversionError(
                    "unmapped_source_weapon", f"unmapped weapon {weapon_id}"
                )
            weapon_class = _nonempty_string(configured.get("weapon_class"), "weapon_class")
            if weapon_class != mapped.native_effective_value:
                raise SourceScenarioConversionError(
                    "weapon_profile_conflict",
                    f"weapon {weapon_id} must map to {mapped.native_effective_value}",
                )
            _validate_weapon_profile(configured, weapon_id)
            if not configured.get("target_domains"):
                warnings.add(f"weapon_{weapon_id}_has_no_native_direct_fire_target")
        if not source_weapons and not _exact_bool(
            platform.get("allows_unarmed"), "platform.allows_unarmed"
        ):
            raise SourceScenarioConversionError(
                "unexpected_unarmed_operator", f"operator {operator_id} is unarmed"
            )
        ammunition = {
            str(key): _exact_nonnegative_int(value, f"remain_bullet_nums.{key}")
            for key, value in _mapping(
                item.get("remain_bullet_nums", {}), "remain_bullet_nums"
            ).items()
        }
        unfolded_raw = item.get("weapon_unfold_state")
        unfolded = (
            _exact_int(unfolded_raw, "weapon_unfold_state") == 1
            if unfolded_raw is not None
            else _exact_bool(defaults.get("weapon_unfolded"), "initial_state_defaults.weapon_unfolded")
        )
        if unfolded_raw is not None and unfolded_raw not in {0, 1}:
            raise SourceScenarioConversionError(
                "invalid_weapon_unfold_state", f"operator {operator_id}"
            )
        row, column = divmod(current_hex, 100)
        source_frame = json_clone(item)
        source_frame.update({
            "speed": default_speed,
            "stop": int(default_stopped),
            "move_state": 0,
            "move_path": [],
            "alive": blood > 0,
            "weapon_unfold_state": int(unfolded),
        })
        raw_attributes = {
            "source_frame": source_frame,
            "source_scenario_id": str(source.get("scenario_id")),
            "source_name": source_name,
            "canonical_type": canonical_type,
            "armor_class": armor_class,
            "ammunition": ammunition,
            "normalized_value": {"armor_class": armor_class},
            "mapping_provenance": {
                "side": "SOURCE_DERIVED:documented color mapping",
                "domain": "SOURCE_DERIVED:versioned Source type mapping",
                "platform": f"{profile_source}:exact type/sub_type profile {profile_key}",
                "armor": f"{profile_source}:source armor code {armor_code}",
                "sensor": f"{profile_source}:platform profile",
                "weapon_ids": "SOURCE_EXACT",
            },
        }
        operator = OperatorState(
            operator_id=operator_id,
            side=side,
            operator_type=native_domain,
            subtype=f"source-{type_code}-{subtype_code}",
            x=float(column),
            y=float(row),
            altitude=_finite_number(item.get("altitude", default_altitude), "altitude"),
            heading=_finite_number(item.get("heading", default_heading), "heading"),
            health=float(blood),
            max_health=float(max_blood),
            speed=default_speed,
            max_speed=basic_speed,
            move_range=0.0,
            sensor_range=sensor_range,
            state="destroyed" if blood == 0 else default_state,
            alive=blood > 0,
            weapon_ids=list(source_weapons),
            raw_attributes=raw_attributes,
            attack_range=0.0,
            attack_damage=0.0,
            deployed=True,
            blood=blood,
            max_blood=max_blood,
            basic_speed=basic_speed,
            current_hex=current_hex,
            cur_pos_millionths=0,
            move_path=[],
            movement_mode="NORMAL",
            stopped=default_stopped,
            weapon_locked=default_weapon_locked,
            weapon_unfolded=unfolded,
            lifecycle_profile_id=(
                None if lifecycle_profile is None else lifecycle_profile.profile_id
            ),
            lifecycle_remaining_ms=(
                None
                if lifecycle_profile is None
                else lifecycle_profile.maximum_lifetime_ms
            ),
        )
        operators.append(operator)
        records.append({
            "source_unit_id": operator_id,
            "canonical_unit_id": operator_id,
            "side": side,
            "display_name": source_name,
            "source_type": {"type": type_code, "sub_type": subtype_code},
            "native_platform_type": canonical_type,
            "native_domain": native_domain,
            "initial_hex": current_hex,
            "movement_profile": {"basic_speed": basic_speed},
            "weapon_profile": list(source_weapons),
            "sensor_profile": {"sensor_range": sensor_range},
            "provenance": profile_source,
        })
    if not any(item.side == "red" for item in operators) or not any(
        item.side == "blue" for item in operators
    ):
        raise SourceScenarioConversionError(
            "both_sides_required", "scenario requires RED and BLUE operators"
        )
    return operators, records, tuple(sorted(warnings))


def _convert_objectives(
    source: Mapping[str, Any], native_map: Any, profile: Mapping[str, Any]
) -> tuple[list[ObjectiveState], list[Mapping[str, Any]]]:
    policy = _mapping(profile.get("objective_policy"), "objective_policy")
    mode = _nonempty_string(policy.get("mode"), "objective_policy.mode")
    if mode not in {"SOURCE_CITIES_AS_NATIVE_OBJECTIVES", "METADATA_ONLY"}:
        raise SourceScenarioConversionError("invalid_objective_policy", mode)
    cities = _sequence(source.get("cities", []), "cities")
    if mode == "METADATA_ONLY":
        return [], []
    radius = _finite_nonnegative(policy.get("radius_hex"), "objective_policy.radius_hex")
    map_ids = {item.hex_id for item in native_map.hexes}
    objectives: list[ObjectiveState] = []
    records: list[Mapping[str, Any]] = []
    for index, raw in enumerate(cities):
        item = _mapping(raw, f"cities[{index}]")
        coord = _exact_nonnegative_int(item.get("coord"), "city.coord")
        if coord not in map_ids:
            raise SourceScenarioConversionError(
                "objective_hex_not_in_map", f"city {index} uses unknown hex {coord}"
            )
        flag = _exact_int(item.get("flag", -1), "city.flag")
        if flag not in {-1, 0, 1}:
            raise SourceScenarioConversionError("invalid_city_flag", str(flag))
        controlling = None if flag == -1 else _SIDES[str(flag)]
        value = _exact_positive_int(item.get("value"), "city.value")
        row, column = divmod(coord, 100)
        objective_id = index + 1
        objectives.append(ObjectiveState(
            objective_id,
            float(column),
            float(row),
            radius,
            value,
            controlling,
        ))
        records.append({
            "objective_id": objective_id,
            "source_hex": coord,
            "source_name": item.get("name"),
            "source_type": item.get("type"),
            "source_flag": flag,
            "source_value": value,
            "native_policy": "explicit Native same-hex objective scoring",
        })
    return objectives, records


def _validate_profile_identity(
    profile: Mapping[str, Any], scenario_id: str, map_id: str
) -> None:
    if profile.get("schema") != SOURCE_SCENARIO_SUPPLEMENT_SCHEMA:
        raise SourceScenarioConversionError("invalid_supplement_schema", "unsupported schema")
    if _source_identity(profile.get("scenario_id"), "supplement.scenario_id") != scenario_id:
        raise SourceScenarioConversionError("supplement_scenario_mismatch", scenario_id)
    if _source_identity(profile.get("map_id"), "supplement.map_id") != map_id:
        raise SourceScenarioConversionError("supplement_map_mismatch", map_id)
    _nonempty_string(profile.get("supplement_id"), "supplement_id")
    _nonempty_string(profile.get("supplement_version"), "supplement_version")
    if profile.get("native_rules_equivalence") is not False:
        raise SourceScenarioConversionError(
            "invalid_equivalence_claim", "native_rules_equivalence must be false"
        )


def _validate_map_profile(profile: Mapping[str, Any], native_map: Any) -> None:
    map_profile = _mapping(profile.get("map_profile"), "map_profile")
    coordinate = _nonempty_string(map_profile.get("coordinate_system"), "coordinate_system")
    if coordinate != "odd-r":
        raise SourceScenarioConversionError("unsupported_coordinate_system", coordinate)
    expected_hex = _exact_positive_int(map_profile.get("hex_size_m"), "hex_size_m")
    if native_map.hex_size_m != expected_hex:
        raise SourceScenarioConversionError(
            "map_hex_size_mismatch", f"{native_map.hex_size_m} != {expected_hex}"
        )
    expected_elevation = _finite_number(
        map_profile.get("elevation_unit_m"), "elevation_unit_m"
    )
    if native_map.elevation_unit_m != expected_elevation:
        raise SourceScenarioConversionError(
            "map_elevation_unit_mismatch",
            f"{native_map.elevation_unit_m} != {expected_elevation}",
        )


def _apply_source_roadblocks(native_map: Any, source: Mapping[str, Any]) -> Any:
    landmarks = _mapping(source.get("landmarks", {}), "landmarks")
    minefields = _sequence(landmarks.get("minefields", []), "landmarks.minefields")
    if minefields:
        raise SourceScenarioConversionError(
            "unsupported_source_minefields",
            "Native simplified runtime has no Source minefield conversion",
        )
    ids = {item.hex_id for item in native_map.hexes}
    roadblocks = tuple(sorted({
        _exact_nonnegative_int(value, "landmarks.roadblocks[]")
        for value in _sequence(landmarks.get("roadblocks", []), "landmarks.roadblocks")
    }))
    unknown = sorted(set(roadblocks) - ids)
    if unknown:
        raise SourceScenarioConversionError(
            "roadblock_hex_not_in_map", f"unknown roadblocks {unknown}"
        )
    return replace(native_map, roadblocks=roadblocks)


def _validate_weapon_profile(value: Mapping[str, Any], weapon_id: int) -> None:
    _finite_nonnegative(value.get("maximum_range"), f"weapon {weapon_id}.maximum_range")
    targets = _sequence(value.get("target_domains"), "target_domains")
    allowed = {UnitDomain.PERSONNEL.value, UnitDomain.VEHICLE.value, UnitDomain.AIR.value}
    if any(str(item) not in allowed for item in targets):
        raise SourceScenarioConversionError(
            "invalid_weapon_target_domain", f"weapon {weapon_id}"
        )
    _nonempty_string(value.get("ammunition_key"), "ammunition_key")
    _exact_bool(value.get("requires_unfolded"), "requires_unfolded")
    _exact_bool(value.get("can_fire_while_moving"), "can_fire_while_moving")
    cooldown = value.get("cooldown_seconds")
    if cooldown is not None:
        _exact_nonnegative_int(cooldown, "cooldown_seconds")
    _nonempty_string(value.get("native_basis"), "native_basis")


def _strict_missing_fields(source: Mapping[str, Any]) -> list[str]:
    missing = [
        "native_conversion_profile.platform_profiles",
        "native_conversion_profile.armor_profiles",
        "native_conversion_profile.weapon_profiles",
        "native_conversion_profile.initial_state_defaults",
        "native_conversion_profile.objective_policy",
        "native_conversion_profile.termination",
        "native_conversion_profile.seed_policy",
    ]
    if any("observe_distance" not in _mapping(item, "operator")
           for item in _sequence(source.get("operators", []), "operators")):
        missing.append("operators[].observe_distance")
    return missing


def _read_object(path: Path, label: str) -> tuple[bytes, Mapping[str, Any]]:
    raw = _read_bytes(path, label)
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceScenarioConversionError("invalid_json", f"invalid {label}") from exc
    if not isinstance(value, Mapping):
        raise SourceScenarioConversionError("invalid_root", f"{label} root must be an object")
    return raw, value


def _read_bytes(path: Path, label: str) -> bytes:
    if not path.is_file():
        raise SourceScenarioConversionError("input_not_found", f"{label} is not a file")
    return path.read_bytes()


def _source_identity(value: object, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SourceScenarioConversionError("invalid_identity", f"{field} must be string or int")
    return _identity(str(value), field)


def _identity(value: object, field: str) -> str:
    return _nonempty_string(value, field)


def _nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceScenarioConversionError("invalid_string", f"{field} must be non-empty")
    return value


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SourceScenarioConversionError("invalid_mapping", f"{field} must be an object")
    return value


def _sequence(value: object, field: str) -> Sequence[Any]:
    if not isinstance(value, (list, tuple)):
        raise SourceScenarioConversionError("invalid_sequence", f"{field} must be an array")
    return value


def _exact_int(value: object, field: str) -> int:
    if type(value) is not int:
        raise SourceScenarioConversionError("invalid_exact_int", f"{field} must be an exact int")
    return value


def _exact_nonnegative_int(value: object, field: str) -> int:
    result = _exact_int(value, field)
    if result < 0:
        raise SourceScenarioConversionError("invalid_nonnegative_int", f"{field} must be non-negative")
    return result


def _exact_positive_int(value: object, field: str) -> int:
    result = _exact_int(value, field)
    if result <= 0:
        raise SourceScenarioConversionError("invalid_positive_int", f"{field} must be positive")
    return result


def _exact_bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise SourceScenarioConversionError("invalid_bool", f"{field} must be bool")
    return value


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SourceScenarioConversionError("invalid_number", f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise SourceScenarioConversionError("invalid_number", f"{field} must be finite")
    return result


def _finite_nonnegative(value: object, field: str) -> float:
    result = _finite_number(value, field)
    if result < 0:
        raise SourceScenarioConversionError("invalid_number", f"{field} must be non-negative")
    return result


def _payload(value: object) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")
