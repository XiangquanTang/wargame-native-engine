"""Strictly verify a Native source-frame replay with a repository scenario."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)
from wargame_native_engine.io.replay import replay_source_archive


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCENARIO = ROOT / "examples/source_movement_scenario.json"


def verify_replay(
    replay: str | Path,
    *,
    scenario_bundle: str | Path = DEFAULT_SCENARIO,
) -> Mapping[str, Any]:
    bundle = ScenarioBundle.from_path(scenario_bundle)
    verification = replay_source_archive(
        bundle.scenario,
        replay,
        strict=True,
        map_runtime_data=bundle.map_runtime_data,
    )
    archive = SourceReplayArchiveImporter().import_path(replay)
    return {
        "scenario_id": bundle.scenario_identity.scenario_id,
        "matches": verification.matches,
        "expected_world_hash": verification.expected_world_hash,
        "actual_world_hash": verification.actual_world_hash,
        "step_count": len(archive.steps),
        "frame_count": len(archive.frames),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument(
        "--scenario-bundle",
        type=Path,
        default=DEFAULT_SCENARIO,
    )
    args = parser.parse_args(argv)
    result = verify_replay(
        args.replay,
        scenario_bundle=args.scenario_bundle,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result["matches"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
