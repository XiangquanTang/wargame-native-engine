"""A reproducible custom-policy integration, with no model download or training.

Run from the repository root with PYTHONPATH=src:
    python examples/evaluate_custom_agent.py --output /tmp/custom-agent-run

Replace decide() with an LLM/JEV adapter that returns a current action ID.
This uniform catalog sampler is an integration example, not a strong baseline.
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

from wargame_native_engine.evaluation import (
    EpisodeContext,
    load_manifest,
    run_benchmark,
)


class SeededCatalogPolicy:
    def __init__(self, context: EpisodeContext):
        self.random = random.Random(context.seed)

    def decide(self, observation):
        action_ids = observation["action_space"]["action_ids"]
        return self.random.choice(action_ids) if action_ids else None

    def close(self):
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    report = run_benchmark(
        load_manifest(repository / "benchmarks/manifests/smoke.json"),
        args.output,
        SeededCatalogPolicy,
        policy_id="uniform-catalog-example/1.0",
        policy_provenance={"selection": "random.Random(episode_seed).choice"},
    )
    print(report["status"], report["semantic_fingerprint"])
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
