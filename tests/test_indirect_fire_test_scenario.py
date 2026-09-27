from __future__ import annotations

from copy import deepcopy
import hashlib

import pytest

from scripts.create_indirect_fire_test_scenario import (
    derive_indirect_fire_test_record,
)
from test_indirect_fire_action_replay import _scenario


def _two_shooter_record() -> dict[str, object]:
    scenario = _scenario().to_dict()
    red = next(value for value in scenario["operators"] if value["side"] == "red")
    blue = next(value for value in scenario["operators"] if value["side"] == "blue")
    blue["weapon_ids"] = [72]
    blue["raw_attributes"]["ammunition"] = {"0": 0}
    red["raw_attributes"]["ammunition"]["0"] = 0
    return scenario


def test_derivative_uses_authority_mapping_and_existing_pools_only() -> None:
    source = _two_shooter_record()
    before = deepcopy(source)

    record, changes = derive_indirect_fire_test_record(
        source,
        scenario_id="two-sided-indirect-ammo-test",
        ammunition=2,
        expected_shooters=2,
        source_file_sha256=hashlib.sha256(b"fixture").hexdigest(),
    )

    assert source == before
    assert [(value["side"], value["shooter_id"], value["weapon_id"])
            for value in changes] == [("red", 1, 72), ("blue", 2, 72)]
    assert all(value["after"] == 2 for value in changes)
    assert [
        value["raw_attributes"]["ammunition"]["0"]
        for value in record["operators"]
    ] == [2, 2]
    provenance = record["metadata"]["native_test_derivative"]
    assert provenance["shooter_count"] == 2
    assert provenance["side_counts"] == {"blue": 1, "red": 1}
    assert provenance["rules_unchanged"] is True


def test_derivative_fails_closed_on_count_or_missing_pool() -> None:
    source = _two_shooter_record()
    with pytest.raises(ValueError, match="shooter count mismatch"):
        derive_indirect_fire_test_record(
            source,
            scenario_id="wrong-count",
            expected_shooters=12,
            source_file_sha256=hashlib.sha256(b"fixture").hexdigest(),
        )

    source["operators"][0]["raw_attributes"]["ammunition"] = {}
    with pytest.raises(ValueError, match="lacks ammunition key"):
        derive_indirect_fire_test_record(
            source,
            scenario_id="missing-pool",
            expected_shooters=2,
            source_file_sha256=hashlib.sha256(b"fixture").hexdigest(),
        )
