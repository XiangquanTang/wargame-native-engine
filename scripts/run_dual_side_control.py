#!/usr/bin/env python3
"""Run one loopback-only RED/BLUE decision-window control service."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import tempfile

from scripts.run_minimal_source_replay import DEFAULT_SCENARIO
from wargame_native_engine.data import ScenarioBundle, ScenarioCatalog
from wargame_native_engine.operations import WargameFacade
from wargame_native_engine.viewer.dual_control_server import (
    create_dual_side_control_server,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one loopback-only dual-side Native decision service",
    )
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument(
        "--scenario-bundle",
        type=Path,
        help="Canonical native_scenario.json emitted by convert_source_scenario.",
    )
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
    scenario_path = args.scenario_bundle or args.scenario
    bundle = ScenarioBundle.from_path(scenario_path)
    catalog = ScenarioCatalog.from_bundles({
        bundle.scenario_identity.scenario_id: bundle,
    })

    with tempfile.TemporaryDirectory(prefix="wargame-dual-control.") as storage:
        app = WargameFacade(catalog, storage_root=storage)
        red_id, blue_id = "local-red-controller", "local-blue-controller"
        match = app.create_match(
            scenario_id=bundle.scenario_identity.scenario_id,
            red_controller_id=red_id,
            blue_controller_id=blue_id,
            seed=args.seed,
            decision_interval_ms=args.decision_interval_ms,
            frame_interval_ms=args.frame_interval_ms,
            deadline_mode="server_monotonic",
            deadline_seconds=args.deadline_seconds,
        )
        server = create_dual_side_control_server(
            match.gateway,
            session_id=match.session_id,
            red_controller_id=red_id,
            blue_controller_id=blue_id,
            red_token=args.red_token,
            blue_token=args.blue_token,
            map_data=match.map_record,
            output_dir=args.output_dir,
            host=args.host,
            port=args.port,
        )
        host, port = server.server_address[:2]
        conversion = bundle.scenario.metadata.get("source_scenario_conversion", {})
        conversion_mode = (
            conversion.get("conversion_mode")
            if isinstance(conversion, dict)
            else None
        )
        print(
            "SCENARIO: "
            f"{bundle.scenario_identity.scenario_id} "
            f"sha256={bundle.scenario_identity.sha256}",
            flush=True,
        )
        print(
            f"MAP: {bundle.map_identity.map_id} sha256={bundle.map_identity.sha256}",
            flush=True,
        )
        print(f"CONVERSION_MODE: {conversion_mode or 'canonical-native'}", flush=True)
        print(f"RED:  http://{host}:{port}/red?token={args.red_token}", flush=True)
        print(f"BLUE: http://{host}:{port}/blue?token={args.blue_token}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            match.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
