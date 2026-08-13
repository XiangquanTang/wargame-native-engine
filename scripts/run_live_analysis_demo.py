#!/usr/bin/env python3
"""Run one minimal Source-compatible episode with synchronous live analysis."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.run_minimal_source_replay import (
    DEFAULT_SCENARIO,
    _blue_controller,
    _red_controller,
    _template,
)
from wargame_native_engine.analysis import (
    EightFactorAnalyzer,
    LiveAnalyzedSession,
    LiveAnalysisUpdate,
    analyze_replay_to_jsonl,
)
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.replay import SourceFrameRecorder
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import (
    SourceCompatibleEnvironment,
)
from wargame_native_engine.version import ENGINE_VERSION


def run_demo(
    output_dir: str | Path,
    *,
    seed: int = 42,
    delta_time_seconds: int = 20,
) -> dict[str, object]:
    root = Path(output_dir)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"refusing to overwrite demo output: {root}")
    root.mkdir(parents=True, exist_ok=True)

    scenario = load_scenario(DEFAULT_SCENARIO)
    environment = SourceCompatibleEnvironment(
        GameSession(NativeEngineBackend(scenario)),
        template=_template(scenario),
    )
    analyzer = EightFactorAnalyzer()
    live = LiveAnalyzedSession(environment, analyzer=analyzer)
    update = live.setup(seed)

    native = update.green_frame["extra"]["native"]
    recorder = SourceFrameRecorder(root / "green_frames")
    recorder.start({
        "scenario_id": scenario.scenario_id,
        "scenario_hash": scenario.scenario_hash,
        "terrain_id": update.green_frame["terrain_id"],
        "episode_id": environment.session.session_id,
        "engine_version": ENGINE_VERSION,
        "ruleset_id": native["ruleset_id"],
        "ruleset_version": native["ruleset_version"],
        "ruleset_hash": native["ruleset_hash"],
        "episode_seed": seed,
        "max_ticks": environment.session.max_ticks,
    })
    recorder.record_initial(
        update.green_frame,
        initial_state_hash=environment.initial_state_hash,
    )
    _print_update(update)

    max_steps = scenario.terminal_rules.max_ticks // delta_time_seconds + 2
    for _ in range(max_steps):
        actions = [
            value
            for value in (
                _red_controller(update.source_result.red),
                _blue_controller(update.source_result.blue),
            )
            if value is not None
        ]
        update = live.step(actions, delta_time_seconds=delta_time_seconds)
        step_record = environment.last_step_record
        if step_record is None:
            raise RuntimeError("environment did not expose step record")
        recorder.record_step(update.green_frame, step_record)
        _print_update(update)
        if update.done:
            break

    outcome = environment.session.result().to_dict()
    recorder.finalize(outcome, final_state_hash=outcome["world_hash"])
    replay = recorder.package_zip(root / "replay.zip")
    offline = root / "frame_analysis.native.jsonl"
    analyze_replay_to_jsonl(replay, offline, analyzer=analyzer)
    online_payload = live.analysis_jsonl().encode("utf-8")
    offline_payload = offline.read_bytes()
    match = online_payload == offline_payload
    summary: dict[str, object] = {
        "frame_count": len(live.frame_analyses),
        "online_offline_match": match,
        "replay": str(replay),
        "analysis": str(offline),
        "winner": outcome["winner"],
        "terminal": outcome["terminal"],
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    if not match:
        raise RuntimeError("live and offline canonical analysis JSONL differ")
    return summary


def _print_update(update: LiveAnalysisUpdate) -> None:
    sides = {item.side: item for item in update.frame_analysis.side_analyses}
    print(json.dumps({
        "frame_index": update.frame_index,
        "red_power": sides["red"].effective_power,
        "blue_power": sides["blue"].effective_power,
        "normalized_advantage": (
            update.frame_analysis.advantage.normalized_advantage
        ),
        "label": update.frame_analysis.advantage.label,
    }, ensure_ascii=False, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Run a minimal live GREEN eight-factor analysis demo"
    )
    value.add_argument("--output-dir", required=True, type=Path)
    value.add_argument("--seed", type=int, default=42)
    value.add_argument("--delta-time-seconds", type=int, default=20)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    run_demo(
        args.output_dir,
        seed=args.seed,
        delta_time_seconds=args.delta_time_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
