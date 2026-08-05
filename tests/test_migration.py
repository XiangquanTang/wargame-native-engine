from __future__ import annotations

from pathlib import Path

from wargame_native_engine.agents import FirstValidActionAgent
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.io.importers import LegacyScenarioImporter
from wargame_native_engine.io.replay import replay_actions
from wargame_native_engine.io.scenario import load_scenario, scenario_from_dict
from wargame_native_engine.io.serialization import file_sha256
from wargame_native_engine.session.episode import EpisodeRunner
from wargame_native_engine.session.game_session import GameSession


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "examples/legacy_scenario_reduced_fixture.json"
RULESET = ROOT / "config/rulesets/native_default_v1.json"


def _import(output=None):
    return LegacyScenarioImporter(RULESET).import_file(
        SOURCE,
        source_identifier="legacy-field-structure-small/sanitized-reduced-fixture",
        output_dir=output)


def test_legacy_scenario_import() -> None:
    result = _import()
    assert len(result.scenario.operators) == 6
    assert result.manifest["mapped_operator_count"] == 6


def test_source_file_is_not_modified() -> None:
    before = file_sha256(SOURCE)
    _import()
    assert file_sha256(SOURCE) == before


def test_unknown_fields_are_preserved() -> None:
    result = _import()
    raw = result.scenario.operators[0].raw_attributes
    assert raw["source_values"]["armor"] == 4
    assert raw["provenance"]["move_range"] == "derived_by_native_rules"


def test_unmapped_fields_are_reported() -> None:
    fields = {item["field"] for item in _import().unmapped_fields["operator_fields"]}
    assert {"armor", "remain_bullet_nums"} <= fields


def test_source_hash_is_recorded() -> None:
    assert _import().manifest["source_hash"] == file_sha256(SOURCE)


def test_native_scenario_schema() -> None:
    result = _import()
    assert scenario_from_dict(result.scenario.to_dict()).scenario_hash == (
        result.scenario.scenario_hash)
    assert load_scenario(ROOT / "examples/migrated_scenario_small.json").scenario_hash == (
        result.scenario.scenario_hash)


def test_imported_scenario_can_reset() -> None:
    engine = NativeEngine(_import().scenario, ruleset=RULESET)
    assert len(engine.reset().operators) == 6


def test_imported_scenario_can_run_multiple_ticks(tmp_path) -> None:
    scenario = _import().scenario
    session = GameSession(NativeEngineBackend(scenario, ruleset=RULESET))
    outcome = EpisodeRunner(
        session, agents={side: FirstValidActionAgent()
                         for side in scenario.side_order},
        output_dir=tmp_path / "game").run(42)
    assert outcome.result["ticks"] > 2


def test_imported_scenario_replay_round_trip(tmp_path) -> None:
    scenario = _import().scenario
    output = tmp_path / "game"
    session = GameSession(NativeEngineBackend(scenario, ruleset=RULESET))
    EpisodeRunner(session, agents={side: FirstValidActionAgent()
                                   for side in scenario.side_order},
                  output_dir=output).run(42)
    assert replay_actions(scenario, output, ruleset=RULESET).matches
