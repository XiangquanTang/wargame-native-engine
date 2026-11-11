"""Compare fixed reset/no-op runtime facts across public non-Map123 fixtures."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from wargame_native_engine.agents.realtime import (
    ManualRealtimeClock,
    RealtimeAgentSession,
)
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.operations import ScenarioMatchFactory


def verify(root: Path, relative: Path, output: Path) -> dict:
    bundle = ScenarioBundle.from_path(root / relative)
    session = RealtimeAgentSession(
        ScenarioMatchFactory(
            bundle=bundle,
            storage_root=output / relative.stem / "storage",
            red_controller_id="qa-red",
            blue_controller_id="qa-blue",
            decision_interval_ms=1000,
            frame_interval_ms=1000,
            replay_mode="legacy-debug",  # This probe explicitly verifies strict re-execution.
        ),
        run_namespace="public-fixture-qa",
        tick_interval_ms=1000,
        clock=ManualRealtimeClock(),
    )
    try:
        reset = session.reset(seed=42)
        bindings = {"red": reset.red_binding, "blue": reset.blue_binding}
        before = {
            side: canonical_hash(session.latest_view(binding).to_record())
            for side, binding in bindings.items()
        }
        session.advance_tick()
        after = {
            side: canonical_hash(session.latest_view(binding).to_record())
            for side, binding in bindings.items()
        }
        facts = canonical_hash(
            [tick.authoritative_record() for tick in session.tick_records]
        )
        publication = session.publish(output / relative.stem / "published")
        replay = replay_source_archive(
            bundle.scenario, publication.replay_path, strict=True
        )
        assert replay.matches
        return {
            "scenario": relative.as_posix(),
            "status": "passed",
            "reset_view_hashes": before,
            "after_view_hashes": after,
            "authoritative_tick_hash": facts,
            "strict_replay_matches": replay.matches,
            "ticks": replay.ticks,
            "expected_world_hash": replay.expected_world_hash,
            "actual_world_hash": replay.actual_world_hash,
        }
    finally:
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo_root", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    examples = args.repo_root / "examples"
    fixtures = sorted(
        [
            *examples.glob("*scenario.json"),
            *(examples / "maps/scenarios").glob("*.json"),
        ]
    )
    results = []
    for fixture in fixtures:
        relative = fixture.relative_to(args.repo_root)
        try:
            result = verify(args.repo_root, relative, args.output_dir)
        except Exception as error:  # noqa: BLE001
            # Harness boundary: report every fixture failure, then exit nonzero.
            # Process-control exceptions remain uncaught.
            result = {
                "scenario": relative.as_posix(),
                "status": "failed",
                "error_type": type(error).__name__,
                "error": str(error),
            }
        results.append(result)
    print(json.dumps(results, indent=2))
    if any(result["status"] != "passed" for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
