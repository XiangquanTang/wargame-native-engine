from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from wargame_native_engine.calibration.replay_driver import HistoricalReplayDriver
from wargame_native_engine.io.importers.historical_replay import HistoricalReplayImporter
from wargame_native_engine.io.importers.historical_scenario import HistoricalScenarioBuilder
from wargame_native_engine.rules.config import load_ruleset


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples" / "historical_replay_reduced_fixture"
RULESET = ROOT / "config" / "rulesets" / "native_default_v1.json"


def _action_replay():
    return HistoricalReplayImporter().import_path(
        FIXTURES / "calibration-action-01").replay


def test_action_level_replay_can_drive_native_engine() -> None:
    result = HistoricalReplayDriver().run(_action_replay(), load_ruleset(RULESET))
    assert result.mode == "REPLAY_DRIVEN_ADJUDICATION"
    assert len(result.frames) > 1
    assert len(result.action_mappings) == len(_action_replay().actions)


def test_state_level_replay_does_not_invent_actions() -> None:
    replay = HistoricalReplayImporter().import_path(FIXTURES / "smoke-state-01").replay
    with pytest.raises(ValueError, match="ACTION_LEVEL"):
        HistoricalReplayDriver().run(replay, load_ruleset(RULESET))


def test_unsupported_action_is_reported() -> None:
    replay = _action_replay()
    bad = replace(replay.actions[0], action_type="FLY")
    replay = replace(replay, actions=(bad,) + replay.actions[1:])
    result = HistoricalReplayDriver().run(replay, load_ruleset(RULESET))
    assert any(item["code"] == "unsupported_action" for item in result.conflicts)
    assert result.action_mappings[0]["mapping_status"] == "UNSUPPORTED"


def test_replay_driven_result_is_deterministic() -> None:
    replay, rules = _action_replay(), load_ruleset(RULESET)
    first = HistoricalReplayDriver().run(replay, rules).to_dict()
    second = HistoricalReplayDriver().run(replay, rules).to_dict()
    assert first == second


def test_replay_driven_uses_initial_frame_only() -> None:
    replay, rules = _action_replay(), load_ruleset(RULESET)
    build = HistoricalScenarioBuilder().build(replay, rules)
    changed_future = replace(
        replay, frames=(replay.frames[0], replace(
            replay.frames[1], operators=tuple(replace(item, x=99.0)
                                               for item in replay.frames[1].operators)))
        + replay.frames[2:])
    changed = HistoricalScenarioBuilder().build(changed_future, rules)
    assert changed.scenario.scenario_hash == build.scenario.scenario_hash
    assert changed.operator_mapping["future_frames_used"] is False


def test_historical_data_is_not_mutated() -> None:
    replay = _action_replay()
    before = replay.to_dict()
    HistoricalReplayDriver().run(replay, load_ruleset(RULESET))
    assert replay.to_dict() == before


def test_action_state_conflict_is_reported_without_rewriting_history() -> None:
    replay = _action_replay()
    changed = replace(
        replay, frames=(replay.frames[0], replace(
            replay.frames[1], operators=(replace(
                replay.frames[1].operators[0], x=77.0),)
                + replay.frames[1].operators[1:])) + replay.frames[2:])
    result = HistoricalReplayDriver().run(changed, load_ruleset(RULESET))
    conflicts = [item for item in result.conflicts
                 if item["code"] == "action_state_conflict"]
    assert conflicts
    assert conflicts[0]["interpretation"].endswith("NativeEngine error")
