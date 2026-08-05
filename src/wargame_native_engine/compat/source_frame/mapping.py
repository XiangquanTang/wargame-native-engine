from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.compat.source_frame.models import (
    ArmorClass, SourceUnitState, UnitDomain, UnitState,
)


@dataclass(frozen=True, slots=True)
class MappingResult:
    source_value: Any
    normalized_value: Any
    native_effective_value: Any
    status: str
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {"source_value": self.source_value,
                "normalized_value": self.normalized_value,
                "native_effective_value": self.native_effective_value,
                "status": self.status, "source": self.source}


class SourceMappings:
    def __init__(self, root: str | Path | None = None) -> None:
        path = (Path(__file__).resolve().parents[4] / "config/source_compat"
                if root is None else Path(root))
        self.unit_types = _load(path / "unit_type_mapping_v1.json")
        self.armors = _load(path / "armor_mapping_v1.json")
        self.actions = _load(path / "action_mapping_v1.json")
        self.weapons = _load(path / "weapon_id_mapping_v1.json")
        self.observation_indices = _load(path / "observation_index_mapping_v1.json")

    def side(self, color: int) -> MappingResult:
        normalized = {0: "red", 1: "blue"}.get(color, f"source-color-{color}")
        status = "DIRECT" if color in {0, 1} else "UNMAPPED"
        return MappingResult(color, normalized, normalized, status,
                             "documented source color mapping")

    def domain(self, type_code: int) -> MappingResult:
        entry = self.unit_types["type_codes"].get(str(type_code))
        if not entry:
            return MappingResult(type_code, UnitDomain.UNKNOWN.value,
                                 UnitDomain.UNKNOWN.value, "UNMAPPED", "configuration")
        return MappingResult(type_code, entry["domain"], entry["domain"],
                             entry["status"], entry["source"])

    def armor(self, armor_code: int) -> MappingResult:
        entry = self.armors["codes"].get(str(armor_code))
        value = ArmorClass.UNKNOWN.value if not entry else entry["armor_class"]
        return MappingResult(armor_code, value, value,
                             "UNMAPPED" if not entry else entry["status"],
                             "configuration" if not entry else entry["source"])

    def weapon(self, weapon_id: int) -> MappingResult:
        value = self.weapons["mappings"].get(str(weapon_id))
        if value is None:
            return MappingResult(weapon_id, "UNKNOWN", "UNKNOWN", "UNMAPPED",
                                 "final adjudication source mapping")
        return MappingResult(weapon_id, value, value, "DIRECT",
                             "USER_CONFIRMED final adjudication source mapping")

    @staticmethod
    def unit_state(unit: SourceUnitState) -> UnitState:
        if unit.close_combat:
            return UnitState.CLOSE_COMBAT
        if unit.suppressed:
            return UnitState.SUPPRESSED
        if unit.marching:
            return UnitState.MARCHING
        if unit.stopped:
            return UnitState.STOPPED
        if unit.speed > 0:
            return UnitState.MOVING
        return UnitState.UNKNOWN


def _load(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"mapping root must be an object: {path}")
    return value
