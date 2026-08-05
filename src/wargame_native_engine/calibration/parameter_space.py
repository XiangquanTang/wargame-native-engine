from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Mapping


ALLOWED_PARAMETERS = {
    "movement.operator_type_ranges.ground_unit",
    "movement.operator_type_ranges.recon_unit",
    "movement.operator_type_ranges.support_unit",
    "visibility.operator_type_ranges.ground_unit",
    "visibility.operator_type_ranges.recon_unit",
    "visibility.operator_type_ranges.support_unit",
    "weapon_types.profiles.1001.attack_range",
    "weapon_types.profiles.1001.damage",
    "weapon_types.profiles.2001.attack_range",
    "weapon_types.profiles.2001.damage",
    "terminal.max_ticks",
    "terminal.objective_score_to_win",
}


@dataclass(frozen=True, slots=True)
class ParameterDefinition:
    parameter: str
    value_type: str
    minimum: float
    maximum: float
    step: float
    baseline: float
    reason: str

    def values(self) -> tuple[float, ...]:
        minimum = Decimal(str(self.minimum))
        maximum = Decimal(str(self.maximum))
        step = Decimal(str(self.step))
        values: list[float] = []
        current = minimum
        while current <= maximum:
            values.append(float(current))
            current += step
        return tuple(values)


@dataclass(frozen=True, slots=True)
class ParameterSpace:
    parameter_space_id: str
    search_method: str
    parameters: tuple[ParameterDefinition, ...]


def load_parameter_space(path: str | Path) -> ParameterSpace:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("parameter space root must be an object")
    if value.get("schema_version") != "native-parameter-space/1.0":
        raise ValueError("unsupported parameter space schema")
    if value.get("search_method") != "grid_search":
        raise ValueError("only bounded grid_search is supported")
    definitions: list[ParameterDefinition] = []
    seen: set[str] = set()
    for item in value.get("parameters", []):
        if not isinstance(item, Mapping):
            raise ValueError("parameter definition must be an object")
        name = str(item["parameter"])
        if name not in ALLOWED_PARAMETERS:
            raise ValueError(f"parameter is structural or unsupported: {name}")
        if name in seen:
            raise ValueError(f"duplicate parameter: {name}")
        seen.add(name)
        definition = ParameterDefinition(
            name, str(item["type"]), float(item["minimum"]),
            float(item["maximum"]), float(item["step"]),
            float(item["baseline"]), str(item["reason"]))
        if definition.value_type not in {"float", "int"}:
            raise ValueError("parameter type must be float or int")
        if (definition.minimum < 0 or definition.maximum < definition.minimum
                or definition.step <= 0
                or not definition.minimum <= definition.baseline <= definition.maximum):
            raise ValueError(f"invalid parameter bounds: {name}")
        if definition.baseline not in definition.values():
            raise ValueError(f"baseline not on parameter grid: {name}")
        definitions.append(definition)
    if not definitions:
        raise ValueError("parameter space must not be empty")
    return ParameterSpace(str(value["parameter_space_id"]),
                          str(value["search_method"]), tuple(definitions))
