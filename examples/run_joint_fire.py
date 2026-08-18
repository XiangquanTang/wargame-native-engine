"""Run one public same-pre-state joint-fire match through product operations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.data import ScenarioBundle, ScenarioCatalog
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.operations import ProductOperationService, WargameFacade


ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "examples/joint_fire_scenario.json"


def run_example(output_dir: str | Path, *, seed: int = 1) -> Mapping[str, Any]:
    output = Path(output_dir)
    if output.exists():
        raise FileExistsError(f"refusing to overwrite example output: {output}")

    bundle = ScenarioBundle.from_path(SCENARIO)
    facade = WargameFacade(
        ScenarioCatalog.from_bundles({
            bundle.scenario_identity.scenario_id: bundle,
        }),
        storage_root=output.parent / f".{output.name}-managed",
    )
    match = facade.create_match(
        scenario_id=bundle.scenario_identity.scenario_id,
        red_controller_id="public-joint-fire-red",
        blue_controller_id="public-joint-fire-blue",
        seed=seed,
        decision_interval_ms=1_000,
        frame_interval_ms=1_000,
    )
    try:
        red = ProductOperationService(match.bind_controller(
            "red", controller_id="public-joint-fire-red",
        ))
        blue = ProductOperationService(match.bind_controller(
            "blue", controller_id="public-joint-fire-blue",
        ))
        red_preview = red.preview_operation(
            [1],
            "SHOOT",
            request_id="public-red-preview",
            target_id=2,
            weapon_choices={1: 36},
        )
        blue_preview = blue.preview_operation(
            [2],
            "SHOOT",
            request_id="public-blue-preview",
            target_id=1,
            weapon_choices={2: 36},
        )
        red_result = red.execute_preview(
            red_preview.preview_token,
            request_id="public-red-execute",
        )
        blue_result = blue.execute_preview(
            blue_preview.preview_token,
            request_id="public-blue-execute",
        )
        publication = match.publish(output)
    finally:
        match.close()

    verification = replay_source_archive(
        bundle.scenario,
        publication.replay_path,
        strict=True,
    )
    archive = SourceReplayArchiveImporter().import_path(publication.replay_path)
    final_units = {
        ("red" if unit.color == 0 else "blue"): {
            "operator_id": unit.obj_id,
            "blood": unit.blood,
            "alive": unit.alive,
            "current_hex": unit.current_hex,
        }
        for unit in archive.frames[-1].operators
    }
    summary = {
        "scenario_id": bundle.scenario_identity.scenario_id,
        "seed": seed,
        "red_submit_state": red_result.product_state,
        "blue_advanced": blue_result.advanced,
        "winner": blue_result.winner,
        "outcome": blue_result.outcome,
        "final_units": final_units,
        "replay": str(publication.replay_path),
        "strict_replay_matches": verification.matches,
        "final_state_hash": verification.actual_world_hash,
    }
    (output / "joint_fire_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=1)
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
