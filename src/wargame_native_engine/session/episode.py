from __future__ import annotations

from dataclasses import dataclass
import time
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.agents.protocol import (
    Agent, AgentAction, AgentResetContext,
)
from wargame_native_engine.io.replay import ReplayWriter
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.version import RULESET_VERSION


def make_run_id(*, scenario_hash: str, seed: int,
                agents: Mapping[str, str], max_ticks: int) -> str:
    return "run-" + canonical_hash({
        "scenario_hash": scenario_hash, "seed": int(seed),
        "agents": dict(agents), "max_ticks": int(max_ticks),
        "ruleset_version": RULESET_VERSION,
    })[:20]


@dataclass(frozen=True, slots=True)
class EpisodeOutcome:
    run_id: str
    output_dir: str
    result: Mapping[str, Any]
    runtime_seconds: float
    closed: bool

    def to_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "output_dir": self.output_dir,
                "result": dict(self.result),
                "runtime_seconds": self.runtime_seconds,
                "closed": self.closed}


class EpisodeRunner:
    def __init__(self, session: GameSession, *, agents: Mapping[str, Agent],
                 output_dir: str | Path,
                 project_commit: str | None = None) -> None:
        self.session = session
        self.agents = dict(agents)
        self.output_dir = Path(output_dir)
        self.project_commit = project_commit
        if set(self.agents) != set(session.scenario.side_order):
            raise ValueError("agents must exactly match scenario sides")

    def run(self, seed: int = 42) -> EpisodeOutcome:
        started = time.monotonic()
        agent_ids = {side: self.agents[side].agent_id
                     for side in self.session.scenario.side_order}
        max_ticks = self.session.max_ticks
        run_id = make_run_id(
            scenario_hash=self.session.scenario.scenario_hash, seed=seed,
            agents=agent_ids, max_ticks=max_ticks,
        )
        writer: ReplayWriter | None = None
        failure: BaseException | None = None
        result_value: Mapping[str, Any] = {}
        try:
            self.session.reset(seed)
            writer = ReplayWriter(
                self.output_dir, run_id=run_id,
                scenario=self.session.scenario, seed=seed,
                agents=agent_ids, max_ticks=max_ticks,
                project_commit=self.project_commit,
            )
            writer.record_initial_state(self.session.snapshot())
            for side in self.session.scenario.side_order:
                self.agents[side].reset(AgentResetContext(
                    self.session.session_id, self.session.scenario.scenario_id,
                    side, seed, RULESET_VERSION,
                ))
            while not self.session.is_terminal():
                batch: dict[str, AgentAction | None] = {}
                for side in self.session.current_seats():
                    observation = self.session.observe(side)
                    valid = self.session.valid_actions(side)
                    selected = self.agents[side].act(
                        observation=observation, valid_actions=valid,
                        context=self.session.agent_context(side),
                    )
                    if selected is not None:
                        legal = {item.action_id for item in valid}
                        if selected.valid_action_id not in legal:
                            raise ValueError(f"agent_action_not_valid:{side}")
                    batch[side] = selected
                step = self.session.step(batch)
                writer.record_step(step, self.session.snapshot())
            result_value = self.session.result().to_dict()
        except BaseException as exc:
            failure = exc
        finally:
            for agent in self.agents.values():
                try:
                    agent.close()
                except Exception:
                    pass
            self.session.close()
        runtime = time.monotonic() - started
        if writer is not None:
            if failure is None:
                summary = {"run_id": run_id, "result": result_value,
                           "runtime_seconds": runtime, "closed": self.session.closed}
                writer.finalize(summary, status="complete")
            else:
                summary = {"run_id": run_id, "result": result_value,
                           "runtime_seconds": runtime, "closed": self.session.closed,
                           "failure": {"type": type(failure).__name__,
                                       "message": str(failure)}}
                writer.finalize(summary, status="failed")
        if failure is not None:
            raise failure
        return EpisodeOutcome(run_id, str(self.output_dir), result_value,
                              runtime, self.session.closed)
