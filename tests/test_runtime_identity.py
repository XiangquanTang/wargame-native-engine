from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.io.scenario import NativeMap, load_scenario
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.session.game_session import GameSession


SCENARIO_PATH = Path("examples/source_movement_scenario.json")
IDENTITY_SCENARIO_PATH = Path("examples/minimal_scenario.json")


def test_scenario_hash_reuses_only_immutable_map_fragment(monkeypatch) -> None:
    scenario = load_scenario(IDENTITY_SCENARIO_PATH)
    original_map_to_dict = NativeMap.to_dict
    calls = 0

    def counted_map_to_dict(self):
        nonlocal calls
        calls += 1
        return original_map_to_dict(self)

    monkeypatch.setattr(NativeMap, "to_dict", counted_map_to_dict)

    expected = canonical_hash(scenario.to_dict())
    assert calls == 1
    calls = 0
    assert scenario.scenario_hash == expected
    assert scenario.scenario_hash == expected
    assert calls == 0

    scenario.metadata["identity_probe"] = {"value": 1}
    scenario.operators[0].raw_attributes["identity_probe"] = [2]
    scenario.objectives[0].value += 1
    red_operator_id = next(
        item.operator_id for item in scenario.operators if item.side == "red"
    )
    scenario.deployment_positions["red"][red_operator_id] = (1.0, 1.0)
    expected_after_mutation = canonical_hash(scenario.to_dict())
    assert calls == 1
    calls = 0
    assert scenario.scenario_hash == expected_after_mutation
    assert scenario.scenario_hash != expected
    assert calls == 0


def test_scenario_identity_does_not_cross_instances_or_replace() -> None:
    first = load_scenario(IDENTITY_SCENARIO_PATH)
    changed_record = deepcopy(first.to_dict())
    changed_record["metadata"]["same_id_different_content"] = True
    second = type(first)(
        scenario_id=first.scenario_id,
        map=first.map,
        side_order=first.side_order,
        operators=first.operators,
        objectives=first.objectives,
        deployment_positions=first.deployment_positions,
        terminal_rules=first.terminal_rules,
        metadata=changed_record["metadata"],
        initial_stage=first.initial_stage,
    )
    replaced = replace(first, metadata={"replacement": True})

    assert first.scenario_id == second.scenario_id == replaced.scenario_id
    assert first.scenario_hash == canonical_hash(first.to_dict())
    assert second.scenario_hash == canonical_hash(second.to_dict())
    assert replaced.scenario_hash == canonical_hash(replaced.to_dict())
    assert len({first.scenario_hash, second.scenario_hash, replaced.scenario_hash}) == 3


def test_sim_time_scalar_tracks_reset_step_restore_without_snapshot() -> None:
    session = GameSession(NativeEngineBackend(load_scenario(SCENARIO_PATH)))
    with pytest.raises(RuntimeError, match="session_not_reset"):
        session.sim_time_ms()

    session.reset(42)
    initial = deepcopy(session.snapshot())
    rng_before = deepcopy(initial["rng"])
    events_before = session.backend.engine._last_events  # noqa: SLF001
    snapshot_spy = Mock(wraps=session.backend.snapshot)
    session.backend.snapshot = snapshot_spy  # type: ignore[method-assign]

    assert [session.sim_time_ms() for _ in range(3)] == [0, 0, 0]
    assert snapshot_spy.call_count == 0
    assert session.backend.engine._rng.snapshot() == rng_before  # noqa: SLF001
    assert session.backend.engine._last_events == events_before  # noqa: SLF001

    session.step_ms((), delta_time_ms=1_250)
    assert session.sim_time_ms() == 1_250
    stepped = session.snapshot()
    assert session.sim_time_ms() == stepped["world"]["elapsed_time_ms"]

    session.restore(initial)
    assert session.sim_time_ms() == initial["world"]["elapsed_time_ms"] == 0
    session.reset(7)
    assert session.sim_time_ms() == 0
    session.close()
    with pytest.raises(RuntimeError, match="session_closed"):
        session.sim_time_ms()
