from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

import pytest

from wargame_native_engine.data import (
    DataProvenance,
    EXPLICIT_NATIVE_SUPPLEMENT,
    STRICT_SOURCE,
    ScenarioBundle,
    SourceScenarioConversionError,
    convert_source_scenario,
    publish_source_scenario_conversion,
)


EXTERNAL_ROOT = Path(os.environ.get(
    "WARGAME_SOURCE_SNAPSHOT_ROOT",
    "local_data/source_snapshot",
))
REAL_SUPPLEMENT = Path(
    "config/source_scenario_conversion/scenarios/"
    "2201010105.native-supplement.json"
)


def _cell(row: int, column: int, neighbors: list[int]) -> dict[str, object]:
    return {
        "r": row,
        "c": column,
        "pos": row * 100 + column,
        "elev": 100,
        "cond": 0,
        "neighbors": neighbors,
        "roads": [0] * 6,
        "rivers": [0] * 6,
    }


def _source_documents() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    source_map = {
        "map_id": 7,
        "coordinate_system": "oddr",
        "scale": 200,
        "ele_grade": 10,
        "max_row": 2,
        "max_column": 2,
        "map_data": [
            [
                _cell(0, 0, [1, -1, -1, -1, -1, 100]),
                _cell(0, 1, [-1, -1, -1, 0, 100, 101]),
            ],
            [
                _cell(1, 0, [101, 1, 0, -1, -1, -1]),
                _cell(1, 1, [-1, -1, 1, 100, -1, -1]),
            ],
        ],
    }
    source = {
        "scenario_id": "9001",
        "operators": [
            {
                "obj_id": 1, "color": 0, "type": 2, "sub_type": 0,
                "name": "Red vehicle", "cur_hex": 0, "cur_pos": 0,
                "blood": 3, "max_blood": 3, "basic_speed": 36,
                "armor": 4, "carry_weapon_ids": [36],
                "remain_bullet_nums": {"0": 3}, "weapon_unfold_state": 1,
            },
            {
                "obj_id": 2, "color": 1, "type": 1, "sub_type": 2,
                "name": "Blue infantry", "cur_hex": 101, "cur_pos": 0,
                "blood": 3, "max_blood": 3, "basic_speed": 5,
                "armor": 0, "carry_weapon_ids": [29],
                "remain_bullet_nums": {"0": 3}, "weapon_unfold_state": 1,
            },
        ],
        "time": {"cur_step": 0, "tick": 1, "max_time": 20},
        "cities": [{"coord": 1, "value": 1, "flag": -1, "name": "Point"}],
        "landmarks": {"roadblocks": [], "minefields": []},
    }
    supplement = {
        "schema": "source-scenario-native-supplement/1.0",
        "supplement_id": "test-native-supplement",
        "supplement_version": "1.0.0",
        "scenario_id": "9001",
        "map_id": "7",
        "native_rules_equivalence": False,
        "map_profile": {"coordinate_system": "odd-r", "hex_size_m": 200, "elevation_unit_m": 10},
        "platform_profiles": {
            "2:0": {"source_names": ["Red vehicle"], "native_domain": "VEHICLE", "canonical_type": "HEAVY_TANK", "sensor_range": 25, "allows_unarmed": False, "native_basis": "test"},
            "1:2": {"source_names": ["Blue infantry"], "native_domain": "PERSONNEL", "canonical_type": "INFANTRY", "sensor_range": 10, "allows_unarmed": False, "native_basis": "test"},
        },
        "armor_profiles": {"0": "NONE", "4": "COMPOSITE"},
        "weapon_profiles": {
            "36": {"weapon_class": "large_direct_fire_gun", "maximum_range": 10, "target_domains": ["PERSONNEL", "VEHICLE"], "ammunition_key": "0", "cooldown_seconds": None, "requires_unfolded": True, "can_fire_while_moving": False, "native_basis": "test"},
            "29": {"weapon_class": "infantry_light_weapon", "maximum_range": 3, "target_domains": ["PERSONNEL", "VEHICLE"], "ammunition_key": "0", "cooldown_seconds": None, "requires_unfolded": False, "can_fire_while_moving": False, "native_basis": "test"},
        },
        "initial_state_defaults": {"altitude": 0, "heading": 0, "speed": 0, "state": "STOPPED", "stopped": True, "weapon_locked": False, "weapon_unfolded": True},
        "objective_policy": {"mode": "SOURCE_CITIES_AS_NATIVE_OBJECTIVES", "radius_hex": 0},
        "termination": {"max_ticks": 20, "elimination": True, "objective_score_to_win": None, "native_basis": "test"},
        "seed_policy": {"default_seed": 42, "native_basis": "test"},
    }
    return source, source_map, supplement


def _write_documents(tmp_path: Path, *, source=None, supplement=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    default_source, source_map, default_supplement = _source_documents()
    scenario_path = tmp_path / "source.json"
    map_path = tmp_path / "basic.json"
    supplement_path = tmp_path / "supplement.json"
    scenario_path.write_text(json.dumps(source or default_source, ensure_ascii=False), encoding="utf-8")
    map_path.write_text(json.dumps(source_map, ensure_ascii=False), encoding="utf-8")
    supplement_path.write_text(json.dumps(supplement or default_supplement, ensure_ascii=False), encoding="utf-8")
    return scenario_path, map_path, supplement_path


def _convert(tmp_path: Path, *, source=None, supplement=None):
    scenario_path, map_path, supplement_path = _write_documents(
        tmp_path, source=source, supplement=supplement
    )
    result = convert_source_scenario(
        scenario_path,
        map_path,
        conversion_mode=EXPLICIT_NATIVE_SUPPLEMENT,
        source_scenario_identity="fixtures/source.json",
        source_map_identity="fixtures/basic.json",
        expected_scenario_id="9001",
        expected_map_id="7",
        supplement_path=supplement_path,
    )
    return result, scenario_path, map_path, supplement_path


def test_strict_source_rejects_missing_native_contract(tmp_path: Path) -> None:
    scenario_path, map_path, _ = _write_documents(tmp_path)
    with pytest.raises(SourceScenarioConversionError) as caught:
        convert_source_scenario(
            scenario_path, map_path,
            conversion_mode=STRICT_SOURCE,
            source_scenario_identity="fixtures/source.json",
            source_map_identity="fixtures/basic.json",
            expected_scenario_id="9001", expected_map_id="7",
        )
    assert caught.value.code == "strict_source_missing_fields"
    assert "native_conversion_profile.weapon_profiles" in caught.value.details["missing_fields"]


def test_supplement_conversion_is_deterministic_and_does_not_modify_inputs(tmp_path: Path) -> None:
    first, scenario_path, map_path, supplement_path = _convert(tmp_path)
    before = [path.read_bytes() for path in (scenario_path, map_path, supplement_path)]
    second = convert_source_scenario(
        scenario_path, map_path,
        conversion_mode=EXPLICIT_NATIVE_SUPPLEMENT,
        source_scenario_identity="fixtures/source.json",
        source_map_identity="fixtures/basic.json",
        expected_scenario_id="9001", expected_map_id="7",
        supplement_path=supplement_path,
    )
    assert first.scenario_payload == second.scenario_payload
    assert first.manifest_payload == second.manifest_payload
    assert first.scenario_payload.endswith(b"\n")
    assert b"/home/" not in first.scenario_payload
    assert b"/home/" not in first.manifest_payload
    assert first.scenario.initial_stage.value == "combat"
    assert [item.current_hex for item in first.scenario.operators] == [0, 101]
    assert first.scenario.metadata["source_scenario_conversion"]["native_rules_equivalence"] is False
    assert [path.read_bytes() for path in (scenario_path, map_path, supplement_path)] == before


def test_supplement_change_changes_output_hash(tmp_path: Path) -> None:
    first, _, _, _ = _convert(tmp_path / "first")
    _, _, supplement = _source_documents()
    changed = deepcopy(supplement)
    changed["seed_policy"]["default_seed"] = 43
    second, _, _, _ = _convert(tmp_path / "second", supplement=changed)
    assert first.scenario_payload_sha256 != second.scenario_payload_sha256
    assert first.manifest["profile"]["content_sha256"] != second.manifest["profile"]["content_sha256"]


@pytest.mark.parametrize("mutation", ["duplicate_id", "unknown_hex", "unknown_platform", "unknown_weapon"])
def test_invalid_source_facts_are_rejected(tmp_path: Path, mutation: str) -> None:
    source, _, supplement = _source_documents()
    source = deepcopy(source)
    supplement = deepcopy(supplement)
    if mutation == "duplicate_id":
        source["operators"][1]["obj_id"] = 1
    elif mutation == "unknown_hex":
        source["operators"][1]["cur_hex"] = 9999
    elif mutation == "unknown_platform":
        source["operators"][1]["sub_type"] = 99
    else:
        source["operators"][1]["carry_weapon_ids"] = [999]
        supplement["weapon_profiles"]["999"] = {
            **supplement["weapon_profiles"]["29"],
            "weapon_class": "unmapped",
        }
    with pytest.raises(SourceScenarioConversionError):
        _convert(tmp_path, source=source, supplement=supplement)


def test_published_conversion_is_catalog_ready_and_protected(tmp_path: Path) -> None:
    result, _, _, _ = _convert(tmp_path / "inputs")
    published = publish_source_scenario_conversion(result, tmp_path / "output")
    bundle = ScenarioBundle.from_path(published.scenario_path)
    bundle.validate()
    assert bundle.map_identity.sha256 == result.manifest["native_map"]["sha256"]
    assert bundle.render_map_record()["map_identity"]["sha256"] == bundle.map_identity.sha256
    with pytest.raises(FileExistsError):
        publish_source_scenario_conversion(result, tmp_path / "output")


@pytest.mark.skipif(not EXTERNAL_ROOT.is_dir(), reason="read-only Source snapshot unavailable")
def test_real_map_123_scenario_converts_with_complete_identity_and_source_fields() -> None:
    scenario_path = EXTERNAL_ROOT / "scenarios/2201010105.json"
    map_path = EXTERNAL_ROOT / "maps/123/basic.json"
    before_scenario = hashlib.sha256(scenario_path.read_bytes()).hexdigest()
    before_map = hashlib.sha256(map_path.read_bytes()).hexdigest()
    result = convert_source_scenario(
        scenario_path, map_path,
        conversion_mode=EXPLICIT_NATIVE_SUPPLEMENT,
        source_scenario_identity="scenarios/2201010105.json",
        source_map_identity="maps/123/basic.json",
        expected_scenario_id="2201010105", expected_map_id="123",
        supplement_path=REAL_SUPPLEMENT,
    )
    scenario = result.scenario
    assert scenario.scenario_id == "source-2201010105-native-v1"
    assert len(scenario.operators) == 49
    assert len(scenario.map.hexes) == 10_000
    assert len(scenario.objectives) == 7
    assert sum(item.side == "red" for item in scenario.operators) == 26
    assert sum(item.side == "blue" for item in scenario.operators) == 23
    assert scenario.operators[0].raw_attributes["source_frame"]["name"] == "重型坦克"
    assert scenario.operators[0].raw_attributes["armor_class"] == "COMPOSITE"
    assert result.manifest["coverage"]["unit_mapping"] == {"mapped": 49, "total": 49}
    assert result.manifest["source_scenario"]["sha256"] == before_scenario
    assert result.manifest["source_map"]["sha256"] == before_map
    bundle = ScenarioBundle.from_record(
        scenario.to_dict(),
        provenance=DataProvenance(
            source_sha256=before_scenario,
            source_kind="source_scenario_conversion",
            converter_id="source-scenario-converter",
            converter_version="1.0.0",
        ),
    )
    assert bundle.map_identity.sha256 == result.manifest["native_map"]["sha256"]
    assert hashlib.sha256(scenario_path.read_bytes()).hexdigest() == before_scenario
    assert hashlib.sha256(map_path.read_bytes()).hexdigest() == before_map
