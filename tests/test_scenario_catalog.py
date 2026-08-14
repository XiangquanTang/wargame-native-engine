from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from wargame_native_engine.data import (
    DataLoadError,
    ScenarioBundle,
    ScenarioCatalog,
)
from wargame_native_engine.io.serialization import canonical_hash


SCENARIO = Path("examples/source_movement_scenario.json")


def test_bundle_is_deterministic_detached_and_shares_map_identity() -> None:
    before = SCENARIO.read_bytes()
    first = ScenarioBundle.from_path(SCENARIO)
    second = ScenarioBundle.from_path(SCENARIO)

    assert first.canonical_payload == second.canonical_payload
    assert first.scenario_identity == second.scenario_identity
    assert first.map_identity == second.map_identity
    assert first.provenance.source_sha256 == hashlib.sha256(before).hexdigest()
    assert first.map_identity.sha256 == canonical_hash(first.scenario.map.to_dict())
    render = first.render_map_record()
    assert render["map_identity"]["sha256"] == first.map_identity.sha256
    assert render["scenario_identity"]["sha256"] == first.scenario_identity.sha256

    detached = first.scenario
    detached.operators[0].raw_attributes["caller_mutation"] = True
    assert "caller_mutation" not in first.scenario.operators[0].raw_attributes
    assert SCENARIO.read_bytes() == before


def test_catalog_uses_document_identity_not_filename(tmp_path: Path) -> None:
    value = json.loads(SCENARIO.read_text(encoding="utf-8"))
    (tmp_path / "not-the-id.json").write_text(
        json.dumps(value), encoding="utf-8"
    )
    (tmp_path / "readme.json").write_text(
        json.dumps({"description": "not a Native scenario"}), encoding="utf-8"
    )
    catalog = ScenarioCatalog.open(tmp_path)

    assert catalog.scenario_ids == (str(value["scenario_id"]),)
    assert catalog.load_scenario(str(value["scenario_id"])).scenario.map.hexes


def test_catalog_rejects_duplicate_identity_and_missing_rules(tmp_path: Path) -> None:
    value = json.loads(SCENARIO.read_text(encoding="utf-8"))
    for name in ("a.json", "b.json"):
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(DataLoadError, match="duplicate_scenario_id"):
        ScenarioCatalog.open(tmp_path)

    value.pop("terminal_rules")
    with pytest.raises(DataLoadError, match="invalid_native_scenario"):
        ScenarioBundle.from_record(
            value,
            provenance=ScenarioBundle.from_path(SCENARIO).provenance,
        )
