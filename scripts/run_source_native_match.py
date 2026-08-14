"""Run the verified Source-Scenario Native product acceptance path.

The input is an already converted canonical NativeScenario.  All operations
go through ProductOperationService, SideController, CommandGateway and
GameSession; this script never touches WorldState.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any, Mapping, Sequence

from wargame_native_engine.analysis import analyze_replay_to_jsonl
from wargame_native_engine.data import ScenarioBundle, ScenarioCatalog
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.operations import (
    ProductOperationService,
    WargameFacade,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-bundle", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--decision-interval-ms", type=int, default=60_000)
    parser.add_argument("--frame-interval-ms", type=int, default=60_000)
    parser.add_argument("--max-decision-windows", type=int, default=20)
    parser.add_argument("--move-unit-ids", default="27,28,29")
    parser.add_argument("--move-destination", type=int, default=2940)
    parser.add_argument("--shoot-unit-id", type=int, default=71)
    parser.add_argument("--shoot-staging-hex", type=int, default=3327)
    parser.add_argument("--shoot-target-id", type=int, default=75)
    parser.add_argument("--weapon-id", type=int, default=73)
    return parser


def run_source_native_match(
    scenario_bundle: str | Path,
    output_dir: str | Path,
    *,
    seed: int = 42,
    decision_interval_ms: int = 60_000,
    frame_interval_ms: int = 60_000,
    max_decision_windows: int = 20,
    move_unit_ids: Sequence[int] = (27, 28, 29),
    move_destination: int = 2940,
    shoot_unit_id: int = 71,
    shoot_staging_hex: int = 3327,
    shoot_target_id: int = 75,
    weapon_id: int = 73,
) -> Mapping[str, Any]:
    target = Path(output_dir)
    if target.exists():
        raise FileExistsError(f"acceptance output already exists: {target}")
    if type(max_decision_windows) is not int or max_decision_windows <= 0:
        raise ValueError("max_decision_windows must be a positive exact int")
    actor_ids = _exact_unique_ids(move_unit_ids)
    if len(actor_ids) < 3:
        raise ValueError("real MOVE acceptance requires at least three units")

    bundle_started = perf_counter()
    bundle = ScenarioBundle.from_path(scenario_bundle)
    bundle_elapsed_ms = _elapsed_ms(bundle_started)
    catalog = ScenarioCatalog.from_bundles({
        bundle.scenario_identity.scenario_id: bundle,
    })

    with TemporaryDirectory(prefix="wargame-real-source-match.") as storage:
        app = WargameFacade(catalog, storage_root=storage)
        setup_started = perf_counter()
        match = app.create_match(
            scenario_id=bundle.scenario_identity.scenario_id,
            red_controller_id="source-real-red",
            blue_controller_id="source-real-blue",
            seed=seed,
            decision_interval_ms=decision_interval_ms,
            frame_interval_ms=frame_interval_ms,
        )
        setup_elapsed_ms = _elapsed_ms(setup_started)
        red = match.bind_controller("RED", controller_id="source-real-red")
        blue = match.bind_controller("BLUE", controller_id="source-real-blue")
        red_product = ProductOperationService(red)
        blue_product = ProductOperationService(blue)
        decision_trace: list[Mapping[str, Any]] = []
        operation_started = perf_counter()
        try:
            initial = _positions(red.observe().source_frame, (*actor_ids, shoot_unit_id))
            if len(set(initial[unit_id] for unit_id in actor_ids)) != len(actor_ids):
                raise RuntimeError("MOVE acceptance units must start in distinct hexes")

            move_preview = red_product.preview_operation(
                actor_ids,
                "MOVE",
                request_id="real-group-move-preview",
                destination_hex=move_destination,
            )
            if move_preview.executable_count != len(actor_ids):
                raise RuntimeError("not all selected real units can execute MOVE")
            if min(len(item.path) for item in move_preview.per_unit_results) < 3:
                raise RuntimeError("real MOVE acceptance requires three or more segments")
            _execute_red_then_blue_wait(
                red_product,
                blue_product,
                move_preview.preview_token,
                "real-group-move",
            )
            _append_trace(decision_trace, red, (*actor_ids, shoot_unit_id))
            while (
                not red.observe().terminal
                and any(
                    _position(red.observe().source_frame, unit_id) != move_destination
                    for unit_id in actor_ids
                )
            ):
                _wait_both(red_product, blue_product, "group-arrival")
                _append_trace(decision_trace, red, (*actor_ids, shoot_unit_id))
                _enforce_window_limit(decision_trace, max_decision_windows)

            if red.observe().terminal:
                raise RuntimeError("scenario terminated before SHOOT staging")
            shooter_preview = red_product.preview_operation(
                (shoot_unit_id,),
                "MOVE",
                request_id="real-shooter-move-preview",
                destination_hex=shoot_staging_hex,
            )
            if shooter_preview.executable_count != 1:
                raise RuntimeError("real shooter cannot reach the explicit staging hex")
            _execute_red_then_blue_wait(
                red_product,
                blue_product,
                shooter_preview.preview_token,
                "real-shooter-move",
            )
            _append_trace(decision_trace, red, (*actor_ids, shoot_unit_id))

            shoot_action = _matching_shoot(
                red,
                shoot_unit_id,
                shoot_target_id,
                weapon_id,
            )
            while not red.observe().terminal and shoot_action is None:
                _wait_both(red_product, blue_product, "shoot-ready")
                _append_trace(decision_trace, red, (*actor_ids, shoot_unit_id))
                _enforce_window_limit(decision_trace, max_decision_windows)
                shoot_action = _matching_shoot(
                    red,
                    shoot_unit_id,
                    shoot_target_id,
                    weapon_id,
                )
            if shoot_action is None:
                raise RuntimeError("real scenario did not produce the required legal SHOOT")

            shoot_preview = red_product.preview_operation(
                (shoot_unit_id,),
                "SHOOT",
                request_id="real-shoot-preview",
                target_id=shoot_target_id,
                weapon_choices={shoot_unit_id: weapon_id},
            )
            if shoot_preview.executable_count != 1:
                raise RuntimeError("frozen real SHOOT did not survive product preview")
            _execute_red_then_blue_wait(
                red_product,
                blue_product,
                shoot_preview.preview_token,
                "real-shoot",
            )
            _append_trace(decision_trace, red, (*actor_ids, shoot_unit_id))

            while not red.observe().terminal and len(decision_trace) < max_decision_windows:
                _wait_both(red_product, blue_product, "terminal-wait")
                _append_trace(decision_trace, red, (*actor_ids, shoot_unit_id))

            descriptor = match.status
            operation_elapsed_ms = _elapsed_ms(operation_started)
            publication_started = perf_counter()
            publication = match.publish(
                target,
                truncated=not descriptor.terminal,
            )
            publication_elapsed_ms = _elapsed_ms(publication_started)
        finally:
            match.close()

    archive = SourceReplayArchiveImporter().import_path(publication.replay_path)
    strict_match = None
    if publication.terminal:
        strict_match = replay_source_archive(
            bundle.scenario,
            publication.replay_path,
            strict=True,
        ).matches
    offline_path = target / "frame_analysis.offline.jsonl"
    offline_started = perf_counter()
    analyze_replay_to_jsonl(publication.replay_path, offline_path)
    offline_elapsed_ms = _elapsed_ms(offline_started)
    online_offline = publication.analysis_path.read_bytes() == offline_path.read_bytes()
    move_paths = _recorded_move_paths(archive.steps, actor_ids)
    shoot_record = _recorded_shoot(
        archive.steps, shoot_unit_id, shoot_target_id, weapon_id
    )
    report = {
        "schema": "real-source-native-match-acceptance/1.0",
        "status": "passed" if strict_match is True and online_offline else "incomplete",
        "native_rules_equivalence": False,
        "scenario": {
            "scenario_id": bundle.scenario_identity.scenario_id,
            "scenario_sha256": bundle.scenario_identity.sha256,
            "map_id": bundle.map_identity.map_id,
            "map_sha256": bundle.map_identity.sha256,
            "conversion_mode": _conversion_mode(bundle),
            "operator_count": len(bundle.scenario.operators),
            "red_count": sum(item.side == "red" for item in bundle.scenario.operators),
            "blue_count": sum(item.side == "blue" for item in bundle.scenario.operators),
        },
        "timing": {
            "decision_interval_ms": decision_interval_ms,
            "frame_interval_ms": frame_interval_ms,
            "bundle_load_ms": bundle_elapsed_ms,
            "match_setup_ms": setup_elapsed_ms,
            "operation_runtime_ms": operation_elapsed_ms,
            "publication_ms": publication_elapsed_ms,
            "offline_analysis_ms": offline_elapsed_ms,
        },
        "move": {
            "unit_ids": list(actor_ids),
            "initial_positions": {str(key): value for key, value in initial.items()},
            "destination_hex": move_destination,
            "preview": move_preview.to_record(),
            "recorded_paths": move_paths,
            "frame_positions": _archive_positions(archive.frames, actor_ids),
        },
        "shoot": {
            "actor_id": shoot_unit_id,
            "staging_hex": shoot_staging_hex,
            "target_id": shoot_target_id,
            "weapon_id": weapon_id,
            "staging_preview": shooter_preview.to_record(),
            "preview": shoot_preview.to_record(),
            "recorded_action": shoot_record,
            "judge_info": _shoot_judge_info(archive.frames, shoot_unit_id),
        },
        "runtime": {
            "terminal": publication.terminal,
            "truncated": publication.truncated,
            "winner": archive.manifest.get("winner"),
            "termination_reason": archive.manifest.get("termination_reason"),
            "frame_count": publication.frame_count,
            "step_count": publication.step_count,
            "decision_window_count": len(decision_trace),
            "final_sim_time_ms": (
                0 if not archive.steps else archive.steps[-1].get("sim_time_ms")
            ),
            "decision_trace": decision_trace,
        },
        "evidence": {
            "replay_path": "replay.zip",
            "replay_sha256": _sha256(publication.replay_path),
            "analysis_path": "frame_analysis.native.jsonl",
            "analysis_sha256": _sha256(publication.analysis_path),
            "offline_analysis_path": "frame_analysis.offline.jsonl",
            "offline_analysis_sha256": _sha256(offline_path),
            "control_audit_path": "control_audit.native.jsonl",
            "control_audit_sha256": (
                None
                if publication.control_audit_path is None
                else _sha256(publication.control_audit_path)
            ),
            "strict_replay_match": strict_match,
            "online_offline_analysis_match": online_offline,
        },
    }
    report_path = target / "real_scenario_runtime.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def _execute_red_then_blue_wait(
    red: ProductOperationService,
    blue: ProductOperationService,
    red_preview_token: str,
    request_prefix: str,
) -> None:
    red.execute_preview(
        red_preview_token,
        request_id=f"{request_prefix}:red-execute",
    )
    wait = blue.preview_operation(
        (),
        "WAIT",
        request_id=f"{request_prefix}:blue-preview",
        whole_side_wait=True,
    )
    blue.execute_preview(
        wait.preview_token,
        request_id=f"{request_prefix}:blue-execute",
    )


def _wait_both(
    red: ProductOperationService,
    blue: ProductOperationService,
    purpose: str,
) -> None:
    window = red.controller.window().decision_window_index
    prefix = f"{purpose}:{window}"
    wait = red.preview_operation(
        (), "WAIT", request_id=f"{prefix}:red-preview", whole_side_wait=True
    )
    red.execute_preview(wait.preview_token, request_id=f"{prefix}:red-execute")
    wait = blue.preview_operation(
        (), "WAIT", request_id=f"{prefix}:blue-preview", whole_side_wait=True
    )
    blue.execute_preview(wait.preview_token, request_id=f"{prefix}:blue-execute")


def _append_trace(
    trace: list[Mapping[str, Any]], controller: Any, unit_ids: Sequence[int]
) -> None:
    view = controller.observe()
    window = controller.window()
    completed_index = (
        window.decision_window_index - 1
        if window.lifecycle_state == "OPEN"
        else window.decision_window_index
    )
    trace.append({
        "decision_window_index": completed_index,
        "frame_index": view.frame_index,
        "sim_time_ms": view.sim_time_ms,
        "positions": {
            str(key): value
            for key, value in _positions(view.source_frame, unit_ids).items()
        },
        "terminal": view.terminal,
    })


def _enforce_window_limit(trace: Sequence[object], maximum: int) -> None:
    if len(trace) >= maximum:
        raise RuntimeError("real scenario acceptance exceeded max decision windows")


def _matching_shoot(controller: Any, actor: int, target: int, weapon: int):
    return next((
        item
        for item in controller.observe().valid_actions
        if item.get("action", {}).get("action_type") == "SHOOT"
        and item.get("action", {}).get("actor_id") == actor
        and item.get("action", {}).get("target_id") == target
        and item.get("action", {}).get("parameters", {}).get("weapon_id") == weapon
    ), None)


def _position(frame: Mapping[str, Any], unit_id: int) -> int:
    return _positions(frame, (unit_id,))[unit_id]


def _positions(frame: Mapping[str, Any], unit_ids: Sequence[int]) -> dict[int, int]:
    wanted = set(unit_ids)
    values = {
        item.get("obj_id"): item.get("cur_hex")
        for item in (*frame.get("operators", ()), *frame.get("passengers", ()))
        if isinstance(item, Mapping) and item.get("obj_id") in wanted
    }
    if set(values) != wanted or any(type(value) is not int for value in values.values()):
        raise RuntimeError("side frame does not contain required friendly unit positions")
    return {int(key): int(value) for key, value in values.items()}


def _archive_positions(frames: Sequence[Any], unit_ids: Sequence[int]):
    return {
        str(unit_id): [
            {
                "frame_index": index,
                "cur_step": frame.time.cur_step,
                "hex": next(item.current_hex for item in frame.all_units if item.obj_id == unit_id),
            }
            for index, frame in enumerate(frames)
        ]
        for unit_id in unit_ids
    }


def _recorded_move_paths(steps: Sequence[Mapping[str, Any]], unit_ids: Sequence[int]):
    wanted = set(unit_ids)
    result = {}
    for step in steps:
        for action in step.get("ordered_actions", ()):
            actor = action.get("actor_id")
            if action.get("action_type") == "MOVE" and actor in wanted:
                result[str(actor)] = {
                    "frame_index": step.get("frame_index"),
                    "sim_time_ms": step.get("sim_time_ms"),
                    "path": list(action.get("parameters", {}).get("path", ())),
                }
    if set(result) != {str(item) for item in wanted}:
        raise RuntimeError("Replay did not record every accepted real MOVE path")
    return result


def _recorded_shoot(
    steps: Sequence[Mapping[str, Any]], actor: int, target: int, weapon: int
):
    for step in steps:
        for action in step.get("ordered_actions", ()):
            if (
                action.get("action_type") == "SHOOT"
                and action.get("actor_id") == actor
                and action.get("target_id") == target
                and action.get("parameters", {}).get("weapon_id") == weapon
            ):
                return {
                    "frame_index": step.get("frame_index"),
                    "sim_time_ms": step.get("sim_time_ms"),
                    "action": action,
                }
    raise RuntimeError("Replay did not contain the accepted real SHOOT")


def _shoot_judge_info(frames: Sequence[Any], actor: int):
    for index, frame in enumerate(frames):
        if any(
            isinstance(action, Mapping)
            and action.get("action_type") == "SHOOT"
            and action.get("actor_id") == actor
            for action in frame.actions
        ):
            return {"frame_index": index, "judge_info": frame.judge_info}
    raise RuntimeError("GREEN frame did not contain SHOOT adjudication evidence")


def _conversion_mode(bundle: ScenarioBundle) -> str | None:
    value = bundle.scenario.metadata.get("source_scenario_conversion")
    return value.get("conversion_mode") if isinstance(value, Mapping) else None


def _exact_unique_ids(values: Sequence[int]) -> tuple[int, ...]:
    result = tuple(values)
    if any(type(value) is not int or value < 0 for value in result):
        raise ValueError("unit ids must be non-negative exact ints")
    if len(set(result)) != len(result):
        raise ValueError("unit ids must be unique")
    return result


def _elapsed_ms(start: float) -> float:
    return round((perf_counter() - start) * 1000, 3)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_source_native_match(
        args.scenario_bundle,
        args.output_dir,
        seed=args.seed,
        decision_interval_ms=args.decision_interval_ms,
        frame_interval_ms=args.frame_interval_ms,
        max_decision_windows=args.max_decision_windows,
        move_unit_ids=tuple(int(value) for value in args.move_unit_ids.split(",")),
        move_destination=args.move_destination,
        shoot_unit_id=args.shoot_unit_id,
        shoot_staging_hex=args.shoot_staging_hex,
        shoot_target_id=args.shoot_target_id,
        weapon_id=args.weapon_id,
    )
    print(json.dumps({
        "status": report["status"],
        "scenario": report["scenario"],
        "runtime": report["runtime"],
        "evidence": report["evidence"],
        "report_path": str(Path(args.output_dir) / "real_scenario_runtime.json"),
    }, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
