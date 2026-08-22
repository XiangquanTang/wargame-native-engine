#!/usr/bin/env python3
"""Run deterministic tactical rule Agents on the public close-combat demo."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import json
from pathlib import Path
import tempfile
from typing import Any

from scripts.run_realtime_agent_match import _MatchFactory
from wargame_native_engine.agents import (
    AgentDecisionContext,
    CommandStatus,
    RealtimeAgentSession,
    RealtimeExecutionMode,
    RealtimeLocalAgentRunner,
    SeatDecision,
    SeatOperation,
)
from wargame_native_engine.agents.protocol import AgentResetContext
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.io.replay import replay_source_archive


DEFAULT_SCENARIO = Path("examples/public_close_combat_scenario.json")


class TacticalRuleSeatAgent:
    """Small deterministic controller using only detached seat capabilities.

    The policy prefers legal fire, unfolds a stowed weapon after contact, and
    otherwise advances toward the opposite map edge.  It never receives GREEN
    state or returns low-level action identifiers or client-authored paths.
    """

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.contact_observation_count = 0
        self.contact_ids: set[int] = set()

    def reset(self, context: AgentResetContext) -> None:
        del context
        self.contact_observation_count = 0
        self.contact_ids.clear()

    def act(self, *, context: AgentDecisionContext) -> SeatDecision:
        visible = context.observation.get("visible_enemies", ())
        if isinstance(visible, Sequence) and not isinstance(visible, (str, bytes)):
            contacts = {
                value.get("operator_id")
                for value in visible
                if isinstance(value, Mapping)
                and type(value.get("operator_id")) is int
            }
        else:
            contacts = set()
        if contacts:
            self.contact_observation_count += 1
            self.contact_ids.update(contacts)

        actions = _candidate_operations(context)
        for operation_type in (
            "SHOOT",
            "WEAPON_UNFOLD",
            "STOP_MOVE",
            "MOVE",
            "WEAPON_LOCK",
        ):
            candidates = actions.get(operation_type, ())
            if candidates and (contacts or operation_type in {"MOVE", "WEAPON_LOCK"}):
                return SeatDecision.single(candidates[0])
        return SeatDecision()

    def close(self) -> None:
        return None


def _candidate_operations(
    context: AgentDecisionContext,
) -> dict[str, tuple[SeatOperation, ...]]:
    result: dict[str, list[SeatOperation]] = {}
    controllable = set(context.operation_capabilities.controllable_actor_ids)
    legal_ids = {
        value
        for value in context.observation.get("legal_action_ids", ())
        if isinstance(value, str)
    }
    for record in context.operation_capabilities.frozen_actions:
        if not isinstance(record, Mapping):
            continue
        if legal_ids and record.get("action_id") not in legal_ids:
            continue
        action = record.get("action")
        if not isinstance(action, Mapping) or action.get("side") != context.side:
            continue
        actor_id = action.get("actor_id")
        operation_type = action.get("action_type")
        if type(actor_id) is not int or actor_id not in controllable:
            continue
        if not isinstance(operation_type, str):
            continue
        parameters = action.get("parameters", {})
        if not isinstance(parameters, Mapping):
            continue
        operation = _operation_from_action(
            actor_id, operation_type, action, parameters
        )
        if operation is not None:
            result.setdefault(operation_type, []).append(operation)
    return {
        operation_type: tuple(sorted(values, key=_operation_key))
        for operation_type, values in result.items()
    }


def _operation_from_action(
    actor_id: int,
    operation_type: str,
    action: Mapping[str, Any],
    parameters: Mapping[str, Any],
) -> SeatOperation | None:
    if operation_type == "SHOOT":
        target_id = action.get("target_id")
        if type(target_id) is not int:
            return None
        weapon_id = parameters.get("weapon_id")
        choices = () if type(weapon_id) is not int else ((actor_id, weapon_id),)
        return SeatOperation(
            "SHOOT", (actor_id,), target_id=target_id, weapon_choices=choices
        )
    if operation_type == "MOVE":
        path = parameters.get("path")
        if (
            isinstance(path, Sequence)
            and not isinstance(path, (str, bytes))
            and path
            and type(path[-1]) is int
        ):
            # Preserve the server-issued path endpoint; the product layer
            # replans and revalidates it at the authoritative apply tick.
            destination = path[-1]
            return SeatOperation(
                "MOVE",
                (actor_id,),
                destination_hex=destination,
                movement_mode=str(parameters.get("movement_mode", "NORMAL")),
            )
        return None
    if operation_type in {"STOP_MOVE", "WEAPON_LOCK", "WEAPON_UNFOLD"}:
        return SeatOperation(operation_type, (actor_id,))
    return None


def _operation_key(value: SeatOperation) -> tuple[object, ...]:
    return (
        value.actor_ids,
        -1 if value.target_id is None else value.target_id,
        -1 if value.destination_hex is None else value.destination_hex,
        value.weapon_choices,
    )


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Run the public deterministic tactical Agent showcase"
    )
    value.add_argument("--scenario-bundle", type=Path, default=DEFAULT_SCENARIO)
    value.add_argument("--map-runtime-data", type=Path)
    value.add_argument("--output-dir", type=Path, required=True)
    value.add_argument("--run-namespace", default="public-tactical-agent-demo")
    value.add_argument("--seed", type=int, default=42)
    value.add_argument("--tick-interval-ms", type=int, default=100)
    value.add_argument("--max-ticks", type=int, default=8)
    value.add_argument("--timeout-seconds", type=float, default=60.0)
    value.add_argument("--shutdown-timeout-seconds", type=float, default=60.0)
    value.add_argument("--overwrite", action="store_true")
    return value


def run_demo(args: argparse.Namespace) -> dict[str, object]:
    bundle = ScenarioBundle.from_path(args.scenario_bundle)
    if args.map_runtime_data is not None:
        bundle = bundle.with_map_runtime_data(args.map_runtime_data)
    red_agent = TacticalRuleSeatAgent("public-tactical-red")
    blue_agent = TacticalRuleSeatAgent("public-tactical-blue")
    with tempfile.TemporaryDirectory(prefix="wargame-tactical-agent.") as storage:
        session = RealtimeAgentSession(
            _MatchFactory(
                bundle,
                storage_root=Path(storage),
                tick_interval_ms=args.tick_interval_ms,
            ),
            run_namespace=args.run_namespace,
            tick_interval_ms=args.tick_interval_ms,
            execution_mode=RealtimeExecutionMode.BACKGROUND,
        )
        runner = RealtimeLocalAgentRunner(
            session, red_agent=red_agent, blue_agent=blue_agent
        )
        try:
            result = runner.run(
                args.output_dir,
                seed=args.seed,
                max_ticks=args.max_ticks,
                timeout_seconds=args.timeout_seconds,
                shutdown_timeout_seconds=args.shutdown_timeout_seconds,
                overwrite=args.overwrite,
            )
            events = session.command_events
            applied = [value for value in events if value.status is CommandStatus.APPLIED]
            operation_counts: dict[str, int] = {}
            for event in applied:
                for operation in event.decision.operations:
                    operation_counts[operation.operation_type] = (
                        operation_counts.get(operation.operation_type, 0) + 1
                    )
            publication = result.publication
            strict = replay_source_archive(
                bundle.scenario,
                publication.replay_path,
                strict=True,
                map_runtime_data=bundle.map_runtime_data,
            )
            report: dict[str, object] = {
                "run_namespace": publication.run_namespace,
                "episode_id": publication.episode_id,
                "scenario_id": bundle.scenario_identity.scenario_id,
                "map_id": bundle.map_identity.map_id,
                "tick_count": publication.tick_count,
                "command_count": publication.accepted_command_count,
                "applied_count": publication.applied_command_count,
                "contact_count": len(red_agent.contact_ids | blue_agent.contact_ids),
                "contact_observation_count": (
                    red_agent.contact_observation_count
                    + blue_agent.contact_observation_count
                ),
                "shoot_count": operation_counts.get("SHOOT", 0),
                "operation_counts": dict(sorted(operation_counts.items())),
                "agent_failure_count": len(result.agent_failures),
                "terminal": publication.terminal,
                "truncated": publication.truncated,
                "winner": publication.winner,
                "outcome": publication.outcome,
                "strict_replay_matches": strict.matches,
                "sample_package": str(publication.output_dir),
                "replay": str(publication.replay_path),
            }
        finally:
            session.close()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return report


def main(argv: list[str] | None = None) -> int:
    run_demo(parser().parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
