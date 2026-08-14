#!/usr/bin/env python3
"""Run one loopback-only RED/BLUE decision-window control service."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import tempfile

from scripts.run_minimal_source_replay import DEFAULT_SCENARIO, _template
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.gateway import CommandGateway, SessionManager
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment
from wargame_native_engine.viewer.dual_control_server import (
    create_dual_side_control_server,
)
from wargame_native_engine.viewer.live_control import native_scenario_map_record


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one loopback-only dual-side Native decision service",
    )
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--decision-interval-ms", type=int, default=10_000)
    parser.add_argument("--frame-interval-ms", type=int, default=1_000)
    parser.add_argument("--deadline-seconds", type=float, default=30.0)
    parser.add_argument("--red-token", default="local-red-seat")
    parser.add_argument("--blue-token", default="local-blue-seat")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    scenario = load_scenario(args.scenario)

    def factory() -> SourceCompatibleEnvironment:
        return SourceCompatibleEnvironment(
            GameSession(NativeEngineBackend(scenario)),
            template=_template(scenario),
        )

    with tempfile.TemporaryDirectory(prefix="wargame-dual-control.") as storage:
        manager = SessionManager(factory, storage_root=storage)
        gateway = CommandGateway(
            manager,
            decision_interval_ms=args.decision_interval_ms,
            frame_interval_ms=args.frame_interval_ms,
            deadline_mode="server_monotonic",
            deadline_seconds=args.deadline_seconds,
        )
        red_id, blue_id = "local-red-controller", "local-blue-controller"
        descriptor = gateway.create_session(
            red_controller_id=red_id,
            blue_controller_id=blue_id,
            seed=args.seed,
        )
        server = create_dual_side_control_server(
            gateway,
            session_id=descriptor.session_id,
            red_controller_id=red_id,
            blue_controller_id=blue_id,
            red_token=args.red_token,
            blue_token=args.blue_token,
            map_data=native_scenario_map_record(scenario),
            output_dir=args.output_dir,
            host=args.host,
            port=args.port,
        )
        host, port = server.server_address[:2]
        print(f"RED:  http://{host}:{port}/red?token={args.red_token}", flush=True)
        print(f"BLUE: http://{host}:{port}/blue?token={args.blue_token}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            gateway.close_session(descriptor.session_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
