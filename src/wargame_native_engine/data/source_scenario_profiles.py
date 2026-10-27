"""Explicit, versioned Native launch policy for supported fresh Source scenarios.

This policy does not assert Source-rule equivalence.  Raw source facts remain in
conversion evidence; values absent from Source are named Native conventions.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.io.serialization import json_clone
from wargame_native_engine.weapon_names import weapon_display_name
from wargame_native_engine.data.source_scenario import SourceScenarioConversionError

_ROOT = Path(__file__).resolve().parents[3]
# Exact supported labels, not substring guessing. Names are preserved verbatim.
_PLATFORMS = {
    (1, 2, "步兵"): ("PERSONNEL", "INFANTRY"),
    (1, 2, "步兵小队"): ("PERSONNEL", "INFANTRY"),
    (2, 0, "重型坦克"): ("VEHICLE", "HEAVY_TANK"),
    (2, 0, "中型坦克"): ("VEHICLE", "MEDIUM_TANK"),
    (2, 0, "轻型坦克"): ("VEHICLE", "LIGHT_TANK"),
    (2, 1, "重型战车"): ("VEHICLE", "HEAVY_COMBAT_VEHICLE"),
    (2, 1, "中型战车"): ("VEHICLE", "MEDIUM_COMBAT_VEHICLE"),
    (2, 1, "轻型战车"): ("VEHICLE", "LIGHT_COMBAT_VEHICLE"),
    (3, 5, "无人机"): ("AIR", "DRONE"),
}


def _reject(code: str, message: str) -> None:
    raise SourceScenarioConversionError(code, message)


def build_source_scenario_profile(
    source: Mapping[str, Any], map_id: str, *, hex_size_m: int = 200,
    elevation_unit_m: float = 10,
) -> dict[str, Any]:
    """Build an auditable supplement, never mutate the supplied source object."""
    scenario_id = source.get("scenario_id")
    if type(scenario_id) not in (str, int) or not str(scenario_id):
        _reject("scenario_id_required", "Source scenario_id must be a string or integer")
    operators = source.get("operators")
    if not isinstance(operators, list) or not operators:
        _reject("operators_required", "Source operators must be a nonempty list")
    clock = source.get("time")
    if not isinstance(clock, Mapping):
        _reject("source_time_required", "time is required for an explicit fresh launch")
    if type(clock.get("cur_step")) is not int or clock["cur_step"] != 0:
        _reject("fresh_source_scenario_required", "only cur_step=0 initial scenarios are supported")
    tick, duration = clock.get("tick"), clock.get("max_time")
    if type(tick) not in (int, float) or tick != 1:
        _reject("unsupported_source_tick", "Native launch currently requires tick=1 second")
    if type(duration) is not int or duration <= 0:
        _reject("invalid_source_max_time", "max_time must be a positive integer number of seconds")
    profile = json.loads((_ROOT / "config/source_scenario_conversion/scenarios/2201010105.native-supplement.json").read_text())
    profile.update({
        "supplement_id": f"source-{scenario_id}-native-launch-v1",
        "supplement_version": "1.1.0",
        "scenario_id": str(scenario_id), "map_id": str(map_id),
        "map_profile": {"coordinate_system": "odd-r", "hex_size_m": hex_size_m,
                        "elevation_unit_m": elevation_unit_m},
        "initial_transport_policy": "SOURCE_EXACT_SINGLE",
        "initial_source_policy": "FRESH_SOURCE_V1",
        "armor_profiles": {"0": "NONE", "1": "LIGHT", "2": "MEDIUM",
                           "3": "HEAVY", "4": "COMPOSITE"},
        "armor_profile_basis": "User-supplied q_observation.py armor field: 0 none, 1 light, 2 medium, 3 heavy, 4 composite",
        "operator_profiles": {}, "platform_profiles": {},
        "missing_source_fields": [],
    })
    weapon_users: dict[int, list[Mapping[str, Any]]] = {}
    ids = set()
    for unit in operators:
        if not isinstance(unit, Mapping):
            _reject("invalid_operator", "each operator must be an object")
        obj_id = unit.get("obj_id")
        if type(obj_id) is not int or obj_id < 0 or obj_id in ids:
            _reject("invalid_operator_id", "operator IDs must be unique nonnegative integers")
        ids.add(obj_id)
        key = (unit.get("type"), unit.get("sub_type"), unit.get("name"))
        if type(key[0]) is not int or type(key[1]) is not int or key not in _PLATFORMS:
            _reject("unsupported_source_platform", f"unsupported exact platform {key!r}")
        domain, canonical = _PLATFORMS[key]
        item = {"source_names": [unit["name"]], "native_domain": domain,
                "canonical_type": canonical, "sensor_range": 25,
                "allows_unarmed": domain == "AIR",
                "native_basis": "Exact source label/type binding; Native ObservationProfiles authority governs per-target ranges"}
        profile["operator_profiles"][str(obj_id)] = item
        profile["platform_profiles"].setdefault(f"{key[0]}:{key[1]}", json_clone(item))
        for field in ("altitude", "heading", "observe_distance"):
            if field not in unit:
                profile["missing_source_fields"].append(f"operators[{obj_id}].{field}")
        weapons = unit.get("carry_weapon_ids", [])
        if not isinstance(weapons, list) or any(type(w) is not int for w in weapons):
            _reject("invalid_source_weapons", "carry_weapon_ids must contain integers")
        for weapon in weapons:
            weapon_users.setdefault(weapon, []).append(unit)
    mapping = json.loads((_ROOT / "config/source_compat/weapon_id_mapping_v1.json").read_text())["mappings"]
    tables = json.loads((_ROOT / "config/adjudication/source_tables_v1/native_adjudication_tables_final.json").read_text())["direct_fire"]
    # ID 5 has an explicit Source identity in the KB and uses its existing
    # portable-missile vehicle row (no personnel or air effects).  Override only
    # this new launch supplement; historical delivered supplements stay intact.
    if 5 in weapon_users:
        row = tables["vehicle_attack_levels"]["portable_missile"]
        profile["weapon_profiles"]["5"] = {
            "weapon_class": "portable_missile_ground",
            "maximum_range": row["range"], "target_domains": ["VEHICLE"],
            "ammunition_key": "100", "cooldown_seconds": None,
            "requires_unfolded": False, "can_fire_while_moving": False,
            "native_basis": "KB ID 5 ground variant uses portable_missile vehicle attack row; "
                "standard direct-fire cooldown and vehicle-loss adjudication; "
                "Native infantry no-unfold-operation and ammunition pool 100 conventions retained, "
                "not a claimed Source class mapping",
        }
    for weapon, users in weapon_users.items():
        if str(weapon) in profile["weapon_profiles"]:
            continue
        canonical = mapping.get(str(weapon))
        if canonical is None:
            _reject("unmapped_source_weapon", f"unmapped weapon {weapon}")
        domains, ranges = [], []
        for table_name, domain in (("personnel_attack_levels", "PERSONNEL"),
                                   ("vehicle_attack_levels", "VEHICLE")):
            entry = tables[table_name].get({"vehicle_missile": "vehicle_missile_gun_launched"}.get(canonical, canonical))
            if entry:
                domains.append(domain)
                ranges.append(entry["range"])
        if not domains:
            _reject("unsupported_launch_weapon", f"weapon {weapon} has no supported direct-fire profile")
        if weapon in (37, 56):
            ammunition_key = "0"
            ammo_basis = "Native non-missile pool 0 for direct-fire guns"
        elif weapon in (69, 71):
            pools = set()
            for user in users:
                ammo = user.get("remain_bullet_nums", {})
                if not isinstance(ammo, Mapping):
                    _reject("invalid_ammunition", "remain_bullet_nums must be an object")
                active = [k for k in ("100", "101", "102") if type(ammo.get(k, 0)) is int and ammo.get(k, 0) > 0]
                if len(active) != 1:
                    _reject("ambiguous_native_missile_pool", f"weapon {weapon} requires one nonempty missile pool for this Native launch policy")
                pools.add(active[0])
            if len(pools) != 1:
                _reject("conflicting_native_missile_pool", f"weapon {weapon} carriers use different missile pools")
            ammunition_key = next(iter(pools))
            ammo_basis = "Explicit Native convention: sole nonempty source missile pool; not a claimed Source weapon-to-ammo-class mapping"
        else:
            _reject("unsupported_launch_weapon", f"weapon {weapon} requires an explicit execution policy")
        profile["weapon_profiles"][str(weapon)] = {
            "weapon_class": canonical, "maximum_range": min(ranges),
            "target_domains": domains, "ammunition_key": ammunition_key,
            "cooldown_seconds": None, "requires_unfolded": weapon != 71,
            "can_fire_while_moving": weapon == 37,
            "native_basis": "Native curated direct-fire tables; shared conservative range. " + ammo_basis,
        }
    profile["weapon_profiles"] = {str(w): profile["weapon_profiles"][str(w)] for w in sorted(weapon_users)}
    # Display-only labels; all numeric mechanics and source evidence stay intact.
    for weapon_id, weapon_profile in profile["weapon_profiles"].items():
        weapon_profile["display_name"] = weapon_display_name(
            int(weapon_id), weapon_profile.get("display_name"))
    profile["termination"] = {"max_ticks": duration, "elimination": True,
        "objective_score_to_win": None,
        "native_basis": "Explicit Native one-second tick horizon from source max_time; Native elimination policy retained"}
    profile["initial_defaults_basis"] = (
        "Absent altitude/heading remain explicit Native zero defaults, not recovered Source values; "
        "sensor_range 25 is the existing Native envelope while ObservationProfiles selects target-specific limits; "
        "seed 42 and objective/termination policies are Native conventions")
    return profile
