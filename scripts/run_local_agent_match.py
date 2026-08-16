#!/usr/bin/env python3
"""Run one deterministic in-process RED/BLUE Agent match."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
from typing import Sequence

from scripts.run_minimal_source_replay import DEFAULT_SCENARIO, _template
from wargame_native_engine.agents import (
    AgentAction,
    AgentResetContext,
    AgentStepContext,
    LocalAgentMatchRunner,
    WaitSeatAgent,
    run_local_agent_match as run_product_agent_match,
)
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.core.action import (
    ActionType,
    ValidAction,
    valid_action_sort_key,
)
from wargame_native_engine.core.observation import SideObservation
from wargame_native_engine.data import ScenarioBundle, ScenarioCatalog
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.operations import WargameFacade
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import (
    SourceCompatibleEnvironment,
)


class _PreferredValidAgent:
    """Demo-only deterministic policy over current native valid actions."""

    def __init__(self, agent_id: str, preferences: Sequence[ActionType]) -> None:
        self.agent_id = agent_id
        self.preferences = tuple(preferences)

    def reset(self, context: AgentResetContext) -> None:
        del context

    def act(
        self,
        *,
        observation: SideObservation,
        valid_actions: Sequence[ValidAction],
        context: AgentStepContext,
    ) -> AgentAction | None:
        del context
        legal = set(observation.legal_action_ids)
        values = tuple(sorted(
            (item for item in valid_actions if item.action_id in legal),
            key=valid_action_sort_key,
        ))
        for action_type in self.preferences:
            matching = tuple(
                item for item in values if item.action.action_type is action_type
            )
            if not matching:
                continue
            if action_type is ActionType.MOVE:
                selected = max(
                    matching,
                    key=lambda item: tuple(item.action.parameters.get("path", ())),
                )
            else:
                selected = matching[0]
            return AgentAction.from_valid_action(selected)
        return None

    def close(self) -> None:
        return None


def run_demo(output_dir: str | Path, *, seed: int = 42):
    scenario = load_scenario(DEFAULT_SCENARIO)
    environment = SourceCompatibleEnvironment(
        GameSession(NativeEngineBackend(scenario)),
        template=_template(scenario),
    )
    red = _PreferredValidAgent(
        "demo-red",
        (ActionType.SHOOT, ActionType.MOVE, ActionType.WAIT),
    )
    blue = _PreferredValidAgent("demo-blue", (ActionType.WAIT,))
    result = LocalAgentMatchRunner(
        environment=environment,
        red_agent=red,
        blue_agent=blue,
        delta_time_seconds=20,
    ).run(output_dir, seed=seed)
    report = {
        "status": result.status,
        "red_agent_id": result.summary["red_agent_id"],
        "blue_agent_id": result.summary["blue_agent_id"],
        "step_count": result.step_count,
        "frame_count": result.frame_count,
        "winner": result.winner,
        "outcome": result.outcome,
        "strict_replay_match": result.strict_replay_match,
        "online_offline_analysis_match": result.online_offline_analysis_match,
        "replay": str(result.replay_path),
        "analysis": str(result.analysis_path),
        "summary": str(result.summary_path),
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return report


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Run one deterministic local RED/BLUE Agent match"
    )
    value.add_argument("--output-dir", required=True, type=Path)
    value.add_argument("--run-namespace", required=True)
    value.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    value.add_argument(
        "--scenario-bundle",
        type=Path,
        help="Canonical native_scenario.json; overrides --scenario.",
    )
    value.add_argument(
        "--map-runtime-data",
        type=Path,
        help="Optional canonical map runtime directory for this map identity.",
    )
    value.add_argument("--seed", type=int, default=42)
    value.add_argument("--decision-interval-ms", type=int, default=10_000)
    value.add_argument("--frame-interval-ms", type=int, default=1_000)
    value.add_argument("--max-decision-windows", type=int, default=3)
    value.add_argument("--red-agent", choices=("wait",), default="wait")
    value.add_argument("--blue-agent", choices=("wait",), default="wait")
    value.add_argument("--overwrite", action="store_true")
    return value


def run_seat_match(args: argparse.Namespace) -> dict[str, object]:
    scenario_path = args.scenario_bundle or args.scenario
    bundle = ScenarioBundle.from_path(scenario_path)
    if args.map_runtime_data is not None:
        bundle = bundle.with_map_runtime_data(args.map_runtime_data)
    catalog = ScenarioCatalog.from_bundles({
        bundle.scenario_identity.scenario_id: bundle,
    })
    with tempfile.TemporaryDirectory(
        prefix="wargame-local-seat-match."
    ) as storage:
        app = WargameFacade(catalog, storage_root=storage)
        match = app.create_match(
            scenario_id=bundle.scenario_identity.scenario_id,
            red_controller_id="local-agent-red",
            blue_controller_id="local-agent-blue",
            seed=args.seed,
            decision_interval_ms=args.decision_interval_ms,
            frame_interval_ms=args.frame_interval_ms,
        )
        try:
            result = run_product_agent_match(
                match,
                red_agent=WaitSeatAgent("local-red-wait"),
                blue_agent=WaitSeatAgent("local-blue-wait"),
                run_namespace=args.run_namespace,
                output_dir=args.output_dir,
                max_decision_windows=args.max_decision_windows,
                overwrite=args.overwrite,
            )
        finally:
            match.close()
    report = {
        "status": "completed" if result.terminal else "truncated",
        "scenario_id": bundle.scenario_identity.scenario_id,
        "map_id": bundle.map_identity.map_id,
        "red_agent": args.red_agent,
        "blue_agent": args.blue_agent,
        "run_namespace": result.run_namespace,
        "episode_id": result.episode_id,
        "decision_window_count": result.decision_window_count,
        "fact_frame_count": result.fact_frame_count,
        "transition_count": result.transition_count,
        "terminal": result.terminal,
        "truncated": result.truncated,
        "winner": result.winner,
        "outcome": result.outcome,
        "strict_replay_match": result.strict_replay_match,
        "online_offline_analysis_match": (
            result.online_offline_analysis_match
        ),
        "replay": str(result.replay_path),
        "analysis": str(result.analysis_path),
        "transitions": str(result.transition_path),
        "summary": str(result.summary_path),
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return report


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    run_seat_match(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
