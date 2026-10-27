#!/usr/bin/env python3
"""Run continuous fixed-tick RED/BLUE browser control on loopback."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from scripts.realtime_run_storage import realtime_run_storage

from wargame_native_engine.operations import ScenarioMatchFactory

DEFAULT_SCENARIO = Path("examples/source_movement_scenario.json")
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
    parser.add_argument("--analysis-model", choices=("source", "native"), default="source",
                        help="Source-compatible factors or explicit Native fact adaptation")
    parser.add_argument("--json", action="store_true", help="print full startup and publication metadata as JSON")
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.tick_interval_ms <= 0:
        raise ValueError("tick_interval_ms must be positive")
    bundle = ScenarioBundle.from_path(args.scenario_bundle)
    if args.map_runtime_data is not None:
        bundle = bundle.with_map_runtime_data(args.map_runtime_data)
    with realtime_run_storage(args.output_dir, overwrite=args.overwrite) as storage:
        session = RealtimeAgentSession(
            ScenarioMatchFactory(
                bundle=bundle,
                storage_root=Path(storage),
                red_controller_id="realtime-local-red",
                blue_controller_id="realtime-local-blue",
                decision_interval_ms=args.tick_interval_ms,
                frame_interval_ms=args.tick_interval_ms,
                analysis_model=args.analysis_model,
            ),
            run_namespace=args.run_namespace,
            tick_interval_ms=args.tick_interval_ms,
            execution_mode=RealtimeExecutionMode.BACKGROUND,
            max_catch_up_ticks_per_batch=args.max_catch_up_ticks_per_batch,
        )
        server = None
        try:
            reset = session.reset(seed=args.seed)
            scenario = bundle.scenario
            server = create_realtime_dual_control_server(
                session,
                red_binding=reset.red_binding,
                blue_binding=reset.blue_binding,
                red_token=args.red_token,
                blue_token=args.blue_token,
                map_data=bundle.render_map_record(),
                weapon_profiles=source_weapon_profiles(scenario.metadata),
                host=args.host,
                port=args.port,
            )
            host, port = server.server_address[:2]
            from scripts.startup_report import print_startup
            print_startup(bundle, scenario, args, host, port)
            session.start_realtime()
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            # Mark the server consumed before close: a failed close must not be
            # retried, but the session still needs its own cleanup attempt.
            closing_server, server = server, None
            closing_server.server_close()
            publication = session.publish(
                args.output_dir,
                truncated=None,
                overwrite=args.overwrite,
                shutdown_timeout_seconds=args.shutdown_timeout_seconds,
                cancellation_reason=RealtimeCancellationReason.USER_STOP,
            )
        except BaseException as primary_error:
            # Cleanup is best-effort on the failure path.  It must never hide
            # the publication/background exception and its causal chain.
            if server is not None:
                try:
                    server.server_close()
                except BaseException as cleanup_error:
                    _note_cleanup_error(primary_error, "server", cleanup_error)
            try:
                session.close(timeout_seconds=args.shutdown_timeout_seconds)
            except BaseException as cleanup_error:
                _note_cleanup_error(primary_error, "session", cleanup_error)
            raise
        else:
            session.close(timeout_seconds=args.shutdown_timeout_seconds)
        publication_record = {
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
        }
        if args.json:
            print(json.dumps(publication_record, ensure_ascii=False, sort_keys=True), flush=True)
        else:
            print(f"SAVED: ticks={publication.tick_count} commands={publication.applied_command_count}", flush=True)
            print(f"REPLAY: {publication.replay_path}", flush=True)
            print(f"OUTPUT: {publication.output_dir}", flush=True)
    return 0


def _note_cleanup_error(
    primary_error: BaseException,
    component: str,
    cleanup_error: BaseException,
) -> None:
    try:
        try:
            message = str(cleanup_error)[:2048]
        except BaseException:
            message = "<exception message unavailable>"
        note = (
            f"realtime {component} cleanup also failed: "
            f"{type(cleanup_error).__name__}: {message}"
        )
        add_note = getattr(primary_error, "add_note", None)
        if callable(add_note):
            add_note(note)
        else:
            print(note, file=sys.stderr, flush=True)
    except BaseException:
        # Failure reporting must not replace the primary launcher exception.
        pass


if __name__ == "__main__":
    raise SystemExit(main())
