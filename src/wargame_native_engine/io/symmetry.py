from __future__ import annotations

from typing import Any, Mapping

from wargame_native_engine.io.scenario import NativeScenario, scenario_from_dict
from wargame_native_engine.io.serialization import json_clone


class ScenarioSymmetryTransformer:
    """Deterministic transformations for red/blue procedural checks."""

    def __init__(self, side_pairs: Mapping[str, str] | None = None) -> None:
        self.side_pairs = dict(side_pairs or {"red": "blue", "blue": "red"})
        if any(self.side_pairs.get(value) != key
               for key, value in self.side_pairs.items()):
            raise ValueError("side mapping must be reversible")

    def swap_sides(self, scenario: NativeScenario) -> NativeScenario:
        value = scenario.to_dict()
        value["scenario_id"] = f"{scenario.scenario_id}-side-swap"
        for operator in value["operators"]:
            operator["side"] = self._side(str(operator["side"]))
        positions = value["deployment"]["positions"]
        value["deployment"]["positions"] = {
            self._side(str(side)): entries for side, entries in positions.items()}
        for objective in value["objectives"]:
            if objective.get("controlling_side") is not None:
                objective["controlling_side"] = self._side(
                    str(objective["controlling_side"]))
        metadata = json_clone(value.get("metadata", {}))
        metadata["symmetry_transform"] = "swap_sides"
        metadata["parent_scenario_hash"] = scenario.scenario_hash
        value["metadata"] = metadata
        return scenario_from_dict(value)

    def mirror_positions(self, scenario: NativeScenario, *,
                         axis: str = "vertical") -> NativeScenario:
        if axis not in {"vertical", "horizontal"}:
            raise ValueError("axis must be vertical or horizontal")
        value = scenario.to_dict()
        width = float(value["map"]["width"])
        height = float(value["map"]["height"])

        def mirror(position: list[float]) -> list[float]:
            if axis == "vertical":
                return [width - float(position[0]), float(position[1])]
            return [float(position[0]), height - float(position[1])]

        value["scenario_id"] = f"{scenario.scenario_id}-mirror-{axis}"
        for operator in value["operators"]:
            operator["x"], operator["y"] = mirror(
                [operator["x"], operator["y"]])
            heading = float(operator.get("heading", 0.0))
            operator["heading"] = ((180.0 - heading) % 360.0
                                   if axis == "vertical" else (-heading) % 360.0)
        for entries in value["deployment"]["positions"].values():
            for operator_id, position in list(entries.items()):
                entries[operator_id] = mirror(position)
        for objective in value["objectives"]:
            objective["x"], objective["y"] = mirror(
                [objective["x"], objective["y"]])
        value["map"]["blocked_positions"] = sorted(
            mirror(position) for position in value["map"]["blocked_positions"])
        metadata = json_clone(value.get("metadata", {}))
        metadata["symmetry_transform"] = f"mirror_{axis}"
        metadata["parent_scenario_hash"] = scenario.scenario_hash
        value["metadata"] = metadata
        return scenario_from_dict(value)

    def swap_agents(self, agents: Mapping[str, Any]) -> dict[str, Any]:
        return {self._side(side): agent for side, agent in agents.items()}

    def reverse_result(self, result: Mapping[str, Any]) -> dict[str, Any]:
        value = json_clone(result)
        for name in ("winner", "loser"):
            if value.get(name) is not None:
                value[name] = self._side(str(value[name]))
        for name in ("action_counts", "error_counts"):
            if isinstance(value.get(name), Mapping):
                value[name] = {self._side(str(side)): count
                               for side, count in value[name].items()}
        return value

    def _side(self, side: str) -> str:
        if side not in self.side_pairs:
            raise ValueError(f"side has no symmetry mapping: {side}")
        return self.side_pairs[side]
