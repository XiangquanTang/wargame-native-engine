"""Primary local-Agent runner over the product Operations Facade."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import tempfile
from typing import Sequence

from wargame_native_engine.agents.authorization import (
    freeze_valid_actions,
    resolve_agent_selection,
)
from wargame_native_engine.agents.protocol import Agent, AgentAction
from wargame_native_engine.analysis import EightFactorAnalyzer, analyze_replay_to_jsonl
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.operations.facade import MatchHandle


@dataclass(frozen=True, slots=True)
class OperationAgentMatchResult:
    output_dir: Path
    terminal: bool
    truncated: bool
    decision_window_count: int
    frame_count: int
    strict_replay_match: bool | None
    online_offline_analysis_match: bool


class OperationAgentMatchRunner:
    """Drive Agents through SideController and the existing Gateway only."""

    def __init__(
        self,
        match: MatchHandle,
        *,
        red_agent: Agent,
        blue_agent: Agent,
    ) -> None:
        self.match = match
        self.agents = {"red": red_agent, "blue": blue_agent}
        self.controllers = {
            "red": match.bind_controller(
                "red", controller_id=match.red_controller_id
            ),
            "blue": match.bind_controller(
                "blue", controller_id=match.blue_controller_id
            ),
        }

    def run(
        self,
        output_dir: str | Path,
        *,
        max_decision_windows: int | None = None,
        overwrite: bool = False,
    ) -> OperationAgentMatchResult:
        if max_decision_windows is not None and (
            type(max_decision_windows) is not int or max_decision_windows <= 0
        ):
            raise ValueError("max_decision_windows must be positive or None")
        manager = self.match.gateway.manager
        try:
            for side in ("red", "blue"):
                agent = self.agents[side]
                context = manager.agent_reset_context(
                    self.match.session_id, side, agent.agent_id
                )
                agent.reset(deepcopy(context))
            processed = 0
            while not manager.descriptor(self.match.session_id).terminal:
                if (
                    max_decision_windows is not None
                    and processed >= max_decision_windows
                ):
                    break
                selections = {
                    side: self._action_ids(side)
                    for side in ("red", "blue")
                }
                window = self.controllers["red"].window().decision_window_index
                for side in ("red", "blue"):
                    controller = self.controllers[side]
                    controller.clear_staged()
                    for action_id in selections[side]:
                        controller.stage_action(
                            action_id, expected_window=window
                        )
                for side in ("red", "blue"):
                    self.controllers[side].submit_batch(
                        request_id=f"agent:{processed}:{side}",
                        expected_window=window,
                    )
                processed += 1
            descriptor = manager.descriptor(self.match.session_id)
            truncated = not descriptor.terminal
            publication = self.match.gateway.publish_session(
                self.match.session_id,
                self.match.red_controller_id,
                str(output_dir),
                truncated=truncated,
                overwrite=overwrite,
            )
            strict_match = None
            if descriptor.terminal:
                strict_match = replay_source_archive(
                    self.match.bundle.scenario,
                    publication.replay_path,
                    strict=True,
                ).matches
            with tempfile.TemporaryDirectory(
                prefix="wargame-operation-agent-analysis."
            ) as temporary:
                offline = Path(temporary) / "analysis.jsonl"
                analyze_replay_to_jsonl(
                    publication.replay_path,
                    offline,
                    analyzer=EightFactorAnalyzer(),
                )
                online_offline = (
                    publication.analysis_path.read_bytes() == offline.read_bytes()
                )
            return OperationAgentMatchResult(
                publication.output_dir,
                descriptor.terminal,
                truncated,
                processed,
                publication.frame_count,
                strict_match,
                online_offline,
            )
        finally:
            for agent in self.agents.values():
                try:
                    agent.close()
                except Exception:
                    pass

    def _action_ids(self, side: str) -> tuple[str, ...]:
        manager = self.match.gateway.manager
        agent = self.agents[side]
        observation, valid_actions, context = manager.agent_step_input(
            self.match.session_id, side, agent.agent_id
        )
        frozen = freeze_valid_actions(side, valid_actions)
        batch_method = getattr(agent, "act_batch", None)
        if callable(batch_method):
            raw = batch_method(
                observation=deepcopy(observation),
                valid_actions=deepcopy(valid_actions),
                context=deepcopy(context),
            )
            if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
                raise TypeError("Agent act_batch must return a sequence")
            selected = tuple(raw)
        else:
            value = agent.act(
                observation=deepcopy(observation),
                valid_actions=deepcopy(valid_actions),
                context=deepcopy(context),
            )
            selected = () if value is None else (value,)
        result = []
        for value in selected:
            if not isinstance(value, AgentAction):
                raise TypeError("Agent selections must be AgentAction values")
            resolved = resolve_agent_selection(side, value, frozen)
            if resolved is None:
                continue
            result.append(value.valid_action_id)
        return tuple(result)
