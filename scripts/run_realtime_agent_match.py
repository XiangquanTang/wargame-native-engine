#!/usr/bin/env python3
"""Run one fixed-tick, non-blocking local RED/BLUE Agent match."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from wargame_native_engine.agents import (
    RealtimeAgentSession,
    RealtimeExecutionMode,
    RealtimeLocalAgentRunner,
    WaitSeatAgent,
)
from wargame_native_engine.data import ScenarioBundle, ScenarioCatalog
from wargame_native_engine.operations import WargameFacade


DEFAULT_SCENARIO = Path("examples/source_movement_scenario.json")


class _MatchFactory:
    def __init__(
        self,
        bundle: ScenarioBundle,
        *,
        storage_root: Path,
        tick_interval_ms: int,
    ) -> None:
        self.bundle = bundle
        self.storage_root = storage_root
        self.tick_interval_ms = tick_interval_ms

    def __call__(self, seed: int, episode_sequence: int):
        app = WargameFacade(
            ScenarioCatalog.from_bundles({
                self.bundle.scenario_identity.scenario_id: self.bundle,
            }),
            storage_root=self.storage_root / f"episode-{episode_sequence}",
        )
        return app.create_match(
            scenario_id=self.bundle.scenario_identity.scenario_id,
            red_controller_id="realtime-local-red",
            blue_controller_id="realtime-local-blue",
            seed=seed,
            decision_interval_ms=self.tick_interval_ms,
            frame_interval_ms=self.tick_interval_ms,
        )


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Run a Native fixed-tick realtime local Agent match"
    )
    value.add_argument("--scenario-bundle", type=Path, default=DEFAULT_SCENARIO)
    value.add_argument("--map-runtime-data", type=Path)
    value.add_argument("--output-dir", type=Path, required=True)
    value.add_argument("--run-namespace", required=True)
    value.add_argument("--seed", type=int, default=42)
    value.add_argument("--tick-interval-ms", type=int, default=100)
    value.add_argument("--max-ticks", type=int, default=5)
    value.add_argument("--timeout-seconds", type=float, default=60.0)
    value.add_argument("--shutdown-timeout-seconds", type=float, default=120.0)
    value.add_argument("--max-catch-up-ticks-per-batch", type=int, default=4)
    value.add_argument("--red-agent", choices=("wait",), default="wait")
    value.add_argument("--blue-agent", choices=("wait",), default="wait")
    value.add_argument("--overwrite", action="store_true")
    return value


def run_smoke(args: argparse.Namespace) -> dict[str, object]:
    bundle = ScenarioBundle.from_path(args.scenario_bundle)
    if args.map_runtime_data is not None:
        bundle = bundle.with_map_runtime_data(args.map_runtime_data)
    if args.tick_interval_ms <= 0 or args.max_ticks <= 0:
        raise ValueError("tick interval and max ticks must be positive")
    with tempfile.TemporaryDirectory(prefix="wargame-realtime-agent.") as storage:
        session = RealtimeAgentSession(
            _MatchFactory(
                bundle,
                storage_root=Path(storage),
                tick_interval_ms=args.tick_interval_ms,
            ),
            run_namespace=args.run_namespace,
            tick_interval_ms=args.tick_interval_ms,
            execution_mode=RealtimeExecutionMode.BACKGROUND,
            max_catch_up_ticks_per_batch=args.max_catch_up_ticks_per_batch,
        )
        runner = RealtimeLocalAgentRunner(
            session,
            red_agent=WaitSeatAgent("realtime-red-wait"),
            blue_agent=WaitSeatAgent("realtime-blue-wait"),
        )
        try:
            result = runner.run(
                args.output_dir,
                seed=args.seed,
                max_ticks=args.max_ticks,
                timeout_seconds=args.timeout_seconds,
                shutdown_timeout_seconds=args.shutdown_timeout_seconds,
                overwrite=args.overwrite,
            )
            publication = result.publication
            report = {
                "run_namespace": publication.run_namespace,
                "episode_id": publication.episode_id,
                "scenario_id": bundle.scenario_identity.scenario_id,
                "map_id": bundle.map_identity.map_id,
                "tick_interval_ms": args.tick_interval_ms,
                "tick_count": publication.tick_count,
                "fact_frame_count": publication.fact_frame_count,
                "accepted_command_count": publication.accepted_command_count,
                "applied_command_count": publication.applied_command_count,
                "superseded_command_count": publication.superseded_command_count,
                "rejected_command_count": publication.rejected_command_count,
                "cancelled_command_counts": {
                    "terminal": publication.cancelled_terminal_command_count,
                    "truncation": publication.cancelled_truncation_command_count,
                    "runner_limit": publication.cancelled_runner_limit_command_count,
                    "user_stop": publication.cancelled_user_stop_command_count,
                    "failure": publication.cancelled_failure_command_count,
                },
                "terminal": publication.terminal,
                "truncated": publication.truncated,
                "winner": publication.winner,
                "outcome": publication.outcome,
                "agent_failure_count": len(result.agent_failures),
                "agent_inference_duration_ns": {
                    side: list(values)
                    for side, values in result.agent_inference_duration_ns.items()
                },
                "agent_submit_duration_ns": {
                    side: list(values)
                    for side, values in result.agent_submit_duration_ns.items()
                },
                "reset_duration_ns": result.reset_duration_ns,
                "shutdown_duration_ns": result.shutdown_duration_ns,
                "publication_duration_ns": result.publication_duration_ns,
                "sample_package": str(publication.output_dir),
                "replay": str(publication.replay_path),
                "analysis": str(publication.analysis_path),
                "seat_commands": str(publication.command_path),
                "realtime_ticks": str(publication.tick_path),
                "summary": str(publication.summary_path),
            }
        finally:
            session.close()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return report


def main(argv: list[str] | None = None) -> int:
    run_smoke(parser().parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
