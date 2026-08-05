from __future__ import annotations

from collections import OrderedDict
import json
from pathlib import Path

from wargame_native_engine.agents import FirstValidActionAgent
from wargame_native_engine.cli.evaluate_fairness import evaluate, main
from wargame_native_engine.core.action import ActionType
from wargame_native_engine.core.action import EngineAction, ValidAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.io.scenario import load_scenario, scenario_from_dict
from wargame_native_engine.io.symmetry import ScenarioSymmetryTransformer
from wargame_native_engine.rules.config import load_ruleset

from conftest import action_of, advance_to_combat


ROOT = Path(__file__).resolve().parents[1]
RULESET_PATH = ROOT / "config/rulesets/native_default_v1.json"
MIGRATED = ROOT / "examples/migrated_scenario_small.json"


def _simultaneous_scenario(scenario):
    value = scenario.to_dict()
    value["scenario_id"] = "simultaneous-duel"
    value["operators"][0].update({"x": 3.0, "health": 10.0,
                                   "max_health": 10.0, "attack_damage": 10.0})
    value["operators"][1].update({"x": 4.0, "health": 10.0,
                                   "max_health": 10.0, "attack_damage": 10.0})
    value["deployment"]["positions"]["red"]["101"] = [3.0, 3.0]
    value["deployment"]["positions"]["blue"]["201"] = [4.0, 3.0]
    return scenario_from_dict(value)


def _selected(agent, engine, side):
    return agent.act(observation=engine.observe(side),
                     valid_actions=engine.valid_actions(side),
                     context=engine_context(engine, side)).action


def engine_context(engine, side):
    from wargame_native_engine.agents.protocol import AgentStepContext
    observation = engine.observe(side)
    return AgentStepContext("test", side, observation.tick,
                            observation.stage, 42)


def test_side_swap_transform(scenario) -> None:
    transformed = ScenarioSymmetryTransformer().swap_sides(scenario)
    original = {item.operator_id: item.side for item in scenario.operators}
    swapped = {item.operator_id: item.side for item in transformed.operators}
    assert swapped == {key: ("blue" if side == "red" else "red")
                       for key, side in original.items()}
    assert transformed.scenario_hash != scenario.scenario_hash


def test_position_mirror_transform(scenario) -> None:
    transformed = ScenarioSymmetryTransformer().mirror_positions(
        scenario, axis="vertical")
    by_id = {item.operator_id: item for item in transformed.operators}
    for original in scenario.operators:
        assert by_id[original.operator_id].x == scenario.map.width - original.x
        assert by_id[original.operator_id].y == original.y


def test_action_batch_order_independence(scenario) -> None:
    engines = [NativeEngine(scenario, ruleset=RULESET_PATH) for _ in range(2)]
    for engine in engines:
        engine.reset(42)
        advance_to_combat(engine)
    actions = {side: engines[0].valid_actions(side)[0].action
               for side in scenario.side_order}
    first = engines[0].step(OrderedDict((side, actions[side])
                                       for side in ("red", "blue")))
    second = engines[1].step(OrderedDict((side, actions[side])
                                        for side in ("blue", "red")))
    assert engines[0].result().world_hash == engines[1].result().world_hash
    assert [item.to_dict() for item in first.events] == [
        item.to_dict() for item in second.events]


def test_simultaneous_damage_is_symmetric(scenario) -> None:
    engine = NativeEngine(_simultaneous_scenario(scenario), ruleset=RULESET_PATH)
    engine.reset()
    advance_to_combat(engine)
    result = engine.step({side: action_of(engine, side, ActionType.ATTACK).action
                          for side in engine.scenario.side_order})
    damaged = {item.target_id: item.data["after"] for item in result.events
               if item.event_type.value == "operator_damaged"}
    assert damaged == {101: 0.0, 201: 0.0}


def test_simultaneous_elimination_rule(scenario) -> None:
    engine = NativeEngine(_simultaneous_scenario(scenario), ruleset=RULESET_PATH)
    engine.reset()
    advance_to_combat(engine)
    engine.step({side: action_of(engine, side, ActionType.ATTACK).action
                 for side in engine.scenario.side_order})
    result = engine.result()
    assert result.terminal and result.winner is None
    assert result.reason == "mutual_elimination"


def test_first_valid_is_side_symmetric(scenario) -> None:
    engine = NativeEngine(_simultaneous_scenario(scenario), ruleset=RULESET_PATH)
    engine.reset()
    advance_to_combat(engine)
    agent = FirstValidActionAgent()
    red = _selected(agent, engine, "red")
    blue = _selected(agent, engine, "blue")
    assert red.action_type is blue.action_type is ActionType.ATTACK
    assert (red.actor_id, red.target_id) == (101, 201)
    assert (blue.actor_id, blue.target_id) == (201, 101)


def test_first_valid_semantics_survive_monotonic_id_renumber(scenario) -> None:
    agent = FirstValidActionAgent()
    engine = NativeEngine(scenario, ruleset=RULESET_PATH)
    engine.reset()
    observation = engine.observe("red")
    original = (
        ValidAction(EngineAction("red", 1, ActionType.MOVE,
                                 target_position=(2.0, 1.0)), 1),
        ValidAction(EngineAction("red", 2, ActionType.MOVE,
                                 target_position=(3.0, 1.0)), 1),
    )
    renumbered = tuple(ValidAction(EngineAction(
        item.action.side, item.action.actor_id + 1000,
        item.action.action_type, target_position=item.action.target_position),
        item.priority) for item in original)
    original_observation = observation.__class__(
        side=observation.side, tick=observation.tick, stage=observation.stage,
        friendly_operators=observation.friendly_operators,
        visible_enemies=observation.visible_enemies,
        objectives=observation.objectives,
        legal_action_ids=tuple(item.action_id for item in original),
        mission=observation.mission)
    renumbered_observation = original_observation.__class__(
        side=observation.side, tick=observation.tick, stage=observation.stage,
        friendly_operators=observation.friendly_operators,
        visible_enemies=observation.visible_enemies,
        objectives=observation.objectives,
        legal_action_ids=tuple(item.action_id for item in renumbered),
        mission=observation.mission)
    left = agent.act(observation=original_observation, valid_actions=original,
                     context=engine_context(engine, "red")).action
    right = agent.act(observation=renumbered_observation,
                      valid_actions=tuple(reversed(renumbered)),
                      context=engine_context(engine, "red")).action
    assert left.action_type == right.action_type
    assert left.target_position == right.target_position
    assert right.actor_id - left.actor_id == 1000


def test_dictionary_order_does_not_change_world_hash(scenario) -> None:
    engines = [NativeEngine(scenario, ruleset=RULESET_PATH) for _ in range(2)]
    for engine in engines:
        engine.reset(42)
    world = engines[1]._require_world()
    world.operators = dict(reversed(list(world.operators.items())))
    world.sides = dict(reversed(list(world.sides.items())))
    for engine in engines:
        engine.step({side: engine.valid_actions(side)[0].action
                     for side in scenario.side_order})
    assert engines[0].result().world_hash == engines[1].result().world_hash


def test_snapshot_resume_matches_uninterrupted_run(scenario) -> None:
    original = NativeEngine(scenario, ruleset=RULESET_PATH)
    original.reset(123)
    advance_to_combat(original)
    snapshot = original.snapshot()
    resumed = NativeEngine(scenario, ruleset=RULESET_PATH)
    resumed.restore(snapshot)
    original_hashes, resumed_hashes = [], []
    while not original.is_terminal():
        original.step({side: original.valid_actions(side)[0].action
                       for side in scenario.side_order})
        original_hashes.append(original.result().world_hash)
    while not resumed.is_terminal():
        resumed.step({side: resumed.valid_actions(side)[0].action
                      for side in scenario.side_order})
        resumed_hashes.append(resumed.result().world_hash)
    assert original_hashes == resumed_hashes


def test_seed_reproducibility_across_batch_runs(tmp_path) -> None:
    rules = load_ruleset(RULESET_PATH)
    scenario = load_scenario(MIGRATED)
    summaries = [evaluate(
        scenario=scenario, ruleset=rules,
        red_agent="random-legal", blue_agent="random-legal", seeds=(3, 4),
        swap_sides=True, mirror=True, output=tmp_path / f"batch-{index}")
        for index in range(2)]
    assert summaries[0] == summaries[1]


def test_replay_hash_matches_after_strict_replay(tmp_path) -> None:
    rules = load_ruleset(RULESET_PATH)
    summary = evaluate(
        scenario=load_scenario(MIGRATED), ruleset=rules,
        red_agent="random-legal", blue_agent="random-legal", seeds=(5,),
        swap_sides=False, mirror=False, output=tmp_path / "batch")
    assert summary["replay_mismatch_count"] == 0
    assert summary["state_hash_mismatch_count"] == 0


def test_fairness_cli_outputs_summary(tmp_path) -> None:
    output = tmp_path / "cli"
    code = main([
        "--scenario", str(MIGRATED), "--ruleset", str(RULESET_PATH),
        "--red-agent", "random-legal", "--blue-agent", "random-legal",
        "--seeds", "7:7", "--swap-sides", "--mirror",
        "--output", str(output),
    ])
    assert code == 0
    assert {"manifest.json", "games.jsonl", "summary.json", "failures.jsonl"} <= {
        item.name for item in output.iterdir()}
    records = [json.loads(line) for line in (output / "games.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert all(item["agent_seeds"]["red"] != item["agent_seeds"]["blue"]
               for item in records)
