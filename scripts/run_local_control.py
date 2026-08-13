#!/usr/bin/env python3
"""Run one loopback-only human-vs-NoOp Native control session."""

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
from wargame_native_engine.session.source_environment import (
    SourceCompatibleEnvironment,
)
from wargame_native_engine.viewer.live_control import native_scenario_map_record
from wargame_native_engine.viewer.live_server import create_live_control_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one local side-observation Native control page",
    )
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--perspective", choices=("red", "blue"), default="red")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    scenario = load_scenario(args.scenario)

    def environment_factory() -> SourceCompatibleEnvironment:
        return SourceCompatibleEnvironment(
            GameSession(NativeEngineBackend(scenario)),
            template=_template(scenario),
        )

    with tempfile.TemporaryDirectory(prefix="wargame-live-control.") as storage:
        manager = SessionManager(environment_factory, storage_root=storage)
        gateway = CommandGateway(manager)
        red_id = "local-red-controller"
        blue_id = "local-blue-controller"
        descriptor = gateway.create_session(
            red_controller_id=red_id,
            blue_controller_id=blue_id,
            seed=args.seed,
        )
        selected_id = red_id if args.perspective == "red" else blue_id
        opponent_id = blue_id if args.perspective == "red" else red_id
        server = create_live_control_server(
            gateway,
            session_id=descriptor.session_id,
            controller_id=selected_id,
            opponent_controller_id=opponent_id,
            perspective=args.perspective,
            map_data=native_scenario_map_record(scenario),
            output_dir=args.output_dir,
            host=args.host,
            port=args.port,
        )
        host, port = server.server_address[:2]
        print(
            f"Native local control: http://{host}:{port}/ "
            f"({args.perspective.upper()} vs NoOp)",
            flush=True,
        )
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
