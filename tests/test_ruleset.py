from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from pathlib import Path

import pytest

from wargame_native_engine.agents import FirstValidActionAgent
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.io.replay import ReplayReader, replay_actions
from wargame_native_engine.rules.config import load_config_document, load_ruleset
from wargame_native_engine.session.episode import EpisodeRunner
from wargame_native_engine.session.game_session import GameSession


ROOT = Path(__file__).resolve().parents[1]
RULESET = ROOT / "config/rulesets/native_default_v1.json"


def test_ruleset_schema_validation() -> None:
    rules = load_ruleset(RULESET)
    assert rules.ruleset_id == "native-default"
    assert rules.ruleset_version == "1.0.0"
    assert rules.ruleset_type == "native_simplified"
    assert load_config_document(
        ROOT / "config/operators/operator_types.json",
        "operator_types")["config_version"] == "1.0.0"


def test_ruleset_hash_is_stable() -> None:
    assert load_ruleset(RULESET).content_hash == load_ruleset(RULESET).content_hash
    assert len(load_ruleset(RULESET).content_hash) == 64


def test_ruleset_is_immutable_during_episode(scenario) -> None:
    engine = NativeEngine(scenario, ruleset=RULESET)
    engine.reset()
    with pytest.raises(FrozenInstanceError):
        engine.ruleset.movement.default_move_range = 99.0


def test_replay_records_ruleset_version(scenario, tmp_path) -> None:
    output = tmp_path / "replay"
    rules = load_ruleset(RULESET)
    session = GameSession(NativeEngineBackend(scenario, ruleset=rules))
    EpisodeRunner(session, agents={side: FirstValidActionAgent()
                                   for side in scenario.side_order},
                  output_dir=output).run(42)
    manifest = ReplayReader().read(output)["manifest"]
    assert (manifest["ruleset_id"], manifest["ruleset_version"],
            manifest["ruleset_hash"]) == (
                rules.ruleset_id, rules.ruleset_version, rules.content_hash)


def test_replay_rejects_wrong_ruleset_hash(scenario, tmp_path) -> None:
    output = tmp_path / "replay"
    session = GameSession(NativeEngineBackend(scenario, ruleset=RULESET))
    EpisodeRunner(session, agents={side: FirstValidActionAgent()
                                   for side in scenario.side_order},
                  output_dir=output).run(42)
    config = json.loads(RULESET.read_text(encoding="utf-8"))
    config["combat"]["default_damage"] = 11.0
    for key, directory, filename in (
        ("operator_types", "operators", "operator_types.json"),
        ("weapon_types", "weapons", "weapon_types.json"),
        ("terrain_types", "terrain", "terrain_types.json")):
        config[key] = str((ROOT / "config" / directory / filename).resolve())
    wrong = tmp_path / "wrong-ruleset.json"
    wrong.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="ruleset_hash"):
        replay_actions(scenario, output, ruleset=wrong)


def test_magic_numbers_removed_from_core_systems() -> None:
    systems = ROOT / "src/wargame_native_engine/systems"
    source = "\n".join(path.read_text(encoding="utf-8")
                       for path in systems.glob("*.py"))
    assert "1e-9" not in source
    assert "max_ticks = 500" not in source
    assert "damage = 20" not in source
    assert "distance <= 100" not in source


def test_invalid_ruleset_fails_before_reset(scenario, tmp_path) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"schema_version":"native-ruleset/1.0"}',
                       encoding="utf-8")
    with pytest.raises(ValueError, match="missing required"):
        NativeEngine(scenario, ruleset=invalid)
