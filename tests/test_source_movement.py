from __future__ import annotations

from copy import deepcopy

import pytest

from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.event import EventType
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.importer import SourceFrameImporter
from wargame_native_engine.io.replay import ReplayWriter, replay_actions
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.movement.models import MovementMode
from wargame_native_engine.movement.policy import load_source_movement_policy
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment


def _cell(pos: int, *, cond: int | str = 0, elev: float | None = 0,
          east: int = -1, west: int = -1,
          east_road: int = 0, west_road: int = 0,
          east_river: int = 0, west_river: int = 0) -> dict:
    return {"pos": pos, "elev": elev, "cond": cond,
            "neighbors": [east, -1, -1, west, -1, -1],
            "roads": [east_road, 0, 0, west_road, 0, 0],
            "rivers": [east_river, 0, 0, west_river, 0, 0]}


def _scenario_document(*, terrain: dict[int, int | str] | None = None,
                       elevation: dict[int, float | None] | None = None,
                       roads: dict[tuple[int, int], int] | None = None,
                       rivers: dict[tuple[int, int], int] | None = None,
                       mover_domain: str = "VEHICLE",
                       mover_speed: float = 36,
                       elevation_kind: str = "LEVEL",
                       elevation_unit_m: float | None = 20) -> dict:
    terrain, elevation = terrain or {}, elevation or {}
    roads, rivers = roads or {}, rivers or {}
    positions = list(range(101, 107))
    cells = []
    for index, pos in enumerate(positions):
        east = positions[index + 1] if index + 1 < len(positions) else -1
        west = positions[index - 1] if index > 0 else -1
        cells.append(_cell(
            pos, cond=terrain.get(pos, 0), elev=elevation.get(pos, 0),
            east=east, west=west,
            east_road=roads.get((pos, east), 0),
            west_road=roads.get((west, pos), 0),
            east_river=rivers.get((pos, east), 0),
            west_river=rivers.get((west, pos), 0)))
    return {
        "schema_version": "native-scenario/1.0",
        "scenario_id": "source-movement-test",
        "initial_stage": "combat",
        "map": {"map_id": "source-hex-test", "width": 20,
                "height": 20, "distance_unit": "hex",
                "blocked_positions": [], "hexes": cells,
                "hex_size_m": 200, "elevation_unit_m": elevation_unit_m,
                "elevation_kind": elevation_kind, "roadblocks": []},
        "sides": ["red", "blue"],
        "operators": [
            _operator(1, "red", mover_domain, 101, mover_speed),
            _operator(3, "red", "VEHICLE", 105, 36),
            _operator(2, "blue", "PERSONNEL", 106, 5),
        ],
        "objectives": [],
        "deployment": {"positions": {
            "red": {"1": [1, 1], "3": [5, 1]},
            "blue": {"2": [6, 1]}}},
        "terminal_rules": {"max_ticks": 1000, "elimination": True,
                           "objective_score_to_win": None},
        "metadata": {"source_compatible": {"weapons": {
            "36": {"weapon_class": "large_direct_fire_gun",
                   "maximum_range": 10,
                   "target_domains": ["PERSONNEL", "VEHICLE"],
                   "ammunition_key": "0", "requires_unfolded": True,
                   "can_fire_while_moving": False}}}},
    }


def _operator(operator_id: int, side: str, domain: str, current_hex: int,
              basic_speed: float) -> dict:
    row, col = divmod(current_hex, 100)
    return {"operator_id": operator_id, "side": side,
            "operator_type": domain, "subtype": None,
            "x": col, "y": row, "altitude": 0, "heading": 0,
            "health": 100, "max_health": 100, "blood": 3, "max_blood": 3,
            "speed": 0, "max_speed": basic_speed,
            "basic_speed": basic_speed, "move_range": 0,
            "sensor_range": 25, "state": "STOPPED", "alive": True,
            "deployed": True, "weapon_ids": [36], "attack_range": 0,
            "attack_damage": 0, "current_hex": current_hex,
            "cur_pos_millionths": 0, "move_path": [],
            "movement_mode": "NORMAL", "stopped": True,
            "weapon_unfolded": True,
            "raw_attributes": {"armor_class": "MEDIUM",
                               "ammunition": {"0": 20}}}


def _engine(**kwargs) -> NativeEngine:
    engine = NativeEngine(scenario_from_dict(_scenario_document(**kwargs)))
    engine.reset(42)
    return engine


def _move(path: list[int], *, actor: int = 1, side: str = "red",
          mode: str = "NORMAL") -> EngineAction:
    return EngineAction(side, actor, ActionType.MOVE,
                        parameters={"path": path, "movement_mode": mode})


def _operator_state(engine: NativeEngine, operator_id: int = 1):
    return engine._require_world().operators[operator_id]


def test_adjacent_path_is_accepted_without_instant_jump() -> None:
    engine = _engine()
    result = engine.step([_move([102])], delta_time_seconds=1)
    unit = _operator_state(engine)
    assert result.decisions[0].accepted
    assert unit.current_hex == 101
    assert unit.cur_pos == pytest.approx(0.05)
    assert unit.move_path == [102]
    assert unit.speed == pytest.approx(0.05)


def test_non_adjacent_path_is_rejected() -> None:
    engine = _engine()
    result = engine.step([_move([103])])
    assert not result.decisions[0].accepted
    assert result.decisions[0].code == "PATH_NOT_ADJACENT"
    assert _operator_state(engine).current_hex == 101


def test_path_advances_segment_by_segment_and_emits_each_arrival() -> None:
    engine = _engine()
    result = engine.step([_move([102, 103, 104])], delta_time_seconds=60)
    unit = _operator_state(engine)
    assert unit.current_hex == 104
    assert unit.move_path == []
    arrivals = [item for item in result.events
                if item.event_type is EventType.ARRIVED_HEX]
    assert [item.data["to_hex"] for item in arrivals] == [102, 103, 104]
    assert len(engine._require_world().movement_evidence) == 3


def test_large_delta_preserves_remainder_on_next_segment() -> None:
    engine = _engine(terrain={102: 1, 103: 2})
    engine.step([_move([102, 103])], delta_time_seconds=80)
    unit = _operator_state(engine)
    assert unit.current_hex == 102
    assert unit.move_path == [103]
    assert unit.cur_pos == pytest.approx(2 / 3, abs=1e-6)
    assert unit.speed == pytest.approx((36 / 3) / 3.6 / 200, abs=1e-6)


@pytest.mark.parametrize(("cond", "expected"), [
    (0, (1, 1)), (1, (1, 2)), (2, (1, 3)),
    (4, (1, 4)), (3, (1, 4)),
])
def test_vehicle_target_terrain_multipliers(cond, expected) -> None:
    engine = _engine(terrain={102: cond})
    result = engine.step([_move([102])], delta_time_seconds=1)
    assert result.decisions[0].accepted
    assert engine._require_world().movement_evidence[0][
        "terrain_multiplier"] == list(expected)


def test_small_river_edge_uses_half_speed() -> None:
    engine = _engine(rivers={(101, 102): 1})
    engine.step([_move([102])])
    evidence = engine._require_world().movement_evidence[0]
    assert evidence["terrain_type"] == "SMALL_RIVER"
    assert evidence["terrain_multiplier"] == [1, 2]


def test_unknown_terrain_is_rejected() -> None:
    engine = _engine(terrain={102: 99})
    result = engine.step([_move([102])])
    assert result.decisions[0].code == "UNKNOWN_TERRAIN"


@pytest.mark.parametrize(("level", "expected"), [
    (0, (1, 1)), (1, (1, 1)), (2, (1, 2)),
    (3, (1, 3)), (4, (1, 4)), (5, (1, 5)),
])
def test_vehicle_discrete_slope_multipliers(level, expected) -> None:
    engine = _engine(elevation={101: 0, 102: level})
    result = engine.step([_move([102])])
    assert result.decisions[0].accepted
    assert engine._require_world().movement_evidence[0][
        "slope_multiplier"] == list(expected)


def test_vehicle_slope_six_is_impassable() -> None:
    engine = _engine(elevation={101: 0, 102: 6})
    result = engine.step([_move([102])])
    assert result.decisions[0].code == "SLOPE_IMPASSABLE"


def test_unknown_elevation_unit_rejects_move_without_guessing() -> None:
    document = _scenario_document()
    document["map"]["elevation_unit_m"] = None
    engine = NativeEngine(scenario_from_dict(document))
    engine.reset()
    result = engine.step([_move([102])])
    assert result.decisions[0].code == "UNKNOWN_ELEVATION_UNIT"


def test_continuous_road_overrides_terrain_and_slope() -> None:
    engine = _engine(terrain={102: 2}, elevation={102: 6},
                     roads={(101, 102): 1})
    result = engine.step([_move([102])])
    evidence = engine._require_world().movement_evidence[0]
    assert result.decisions[0].accepted
    assert evidence["road_override"] is True
    assert evidence["terrain_multiplier"] == [1, 1]
    assert evidence["slope_multiplier"] == [1, 1]


def test_one_sided_road_does_not_override() -> None:
    document = _scenario_document(terrain={102: 1}, elevation={102: 2},
                                  roads={(101, 102): 1})
    document["map"]["hexes"][1]["roads"][3] = 0
    engine = NativeEngine(scenario_from_dict(document))
    engine.reset()
    engine.step([_move([102])])
    evidence = engine._require_world().movement_evidence[0]
    assert evidence["road_override"] is False
    assert evidence["terrain_multiplier"] == [1, 2]
    assert evidence["slope_multiplier"] == [1, 2]


@pytest.mark.parametrize(("road", "speed"), [(1, 40), (2, 60), (3, 90)])
def test_marching_uses_absolute_road_speed(road, speed) -> None:
    engine = _engine(roads={(101, 102): road})
    unit = _operator_state(engine)
    unit.weapon_locked = True
    unit.movement_mode = "MARCHING"
    result = engine.step([_move([102], mode="MARCHING")])
    assert result.decisions[0].accepted
    assert engine._require_world().movement_evidence[0]["effective_speed"] == speed


def test_marching_cannot_leave_road() -> None:
    engine = _engine()
    unit = _operator_state(engine)
    unit.weapon_locked = True
    unit.movement_mode = "MARCHING"
    result = engine.step([_move([102], mode="MARCHING")])
    assert result.decisions[0].code == "MARCHING_REQUIRES_CONTINUOUS_ROAD"


def test_personnel_terrain_does_not_change_speed() -> None:
    engine = _engine(mover_domain="PERSONNEL", mover_speed=5,
                     terrain={102: 2})
    engine.step([_move([102])])
    assert engine._require_world().movement_evidence[0][
        "effective_speed"] == 5


def test_personnel_height_above_sixty_metres_halves_speed() -> None:
    engine = _engine(mover_domain="PERSONNEL", mover_speed=5,
                     elevation={102: 4}, elevation_unit_m=20)
    engine.step([_move([102])])
    evidence = engine._require_world().movement_evidence[0]
    assert evidence["slope_multiplier"] == [1, 2]
    assert evidence["effective_speed"] == 2.5


@pytest.mark.parametrize(("mode", "speed"), [("CHARGE_1", 10),
                                               ("CHARGE_2", 20)])
def test_personnel_charge_speed(mode, speed) -> None:
    engine = _engine(mover_domain="PERSONNEL", mover_speed=5)
    engine.step([_move([102], mode=mode)])
    assert engine._require_world().movement_evidence[0][
        "effective_speed"] == speed


def test_first_fatigue_prevents_second_charge() -> None:
    engine = _engine(mover_domain="PERSONNEL", mover_speed=5)
    _operator_state(engine).fatigue_level = 1
    result = engine.step([_move([102], mode="CHARGE_2")])
    assert result.decisions[0].code == "FATIGUE_PREVENTS_CHARGE_2"


def test_second_fatigue_prevents_movement() -> None:
    engine = _engine(mover_domain="PERSONNEL", mover_speed=5)
    _operator_state(engine).fatigue_level = 2
    result = engine.step([_move([102])])
    assert result.decisions[0].code == "FATIGUE_PREVENTS_MOVEMENT"


def test_roadblock_is_impassable() -> None:
    engine = _engine(terrain={102: 5})
    result = engine.step([_move([102])])
    assert result.decisions[0].code == "ROADBLOCK_IMPASSABLE"


def test_destroyed_unit_cannot_move() -> None:
    engine = _engine()
    unit = _operator_state(engine)
    unit.alive = False
    result = engine.step([_move([102])])
    assert result.decisions[0].code == "ACTOR_DESTROYED"


def test_canonical_artillery_cannot_move_without_name_guessing() -> None:
    engine = _engine()
    _operator_state(engine).raw_attributes["canonical_type"] = "ARTILLERY"
    result = engine.step([_move([102])])
    assert result.decisions[0].code == "ARTILLERY_CANNOT_MOVE"


def _run_chunks(chunk: int) -> dict:
    engine = _engine(terrain={103: 1})
    elapsed = 0
    engine.step([_move([102, 103])], delta_time_seconds=chunk)
    elapsed += chunk
    while elapsed < 100:
        engine.step([], delta_time_seconds=min(chunk, 100 - elapsed))
        elapsed += min(chunk, 100 - elapsed)
    return engine.snapshot()


def test_delta_time_granularity_has_identical_world_and_evidence() -> None:
    one, five, twenty = _run_chunks(1), _run_chunks(5), _run_chunks(20)
    assert one["world"] == five["world"] == twenty["world"]
    assert canonical_hash(one["world"]) == canonical_hash(twenty["world"])


def test_snapshot_restore_continues_same_movement() -> None:
    first = _engine()
    first.step([_move([102, 103])], delta_time_seconds=7)
    snapshot = first.snapshot()
    second = _engine()
    second.restore(snapshot)
    first.step([], delta_time_seconds=33)
    second.step([], delta_time_seconds=33)
    assert first.snapshot() == second.snapshot()


def test_stop_move_completes_current_segment_then_waits_seventy_five_seconds() -> None:
    engine = _engine()
    engine.step([_move([102, 103])], delta_time_seconds=1)
    stop = EngineAction("red", 1, ActionType.STOP_MOVE)
    result = engine.step([stop], delta_time_seconds=19)
    unit = _operator_state(engine)
    assert unit.current_hex == 102
    assert unit.move_path == []
    assert unit.stopped is False
    assert unit.move_to_stop_remaining_ms == 75_000
    assert EventType.STOPPING_STARTED in {item.event_type for item in result.events}
    engine.step([], delta_time_seconds=74)
    assert unit.stopped is False
    engine.step([], delta_time_seconds=1)
    assert unit.stopped is True
    assert unit.state == "STOPPED"


def test_basic_speed_is_immutable_while_segment_speed_changes() -> None:
    engine = _engine(terrain={102: 2})
    unit = _operator_state(engine)
    engine.step([_move([102])])
    assert unit.basic_speed == 36
    assert unit.speed == pytest.approx((36 / 3) / 3.6 / 200, abs=1e-6)


def test_charge_completion_adds_fatigue_and_stopped_recovery_removes_it() -> None:
    engine = _engine(mover_domain="PERSONNEL", mover_speed=20)
    engine.step([_move([102], mode="CHARGE_1")], delta_time_seconds=36)
    unit = _operator_state(engine)
    assert unit.current_hex == 102 and unit.fatigue_level == 1
    engine.step([], delta_time_seconds=75)
    assert unit.stopped and unit.fatigue_level == 1
    engine.step([], delta_time_seconds=75)
    assert unit.fatigue_level == 0


def test_suppressed_personnel_finishes_segment_and_preserves_remaining_path() -> None:
    engine = _engine(mover_domain="PERSONNEL", mover_speed=5)
    engine.step([_move([102, 103])], delta_time_seconds=10)
    unit = _operator_state(engine)
    unit.state = "SUPPRESSED"
    unit.suppression_remaining_ms = 150_000
    engine.step([], delta_time_seconds=134)
    assert unit.current_hex == 102
    assert unit.move_path == [103]
    assert unit.state == "TRANSITIONING_TO_STOP"


def test_simultaneous_stack_arrivals_follow_action_sequence() -> None:
    document = _scenario_document()
    document["operators"][1]["current_hex"] = 102
    document["operators"][1]["x"] = 2
    document["operators"].extend([
        _operator(4, "red", "VEHICLE", 102, 36),
        _operator(5, "red", "VEHICLE", 102, 36),
        _operator(6, "red", "VEHICLE", 103, 36),
    ])
    document["deployment"]["positions"]["red"].update({
        "4": [2, 1], "5": [2, 1], "6": [3, 1]})
    engine = NativeEngine(scenario_from_dict(document))
    engine.reset()
    result = engine.step([_move([102], actor=1),
                          _move([102], actor=6)], delta_time_seconds=20)
    assert _operator_state(engine, 1).current_hex == 102
    assert _operator_state(engine, 6).current_hex == 103
    assert _operator_state(engine, 6).movement_blocked_reason == (
        "FRIENDLY_STACK_LIMIT")
    blocked = [item for item in result.events
               if item.event_type is EventType.MOVEMENT_BLOCKED]
    assert len(blocked) == 1 and blocked[0].actor_id == 6


def test_move_shoot_and_stop_share_one_ordered_engine_step() -> None:
    document = _scenario_document()
    document["operators"].append(_operator(4, "red", "VEHICLE", 103, 36))
    document["deployment"]["positions"]["red"]["4"] = [3, 1]
    engine = NativeEngine(scenario_from_dict(document))
    engine.reset(42)
    engine.step([_move([102, 103])], delta_time_seconds=1)
    stop = EngineAction("red", 1, ActionType.STOP_MOVE)
    shoot = EngineAction("red", 3, ActionType.SHOOT, target_id=2,
                         parameters={"weapon_id": 36})
    move = _move([102], actor=4)
    result = engine.step([stop, shoot, move], delta_time_seconds=1)
    assert [item.action_id for item in result.actions] == [
        stop.action_id, shoot.action_id, move.action_id]
    assert all(item.accepted for item in result.decisions)
    types = {item.event_type for item in result.events}
    assert {EventType.STOP_MOVE_REQUESTED, EventType.DIRECT_FIRE_REQUESTED,
            EventType.MOVEMENT_STARTED} <= types
    assert result.action_sequences == (1, 2, 3)


def test_moving_unit_cannot_use_stop_required_direct_fire() -> None:
    engine = _engine()
    engine.step([_move([102])])
    shoot = EngineAction("red", 1, ActionType.SHOOT, target_id=2,
                         parameters={"weapon_id": 36})
    result = engine.step([shoot])
    assert not result.decisions[0].accepted
    assert result.decisions[0].code == "ATTACKER_MUST_STOP"


def test_other_unit_direct_fire_is_not_broken_by_movement() -> None:
    engine = _engine()
    shoot = EngineAction("red", 3, ActionType.SHOOT, target_id=2,
                         parameters={"weapon_id": 36})
    result = engine.step([_move([102]), shoot])
    assert all(item.accepted for item in result.decisions)
    assert any(item.event_type is EventType.COMBAT_RESULT_LOOKED_UP
               for item in result.events)


def test_weapon_lock_and_marching_transitions_each_take_seventy_five_seconds() -> None:
    engine = _engine(roads={(101, 102): 1})
    lock = EngineAction("red", 1, ActionType.WEAPON_LOCK)
    engine.step([lock], delta_time_seconds=74)
    unit = _operator_state(engine)
    assert not unit.weapon_locked and unit.transition_remaining_ms == 1000
    engine.step([], delta_time_seconds=1)
    assert unit.weapon_locked and unit.weapon_unfolded is False
    march = EngineAction("red", 1, ActionType.CHANGE_STATE,
                         parameters={"state": "MARCHING"})
    engine.step([march], delta_time_seconds=75)
    assert unit.movement_mode == "MARCHING" and unit.stopped


def test_marching_state_cannot_shoot() -> None:
    engine = _engine(roads={(101, 102): 1})
    unit = _operator_state(engine)
    unit.weapon_locked = True
    unit.weapon_unfolded = True
    unit.movement_mode = "MARCHING"
    shoot = EngineAction("red", 1, ActionType.SHOOT, target_id=2,
                         parameters={"weapon_id": 36})
    result = engine.step([shoot])
    assert result.decisions[0].code == "ATTACKER_MUST_STOP"


def test_observation_uses_old_hex_until_center_then_refreshes() -> None:
    document = _scenario_document(terrain={106: 1})
    document["operators"][2]["raw_attributes"]["concealed"] = True
    engine = NativeEngine(scenario_from_dict(document))
    engine.reset()
    observer = _operator_state(engine)
    assert observer.raw_attributes["see_enemy_bop_ids"] == []
    engine.step([_move([102, 103, 104])], delta_time_seconds=59)
    assert observer.current_hex == 103
    assert observer.raw_attributes["see_enemy_bop_ids"] == []
    engine.step([], delta_time_seconds=1)
    assert observer.current_hex == 104
    assert observer.raw_attributes["see_enemy_bop_ids"] == [2]


def test_source_valid_actions_cache_is_detached_rng_neutral_and_state_keyed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine()
    calls = 0
    original = engine._source_valid_actions

    def counted(side: str):
        nonlocal calls
        calls += 1
        return original(side)

    monkeypatch.setattr(engine, "_source_valid_actions", counted)
    rng_before = engine.snapshot()["rng"]
    first = engine.valid_actions("red")
    second = engine.valid_actions("red")
    assert calls == 1
    assert tuple(item.to_dict() for item in first) == tuple(
        item.to_dict() for item in second
    )
    assert engine.snapshot()["rng"] == rng_before

    move = next(
        item for item in first if item.action.action_type is ActionType.MOVE
    )
    move.action.parameters["path"].append(999)
    third = engine.valid_actions("red")
    assert calls == 1
    assert all(
        999 not in item.action.parameters.get("path", ()) for item in third
    )
    engine.valid_actions("blue")
    engine.valid_actions("red")
    assert calls == 2

    engine._require_world().operators[1].weapon_locked = True
    engine.valid_actions("red")
    assert calls == 3


def _source_template(document: dict):
    units = []
    for item in document["operators"]:
        units.append({"obj_id": item["operator_id"],
                      "color": 0 if item["side"] == "red" else 1,
                      "type": 1 if item["operator_type"] == "PERSONNEL" else 2,
                      "sub_type": 0, "name": f"unit-{item['operator_id']}",
                      "owner": item["side"], "armor": 0,
                      "cur_hex": item["current_hex"], "cur_pos": 0,
                      "basic_speed": item["basic_speed"], "speed": 0,
                      "move_state": 0, "stop": 1, "move_path": [],
                      "move_to_stop_remain_time": 0,
                      "blood": 3, "max_blood": 3,
                      "carry_weapon_ids": [36],
                      "remain_bullet_nums": {"0": 20},
                      "observe_distance": [], "passenger_ids": [],
                      "launch_ids": [], "on_board": 0, "close_combat": 0,
                      "altitude": 0, "weapon_unfold_state": 1,
                      "weapon_cool_time": 0})
    value = {"operators": units, "passengers": [],
             "time": {"cur_step": 0, "tick": 1, "max_time": 1000,
                      "max_step": 1000, "stage": 2},
             "jm_points": [], "cities": [], "scores": {}, "landmarks": {},
             "terrain_id": 999, "judge_info": [],
             "role_and_grouping_info": {}, "communication": [],
             "scenario_id": document["scenario_id"], "valid_actions": {},
             "actions": []}
    return SourceFrameImporter().from_dict(value, source_hash="fixture-hash")


def test_source_environment_exports_movement_fields_and_frame_action_timing() -> None:
    document = _scenario_document()
    scenario = scenario_from_dict(document)
    session = GameSession(NativeEngineBackend(scenario))
    env = SourceCompatibleEnvironment(session,
                                      template=_source_template(document))
    initial = env.setup(42)
    assert initial["actions"] == [] and initial["judge_info"] == []
    frame, done = env.step([_move([102])], delta_time_seconds=1)
    unit = next(item for item in frame["operators"] if item["obj_id"] == 1)
    assert done is False
    assert frame["actions"] == [_move([102]).to_dict()]
    assert frame["judge_info"] == []
    assert unit["basic_speed"] == 36
    assert unit["speed"] == pytest.approx(0.05)
    assert unit["cur_hex"] == 101 and unit["cur_pos"] == pytest.approx(0.05)
    assert unit["move_path"] == [102] and unit["stop"] == 0
    assert unit["extra"]["native"]["movement_mode"] == "NORMAL"


def test_source_environment_step_routes_move_and_shoot_together() -> None:
    document = _scenario_document()
    scenario = scenario_from_dict(document)
    env = SourceCompatibleEnvironment(
        GameSession(NativeEngineBackend(scenario)),
        template=_source_template(document))
    env.setup(42)
    shoot = EngineAction("red", 3, ActionType.SHOOT, target_id=2,
                         parameters={"weapon_id": 36})
    frame, done = env.step([_move([102]), shoot])
    assert done is False
    assert frame["actions"] == [_move([102]).to_dict(), shoot.to_dict()]
    assert frame["judge_info"]
    mover = next(item for item in frame["operators"] if item["obj_id"] == 1)
    assert mover["cur_hex"] == 101 and mover["cur_pos"] > 0


def test_source_movement_strict_replay_round_trip(tmp_path) -> None:
    scenario = scenario_from_dict(_scenario_document(terrain={103: 1}))
    engine = NativeEngine(scenario)
    engine.reset(42)
    output = tmp_path / "movement-replay"
    writer = ReplayWriter(output, run_id="movement", scenario=scenario,
                          seed=42, agents={"red": "test", "blue": "test"},
                          max_ticks=1000, ruleset=engine.ruleset,
                          project_commit=None)
    writer.record_initial_state(engine.snapshot())
    first = engine.step([_move([102, 103])], delta_time_seconds=5)
    writer.record_step(first, engine.snapshot())
    for _ in range(7):
        result = engine.step([], delta_time_seconds=5)
        writer.record_step(result, engine.snapshot())
    summary = {"result": engine.result().to_dict()}
    writer.finalize(summary, status="complete")
    verification = replay_actions(scenario, output)
    assert verification.matches


def test_action_sequence_and_state_hash_are_seed_reproducible() -> None:
    values = []
    for _ in range(2):
        engine = _engine()
        result = engine.step([_move([102, 103])], delta_time_seconds=25)
        values.append((result.to_dict(), canonical_hash(engine.snapshot()["world"])))
    assert values[0] == values[1]


def test_movement_policy_hash_and_derived_hex_size_are_explicit() -> None:
    first = load_source_movement_policy()
    second = load_source_movement_policy()
    assert first.content_hash == second.content_hash
    assert first.hex_size_m == 200
    assert first.hex_size_status == "DERIVED_SOURCE_PARAMETER"


def test_move_cannot_bypass_side_control() -> None:
    engine = _engine()
    result = engine.step([_move([102], side="blue")])
    assert result.decisions[0].code == "ACTOR_NOT_CONTROLLED"


def test_air_unit_does_not_receive_ground_movement_rules() -> None:
    engine = _engine(mover_domain="AIR", mover_speed=100)
    result = engine.step([_move([102])])
    assert result.decisions[0].code == "AIR_MOVEMENT_UNSUPPORTED"


def test_transitioning_unit_cannot_start_move() -> None:
    engine = _engine()
    unit = _operator_state(engine)
    unit.transition_kind = "WEAPON_LOCK"
    unit.transition_remaining_ms = 10_000
    result = engine.step([_move([102])])
    assert result.decisions[0].code == "TRANSITION_IN_PROGRESS"


def test_stack_limit_is_prechecked_at_path_submission() -> None:
    document = _scenario_document()
    document["operators"][1]["current_hex"] = 102
    document["operators"][1]["x"] = 2
    for operator_id in (4, 5, 6):
        document["operators"].append(
            _operator(operator_id, "red", "VEHICLE", 102, 36))
        document["deployment"]["positions"]["red"][str(operator_id)] = [2, 1]
    engine = NativeEngine(scenario_from_dict(document))
    engine.reset()
    result = engine.step([_move([102])])
    assert result.decisions[0].code == "FRIENDLY_STACK_LIMIT"


def test_movement_evidence_contains_complete_policy_and_ordering_fields() -> None:
    engine = _engine(terrain={102: 1}, elevation={102: 2})
    engine.step([_move([102])])
    evidence = engine._require_world().movement_evidence[0]
    required = {
        "event_id", "action_id", "action_sequence", "event_sequence",
        "event_time", "unit_id", "from_hex", "to_hex", "base_speed",
        "terrain_type", "terrain_multiplier", "elevation_from",
        "elevation_to", "elevation_difference", "slope_level",
        "slope_multiplier", "road_type", "road_override", "movement_mode",
        "mode_multiplier", "effective_speed", "segment_distance_m",
        "traversal_seconds", "progress_before", "progress_after", "arrived",
        "blocked", "reason_code", "movement_policy_id",
        "movement_policy_version", "movement_policy_hash",
    }
    assert required <= set(evidence)


def test_valid_actions_recompute_after_arrival() -> None:
    engine = _engine()
    before = {item.action.action_id for item in engine.valid_actions("red")}
    engine.step([_move([102])], delta_time_seconds=20)
    after = {item.action.action_id for item in engine.valid_actions("red")}
    assert before != after
    assert not any(item.action.actor_id == 1
                   and item.action.action_type in {ActionType.MOVE, ActionType.SHOOT}
                   for item in engine.valid_actions("red"))
