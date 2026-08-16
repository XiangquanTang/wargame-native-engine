#!/usr/bin/env python3
"""Run one deterministic project-native two-seat training smoke."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from wargame_native_engine.agents import SeatDecision, WaitSeatAgent
from wargame_native_engine.data import ScenarioBundle, ScenarioCatalog
from wargame_native_engine.operations import WargameFacade
from wargame_native_engine.training import NativeMultiSeatEnv


DEFAULT_SCENARIO = Path("examples/source_movement_scenario.json")
SUMMARY_SCHEMA = "native-training-smoke-summary/1.1"


class _MatchFactory:
    def __init__(
        self,
        bundle: ScenarioBundle,
        *,
        storage_root: Path,
        decision_interval_ms: int,
        frame_interval_ms: int,
    ) -> None:
        self.bundle = bundle
        self.storage_root = storage_root
        self.decision_interval_ms = decision_interval_ms
        self.frame_interval_ms = frame_interval_ms

    def __call__(self, seed: int, episode_sequence: int):
        catalog = ScenarioCatalog.from_bundles({
            self.bundle.scenario_identity.scenario_id: self.bundle,
        })
        app = WargameFacade(
            catalog,
            storage_root=self.storage_root / f"episode-{episode_sequence}",
        )
        return app.create_match(
            scenario_id=self.bundle.scenario_identity.scenario_id,
            red_controller_id="training-smoke-red",
            blue_controller_id="training-smoke-blue",
            seed=seed,
            decision_interval_ms=self.decision_interval_ms,
            frame_interval_ms=self.frame_interval_ms,
        )


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Run a deterministic Native RED/BLUE training smoke"
    )
    value.add_argument(
        "--scenario-bundle", type=Path, default=DEFAULT_SCENARIO
    )
    value.add_argument("--map-runtime-data", type=Path)
    value.add_argument("--output-dir", type=Path, required=True)
    value.add_argument("--run-namespace", required=True)
    value.add_argument("--seed", type=int, default=42)
    value.add_argument("--decision-interval-ms", type=int, default=2_000)
    value.add_argument("--frame-interval-ms", type=int, default=1_000)
    value.add_argument("--max-decision-windows", type=int, default=2)
    value.add_argument("--red-agent", choices=("wait",), default="wait")
    value.add_argument("--blue-agent", choices=("wait",), default="wait")
    value.add_argument("--overwrite", action="store_true")
    return value


def run_smoke(args: argparse.Namespace) -> dict[str, object]:
    bundle = ScenarioBundle.from_path(args.scenario_bundle)
    if args.map_runtime_data is not None:
        bundle = bundle.with_map_runtime_data(args.map_runtime_data)
    if args.decision_interval_ms <= 0 or args.frame_interval_ms <= 0:
        raise ValueError("decision/frame intervals must be positive")
    if args.max_decision_windows <= 0:
        raise ValueError("max_decision_windows must be positive")

    with tempfile.TemporaryDirectory(prefix="wargame-training-smoke.") as storage:
        environment = NativeMultiSeatEnv(
            _MatchFactory(
                bundle,
                storage_root=Path(storage),
                decision_interval_ms=args.decision_interval_ms,
                frame_interval_ms=args.frame_interval_ms,
            ),
            run_namespace=args.run_namespace,
            max_decision_windows=args.max_decision_windows,
        )
        agents = {
            "red": WaitSeatAgent("training-smoke-red-wait"),
            "blue": WaitSeatAgent("training-smoke-blue-wait"),
        }
        last = None
        try:
            initial = environment.reset(seed=args.seed)
            for side in ("red", "blue"):
                agents[side].reset(environment.agent_reset_context(
                    side, agents[side].agent_id
                ))
            while environment.decision_window_count < args.max_decision_windows:
                if environment.match.status.terminal:
                    break
                decisions: dict[str, SeatDecision] = {}
                for side in ("red", "blue"):
                    value = agents[side].act(
                        context=environment.decision_context(side)
                    )
                    if not isinstance(value, SeatDecision):
                        raise TypeError("LocalSeatAgent.act must return SeatDecision")
                    decisions[side] = value
                last = environment.step(decisions["red"], decisions["blue"])
                if last.terminated or last.truncated:
                    break

            publication = environment.publish_training_package(
                args.output_dir,
                summary_schema=SUMMARY_SCHEMA,
                outcome=None if last is None else last.outcome,
                summary_extra={
                "red_agent_id": agents["red"].agent_id,
                "blue_agent_id": agents["blue"].agent_id,
                "initial_fact_frame_index": initial.fact_frame_index,
                },
                overwrite=args.overwrite,
            )
            summary = json.loads(
                publication.summary_path.read_text(encoding="utf-8")
            )
            report = {
                **summary,
                "replay": str(publication.replay_path),
                "analysis": str(publication.analysis_path),
                "transitions": str(publication.transition_path),
                "summary": str(publication.summary_path),
                "sample_package": str(publication.output_dir),
            }
        finally:
            for agent in agents.values():
                agent.close()
            environment.close()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return report


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    run_smoke(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
