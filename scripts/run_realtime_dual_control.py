#!/usr/bin/env python3
"""Run continuous fixed-tick RED/BLUE browser control on loopback."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
from threading import Thread
from scripts.realtime_run_storage import realtime_run_storage

from wargame_native_engine.operations import ScenarioMatchFactory

DEFAULT_SCENARIO = Path("examples/source_movement_scenario.json")
from wargame_native_engine.agents import (
    ManualRealtimeClock,
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
    parser.add_argument("--red-policy", choices=("human", "simple-capture"), default="human")
    parser.add_argument("--blue-policy", choices=("human", "simple-capture"), default="human")
    parser.add_argument("--max-ticks", type=int,
                        help="stop and save after this many ticks (a truncation, not a win)")
    parser.add_argument("--fast", action="store_true",
                        help="bounded deterministic offline run; requires both simple-capture seats and --max-ticks")
    parser.add_argument("--replay-mode", choices=("compact", "legacy-debug"), default="compact",
                        help="Compact lossless replay by default; legacy-debug opts into strict replay/analysis evidence")
    parser.add_argument("--analysis-model", choices=("source", "native"), default="source",
                        help="Source-compatible factors or explicit Native fact adaptation")
    parser.add_argument("--json", action="store_true", help="print full startup and publication metadata as JSON")
    parser.add_argument("--overwrite", action="store_true")
    return parser


def validate_launch_options(args) -> None:
    """Shared by the raw-map launcher before it writes conversion output."""
    if args.tick_interval_ms <= 0 or args.max_catch_up_ticks_per_batch <= 0:
        raise ValueError("tick interval and catch-up batch size must be positive")
    if not 1 <= args.port <= 65535:
        raise ValueError("port must be in 1..65535")
    if not math.isfinite(args.shutdown_timeout_seconds) or args.shutdown_timeout_seconds <= 0:
        raise ValueError("shutdown timeout must be finite and positive")
    if args.max_ticks is not None and args.max_ticks <= 0:
        raise ValueError("max-ticks must be positive")
    if args.fast and (
        args.max_ticks is None or args.red_policy == "human" or args.blue_policy == "human"
    ):
        raise ValueError("--fast requires --max-ticks and both seats using simple-capture")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validate_launch_options(args)
    policy_names = {"red": args.red_policy, "blue": args.blue_policy}
    host_driven = args.max_ticks is not None or any(name != "human" for name in policy_names.values())
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
                replay_mode=args.replay_mode,
            ),
            run_namespace=args.run_namespace,
            tick_interval_ms=args.tick_interval_ms,
            execution_mode=(RealtimeExecutionMode.MANUAL if host_driven else RealtimeExecutionMode.BACKGROUND),
            clock=ManualRealtimeClock() if host_driven else None,
            max_catch_up_ticks_per_batch=args.max_catch_up_ticks_per_batch,
        )
        server = None
        server_thread = None
        policy_report = None
        try:
            reset = session.reset(seed=args.seed)
            scenario = bundle.scenario
            host, port = None, None
            if not args.fast:
                server = create_realtime_dual_control_server(
                    session,
                    red_binding=reset.red_binding,
                    blue_binding=reset.blue_binding,
                    red_token=args.red_token,
                    blue_token=args.blue_token,
                    map_data=bundle.render_map_record(),
                    weapon_profiles=source_weapon_profiles(scenario.metadata),
                    seat_policies=policy_names,
                    host=args.host,
                    port=args.port,
                )
                host, port = server.server_address[:2]
            from scripts.startup_report import print_startup
            print_startup(bundle, scenario, args, host, port)
            reason = RealtimeCancellationReason.USER_STOP
            if host_driven:
                from scripts.rule_policy_host import create_simple_policies, run_policy_loop
                bindings = {"red": reset.red_binding, "blue": reset.blue_binding}
                policies = create_simple_policies(session, bindings, policy_names)
                server_errors = []
                def serve():
                    try:
                        server.serve_forever(poll_interval=0.05)
                    except BaseException as error:
                        server_errors.append(error)
                def check_server():
                    if server_errors:
                        raise RuntimeError("rule_policy_viewer_failed") from server_errors[0]
                    if server_thread is not None and not server_thread.is_alive():
                        raise RuntimeError("rule_policy_viewer_stopped")
                try:
                    if server is not None:
                        server_thread = Thread(target=serve, name="rule-policy-viewer", daemon=True)
                        server_thread.start()
                    reason, policy_report = run_policy_loop(
                        session, bindings, policies, max_ticks=args.max_ticks,
                        paced=not args.fast, check_server=check_server,
                    )
                finally:
                    for policy in policies.values():
                        policy.close()
            else:
                session.start_realtime()
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
            # Mark the server consumed before close: a failed close must not be
            # retried, but the session still needs its own cleanup attempt.
            if server is not None:
                if server_thread is not None:
                    server.shutdown()
                    server_thread.join(args.shutdown_timeout_seconds)
                    if server_thread.is_alive():
                        raise TimeoutError("rule_policy_viewer_shutdown_timeout")
                closing_server, server = server, None
                closing_server.server_close()
            publication = session.publish(
                args.output_dir,
                truncated=None,
                overwrite=args.overwrite,
                shutdown_timeout_seconds=args.shutdown_timeout_seconds,
                cancellation_reason=reason,
            )
            if policy_report is not None:
                from collections import Counter
                policy_report["seat_policies"] = policy_names
                policy_report["command_status_counts"] = dict(Counter(
                    event.status.value for event in session.command_events
                ))
                policy_report["command_errors"] = dict(Counter(
                    event.error_code for event in session.command_events if event.error_code
                ))
                (publication.output_dir / "policy-summary.json").write_text(
                    json.dumps(policy_report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8",
                )
        except BaseException as primary_error:
            # Cleanup is best-effort on the failure path.  It must never hide
            # the publication/background exception and its causal chain.
            if server is not None:
                try:
                    if server_thread is not None:
                        server.shutdown()
                        server_thread.join(args.shutdown_timeout_seconds)
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
            "analysis": None if publication.analysis_path is None else str(publication.analysis_path),
            "replay_mode": args.replay_mode,
            "seat_commands": None if publication.command_path is None else str(publication.command_path),
            "realtime_ticks": None if publication.tick_path is None else str(publication.tick_path),
            "policy_summary": None if policy_report is None else str(publication.output_dir / "policy-summary.json"),
        }
        if args.json:
            print(json.dumps(publication_record, ensure_ascii=False, sort_keys=True), flush=True)
        else:
            print(f"SAVED: ticks={publication.tick_count} commands={publication.applied_command_count}", flush=True)
            print(f"REPLAY ({args.replay_mode}): {publication.replay_path}", flush=True)
            print(f"OUTPUT: {publication.output_dir}", flush=True)
        if policy_report is not None:
            print(f"POLICY SUMMARY: {publication.output_dir / 'policy-summary.json'}", flush=True)
            if policy_report["stop_reason"] == "failure":
                print("POLICY FAILED: execution rejected; stopped and saved partial replay.", file=sys.stderr, flush=True)
                for rejected in policy_report["non_applied_commands_first_100"]:
                    print(json.dumps(rejected, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)
                return 2
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
