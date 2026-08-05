from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.core.world import ObjectiveState
from wargame_native_engine.io.scenario import (
    NativeMap, NativeScenario, scenario_from_dict,
)
from wargame_native_engine.io.serialization import (
    canonical_hash, file_sha256, json_clone,
)
from wargame_native_engine.rules.config import Ruleset, load_ruleset
from wargame_native_engine.rules.terminal_rules import TerminalRules


IMPORTER_VERSION = "legacy-scenario/1.0.0"


@dataclass(frozen=True, slots=True)
class MigrationResult:
    migration_id: str
    scenario: NativeScenario
    manifest: Mapping[str, Any]
    field_mapping: Mapping[str, Any]
    unmapped_fields: Mapping[str, Any]
    warnings: tuple[Mapping[str, Any], ...]

    def write(self, output_dir: str | Path) -> Path:
        root = Path(output_dir)
        root.mkdir(parents=True, exist_ok=True)
        values = {
            "migration_manifest.json": self.manifest,
            "field_mapping.json": self.field_mapping,
            "unmapped_fields.json": self.unmapped_fields,
            "warnings.json": {"warnings": list(self.warnings)},
            "native_scenario.json": self.scenario.to_dict(),
        }
        for name, value in values.items():
            (root / name).write_text(json.dumps(
                json_clone(value), ensure_ascii=False, sort_keys=True,
                indent=2) + "\n", encoding="utf-8")
        return root


class LegacyScenarioImporter:
    """Map documented legacy JSON fields without importing legacy runtime code."""

    _OPERATOR_MAPPED = {
        "obj_id", "color", "type", "sub_type", "cur_hex", "cur_pos",
        "altitude", "heading", "blood", "max_blood", "basic_speed",
        "carry_weapon_ids", "state", "alive", "name",
    }

    def __init__(self, ruleset: Ruleset | str | Path | None = None, *,
                 side_map: Mapping[str, str] | None = None,
                 operator_type_map: Mapping[str, str] | None = None,
                 weapon_map: Mapping[int, int] | None = None,
                 health_scale: float = 10.0,
                 speed_scale: float = 1.0 / 36.0) -> None:
        self.ruleset = (ruleset if isinstance(ruleset, Ruleset)
                        else load_ruleset(ruleset))
        self.side_map = dict(side_map or {"0": "red", "1": "blue"})
        self.operator_type_map = dict(operator_type_map or {
            "1": "recon_unit", "2": "ground_unit", "3": "support_unit"})
        self.weapon_map = dict(weapon_map or {})
        self.health_scale = float(health_scale)
        self.speed_scale = float(speed_scale)
        if self.health_scale <= 0 or self.speed_scale <= 0:
            raise ValueError("normalization scales must be positive")

    def import_file(self, source: str | Path, *,
                    source_identifier: str | None = None,
                    output_dir: str | Path | None = None) -> MigrationResult:
        path = Path(source)
        before_hash = file_sha256(path)
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("legacy scenario root must be an object")
        result = self.import_mapping(
            value, source_hash=before_hash,
            source_identifier=source_identifier or str(path))
        if file_sha256(path) != before_hash:
            raise RuntimeError("source file changed during migration")
        if output_dir is not None:
            result.write(output_dir)
        return result

    def import_mapping(self, value: Mapping[str, Any], *, source_hash: str,
                       source_identifier: str) -> MigrationResult:
        raw_operators = value.get("operators")
        if not isinstance(raw_operators, list) or not raw_operators:
            raise ValueError("legacy operators must be a non-empty array")
        map_reference = self._mapping(value.get("map_reference", {}),
                                      "map_reference")
        grid_width = int(map_reference.get("grid_width", 10))
        grid_height = int(map_reference.get("grid_height", 10))
        cell_size = float(map_reference.get("cell_size", 1.0))
        if min(grid_width, grid_height, cell_size) <= 0:
            raise ValueError("legacy grid dimensions must be positive")

        warnings: list[Mapping[str, Any]] = []
        unmapped_counts: dict[str, int] = {}
        operators: list[OperatorState] = []
        positions: dict[str, dict[int, tuple[float, float]]] = {
            side: {} for side in sorted(set(self.side_map.values()))}
        for index, raw in enumerate(raw_operators):
            item = self._mapping(raw, f"operators[{index}]")
            operator_id = int(item["obj_id"])
            side_key = str(item["color"])
            if side_key not in self.side_map:
                raise ValueError(f"unmapped legacy side: {side_key}")
            side = self.side_map[side_key]
            type_key = str(item["type"])
            operator_type = self.operator_type_map.get(type_key, "ground_unit")
            if type_key not in self.operator_type_map:
                warnings.append({"code": "default_operator_type",
                                 "operator_id": operator_id,
                                 "source_type": type_key})
            position, conversion = self._position(
                item, grid_width=grid_width, cell_size=cell_size)
            source_health = float(item.get("blood", item.get("max_blood", 1.0)))
            source_max_health = float(item.get("max_blood", source_health))
            normalized_health = source_health * self.health_scale
            normalized_max_health = source_max_health * self.health_scale
            source_speed = float(item.get("basic_speed", 0.0))
            normalized_speed = source_speed * self.speed_scale
            profile = self.ruleset.operators.profile(operator_type)
            effective_move = float(profile.get(
                "move_range", self.ruleset.movement.default_move_range))
            effective_sensor = float(profile.get(
                "sensor_range", self.ruleset.visibility.default_sensor_range))
            source_weapons = [int(entry)
                              for entry in item.get("carry_weapon_ids", [])]
            native_weapons = sorted({self.weapon_map.get(entry, 3001)
                                     for entry in source_weapons} or {3001})
            unknown = sorted(set(item) - self._OPERATOR_MAPPED)
            for name in unknown:
                unmapped_counts[name] = unmapped_counts.get(name, 0) + 1
            raw_attributes = {
                "source_values": json_clone(item),
                "normalized_values": {
                    "health": normalized_health,
                    "max_health": normalized_max_health,
                    "speed": normalized_speed,
                    "position": list(position),
                },
                "effective_rule_values": {
                    "move_range": effective_move,
                    "sensor_range": effective_sensor,
                    "weapon_ids": native_weapons,
                },
                "provenance": {
                    "move_range": "derived_by_native_rules",
                    "sensor_range": "derived_by_native_rules",
                    "weapon_ids": "simplified_native_mapping",
                    "position": conversion,
                },
            }
            operators.append(OperatorState(
                operator_id=operator_id, side=side,
                operator_type=operator_type,
                subtype=str(item.get("sub_type", "unknown")),
                x=position[0], y=position[1],
                altitude=float(item.get("altitude", 0.0)),
                heading=float(item.get("heading") or 0.0),
                health=normalized_health, max_health=normalized_max_health,
                speed=0.0, max_speed=max(normalized_speed, effective_move),
                move_range=effective_move, sensor_range=effective_sensor,
                state=str(item.get("state", "ready")),
                alive=bool(item.get("alive", normalized_health > 0)),
                weapon_ids=native_weapons, raw_attributes=raw_attributes,
                attack_range=0.0, attack_damage=0.0,
            ))
            positions[side][operator_id] = position

        sides = tuple(sorted(positions))
        if any(not positions[side] for side in sides):
            raise ValueError("legacy scenario must contain operators for every side")
        objectives = self._objectives(value.get("cities", []), warnings)
        time_value = self._mapping(value.get("time", {}), "time")
        max_ticks = int(time_value.get("max_time", self.ruleset.terminal.max_ticks))
        scenario = NativeScenario(
            scenario_id=f"{value.get('scenario_id', 'legacy')}-native-reduced",
            map=NativeMap(
                map_id=f"{value.get('map_id', 'legacy-map')}-native-grid",
                width=grid_width * cell_size,
                height=grid_height * cell_size,
                distance_unit=self.ruleset.coordinate_unit,
            ),
            side_order=sides,
            operators=tuple(sorted(operators, key=lambda item: item.operator_id)),
            objectives=tuple(objectives),
            deployment_positions=positions,
            terminal_rules=TerminalRules(
                max_ticks=max_ticks,
                elimination=self.ruleset.terminal.elimination,
                objective_score_to_win=self.ruleset.terminal.objective_score_to_win,
                mission_score_enabled=self.ruleset.terminal.mission_score_enabled,
                mutual_elimination_result=self.ruleset.terminal.mutual_elimination_result,
                tie_result=self.ruleset.terminal.tie_result,
                evaluation_order=self.ruleset.terminal.evaluation_order,
            ),
            metadata={
                "source_nature": "real_structure_reduced_fixture",
                "source_identifier": source_identifier,
                "source_hash": source_hash,
                "importer_version": IMPORTER_VERSION,
                "ruleset_id": self.ruleset.ruleset_id,
                "ruleset_version": self.ruleset.ruleset_version,
                "ruleset_hash": self.ruleset.content_hash,
                "initial_phase": "deployment",
                "mission_objective": "elimination_or_configured_objective",
            },
        )
        scenario_from_dict(scenario.to_dict())
        mapping = self._field_mapping(grid_width, cell_size)
        unmapped = {"operator_fields": [
            {"field": key, "occurrences": unmapped_counts[key],
             "handling": "preserved_in_raw_attributes_only"}
            for key in sorted(unmapped_counts)],
            "scenario_fields": [
                {"field": key, "handling": "preserved_as_source_context_only"}
                for key in sorted(set(value) - {
                    "scenario_id", "map_id", "map_reference", "operators",
                    "cities", "time"})],
        }
        migration_id = "migration-" + canonical_hash({
            "source_hash": source_hash,
            "ruleset_hash": self.ruleset.content_hash,
            "importer_version": IMPORTER_VERSION,
        })[:20]
        manifest = {
            "schema_version": "native-migration/1.0",
            "migration_id": migration_id,
            "source_identifier": source_identifier,
            "source_hash": source_hash,
            "source_nature": "real_structure_reduced_fixture",
            "importer_version": IMPORTER_VERSION,
            "ruleset_id": self.ruleset.ruleset_id,
            "ruleset_version": self.ruleset.ruleset_version,
            "ruleset_hash": self.ruleset.content_hash,
            "mapped_operator_count": len(operators),
            "defaulted_fields": ["altitude", "heading", "state", "alive"],
            "derived_fields": ["x", "y", "health", "max_health", "max_speed",
                               "move_range", "sensor_range", "weapon_ids"],
            "output_scenario_hash": scenario.scenario_hash,
        }
        return MigrationResult(migration_id, scenario, manifest, mapping,
                               unmapped, tuple(warnings))

    def _position(self, value: Mapping[str, Any], *, grid_width: int,
                  cell_size: float) -> tuple[tuple[float, float], str]:
        raw_position = value.get("cur_pos")
        if isinstance(raw_position, (list, tuple)) and len(raw_position) == 2:
            return ((float(raw_position[0]), float(raw_position[1])),
                    "source_xy_normalized_to_float")
        if "cur_hex" not in value:
            raise ValueError("legacy operator requires cur_pos XY or cur_hex")
        cell = int(value["cur_hex"])
        row, column = divmod(cell, grid_width)
        return (((column + 0.5) * cell_size, (row + 0.5) * cell_size),
                "legacy_linear_cell_to_native_cell_center")

    @staticmethod
    def _objectives(raw: Any,
                    warnings: list[Mapping[str, Any]]) -> list[ObjectiveState]:
        if not isinstance(raw, list):
            raise ValueError("legacy cities must be an array")
        values: list[ObjectiveState] = []
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping):
                warnings.append({"code": "ignored_non_object_city", "index": index})
                continue
            coord = item.get("coord")
            if not isinstance(coord, (list, tuple)) or len(coord) != 2:
                warnings.append({"code": "city_without_xy_ignored", "index": index})
                continue
            values.append(ObjectiveState(
                objective_id=index + 1, x=float(coord[0]), y=float(coord[1]),
                radius=0.5, value=max(1, int(item.get("value", 1))),
            ))
        return values

    def _field_mapping(self, grid_width: int,
                       cell_size: float) -> Mapping[str, Any]:
        return {
            "scenario": {
                "scenario_id": "scenario_id + '-native-reduced'",
                "map_id": "map_id + '-native-grid'",
                "map_bounds": "map_reference grid dimensions * cell_size",
                "sides": f"explicit color mapping {self.side_map}",
                "initial_phase": "native deployment",
                "mission_objective": "native configured terminal rules",
                "max_ticks": "time.max_time",
            },
            "operator": {
                "operator_id": "obj_id", "side": "color via explicit map",
                "operator_type": "type via explicit map", "subtype": "sub_type",
                "x,y": "cur_pos or cur_hex explicit grid conversion",
                "altitude": "altitude or documented zero default",
                "heading": "heading or documented zero default",
                "health": f"blood * {self.health_scale}",
                "speed": f"basic_speed * {self.speed_scale}",
                "move_range": "derived_by_native_rules",
                "sensor_range": "derived_by_native_rules",
                "weapon_ids": "explicit simplified native mapping",
                "state": "state or ready default", "alive": "alive or health > 0",
                "raw_attributes": "complete source record plus provenance",
            },
            "coordinate_conversion": {
                "grid_width": grid_width, "cell_size": cell_size,
                "formula": "x=(cur_hex % width + 0.5)*cell_size; y=(cur_hex // width + 0.5)*cell_size",
            },
        }

    @staticmethod
    def _mapping(value: Any, name: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{name} must be an object")
        return value
