#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.importer import SourceFrameImporter
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.io.importers.historical_replay import HistoricalReplayImporter
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.scenario import NativeScenario, load_scenario
from wargame_native_engine.replay import SourceFrameRecorder
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment
from wargame_native_engine.version import ENGINE_VERSION


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCENARIO = ROOT / "examples/source_movement_scenario.json"


def run_acceptance(output: str | Path, *, seed: int = 42,
                   scenario_path: str | Path = DEFAULT_SCENARIO,
                   delta_time_seconds: int = 20) -> Mapping[str, Any]:
    root = Path(output)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"refusing to overwrite acceptance output: {root}")
    root.mkdir(parents=True, exist_ok=True)
    scenario = load_scenario(scenario_path)
    recorded = _run_episode(scenario, seed, delta_time_seconds,
                            recorder_root=root / "green_frames")
    zip_path = recorded["recorder"].package_zip(root / "replay.zip")
    unrecorded = _run_episode(scenario, seed, delta_time_seconds,
                              recorder_root=None)
    archive = SourceReplayArchiveImporter().import_path(zip_path)
    historical = HistoricalReplayImporter().import_path(zip_path)
    verification = replay_source_archive(scenario, zip_path, strict=True)
    result = recorded["result"]
    report = {
        "status": "passed",
        "scenario_id": scenario.scenario_id,
        "episode_seed": seed,
        "frame_count": len(archive.frames),
        "step_count": len(archive.steps),
        "perspectives": ["RED", "BLUE", "GREEN"],
        "move_actions": recorded["move_actions"],
        "arrived_hex_changes": recorded["arrived_hex_changes"],
        "observation_changes": recorded["observation_changes"],
        "shoot_actions": recorded["shoot_actions"],
        "judge_frames": recorded["judge_frames"],
        "terminal": result["terminal"], "truncated": result["truncated"],
        "termination_reason": result["reason"], "winner": result["winner"],
        "historical_import_evidence": historical.replay.evidence_level.value,
        "historical_import_warnings": len(historical.replay.warnings),
        "archive_warnings": len(archive.warnings),
        "expected_final_hash": verification.expected_world_hash,
        "actual_final_hash": verification.actual_world_hash,
        "strict_replay_matches": verification.matches,
        "recorder_off_final_hash": unrecorded["result"]["world_hash"],
        "recorder_is_observer": (
            unrecorded["result"] == result
            and unrecorded["trace"] == recorded["trace"]),
        "zip_path": str(zip_path),
    }
    required = {
        "move_actions": report["move_actions"] > 0,
        "arrived_hex_changes": report["arrived_hex_changes"] > 0,
        "observation_changes": report["observation_changes"] > 0,
        "shoot_actions": report["shoot_actions"] > 0,
        "judge_frames": report["judge_frames"] > 0,
        "terminal_or_truncated": report["terminal"] or report["truncated"],
        "strict_replay": report["strict_replay_matches"],
        "recorder_observer": report["recorder_is_observer"],
        "historical_import": report["historical_import_warnings"] == 0,
    }
    failed = sorted(key for key, value in required.items() if not value)
    if failed:
        raise RuntimeError("minimal source replay acceptance failed: " + ", ".join(failed))
    (root / "acceptance_summary.json").write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8")
    return report


def _run_episode(scenario: NativeScenario, seed: int, delta_time_seconds: int,
                 *, recorder_root: Path | None) -> dict[str, Any]:
    session = GameSession(NativeEngineBackend(scenario))
    env = SourceCompatibleEnvironment(session, template=_template(scenario))
    bundle = env.setup_views(seed)
    recorder = None
    if recorder_root is not None:
        native = bundle.green["extra"]["native"]
        recorder = SourceFrameRecorder(recorder_root)
        recorder.start({
            "scenario_id": scenario.scenario_id,
            "scenario_hash": scenario.scenario_hash,
            "terrain_id": bundle.green["terrain_id"],
            "episode_id": session.session_id,
            "engine_version": ENGINE_VERSION,
            "ruleset_id": native["ruleset_id"],
            "ruleset_version": native["ruleset_version"],
            "ruleset_hash": native["ruleset_hash"],
            "adjudication_table_set_id": native["adjudication_table_set_id"],
            "adjudication_table_set_version": native[
                "adjudication_table_set_version"],
            "adjudication_table_set_hash": native[
                "adjudication_table_set_hash"],
            "movement_policy_id": native["movement_policy_id"],
            "movement_policy_version": native["movement_policy_version"],
            "movement_policy_hash": native["movement_policy_hash"],
            "perspective_policy_id": native["perspective_policy_id"],
            "perspective_policy_version": native["perspective_policy_version"],
            "perspective_policy_hash": native["perspective_policy_hash"],
            "episode_seed": seed, "max_ticks": session.max_ticks,
        })
        recorder.record_initial(bundle.green,
                                initial_state_hash=env.initial_state_hash)
    prior_red_ids = _unit_ids(bundle.red)
    prior_red_hex = _hex(bundle.green, 1)
    move_actions = arrived_changes = observation_changes = 0
    shoot_actions = judge_frames = 0
    trace = []
    max_steps = scenario.terminal_rules.max_ticks // delta_time_seconds + 2
    for _ in range(max_steps):
        red = _red_controller(bundle.red)
        blue = _blue_controller(bundle.blue)
        actions = [value for value in (red, blue) if value is not None]
        move_actions += sum(item.action_type is ActionType.MOVE for item in actions)
        shoot_actions += sum(item.action_type is ActionType.SHOOT for item in actions)
        bundle, done = env.step_views(actions,
                                      delta_time_seconds=delta_time_seconds)
        if recorder is not None:
            step_record = env.last_step_record
            if step_record is None:
                raise RuntimeError("environment did not expose step record")
            recorder.record_step(bundle.green, step_record)
        current_hex = _hex(bundle.green, 1)
        arrived_changes += int(current_hex != prior_red_hex)
        prior_red_hex = current_hex
        current_red_ids = _unit_ids(bundle.red)
        observation_changes += int(current_red_ids != prior_red_ids)
        prior_red_ids = current_red_ids
        judge_frames += int(bool(bundle.green["judge_info"]))
        trace.append({
            "actions": [item.to_dict() for item in actions],
            "red_ids": sorted(current_red_ids), "red_hex": current_hex,
            "judge_info": bundle.green["judge_info"],
            "state_hash": env.last_step_record["expected_state_hash"],
        })
        if done:
            break
    outcome = session.result().to_dict()
    if recorder is not None:
        recorder.finalize(outcome, final_state_hash=outcome["world_hash"])
    return {"result": outcome, "trace": trace, "recorder": recorder,
            "move_actions": move_actions,
            "arrived_hex_changes": arrived_changes,
            "observation_changes": observation_changes,
            "shoot_actions": shoot_actions, "judge_frames": judge_frames}


def _red_controller(frame: Mapping[str, Any]) -> EngineAction | None:
    actions = _actions(frame)
    shoots = [value for value in actions if value.action_type is ActionType.SHOOT]
    if shoots:
        return shoots[0]
    moves = [value for value in actions if value.action_type is ActionType.MOVE]
    if moves:
        return max(moves, key=lambda value: value.parameters["path"][-1])
    waits = [value for value in actions if value.action_type is ActionType.WAIT]
    return waits[0] if waits else None


def _blue_controller(frame: Mapping[str, Any]) -> EngineAction | None:
    waits = [value for value in _actions(frame)
             if value.action_type is ActionType.WAIT]
    return waits[0] if waits else None


def _actions(frame: Mapping[str, Any]) -> list[EngineAction]:
    return [EngineAction.from_dict(record["action"])
            for records in frame["valid_actions"].values()
            for record in records]


def _unit_ids(frame: Mapping[str, Any]) -> set[int]:
    return {int(value["obj_id"])
            for value in frame["operators"] + frame["passengers"]}


def _hex(frame: Mapping[str, Any], operator_id: int) -> int:
    return int(next(value for value in frame["operators"]
                    if int(value["obj_id"]) == operator_id)["cur_hex"])


def _template(scenario: NativeScenario):
    units = []
    for value in scenario.operators:
        units.append({
            "obj_id": value.operator_id,
            "color": 0 if value.side == "red" else 1,
            "type": 2 if value.operator_type.upper() == "VEHICLE" else 1,
            "sub_type": 0, "name": f"fixture-{value.operator_id}",
            "owner": value.side, "armor": 0, "cur_hex": value.current_hex,
            "cur_pos": 0, "basic_speed": value.basic_speed, "speed": 0,
            "move_state": 0, "stop": 1, "move_path": [],
            "move_to_stop_remain_time": 0, "blood": value.blood,
            "max_blood": value.max_blood,
            "carry_weapon_ids": list(value.weapon_ids),
            "remain_bullet_nums": {"0": 20}, "observe_distance": [],
            "passenger_ids": [], "car": None, "launcher": None,
            "launch_ids": [], "on_board": 0, "close_combat": 0,
            "altitude": value.altitude, "weapon_unfold_state": 1,
            "weapon_cool_time": 0,
        })
    document = {
        "operators": units, "passengers": [],
        "time": {"cur_step": 0, "tick": 1,
                 "max_time": scenario.terminal_rules.max_ticks,
                 "max_step": scenario.terminal_rules.max_ticks, "stage": 2},
        "jm_points": [], "cities": [], "scores": {}, "landmarks": {},
        "terrain_id": scenario.map.map_id, "judge_info": [],
        "role_and_grouping_info": {
            "red": {"side": "red"}, "blue": {"side": "blue"}},
        "communication": [], "scenario_id": scenario.scenario_id,
        "valid_actions": {}, "actions": [],
    }
    return SourceFrameImporter().from_dict(document, source_hash="synthetic-template")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Run Phase 4A.3 minimal GREEN replay")
    value.add_argument("--output", required=True, type=Path)
    value.add_argument("--seed", type=int, default=42)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    report = run_acceptance(args.output, seed=args.seed)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
