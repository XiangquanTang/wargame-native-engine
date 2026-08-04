from __future__ import annotations

import json

from wargame_native_engine.io.scenario import load_scenario, scenario_from_dict


def test_scenario_loader(scenario_path) -> None:
    scenario = load_scenario(scenario_path)
    assert scenario.scenario_id == "minimal-duel"
    assert scenario.map.map_id == "minimal-open-field"
    assert scenario.side_order == ("red", "blue")
    assert len(scenario.operators) == 2
    assert len(scenario.scenario_hash) == 64


def test_importer_preserves_unknown_operator_fields(scenario_path) -> None:
    value = json.loads(scenario_path.read_text(encoding="utf-8"))
    value["operators"][0]["documented_but_unused"] = {"raw": True}
    scenario = scenario_from_dict(value)
    assert scenario.operators[0].raw_attributes["documented_but_unused"] == {
        "raw": True}
