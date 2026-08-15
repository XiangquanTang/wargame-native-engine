#!/usr/bin/env python3
"""Run one deterministic project-native two-seat training smoke."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

from wargame_native_engine.agents import SeatDecision, WaitSeatAgent
from wargame_native_engine.analysis import EightFactorAnalyzer, analyze_replay_to_jsonl
from wargame_native_engine.compat.source_frame.serialization import (
    canonical_source_json,
)
from wargame_native_engine.data import ScenarioBundle, ScenarioCatalog
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.serialization import file_sha256
from wargame_native_engine.operations import WargameFacade
from wargame_native_engine.training import NativeMultiSeatEnv


DEFAULT_SCENARIO = Path("examples/source_movement_scenario.json")
SUMMARY_NAME = "match_summary.native.json"
SUMMARY_SCHEMA = "native-training-smoke-summary/1.0"


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

            publication = environment.publish(
                args.output_dir, overwrite=args.overwrite
            )
            transition_path = environment.write_transitions(
                publication.output_dir / "decision_transitions.native.jsonl"
            )
            archive = SourceReplayArchiveImporter().import_path(
                publication.replay_path
            )
            verification = replay_source_archive(
                bundle.scenario,
                publication.replay_path,
                strict=True,
                map_runtime_data=bundle.map_runtime_data,
            )
            if not verification.matches:
                raise RuntimeError("strict replay did not match")
            with tempfile.TemporaryDirectory(
                prefix="wargame-training-offline-analysis."
            ) as analysis_storage:
                offline = Path(analysis_storage) / "frame_analysis.native.jsonl"
                analyze_replay_to_jsonl(
                    publication.replay_path,
                    offline,
                    analyzer=EightFactorAnalyzer(),
                )
                analysis_match = (
                    offline.read_bytes() == publication.analysis_path.read_bytes()
                )
            if not analysis_match:
                raise RuntimeError("online and offline analysis JSONL differ")

            manifest = archive.manifest
            terminal = bool(manifest.get("termination", False))
            truncated = bool(manifest.get("truncated", False))
            summary = {
                "summary_schema": SUMMARY_SCHEMA,
                "status": "completed" if terminal else "truncated",
                "seed": args.seed,
                "scenario_id": bundle.scenario_identity.scenario_id,
                "scenario_hash": bundle.scenario_identity.sha256,
                "map_id": bundle.map_identity.map_id,
                "map_hash": bundle.map_identity.sha256,
                "red_agent_id": agents["red"].agent_id,
                "blue_agent_id": agents["blue"].agent_id,
                "decision_window_count": environment.decision_window_count,
                "transition_count": len(environment.transitions),
                "fact_frame_count": publication.frame_count,
                "step_count": publication.step_count,
                "terminal": terminal,
                "truncated": truncated,
                "winner": manifest.get("winner"),
                "outcome": None if last is None else last.outcome,
                "final_state_hash": manifest.get("final_state_hash"),
                "replay_sha256": file_sha256(publication.replay_path),
                "analysis_sha256": file_sha256(publication.analysis_path),
                "transitions_sha256": file_sha256(transition_path),
                "strict_replay_match": True,
                "online_offline_analysis_match": True,
                "initial_fact_frame_index": initial.fact_frame_index,
            }
            summary_path = publication.output_dir / SUMMARY_NAME
            _atomic_write(
                summary_path,
                (canonical_source_json(summary) + "\n").encode("utf-8"),
            )
            report = {
                **summary,
                "replay": str(publication.replay_path),
                "analysis": str(publication.analysis_path),
                "transitions": str(transition_path),
                "summary": str(summary_path),
            }
        finally:
            for agent in agents.values():
                agent.close()
            environment.close()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return report


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    run_smoke(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
