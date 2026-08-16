"""Minimal GitHub-source example for one authoritative joint Native tick."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.template import (
    source_frame_template_for_scenario,
)
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.replay import SourceFrameRecorder
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import (
    SourceCompatibleEnvironment,
)


ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "examples/source_movement_scenario.json"


def run_example(output_dir: str | Path, *, seed: int = 42) -> Mapping[str, Any]:
    output = Path(output_dir)
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"refusing to overwrite example output: {output}")
    output.mkdir(parents=True, exist_ok=True)

    scenario = load_scenario(SCENARIO)
    session = GameSession(NativeEngineBackend(scenario))
    environment = SourceCompatibleEnvironment(
        session,
        template=source_frame_template_for_scenario(scenario),
    )
    observations = environment.setup_views(seed)

    recorder = SourceFrameRecorder(output / "green_frames")
    native = observations.green["extra"]["native"]
    recorder.start({
        "scenario_id": scenario.scenario_id,
        "scenario_hash": scenario.scenario_hash,
        "terrain_id": observations.green["terrain_id"],
        "episode_id": session.session_id,
        "engine_version": native["engine_version"],
        "ruleset_id": native["ruleset_id"],
        "ruleset_version": native["ruleset_version"],
        "ruleset_hash": native["ruleset_hash"],
        "episode_seed": seed,
        "max_ticks": session.max_ticks,
    })
    recorder.record_initial(
        observations.green,
        initial_state_hash=environment.initial_state_hash,
    )

    # RED and BLUE choose only from their detached, side-specific operations.
    red_command = _first_wait(observations.red)
    blue_command = _first_wait(observations.blue)
    observations, _ = environment.step_views_ms(
        [red_command, blue_command],
        delta_time_ms=1_000,
    )
    step_record = environment.last_step_record
    if step_record is None:
        raise RuntimeError("example tick did not produce a step record")
    recorder.record_step(observations.green, step_record)

    result = session.result().to_dict()
    published = {**result, "truncated": True, "reason": "example_limit"}
    recorder.finalize(published, final_state_hash=result["world_hash"])
    replay_path = recorder.package_zip(output / "replay.zip")
    verification = replay_source_archive(
        scenario, replay_path, strict=True)
    session.close()

    summary = {
        "scenario_id": scenario.scenario_id,
        "seed": seed,
        "red_visible_operators": len(observations.red["operators"]),
        "blue_visible_operators": len(observations.blue["operators"]),
        "tick": int(observations.green["time"]["tick"]),
        "replay": str(replay_path),
        "strict_replay_matches": verification.matches,
        "final_state_hash": verification.actual_world_hash,
    }
    (output / "example_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def _first_wait(frame: Mapping[str, Any]) -> EngineAction:
    actions = [
        EngineAction.from_dict(record["action"])
        for records in frame["valid_actions"].values()
        for record in records
    ]
    return next(action for action in actions
                if action.action_type is ActionType.WAIT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    print(json.dumps(
        run_example(args.output, seed=args.seed),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
