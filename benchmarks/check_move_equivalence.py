"""Deterministic scripted MOVE fixture, not a planning policy or speed benchmark."""

import argparse
import json
import tempfile
from pathlib import Path

from benchmark_runtime import session_for

from wargame_native_engine.agents.seat import SeatOperation
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.io.serialization import canonical_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bundle = ScenarioBundle.from_path("examples/public_river_valley_scenario.json")
    with tempfile.TemporaryDirectory() as storage:
        session = session_for(bundle, storage, "scripted-move-equivalence")
        reset = session.reset(seed=42)
        try:
            view = session.latest_view(reset.red_binding)
            receipt = session.submit_operation(
                reset.red_binding,
                observation_version=view.observation_version,
                operation=SeatOperation("MOVE", actor_ids=(101,), destination_hex=405),
            )
            assert receipt.accepted
            views = []
            for _ in range(5):
                session.advance_tick()
                views.append(
                    [
                        session.latest_view(binding).to_record()
                        for binding in (reset.red_binding, reset.blue_binding)
                    ]
                )
            events = [event.to_record() for event in session.command_events]
            result = {
                "fixture": "red 101: 404 -> 405; five 1000ms ticks; seed 42",
                "commands": events,
                "command_hash": canonical_hash(events),
                "tick_facts_hash": canonical_hash(
                    [tick.authoritative_record() for tick in session.tick_records]
                ),
                "all_side_views_hash": canonical_hash(views),
            }
            args.output.write_text(json.dumps(result, indent=2) + "\n")
        finally:
            session.close()


if __name__ == "__main__":
    main()
