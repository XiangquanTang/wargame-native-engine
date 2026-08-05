from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.io.serialization import file_sha256, json_clone


FINAL_SCHEMA_VERSION = "native-adjudication-tables/1.2"
FINAL_REVISION = "1.2.1-final"


class UnresolvedTableCellError(ValueError):
    """Raised when a rule explicitly has no executable mapping."""


@dataclass(frozen=True, slots=True)
class TableLookup:
    table_id: str
    cell: str
    value: Any
    status: str = "RESOLVED"
    source_page: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"table_id": self.table_id, "table_cell": self.cell,
                "value": json_clone(self.value), "status": self.status,
                "source_page": self.source_page}


class AdjudicationTableSet:
    """Validated access to the single user-curated adjudication JSON.

    Lookups use exact integer indices only. There is intentionally no generic
    interpolation or fallback path.
    """

    _ALIASES = {
        "LARGE_DIRECT_FIRE_CANNON": "large_direct_fire_gun",
        "MEDIUM_DIRECT_FIRE_CANNON": "medium_direct_fire_gun",
        "SMALL_DIRECT_FIRE_CANNON": "small_direct_fire_gun",
        "RAPID_FIRE_CANNON_GROUND": "rapid_fire_gun_ground",
        "INFANTRY_LIGHT_WEAPON": "infantry_light_weapon",
        "VEHICLE_LIGHT_WEAPON": "vehicle_light_weapon",
    }

    def __init__(self, path: str | Path) -> None:
        requested = Path(path)
        source, expected_file_hash = self._resolve_source(requested)
        self.source_path = source
        self.document = _load(source)
        self.file_hash = file_sha256(source)
        if expected_file_hash is not None and self.file_hash != expected_file_hash:
            raise ValueError("final adjudication JSON file hash mismatch")
        self._validate_metadata()
        self.content_hash = self._content_hash()
        if self.content_hash != self.document["metadata"]["content_sha256"]:
            raise ValueError("final adjudication JSON content_sha256 mismatch")
        self._validate_tables()

    @staticmethod
    def _resolve_source(path: Path) -> tuple[Path, str | None]:
        value = _load(path)
        if "metadata" in value and "direct_fire" in value:
            return path, None
        relative = value.get("final_table_document")
        if not isinstance(relative, str) or not relative:
            raise ValueError("manifest must reference final_table_document")
        expected = value.get("final_table_file_sha256")
        return path.parent / relative, None if expected is None else str(expected)

    @property
    def table_set_id(self) -> str:
        return str(self.document["metadata"]["table_set_id"])

    @property
    def table_set_version(self) -> str:
        return str(self.document["metadata"]["revision"])

    @property
    def schema_version(self) -> str:
        return str(self.document["metadata"]["schema_version"])

    @property
    def source_document_hash(self) -> str:
        return str(self.document["metadata"]["source_document"]["sha256"])

    @property
    def weapon_mappings(self) -> Mapping[str, Any]:
        return json_clone(self.document["source_mappings"]["weapon_id_to_type"])

    def weapon_type(self, weapon_id: int | str) -> TableLookup:
        key = str(weapon_id)
        value = self.document["source_mappings"]["weapon_id_to_type"].get(key)
        if value is None:
            raise UnresolvedTableCellError(f"unmapped source weapon id: {weapon_id}")
        return TableLookup("source_mappings.weapon_id_to_type", f"weapon_id={key}",
                           value)

    def timing(self, name: str) -> int:
        values = self.document["runtime_rules"]["timing"]
        if name not in values:
            raise ValueError(f"unknown runtime timing rule: {name}")
        return int(values[name])

    def attack_level(self, table_name: str, weapon_class: str,
                     strength: int, distance: int) -> TableLookup:
        canonical = self._ALIASES.get(weapon_class, weapon_class)
        distance = _index(distance, "distance")
        strength = _index(strength, "strength")
        direct = self.document["direct_fire"]
        if table_name == "direct_fire_personnel_attack_levels":
            table = direct["personnel_attack_levels"]
            weapon = _object(table.get(canonical), f"personnel weapon {canonical}")
            maximum = int(weapon["range"])
            if distance > maximum:
                raise ValueError(f"distance {distance} outside weapon range {maximum}")
            values = _array(_object(weapon["by_strength"], "by_strength").get(str(strength)),
                            f"strength {strength}")
            value = _at(values, distance, "distance")
            table_id = "direct_fire.personnel_attack_levels"
        elif table_name == "direct_fire_vehicle_attack_levels":
            table = direct["vehicle_attack_levels"]
            weapon = _object(table.get(canonical), f"vehicle weapon {canonical}")
            if weapon.get("lookup") == "infantry_vs_vehicle_attack_levels":
                return self.attack_level("infantry_vs_vehicle_attack_levels",
                                         canonical, strength, distance)
            maximum = int(weapon["range"])
            if distance > maximum:
                raise ValueError(f"distance {distance} outside weapon range {maximum}")
            value = _at(_array(weapon["by_distance"], "by_distance"), distance,
                        "distance")
            table_id = "direct_fire.vehicle_attack_levels"
        elif table_name == "infantry_vs_vehicle_attack_levels":
            table = direct["infantry_vs_vehicle_attack_levels"]
            maximum = int(table["range"])
            if canonical != "infantry_light_weapon":
                raise ValueError("infantry-vs-vehicle table requires infantry_light_weapon")
            if distance > maximum:
                raise ValueError(f"distance {distance} outside weapon range {maximum}")
            values = _array(_object(table["by_strength"], "by_strength").get(str(strength)),
                            f"strength {strength}")
            value = _at(values, distance, "distance")
            table_id = "direct_fire.infantry_vs_vehicle_attack_levels"
        else:
            raise ValueError(f"unknown attack-level table: {table_name}")
        return TableLookup(table_id,
                           f"weapon={canonical};strength={strength};distance={distance}",
                           int(value))

    def elevation_modifier(self, target_minus_shooter: int,
                           distance: int) -> TableLookup:
        difference = int(target_minus_shooter)
        distance = _index(distance, "distance")
        table = self.document["direct_fire"]["elevation_attack_level_modifiers"]
        if difference <= 0:
            return TableLookup("direct_fire.elevation_attack_level_modifiers",
                               f"target_minus_shooter={difference};direction=not_low_to_high",
                               0)
        row = table["by_height_difference"].get(str(difference))
        if row is None or distance not in table["distance_columns"]:
            raise ValueError(
                f"elevation combination outside table: difference={difference}, distance={distance}")
        value = _at(_array(row, "elevation row"), distance - 1, "distance")
        return TableLookup("direct_fire.elevation_attack_level_modifiers",
                           f"target_minus_shooter={difference};distance={distance}",
                           int(value))

    def personnel_base_result(self, attack_level: int,
                              roll: int) -> TableLookup:
        attack_level = _index(attack_level, "attack_level")
        roll = int(roll)
        table = self.document["direct_fire"]["personnel_and_infantry_vs_vehicle_results"]
        if attack_level not in table["attack_levels"]:
            raise ValueError(f"attack level outside personnel result table: {attack_level}")
        row = table["by_roll"].get(str(roll))
        if row is None:
            raise ValueError(f"roll outside personnel result table: {roll}")
        value = _at(_array(row, "personnel result row"), attack_level - 1,
                    "attack_level")
        return TableLookup("direct_fire.personnel_and_infantry_vs_vehicle_results",
                           f"roll={roll};attack_level={attack_level}", value)

    def vehicle_base_losses(self, target_strength: int, attack_level: int,
                            roll: int) -> TableLookup:
        target_strength = _index(target_strength, "target_strength")
        attack_level = _index(attack_level, "attack_level")
        roll = int(roll)
        table = self.document["direct_fire"]["vehicle_base_combat_result_chart"]
        strength = _object(table["resolved_losses_by_target_strength_attack_level_roll"].get(
            str(target_strength)), f"target strength {target_strength}")
        level = _object(strength.get(str(attack_level)), f"attack level {attack_level}")
        if str(roll) not in level:
            raise ValueError(f"roll outside vehicle result table: {roll}")
        value = int(level[str(roll)])
        return TableLookup("direct_fire.vehicle_base_combat_result_chart",
                           f"target_strength={target_strength};attack_level={attack_level};roll={roll}",
                           value)

    def personnel_result_correction(self, modified_roll: int) -> TableLookup:
        table = self.document["direct_fire"]["personnel_result_modifiers"]
        value = _condition_value(table["result_correction_by_modified_roll"],
                                 modified_roll, "correction")
        return TableLookup("direct_fire.personnel_result_modifiers",
                           f"modified_roll={modified_roll}", int(value))

    def vehicle_loss_correction(self, modified_roll: int,
                                armor_class: str) -> TableLookup:
        armor = armor_class.lower()
        allowed = {"composite", "heavy", "medium", "light", "none"}
        if armor not in allowed:
            raise UnresolvedTableCellError(f"unmapped target armor class: {armor_class}")
        table = self.document["direct_fire"]["vehicle_result_modifiers"]
        row = _condition_row(table["armor_loss_correction_by_modified_roll"],
                             modified_roll)
        return TableLookup("direct_fire.vehicle_result_modifiers",
                           f"modified_roll={modified_roll};armor={armor}", int(row[armor]))

    def roll_modifier_config(self, target_kind: str) -> Mapping[str, Any]:
        name = ("personnel_result_modifiers" if target_kind == "personnel"
                else "vehicle_result_modifiers")
        return json_clone(self.document["direct_fire"][name]["roll_modifiers"])

    def _validate_metadata(self) -> None:
        required = {"metadata", "runtime_rules", "direct_fire", "source_mappings",
                    "known_ambiguities", "implementation_scope"}
        missing = sorted(required - set(self.document))
        if missing:
            raise ValueError(f"final adjudication JSON missing sections: {missing}")
        metadata = _object(self.document["metadata"], "metadata")
        if metadata.get("schema_version") != FINAL_SCHEMA_VERSION:
            raise ValueError("unsupported final adjudication schema_version")
        if metadata.get("revision") != FINAL_REVISION:
            raise ValueError("unsupported final adjudication revision")
        if metadata.get("release_status") != "FINAL_FOR_CODEX_IMPLEMENTATION":
            raise ValueError("adjudication JSON is not released for implementation")
        transcription = _object(metadata.get("transcription"), "metadata.transcription")
        if transcription.get("no_interpolation") is not True:
            raise ValueError("adjudication JSON must prohibit interpolation")

    def _content_hash(self) -> str:
        value = copy.deepcopy(self.document)
        value["metadata"].pop("content_sha256", None)
        canonical = json.dumps(value, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _validate_tables(self) -> None:
        direct = _object(self.document["direct_fire"], "direct_fire")
        required = {"personnel_attack_levels", "vehicle_attack_levels",
                    "infantry_vs_vehicle_attack_levels",
                    "elevation_attack_level_modifiers",
                    "vehicle_base_combat_result_chart", "vehicle_result_modifiers",
                    "personnel_and_infantry_vs_vehicle_results",
                    "personnel_result_modifiers"}
        missing = sorted(required - set(direct))
        if missing:
            raise ValueError(f"direct_fire missing tables: {missing}")
        for name, weapon in direct["personnel_attack_levels"].items():
            maximum = int(weapon["range"])
            for strength in range(1, 5):
                values = _array(weapon["by_strength"].get(str(strength)), name)
                if len(values) != maximum + 1:
                    raise ValueError(f"invalid personnel attack row length: {name}/{strength}")
        for name, weapon in direct["vehicle_attack_levels"].items():
            if "lookup" in weapon:
                continue
            if len(_array(weapon["by_distance"], name)) != int(weapon["range"]) + 1:
                raise ValueError(f"invalid vehicle attack row length: {name}")
        special = direct["infantry_vs_vehicle_attack_levels"]
        for strength in range(1, 5):
            if len(_array(special["by_strength"].get(str(strength)), "special")) != 4:
                raise ValueError("invalid infantry-vs-vehicle attack row length")
        elevation = direct["elevation_attack_level_modifiers"]
        if elevation["distance_columns"] != list(range(1, 13)):
            raise ValueError("elevation distance columns must be exact 1..12")
        for difference in range(1, 9):
            if len(_array(elevation["by_height_difference"].get(str(difference)),
                          "elevation")) != 12:
                raise ValueError("invalid elevation modifier row")
        personnel = direct["personnel_and_infantry_vs_vehicle_results"]
        for roll in range(2, 13):
            if len(_array(personnel["by_roll"].get(str(roll)), "personnel result")) != 10:
                raise ValueError("invalid personnel result row")
        vehicle = direct["vehicle_base_combat_result_chart"][
            "resolved_losses_by_target_strength_attack_level_roll"]
        for strength in range(1, 6):
            for level in range(1, 11):
                values = _object(_object(vehicle.get(str(strength)), "vehicle strength").get(
                    str(level)), "vehicle level")
                if set(values) != {str(roll) for roll in range(2, 13)}:
                    raise ValueError("vehicle result table must define every 2d6 roll")
        for roll in range(-10, 21):
            self.personnel_result_correction(roll)
            for armor in ("COMPOSITE", "HEAVY", "MEDIUM", "LIGHT", "NONE"):
                self.vehicle_loss_correction(roll, armor)


def _condition_value(rows: Any, value: int, field: str) -> Any:
    return _condition_row(rows, value)[field]


def _condition_row(rows: Any, value: int) -> Mapping[str, Any]:
    for row in _array(rows, "condition rows"):
        item = _object(row, "condition row")
        if _matches(str(item["condition"]), int(value)):
            return item
    raise ValueError(f"value outside explicit condition rows: {value}")


def _matches(condition: str, value: int) -> bool:
    if condition.startswith("<="):
        return value <= int(condition[2:])
    if condition.startswith(">="):
        return value >= int(condition[2:])
    if ".." in condition:
        low, high = (int(item) for item in condition.split("..", 1))
        return low <= value <= high
    return value == int(condition)


def _index(value: int, name: str) -> int:
    result = int(value)
    if result < 0:
        raise ValueError(f"{name} cannot be negative")
    return result


def _at(values: list[Any], index: int, name: str) -> Any:
    try:
        return values[index]
    except IndexError as exc:
        raise ValueError(f"{name} outside declared table cells: {index}") from exc


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid adjudication configuration {path}: {exc}") from exc
    return dict(_object(value, str(path)))


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return dict(value)


def _array(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value
