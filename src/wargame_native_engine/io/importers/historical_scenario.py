from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.io.importers.historical_replay import HistoricalReplay
from wargame_native_engine.io.scenario import NativeMap, NativeScenario
from wargame_native_engine.rules.config import Ruleset
from wargame_native_engine.rules.terminal_rules import TerminalRules


@dataclass(frozen=True, slots=True)
class HistoricalScenarioBuildResult:
    scenario: NativeScenario
    operator_mapping: Mapping[str, Any]
    warnings: tuple[Mapping[str, Any], ...]


class HistoricalScenarioBuilder:
    """Build a NativeScenario from historical frame zero only."""

    def build(self, replay: HistoricalReplay,
              ruleset: Ruleset) -> HistoricalScenarioBuildResult:
        if not replay.frames:
            raise ValueError("historical replay has no initial frame")
        initial = replay.frames[0]
        map_value = replay.metadata.get("map", {})
        width = float(map_value.get("width", 0.0))
        height = float(map_value.get("height", 0.0))
        if width <= 0 or height <= 0:
            raise ValueError("historical map bounds must be explicit")
        operators: list[OperatorState] = []
        positions: dict[str, dict[int, tuple[float, float]]] = {}
        mappings: list[Mapping[str, Any]] = []
        warnings: list[Mapping[str, Any]] = []
        for source in initial.operators:
            if source.x is None or source.y is None:
                warnings.append({"code": "initial_position_missing",
                                 "operator_id": source.operator_id})
                mappings.append({
                    "historical_operator_id": source.operator_id,
                    "native_operator_id": None, "side": source.side,
                    "operator_type": source.operator_type,
                    "match_status": "UNMATCHED",
                    "reason": "initial_position_missing",
                })
                continue
            profile = ruleset.operators.profile(source.operator_type)
            profile_health = float(profile.get("max_health", 1.0))
            max_health = (source.max_health if source.max_health is not None
                          else max(profile_health, float(source.health or 0.0)))
            health = source.health if source.health is not None else max_health
            move_range = float(dict(ruleset.movement.operator_type_ranges).get(
                source.operator_type, ruleset.movement.default_move_range))
            sensor_range = float(dict(ruleset.visibility.operator_type_ranges).get(
                source.operator_type, ruleset.visibility.default_sensor_range))
            default_values: dict[str, Any] = {}
            if source.max_health is None:
                default_values["max_health"] = {
                    "value": max_health, "source": "native_operator_profile"}
            if source.health is None:
                default_values["health"] = {
                    "value": health, "source": "effective_max_health"}
            weapons = list(source.weapon_ids)
            if not weapons:
                weapons = [1001]
                default_values["weapon_ids"] = {
                    "value": weapons, "source": "native_fixture_default"}
                warnings.append({"code": "weapon_ids_defaulted",
                                 "operator_id": source.operator_id,
                                 "value": weapons})
            operators.append(OperatorState(
                operator_id=source.operator_id, side=source.side,
                operator_type=source.operator_type, subtype="historical_reduced",
                x=source.x, y=source.y, altitude=0.0, heading=0.0,
                health=health, max_health=max_health, speed=0.0,
                max_speed=move_range, move_range=0.0, sensor_range=0.0,
                state=source.state or "ready",
                alive=(health > 0 if source.alive is None else source.alive),
                weapon_ids=weapons, attack_range=0.0, attack_damage=0.0,
                raw_attributes={
                    "source_replay": replay.replay_id,
                    "initial_frame_time": initial.historical_time,
                    "source_raw": source.source_raw,
                    "effective_rule_values": {
                        "move_range": move_range, "sensor_range": sensor_range,
                    },
                    "provenance": {
                        "position": source.field_mapping.get("position"),
                        "health": source.field_mapping.get("health"),
                        "move_range": "derived_by_native_rules",
                        "sensor_range": "derived_by_native_rules",
                    },
                }))
            positions.setdefault(source.side, {})[source.operator_id] = (
                source.x, source.y)
            mappings.append({
                "historical_operator_id": source.operator_id,
                "native_operator_id": source.operator_id,
                "side": source.side, "operator_type": source.operator_type,
                "match_status": "EXACT_ID",
                "default_values": default_values,
                "derived_values": {
                    "move_range": {"value": move_range,
                                   "source": "derived_by_native_rules"},
                    "sensor_range": {"value": sensor_range,
                                     "source": "derived_by_native_rules"},
                },
            })
        sides = tuple(sorted(positions))
        if len(sides) < 2:
            raise ValueError("historical initial frame needs at least two sides")
        max_ticks = max(ruleset.terminal.max_ticks,
                        int(replay.summary.terminal_time or 0) + 10)
        scenario = NativeScenario(
            scenario_id=f"historical-{replay.replay_id}",
            map=NativeMap(map_id=f"historical-{replay.map_id}", width=width,
                          height=height,
                          distance_unit=replay.coordinate_unit),
            side_order=sides,
            operators=tuple(sorted(operators, key=lambda item: item.operator_id)),
            objectives=(), deployment_positions=positions,
            terminal_rules=TerminalRules(
                max_ticks=max_ticks, elimination=ruleset.terminal.elimination,
                objective_score_to_win=ruleset.terminal.objective_score_to_win,
                mission_score_enabled=ruleset.terminal.mission_score_enabled,
                mutual_elimination_result=ruleset.terminal.mutual_elimination_result,
                tie_result=ruleset.terminal.tie_result,
                evaluation_order=ruleset.terminal.evaluation_order),
            metadata={
                "source_nature": "historical_replay_initial_frame",
                "source_replay_id": replay.replay_id,
                "source_hash": replay.source_hash,
                "initial_frame_time": initial.historical_time,
                "future_frames_used": False,
                "ruleset_id": ruleset.ruleset_id,
                "ruleset_version": ruleset.ruleset_version,
                "ruleset_hash": ruleset.content_hash,
                "uncertainty_warnings": warnings,
            })
        return HistoricalScenarioBuildResult(
            scenario,
            {"status": "complete", "initial_frame_time": initial.historical_time,
             "future_frames_used": False, "operators": mappings},
            tuple(warnings))
