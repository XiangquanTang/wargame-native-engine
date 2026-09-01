from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path

import pytest

from wargame_native_engine.viewer.operator_registry import (
    OPERATOR_DESCRIPTOR_SCHEMA,
    OperatorRegistryError,
    default_operator_registry_path,
    load_operator_registry,
    resolve_operator,
)


ROOT = Path(__file__).resolve().parents[1]
JS_RESOLVER = (
    ROOT
    / "src/wargame_native_engine/viewer/static/unit_symbol_resolver.js"
)
SOURCE_SUPPLEMENT = (
    ROOT
    / "config/source_scenario_conversion/scenarios/2201010105.native-supplement.json"
)
ADJUDICATION_TABLES = (
    ROOT
    / "config/adjudication/source_tables_v1/native_adjudication_tables_final.json"
)

EXPECTED_SYMBOLS = {
    "1_2": ("步兵", "infantry", ()),
    "1_16": ("便携防空导弹排", "manpad", ()),
    "2_0": ("重型坦克", "tank", ("armor_heavy",)),
    "2_1": ("重型战车", "armored_vehicle", ("armor_heavy",)),
    "2_3": ("炮兵", "artillery", ()),
    "2_4": ("无人战车", "ugv", ("unmanned",)),
    "2_9": ("侦察型战车", "recon_vehicle", ()),
    "2_10": ("炮兵校射雷达车", "artillery_radar_vehicle", ()),
    "2_13": ("布雷车", "mine_layer_vehicle", ()),
    "2_14": ("扫雷车", "mine_clear_vehicle", ()),
    "2_15": ("防空高炮", "anti_aircraft_gun", ()),
    "2_17": ("车载防空导弹车", "sam_vehicle", ()),
    "2_18": ("皮卡车", "pickup", ()),
    "3_5": ("无人机", "drone", ()),
    "3_6": ("武装直升机", "attack_helicopter", ()),
    "3_7": ("巡飞弹", "loitering_munition", ()),
    "3_8": ("运输直升机", "transport_helicopter", ()),
    "4_11": ("人员战斗工事", "fortification", ()),
    "4_12": ("车辆工事", "fortification", ()),
    "4_20": ("人员隐蔽工事", "fortification", ()),
    "5_19": ("天基侦察算子", "space_asset", ()),
}


def test_registry_covers_curated_exact_operator_identities() -> None:
    registry = load_operator_registry()

    assert set(registry.operators) == set(EXPECTED_SYMBOLS)
    for key, (display_name, base, modifiers) in EXPECTED_SYMBOLS.items():
        raw_type, raw_subtype = key.split("_")
        descriptor = registry.resolve_operator(int(raw_type), int(raw_subtype))
        assert descriptor.key == key
        assert descriptor.display_name == display_name
        assert descriptor.symbol.base == base
        assert descriptor.symbol.modifiers == modifiers
        assert descriptor.status == "MAPPED"


def test_registry_enriches_operator_weapon_references_from_profiles() -> None:
    registry = load_operator_registry()

    assert len(registry.weapon_profiles) == 13
    tank = registry.resolve_operator(2, 0)
    assert tuple(weapon.weapon_id for weapon in tank.weapons) == (43, 36)
    assert tank.weapons[0].to_record() == {
        "weapon_id": 43,
        "name": "车载轻武器",
        "weapon_class": "vehicle_light_weapon",
        "range": 10,
        "target_domain": ["PERSONNEL"],
        "requires_unfolded": True,
        "can_fire_while_moving": False,
        "status": "CURATED_PROFILE",
    }
    unresolved = registry.weapon_profiles[16]
    assert unresolved.status == "UNKNOWN"
    assert unresolved.maximum_range is None
    assert unresolved.requires_unfolded is None
    assert unresolved.can_fire_while_moving is None
    loitering = registry.resolve_operator(3, 7)
    assert tuple(weapon.weapon_id for weapon in loitering.weapons) == (76,)
    assert loitering.weapons[0].status == "CURATED_SELF_AS_MUNITION"


def test_weapon_descriptions_match_existing_machine_profiles_and_tables() -> None:
    registry = load_operator_registry()
    supplement = json.loads(SOURCE_SUPPLEMENT.read_text(encoding="utf-8"))
    tables = json.loads(ADJUDICATION_TABLES.read_text(encoding="utf-8"))

    for raw_id, source in supplement["weapon_profiles"].items():
        weapon_id = int(raw_id)
        if weapon_id not in registry.weapon_profiles:
            continue
        actual = registry.weapon_profiles[weapon_id]
        assert actual.weapon_class == source["weapon_class"]
        assert actual.maximum_range == source["maximum_range"]
        assert list(actual.target_domains) == source["target_domains"]
        assert actual.requires_unfolded is source["requires_unfolded"]
        assert actual.can_fire_while_moving is source["can_fire_while_moving"]

    source_mappings = tables["source_mappings"]["weapon_id_to_type"]
    for weapon_id, actual in registry.weapon_profiles.items():
        if weapon_id == 16:
            assert actual.status == "UNKNOWN"
            continue
        source = source_mappings[str(weapon_id)]
        assert actual.name == source["source_name"]
        assert actual.weapon_class == source["canonical_type"]

    assert registry.weapon_profiles[1].maximum_range == (
        tables["anti_air"]["weapon_attack_levels"]["anti_air_cannon"]["range"]
    )
    assert registry.weapon_profiles[74].maximum_range == (
        tables["direct_fire"]["vehicle_attack_levels"]
        ["medium_missile_portable"]["range"]
    )


def test_unknown_identity_has_strict_non_inferential_fallback() -> None:
    registry = load_operator_registry()

    descriptor = registry.resolve_operator(999, 999)
    assert descriptor.to_record() == {
        "schema": OPERATOR_DESCRIPTOR_SCHEMA,
        "key": "999_999",
        "identity": {"type_code": 999, "subtype_code": 999},
        "display_name": "未知算子",
        "category": "unknown",
        "symbol": {"base": "unknown", "modifiers": []},
        "weapons": [],
        "status": "UNKNOWN",
    }
    with pytest.raises(TypeError, match="exact int"):
        registry.resolve_operator(True, 0)
    with pytest.raises(TypeError, match="exact int"):
        registry.resolve_operator(2, "0")  # type: ignore[arg-type]


def test_viewer_unit_resolution_does_not_modify_input() -> None:
    registry = load_operator_registry()
    viewer_unit = {
        "operator_id": 17,
        "type_code": 2,
        "sub_type_code": 1,
        "icon_key": "legacy_rectangle",
        "name": "display text is not identity",
        "raw": {"future": [1, 2, 3]},
    }
    before = hashlib.sha256(
        json.dumps(viewer_unit, sort_keys=True).encode("utf-8")
    ).hexdigest()

    descriptor = registry.resolve_viewer_unit(viewer_unit)

    after = hashlib.sha256(
        json.dumps(viewer_unit, sort_keys=True).encode("utf-8")
    ).hexdigest()
    assert before == after
    assert descriptor.key == "2_1"
    assert descriptor.symbol.base == "armored_vehicle"


def test_legacy_replay_viewer_record_resolves_without_icon_or_name_inference() -> None:
    registry = load_operator_registry()
    legacy = {
        "type_code": 3,
        "sub_type_code": 6,
        "icon_key": "wrong-old-icon",
        "name": "不是身份来源",
    }
    alternate = {**legacy, "icon_key": "tank", "name": "重型坦克"}

    first = registry.resolve_viewer_unit(legacy)
    second = registry.resolve_viewer_unit(alternate)

    assert first == second
    assert first.key == "3_6"
    assert first.symbol.base == "attack_helicopter"

    standard_spelling = {"type_code": 2, "subtype_code": 0}
    source_spelling = {"type": 2, "sub_type": 0}
    assert registry.resolve_viewer_unit(standard_spelling).key == "2_0"
    assert registry.resolve_viewer_unit(source_spelling).key == "2_0"


def test_incomplete_enemy_identity_remains_unknown_despite_legacy_hints() -> None:
    registry = load_operator_registry()
    obscured_enemy = {
        "type_code": -1,
        "sub_type_code": -1,
        "icon_key": "tank",
        "name": "重型坦克",
        "side": "blue",
    }

    descriptor = registry.resolve_viewer_unit(obscured_enemy)

    assert descriptor.status == "UNKNOWN"
    assert descriptor.symbol.base == "unknown"
    assert descriptor.weapons == ()


def test_registry_and_descriptors_are_immutable_but_records_are_detached() -> None:
    registry = load_operator_registry()
    descriptor = resolve_operator(1, 16, registry=registry)

    with pytest.raises(FrozenInstanceError):
        descriptor.display_name = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        registry.operators["1_2"] = descriptor  # type: ignore[index]

    record = descriptor.to_record()
    record["symbol"]["modifiers"].append("changed")  # type: ignore[index,union-attr]
    assert descriptor.symbol.modifiers == ()


def test_registry_loader_rejects_identity_key_mismatch(tmp_path: Path) -> None:
    document = json.loads(default_operator_registry_path().read_text("utf-8"))
    document["operators"]["2_0"]["identity"]["subtype_code"] = 99
    path = tmp_path / "bad-registry.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(OperatorRegistryError, match="does not match identity"):
        load_operator_registry(path)


def test_javascript_resolver_is_registry_driven_and_non_inferential() -> None:
    source = JS_RESOLVER.read_text(encoding="utf-8")

    assert 'const REGISTRY_SCHEMA = "operator-registry/v1"' in source
    assert "installRegistry(document)" in source
    assert "resolveUnitSymbol" in source
    assert "viewerUnit.icon_key" not in source
    assert "viewerUnit.name" not in source
    assert "base_symbol: baseSymbol" in source
    assert '? "unknown_vehicle" : "unknown"' in source
