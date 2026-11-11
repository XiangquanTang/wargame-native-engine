"""Reproducible tiny-fixture training interface checks, without a learner.

Run with PYTHONPATH pointing at the implementation to verify. Positional paths
select that implementation's repository assets and a fresh output directory.
Only stable observation/catalog and replay identities enter the JSON report;
random session IDs and filesystem paths are deliberately excluded.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from wargame_native_engine.agents import NoOpAgent
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.template import (
    source_frame_template_for_scenario,
)
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment
from wargame_native_engine.training import NativeTrainingEnv
from wargame_native_engine.training.env import NativeTrainingError


def binding_hash(binding) -> str:
    return canonical_hash(
        {
            "observation": binding.observation,
            "actions": binding.action_space,
        }
    )


def verify_case(scenario, output: Path, version: str, side: str) -> dict:
    def factory() -> SourceCompatibleEnvironment:
        return SourceCompatibleEnvironment(
            GameSession(NativeEngineBackend(scenario)),
            template=source_frame_template_for_scenario(scenario),
        )

    run = output / f"{version}-{side}"
    env = NativeTrainingEnv(
        factory,
        controlled_side=side,
        opponent=NoOpAgent("qa-no-op"),
        storage_root=run / "storage",
        max_steps=3,
        decision_interval_ms=1000,
        frame_interval_ms=1000,
        training_interface_version=version,
    )
    try:
        env.reset(seed=42)
        old = env.current_decision_binding
        assert old.observation.side.value.lower() == side
        hashes = [binding_hash(old)]

        # Mutable returned dictionaries must not modify the frozen catalog.
        detached = old.action_space.actions
        if detached:
            detached[0].clear()
        assert env.current_decision_binding.action_space == old.action_space

        steps = []
        for index in range(3):
            binding = env.current_decision_binding
            assert binding.action_space.action_ids
            # Prefer WAIT when represented as a wrapped action, else use the
            # first valid catalog entry. This is a fixed smoke input, no policy.
            action = next(
                (
                    number
                    for number, record in enumerate(binding.action_space.actions)
                    if record.get("action", {}).get("action_type") == "WAIT"
                ),
                0,
            )
            step = env.step_bound_action(action, binding)
            current = env.current_decision_binding
            hashes.append(binding_hash(current))
            steps.append(
                {
                    "reward": step.reward,
                    "terminated": step.terminated,
                    "truncated": step.truncated,
                    "frame": current.observation.frame_index,
                }
            )
            if index == 0:
                try:
                    env.step_bound_action(action, old)
                except NativeTrainingError as error:
                    assert str(error) == "bound_action_snapshot_stale", str(error)
                else:
                    raise AssertionError("stale binding accepted")

        assert steps[-1]["truncated"] and not steps[-1]["terminated"]
        publication = env.publish(run / "published")
        replay = replay_source_archive(scenario, publication.replay_path, strict=True)
        assert replay.matches
        return {
            "version": version,
            "side": side,
            "binding_hashes": hashes,
            "steps": steps,
            "strict_replay_matches": replay.matches,
            "ticks": replay.ticks,
            "expected_world_hash": replay.expected_world_hash,
            "actual_world_hash": replay.actual_world_hash,
            "stale_binding_rejected": True,
            "detached_actions": True,
        }
    finally:
        env.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo_root", type=Path, help="repository containing examples")
    parser.add_argument("output_dir", type=Path, help="fresh replay/storage directory")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    scenario = load_scenario(args.repo_root / "examples/source_movement_scenario.json")
    results = [
        verify_case(scenario, args.output_dir, version, side)
        for version in ("decision-v1", "decision-v2")
        for side in ("red", "blue")
    ]
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
