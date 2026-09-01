#!/usr/bin/env python3
"""Run continuous fixed-tick RED/BLUE browser control on loopback."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from scripts.run_realtime_agent_match import DEFAULT_SCENARIO, _MatchFactory
from wargame_native_engine.agents import (
    RealtimeAgentSession,
    RealtimeCancellationReason,
    RealtimeExecutionMode,
)
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.viewer.realtime_control_server import (
    create_realtime_dual_control_server,
)
from wargame_native_engine.viewer.human_capabilities import source_weapon_profiles


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run loopback-only continuous Native realtime side control",
    )
    parser.add_argument("--scenario-bundle", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--map-runtime-data", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-namespace", required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--tick-interval-ms", type=int, default=1_000)
    parser.add_argument("--max-catch-up-ticks-per-batch", type=int, default=4)
    parser.add_argument("--shutdown-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8003)
    parser.add_argument("--red-token", default="local-red-seat")
    parser.add_argument("--blue-token", default="local-blue-seat")
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.tick_interval_ms <= 0:
        raise ValueError("tick_interval_ms must be positive")
    bundle = ScenarioBundle.from_path(args.scenario_bundle)
    if args.map_runtime_data is not None:
        bundle = bundle.with_map_runtime_data(args.map_runtime_data)
    with tempfile.TemporaryDirectory(
        prefix="wargame-realtime-web.",
    ) as storage:
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
        reset = session.reset(seed=args.seed)
        server = create_realtime_dual_control_server(
            session,
            red_binding=reset.red_binding,
            blue_binding=reset.blue_binding,
            red_token=args.red_token,
            blue_token=args.blue_token,
            map_data=bundle.render_map_record(),
            weapon_profiles=source_weapon_profiles(bundle.scenario.metadata),
            host=args.host,
            port=args.port,
        )
        host, port = server.server_address[:2]
        print(
            f"SCENARIO: {bundle.scenario_identity.scenario_id} "
            f"sha256={bundle.scenario_identity.sha256}",
            flush=True,
        )
        print(
            f"MAP: {bundle.map_identity.map_id} sha256={bundle.map_identity.sha256}",
            flush=True,
        )
        print(f"CONTROL_MODE: realtime-fixed-tick", flush=True)
        print(f"TICK_INTERVAL_MS: {args.tick_interval_ms}", flush=True)
        print(f"RED:  http://{host}:{port}/red?token={args.red_token}", flush=True)
        print(f"BLUE: http://{host}:{port}/blue?token={args.blue_token}", flush=True)
        print("按 Ctrl+C 停止并发布 Replay、分析与实时命令事件。", flush=True)
        session.start_realtime()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        try:
            publication = session.publish(
                args.output_dir,
                truncated=None,
                overwrite=args.overwrite,
                shutdown_timeout_seconds=args.shutdown_timeout_seconds,
                cancellation_reason=RealtimeCancellationReason.USER_STOP,
            )
            print(json.dumps({
                "run_namespace": publication.run_namespace,
                "episode_id": publication.episode_id,
                "tick_count": publication.tick_count,
                "fact_frame_count": publication.fact_frame_count,
                "accepted_command_count": publication.accepted_command_count,
                "applied_command_count": publication.applied_command_count,
                "terminal": publication.terminal,
                "truncated": publication.truncated,
                "sample_package": str(publication.output_dir),
                "replay": str(publication.replay_path),
                "analysis": str(publication.analysis_path),
                "seat_commands": str(publication.command_path),
                "realtime_ticks": str(publication.tick_path),
            }, ensure_ascii=False, sort_keys=True), flush=True)
        finally:
            session.close(timeout_seconds=args.shutdown_timeout_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
