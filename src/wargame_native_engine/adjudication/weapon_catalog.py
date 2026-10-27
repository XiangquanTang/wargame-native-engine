"""A detached weapon index derived from existing authorities, not new rules.

The operator registry supplies names and known model associations only.  Runtime
profiles and adjudication tables remain the authorities for executable effects.
A model association never authorizes a unit to carry or fire a weapon.
"""

from __future__ import annotations

from collections.abc import Mapping
from wargame_native_engine.weapon_names import weapon_display_name
from typing import Any

from wargame_native_engine.adjudication.table_loader import AdjudicationTableSet
from wargame_native_engine.io.serialization import canonical_hash, json_clone
from wargame_native_engine.rules.guidance import GuidanceAuthority
from wargame_native_engine.rules.indirect_fire import (
    IndirectFireAmmunitionAuthority,
    IndirectFireAuthority,
)
from wargame_native_engine.shareddata.operator_registry import load_operator_registry


def build_weapon_catalog(
    runtime_profiles: Mapping[str | int, Any],
    tables: AdjudicationTableSet | None,
    *,
    guidance_authority: GuidanceAuthority | None = None,
    indirect_authority: IndirectFireAuthority | None = None,
    ammunition_authority: IndirectFireAmmunitionAuthority | None = None,
) -> dict[str, Any]:
    """Index all known IDs while retaining missing/unsupported profile evidence."""
    registry = load_operator_registry()
    registry_record = registry.to_record()
    table_mappings = {} if tables is None else tables.weapon_mappings
    profiles = {str(key): value for key, value in runtime_profiles.items()}
    indirect_ids = set() if indirect_authority is None else {
        profile.weapon_id for profile in indirect_authority.profiles
    }
    weapon_ids = sorted(
        {int(key) for key in table_mappings}
        | set(registry.weapon_profiles)
        | {int(key) for key in profiles}
        | indirect_ids
    )
    entries = []
    for weapon_id in weapon_ids:
        descriptor = registry.weapon_profiles.get(weapon_id)
        profile = profiles.get(str(weapon_id))
        has_profile = isinstance(profile, Mapping)
        indirect = (
            None if indirect_authority is None
            else indirect_authority.profile_for_weapon(weapon_id)
        )
        ammunition = (
            None if ammunition_authority is None
            else ammunition_authority.mapping_for_weapon(weapon_id)
        )
        ammunition_key = None if ammunition is None else ammunition.ammunition_key
        ammunition_source = None if ammunition is None else ammunition.source_mapping
        if has_profile:
            ammunition_key = profile.get("ammunition_key", "0")
            ammunition_source = "scenario.metadata.source_compatible.weapons"
        weapon_class = (
            profile.get("weapon_class") if has_profile
            else table_mappings.get(str(weapon_id))
        )
        compatible = [
            {
                "model": f"source-{operator.identity.type_code}-{operator.identity.subtype_code}",
                "name": operator.display_name,
                "category": operator.category,
            }
            for operator in registry.operators.values()
            if any(item.weapon_id == weapon_id for item in operator.weapons)
        ]
        domains = list(profile.get("target_domains", ())) if has_profile else []
        fire_modes = []
        if has_profile and domains:
            fire_modes = ["DIRECT"]
            if guidance_authority is not None:
                fire_modes = [
                    mode.value for mode in guidance_authority.supported_fire_modes(weapon_id)
                ]
        if indirect is not None:
            fire_modes.append("INDIRECT")
        reasons = []
        if not has_profile and indirect is None:
            reasons.append("WEAPON_PROFILE_UNAVAILABLE")
        if tables is None:
            reasons.append("ADJUDICATION_TABLES_UNAVAILABLE")
        if has_profile and not domains and indirect is None:
            reasons.append("NO_DIRECT_TARGET_DOMAINS")
        if indirect is not None and ammunition is None:
            reasons.append("INDIRECT_AMMUNITION_MAPPING_UNAVAILABLE")
        entries.append({
            "weapon_id": weapon_id,
            "name": weapon_display_name(weapon_id, None if descriptor is None else descriptor.name),
            "weapon_class": weapon_class,
            "compatible_unit_models": sorted(compatible, key=lambda item: item["model"]),
            "model_association_authority": "DISPLAY_ONLY_NOT_CARRIAGE_AUTHORIZATION",
            "maximum_range": profile.get("maximum_range") if has_profile else None,
            "target_domains": domains,
            "fire_modes": sorted(fire_modes),
            "ammunition_key": ammunition_key,
            "ammunition_source": ammunition_source,
            "runtime_profile": json_clone(profile) if has_profile else None,
            "indirect_profile": None if indirect is None else {
                "profile_id": indirect.profile_id,
                "artillery_class": indirect.artillery_class.value,
                "range_policy": indirect.range_policy.value,
                "minimum_range": indirect.minimum_range,
                "maximum_range": indirect.maximum_range,
                "target_domain": indirect.target_domain.value,
                "ammunition_key": None if ammunition is None else ammunition.ammunition_key,
                "ammunition_cost": None if ammunition is None else ammunition.ammo_cost_per_mission,
            },
            "status": "PROFILE_AVAILABLE" if not reasons else "UNRESOLVED",
            "reason_codes": reasons,
        })
    result = {
        "schema_version": "native-weapon-catalog/1.0",
        "weapons": entries,
        "sources": {
            "operator_registry_id": registry.registry_id,
            "operator_registry_version": registry.registry_version,
            "operator_registry_hash": canonical_hash(registry_record),
            "runtime_profiles_hash": canonical_hash(profiles),
            "table_set_hash": None if tables is None else tables.content_hash,
            "guidance_authority_hash": (
                None if guidance_authority is None else guidance_authority.content_hash
            ),
            "indirect_authority_hash": (
                None if indirect_authority is None else indirect_authority.content_hash
            ),
            "ammunition_authority_hash": (
                None if ammunition_authority is None else ammunition_authority.content_hash
            ),
        },
        "scope": "Known weapon metadata, not unit possession or current execution permission",
    }
    result["catalog_hash"] = canonical_hash(result)
    return result
