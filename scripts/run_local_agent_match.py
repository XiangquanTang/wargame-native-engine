#!/usr/bin/env python3
"""Run one deterministic in-process RED/BLUE Agent match."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from scripts.run_minimal_source_replay import DEFAULT_SCENARIO, _template
from wargame_native_engine.agents import (
    AgentAction,
    AgentResetContext,
    AgentStepContext,
    LocalAgentMatchRunner,
)
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.core.action import (
    ActionType,
    ValidAction,
    valid_action_sort_key,
)
from wargame_native_engine.core.observation import SideObservation
from wargame_native_engine.io.scenario import load_scenario
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
    value.add_argument("--seed", type=int, default=42)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    run_demo(args.output_dir, seed=args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
