from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.io.importers.historical_replay import (
    EvidenceLevel, HistoricalAction, HistoricalReplay,
)
from wargame_native_engine.io.importers.historical_scenario import (
    HistoricalScenarioBuilder,
)
from wargame_native_engine.rules.config import Ruleset


@dataclass(frozen=True, slots=True)
class ReplayDrivenResult:
    mode: str
    scenario_hash: str
    ruleset_hash: str
    deployment_tick_offset: int
    frames: tuple[Mapping[str, Any], ...]
    events: tuple[Mapping[str, Any], ...]
    action_mappings: tuple[Mapping[str, Any], ...]
    conflicts: tuple[Mapping[str, Any], ...]
    summary: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "scenario_hash": self.scenario_hash,
                "ruleset_hash": self.ruleset_hash,
                "deployment_tick_offset": self.deployment_tick_offset,
                "frames": list(self.frames), "events": list(self.events),
                "action_mappings": list(self.action_mappings),
                "conflicts": list(self.conflicts), "summary": dict(self.summary)}


class HistoricalReplayDriver:
    def run(self, replay: HistoricalReplay, ruleset: Ruleset, *,
            unsupported_policy: str = "skip_tick") -> ReplayDrivenResult:
        if replay.evidence_level is not EvidenceLevel.ACTION_LEVEL:
            raise ValueError("replay-driven mode requires ACTION_LEVEL evidence")
        if unsupported_policy not in {"skip_tick", "stop"}:
            raise ValueError("unsupported action policy")
        build = HistoricalScenarioBuilder().build(replay, ruleset)
        engine = NativeEngine(build.scenario, ruleset=ruleset)
        engine.reset(0)
        self._deploy(engine)
        offset = engine.result().ticks
        frames: list[Mapping[str, Any]] = [
            _native_frame(engine, replay.frames[0].historical_time)]
        events: list[Mapping[str, Any]] = []
        mappings: list[Mapping[str, Any]] = []
        conflicts: list[Mapping[str, Any]] = []
        grouped: dict[float, list[HistoricalAction]] = {}
        for action in replay.actions:
            grouped.setdefault(action.historical_time, []).append(action)
        for historical_time in sorted(grouped):
            if engine.is_terminal():
                break
            batch: dict[str, EngineAction | None] = {}
            unsupported = False
            for historical in sorted(grouped[historical_time],
                                     key=lambda item: (item.side, item.actor_id,
                                                       item.action_type)):
                mapped, record = self._map_action(engine, historical)
                mappings.append(record)
                if mapped is None:
                    unsupported = True
                    conflicts.append({
                        "code": "unsupported_action",
                        "historical_time": historical_time,
                        "side": historical.side, "actor_id": historical.actor_id,
                        "action_type": historical.action_type,
                        "reason": record["reason"],
                    })
                else:
                    if historical.side in batch:
                        unsupported = True
                        conflicts.append({
                            "code": "multiple_actions_per_side_tick",
                            "historical_time": historical_time,
                            "side": historical.side})
                    else:
                        batch[historical.side] = mapped
            if unsupported and unsupported_policy == "stop":
                break
            step = engine.step(batch)
            for event in step.events:
                value = event.to_dict()
                value["historical_time"] = event.tick - offset
                value["native_tick"] = event.tick
                events.append(value)
            frames.append(_native_frame(engine, historical_time + 1.0))
            self._record_state_conflicts(
                replay, frames[-1], historical_time + 1.0,
                ruleset.movement.float_tolerance, conflicts)
        summary = engine.result().to_dict()
        engine.close()
        return ReplayDrivenResult(
            "REPLAY_DRIVEN_ADJUDICATION", build.scenario.scenario_hash,
            ruleset.content_hash, offset, tuple(frames), tuple(events),
            tuple(mappings), tuple(conflicts), summary)

    @staticmethod
    def _record_state_conflicts(
            replay: HistoricalReplay, native_frame: Mapping[str, Any],
            historical_time: float, tolerance: float,
            conflicts: list[Mapping[str, Any]]) -> None:
        historical = next((item for item in replay.frames
                           if abs(item.historical_time - historical_time)
                           <= tolerance), None)
        if historical is None:
            return
        native = {int(item["operator_id"]): item
                  for item in native_frame["operators"]}
        for source in historical.operators:
            target = native.get(source.operator_id)
            if target is None:
                continue
            mismatches: list[str] = []
            if source.x is not None and abs(float(target["x"]) - source.x) > tolerance:
                mismatches.append("x")
            if source.y is not None and abs(float(target["y"]) - source.y) > tolerance:
                mismatches.append("y")
            if (source.health is not None
                    and abs(float(target["health"]) - source.health) > tolerance):
                mismatches.append("health")
            if source.alive is not None and bool(target["alive"]) != source.alive:
                mismatches.append("alive")
            if mismatches:
                conflicts.append({
                    "code": "action_state_conflict",
                    "historical_time": historical_time,
                    "operator_id": source.operator_id,
                    "fields": mismatches,
                    "interpretation": "historical evidence retained; not classified as a NativeEngine error",
                })

    @staticmethod
    def _deploy(engine: NativeEngine) -> None:
        while engine._require_world().stage.value == "deployment":
            batch: dict[str, EngineAction] = {}
            for side in engine.scenario.side_order:
                actions = engine.valid_actions(side)
                selected = next((item.action for item in actions
                                 if item.action.action_type is ActionType.DEPLOY),
                                actions[0].action)
                batch[side] = selected
            engine.step(batch)

    @staticmethod
    def _map_action(engine: NativeEngine,
                    historical: HistoricalAction) -> tuple[
                        EngineAction | None, Mapping[str, Any]]:
        if not historical.executable:
            return None, {"historical_time": historical.historical_time,
                          "actor_id": historical.actor_id,
                          "mapping_status": "UNSUPPORTED",
                          "reason": "historical_action_not_confirmed"}
        try:
            action_type = ActionType(historical.action_type)
        except ValueError:
            return None, {"historical_time": historical.historical_time,
                          "actor_id": historical.actor_id,
                          "mapping_status": "UNSUPPORTED",
                          "reason": "native_action_type_unsupported"}
        for valid in engine.valid_actions(historical.side):
            action = valid.action
            if action.actor_id != historical.actor_id or action.action_type is not action_type:
                continue
            if historical.target_id is not None and action.target_id != historical.target_id:
                continue
            if historical.target_position is not None:
                if action.target_position is None or any(
                        abs(left - right) > engine.ruleset.movement.float_tolerance
                        for left, right in zip(action.target_position,
                                               historical.target_position)):
                    continue
            return action, {
                "historical_time": historical.historical_time,
                "actor_id": historical.actor_id, "side": historical.side,
                "historical_action_type": historical.action_type,
                "native_action_id": action.action_id,
                "mapping_status": historical.mapping_status,
                "reason": "matched_current_native_valid_action",
            }
        return None, {"historical_time": historical.historical_time,
                      "actor_id": historical.actor_id, "side": historical.side,
                      "historical_action_type": historical.action_type,
                      "mapping_status": "UNSUPPORTED",
                      "reason": "no_equivalent_current_native_valid_action"}


def _native_frame(engine: NativeEngine,
                  historical_time: float) -> Mapping[str, Any]:
    snapshot = engine.snapshot()
    world = snapshot["world"]
    return {
        "historical_time": float(historical_time),
        "native_tick": int(world["tick"]),
        "operators": [dict(item) for _, item in sorted(
            world["operators"].items(), key=lambda pair: int(pair[0]))],
        "terminal": world["terminal"], "truncated": world["truncated"],
        "winner": world["winner"], "terminal_reason": world["terminal_reason"],
    }
