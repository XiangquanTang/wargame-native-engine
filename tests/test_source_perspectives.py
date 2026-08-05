from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from wargame_native_engine.core.observation import OperatorObservation, SideObservation
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.world import Stage
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.observations import (
    Perspective, PerspectiveContext, PerspectiveProjector,
    load_perspective_policy,
)
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment
from scripts.run_minimal_source_replay import _template


def _observation(operator_id: int, side: str, *, x: float,
                 raw: dict | None = None) -> OperatorObservation:
    return OperatorObservation(
        operator_id, side, "VEHICLE", "observed", (x, 1.0), 0.0, 0.0,
        67.0, 100.0, "STOPPED", True, True, raw or {})


def _side(side: str, friendly: tuple[OperatorObservation, ...],
          visible: tuple[OperatorObservation, ...]) -> SideObservation:
    return SideObservation(side, 3, Stage.COMBAT, friendly, visible, (), (), {})


def _unit(obj_id: int, color: int, *, passengers: list[int] | None = None,
          hidden_link: int | None = None) -> dict:
    return {
        "obj_id": obj_id, "color": color, "type": 2, "sub_type": 7,
        "name": f"secret-name-{obj_id}", "owner": "red" if color == 0 else "blue",
        "armor": 3, "cur_hex": 100 + obj_id, "cur_pos": 0,
        "basic_speed": 36, "speed": 0, "move_state": 0, "stop": 1,
        "move_path": [999], "move_to_stop_remain_time": 0,
        "blood": 3, "max_blood": 3, "carry_weapon_ids": [36],
        "remain_bullet_nums": {"0": 99}, "weapon_cool_time": 17,
        "passenger_ids": list(passengers or []), "car": None,
        "launcher": hidden_link, "launch_ids": ([hidden_link]
                                                   if hidden_link else []),
        "see_enemy_bop_ids": [1, 2, 20, 21],
        "nested": {"partner_id": hidden_link, "target_obj_id": hidden_link},
        "extra": {"native": {"rng": "secret", "world_hash": "secret"}},
    }


@pytest.fixture
def projection_fixture():
    green = {
        "scenario_id": "perspective-test", "terrain_id": 221,
        "time": {"cur_step": 3, "tick": 1, "max_time": 100,
                 "max_step": 100, "stage": 2},
        "operators": [
            _unit(1, 0, passengers=[3], hidden_link=20),
            _unit(2, 1, passengers=[21], hidden_link=21),
            _unit(20, 1),
        ],
        "passengers": [_unit(3, 0), _unit(21, 1)],
        "cities": [{"id": 1, "name": "public-city"}],
        "scores": {"red": 2, "blue": 1},
        "landmarks": {"public": {"public": True},
                      "red-secret": {"owner": "red"},
                      "blue-secret": {"owner": "blue"}},
        "jm_points": [{"id": 1, "owner": "red"},
                      {"id": 2, "color": 1}, {"id": 3}],
        "role_and_grouping_info": {"red": {"seat": "r"},
                                   "blue": {"seat": "b"}},
        "communication": [{"side": "red", "message": "r"},
                          {"side": "blue", "message": "b"},
                          {"message": "unknown"}],
        "valid_actions": {
            1: [{"action": {"side": "red", "actor_id": 1,
                             "action_type": "WAIT", "target_id": None}},
                {"action": {"side": "red", "actor_id": 1,
                             "action_type": "SHOOT", "target_id": 20}}],
            "2": [{"action": {"side": "blue", "actor_id": 2,
                                "action_type": "WAIT", "target_id": None}}],
        },
        "actions": [
            {"side": "red", "actor_id": 1, "action_type": "WAIT"},
            {"side": "blue", "actor_id": 2, "action_type": "WAIT"},
        ],
        "judge_info": [
            {"actor_id": 1, "target_id": 2,
             "native_evidence": {"rng": 4}, "table_cell": "secret"},
            {"actor_id": 1, "target_id": 20, "att_obj_id": 1,
             "wp_id": 36, "native_evidence": {"rng": 5}},
        ],
        "extra": {"native": {"engine_private": "green-only"}},
        "source_unknown_top": {"preserved": True},
    }
    green["operators"][0]["blood"] = 2
    green["judge_info"].append({
        "actor_id": 20, "target_id": 1, "att_obj_id": 20,
        "wp_id": 99, "native_evidence": {"rng": 6},
    })
    red_friendly = (_observation(1, "red", x=1),
                    _observation(3, "red", x=3))
    blue_friendly = (_observation(2, "blue", x=2),
                     _observation(20, "blue", x=20),
                     _observation(21, "blue", x=21))
    # The raw payload deliberately contains secrets; projection must ignore it.
    red_visible = (_observation(2, "blue", x=2,
                                raw={"ammunition": 999, "passenger_ids": [21]}),)
    blue_visible = (_observation(1, "red", x=1,
                                 raw={"ammunition": 999, "launcher": 3}),)
    context = PerspectiveContext(
        red=_side("red", red_friendly, red_visible),
        blue=_side("blue", blue_friendly, blue_visible),
        session_id="session-test",
        public_metadata={"engine_version": "0.4.0", "ruleset_id": "native-default",
                         "ruleset_version": "1.0.0", "ruleset_hash": "green-only",
                         "protocol_version": "source-compatible-environment/1.0",
                         "state_hash": "green-state", "episode_seed": 42},
    )
    return green, context


def test_perspective_numeric_contract_and_alias() -> None:
    assert Perspective.RED == 0 and Perspective.BLUE == 1
    assert Perspective.GREEN == -1 and Perspective.GOD is Perspective.GREEN


def test_perspective_policy_hash_is_stable() -> None:
    assert load_perspective_policy().content_hash == load_perspective_policy().content_hash


def test_red_blue_green_share_stable_top_level_fields(projection_fixture) -> None:
    green, context = projection_fixture
    bundle = PerspectiveProjector().project(green, context)
    required = {"scenario_id", "terrain_id", "time", "operators", "passengers",
                "cities", "landmarks", "jm_points", "scores", "valid_actions",
                "role_and_grouping_info", "communication", "actions", "judge_info",
                "extra"}
    assert required <= set(bundle.red)
    assert required <= set(bundle.blue)
    assert required <= set(bundle.green)


def test_green_is_omniscient_and_preserves_source_unknowns(projection_fixture) -> None:
    green, context = projection_fixture
    projected = PerspectiveProjector().project(green, context).green
    assert {item["obj_id"] for item in projected["operators"]} == {1, 2, 20}
    assert {item["obj_id"] for item in projected["passengers"]} == {3, 21}
    assert projected["source_unknown_top"] == {"preserved": True}
    assert projected["extra"]["native"]["perspective"] == "GREEN"


def test_side_frames_include_all_friendly_and_only_visible_enemy(projection_fixture) -> None:
    green, context = projection_fixture
    bundle = PerspectiveProjector().project(green, context)
    assert {item["obj_id"] for item in bundle.red["operators"]} == {1, 2}
    assert {item["obj_id"] for item in bundle.red["passengers"]} == {3}
    assert {item["obj_id"] for item in bundle.blue["operators"]} == {1, 2, 20}
    assert {item["obj_id"] for item in bundle.blue["passengers"]} == {21}


def test_visible_enemy_is_rebuilt_without_raw_secrets(projection_fixture) -> None:
    green, context = projection_fixture
    red = PerspectiveProjector().project(green, context).red
    enemy = next(item for item in red["operators"] if item["obj_id"] == 2)
    assert enemy["name"] == "UNKNOWN" and enemy["armor"] == -1
    assert enemy["carry_weapon_ids"] == [] and enemy["remain_bullet_nums"] == {}
    assert enemy["passenger_ids"] == [] and enemy["see_enemy_bop_ids"] == []
    assert "ammunition" not in str(enemy)


def test_hidden_relations_are_cleaned_recursively(projection_fixture) -> None:
    green, context = projection_fixture
    own = next(item for item in PerspectiveProjector().project(
        green, context).red["operators"] if item["obj_id"] == 1)
    assert own["passenger_ids"] == [3]
    assert own["launcher"] is None and own["launch_ids"] == []
    assert own["nested"] == {"partner_id": None, "target_obj_id": None}
    assert 20 not in own["see_enemy_bop_ids"]
    assert "native" not in own["extra"]


def test_hidden_passenger_not_revealed_by_visible_carrier(projection_fixture) -> None:
    green, context = projection_fixture
    red = PerspectiveProjector().project(green, context).red
    assert 21 not in {item["obj_id"] for item in red["passengers"]}
    enemy = next(item for item in red["operators"] if item["obj_id"] == 2)
    assert enemy["passenger_ids"] == []


def test_valid_actions_are_side_and_target_filtered_for_int_or_str_keys(
        projection_fixture) -> None:
    green, context = projection_fixture
    bundle = PerspectiveProjector().project(green, context)
    assert set(bundle.red["valid_actions"]) == {"1"}
    assert len(bundle.red["valid_actions"]["1"]) == 1
    assert set(bundle.blue["valid_actions"]) == {"2"}


def test_actions_are_own_side_only_and_stable(projection_fixture) -> None:
    green, context = projection_fixture
    bundle = PerspectiveProjector().project(green, context)
    assert [item["side"] for item in bundle.green["actions"]] == ["red", "blue"]
    assert [item["side"] for item in bundle.red["actions"]] == ["red"]
    assert [item["side"] for item in bundle.blue["actions"]] == ["blue"]


def test_judge_info_requires_all_enemy_references_visible(projection_fixture) -> None:
    green, context = projection_fixture
    red = PerspectiveProjector().project(green, context).red
    assert len(red["judge_info"]) == 1
    assert red["judge_info"][0]["target_id"] == 2
    assert "native_evidence" not in red["judge_info"][0]
    assert "table_cell" not in red["judge_info"][0]
    assert len(PerspectiveProjector().project(green, context).green["judge_info"]) == 3


def test_hidden_attacker_judge_is_omitted_but_friendly_loss_remains(
        projection_fixture) -> None:
    green, context = projection_fixture
    red = PerspectiveProjector().project(green, context).red
    assert all(record.get("actor_id") != 20 for record in red["judge_info"])
    friendly = next(item for item in red["operators"] if item["obj_id"] == 1)
    assert friendly["blood"] == 2


def test_owned_and_public_auxiliary_fields_are_filtered(projection_fixture) -> None:
    green, context = projection_fixture
    red = PerspectiveProjector().project(green, context).red
    assert red["role_and_grouping_info"] == {"red": {"seat": "r"}}
    assert red["communication"] == [{"message": "r", "side": "red"}]
    assert red["jm_points"] == [{"id": 1, "owner": "red"}]
    assert set(red["landmarks"]) == {"public", "red-secret"}
    assert red["cities"] == green["cities"] and red["scores"] == green["scores"]


def test_side_extra_uses_allowlist_not_green_copy(projection_fixture) -> None:
    green, context = projection_fixture
    native = PerspectiveProjector().project(green, context).red["extra"]["native"]
    assert native["perspective"] == "RED"
    assert native["session_id"] == "session-test"
    assert "state_hash" not in native and "episode_seed" not in native
    assert "ruleset_hash" not in native and "engine_private" not in native


def test_projection_is_pure_and_order_independent(projection_fixture) -> None:
    green, context = projection_fixture
    before = deepcopy(green)
    projector = PerspectiveProjector()
    first = projector.project(green, context)
    second = projector.project(green, context)
    assert green == before
    assert canonical_hash(first.red) == canonical_hash(second.red)
    assert canonical_hash(first.blue) == canonical_hash(second.blue)
    assert canonical_hash(first.green) == canonical_hash(second.green)


def test_bundle_returns_detached_values(projection_fixture) -> None:
    green, context = projection_fixture
    bundle = PerspectiveProjector().project(green, context)
    red = bundle.for_perspective(Perspective.RED)
    red["operators"].clear()
    assert bundle.red["operators"]


def test_side_swap_projection_is_red_blue_symmetric(projection_fixture) -> None:
    green, context = projection_fixture
    original = PerspectiveProjector().project(green, context)
    swapped = deepcopy(green)
    for unit in swapped["operators"] + swapped["passengers"]:
        unit["color"] = 1 - unit["color"]
        unit["owner"] = "blue" if unit["owner"] == "red" else "red"
    for action in swapped["actions"]:
        action["side"] = "blue" if action["side"] == "red" else "red"
    for records in swapped["valid_actions"].values():
        for record in records:
            action = record["action"]
            action["side"] = "blue" if action["side"] == "red" else "red"
    swapped["role_and_grouping_info"] = {
        "red": green["role_and_grouping_info"]["blue"],
        "blue": green["role_and_grouping_info"]["red"],
    }
    swapped_context = PerspectiveContext(
        red=_side("red", tuple(replace(item, side="red")
                               for item in context.blue.friendly_operators),
                  tuple(replace(item, side="blue")
                        for item in context.blue.visible_enemies)),
        blue=_side("blue", tuple(replace(item, side="blue")
                                 for item in context.red.friendly_operators),
                   tuple(replace(item, side="red")
                         for item in context.red.visible_enemies)),
        session_id=context.session_id, public_metadata=context.public_metadata)
    mirrored = PerspectiveProjector().project(swapped, swapped_context)
    assert len(original.red["operators"]) == len(mirrored.blue["operators"])
    assert len(original.red["passengers"]) == len(mirrored.blue["passengers"])
    original_enemy = next(item for item in original.red["operators"]
                          if item["color"] == 1)
    mirrored_enemy = next(item for item in mirrored.blue["operators"]
                          if item["color"] == 0)
    assert set(original_enemy) == set(mirrored_enemy)


def _environment() -> SourceCompatibleEnvironment:
    scenario = load_scenario("examples/source_movement_scenario.json")
    return SourceCompatibleEnvironment(
        GameSession(NativeEngineBackend(scenario)), template=_template(scenario))


def test_environment_setup_views_returns_red_blue_green() -> None:
    bundle = _environment().setup_views(42)
    assert [item["obj_id"] for item in bundle.red["operators"]] == [1]
    assert [item["obj_id"] for item in bundle.blue["operators"]] == [2]
    assert [item["obj_id"] for item in bundle.green["operators"]] == [1, 2]


def test_enemy_enters_and_leaves_side_frame_without_green_filtering() -> None:
    env = _environment()
    bundle = env.setup_views(42)
    assert 2 not in _unit_ids(bundle.red)
    move = EngineAction("red", 1, ActionType.MOVE,
                        parameters={"path": [102, 103, 104],
                                    "movement_mode": "NORMAL"})
    bundle, _ = env.step_views([move], delta_time_seconds=60)
    assert 2 in _unit_ids(bundle.red)
    assert 2 in _unit_ids(bundle.green)
    env.step_views([], delta_time_seconds=75)
    back = EngineAction("red", 1, ActionType.MOVE,
                        parameters={"path": [103, 102, 101],
                                    "movement_mode": "NORMAL"})
    bundle, _ = env.step_views([back], delta_time_seconds=60)
    assert 2 not in _unit_ids(bundle.red)
    assert 2 in _unit_ids(bundle.green)


def test_querying_side_views_does_not_change_green_hash() -> None:
    env = _environment()
    bundle = env.setup_views(42)
    before = canonical_hash(bundle.green)
    bundle.for_perspective(Perspective.RED)
    bundle.for_perspective(Perspective.BLUE)
    assert canonical_hash(bundle.green) == before


def test_snapshot_restore_recreates_same_views() -> None:
    first = _environment()
    first.setup_views(42)
    move = EngineAction("red", 1, ActionType.MOVE,
                        parameters={"path": [102, 103],
                                    "movement_mode": "NORMAL"})
    expected, _ = first.step_views([move], delta_time_seconds=7)
    snapshot = first.session.snapshot()
    restored = _environment()
    actual = restored.restore_views(snapshot)
    # A restored frame intentionally has no current-step action echo.
    expected_values = [dict(expected.red), dict(expected.blue), dict(expected.green)]
    actual_values = [dict(actual.red), dict(actual.blue), dict(actual.green)]
    for value in expected_values + actual_values:
        value["actions"] = []
        value["judge_info"] = []
    assert [canonical_hash(value) for value in expected_values] == [
        canonical_hash(value) for value in actual_values]


def test_perspective_output_is_delta_granularity_deterministic() -> None:
    hashes = []
    for chunk in (1, 5, 20):
        env = _environment()
        env.setup_views(42)
        move = EngineAction("red", 1, ActionType.MOVE,
                            parameters={"path": [102, 103],
                                        "movement_mode": "NORMAL"})
        bundle, _ = env.step_views([move], delta_time_seconds=chunk)
        elapsed = chunk
        while elapsed < 100:
            advance = min(chunk, 100 - elapsed)
            bundle, _ = env.step_views([], delta_time_seconds=advance)
            elapsed += advance
        hashes.append((canonical_hash(bundle.red), canonical_hash(bundle.blue),
                       canonical_hash(bundle.green)))
    assert len(set(hashes)) == 1


def _unit_ids(frame):
    return {int(item["obj_id"])
            for item in frame["operators"] + frame["passengers"]}
