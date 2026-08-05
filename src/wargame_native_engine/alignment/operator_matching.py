from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable, Mapping


@dataclass(frozen=True, slots=True)
class OperatorMatch:
    historical_operator_id: int
    native_operator_id: int | None
    status: str
    evidence: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"historical_operator_id": self.historical_operator_id,
                "native_operator_id": self.native_operator_id,
                "status": self.status, "evidence": dict(self.evidence)}


class OperatorMatcher:
    def match(self, historical: Iterable[Any], native: Iterable[Mapping[str, Any]],
              *, source_mapping: Mapping[int, int] | None = None,
              allow_heuristic: bool = False) -> tuple[OperatorMatch, ...]:
        native_values = list(native)
        by_id = {int(item["operator_id"]): item for item in native_values}
        source_mapping = dict(source_mapping or {})
        used: set[int] = set()
        result: list[OperatorMatch] = []
        for item in sorted(historical, key=lambda value: value.operator_id):
            if item.operator_id in by_id:
                used.add(item.operator_id)
                result.append(OperatorMatch(item.operator_id, item.operator_id,
                                            "EXACT_ID", {}))
                continue
            mapped = source_mapping.get(item.operator_id)
            if mapped in by_id and mapped not in used:
                used.add(mapped)
                result.append(OperatorMatch(item.operator_id, mapped,
                                            "SOURCE_MAPPING",
                                            {"configured_mapping": mapped}))
                continue
            if allow_heuristic and item.x is not None and item.y is not None:
                candidates = [value for value in native_values
                              if int(value["operator_id"]) not in used
                              and value["side"] == item.side
                              and value["operator_type"] == item.operator_type]
                if candidates:
                    choice = min(candidates, key=lambda value: (
                        math.hypot(float(value["x"]) - item.x,
                                   float(value["y"]) - item.y),
                        int(value["operator_id"])))
                    native_id = int(choice["operator_id"])
                    used.add(native_id)
                    result.append(OperatorMatch(
                        item.operator_id, native_id, "HEURISTIC_MATCH",
                        {"rule": "side+operator_type+nearest_initial_position"}))
                    continue
            result.append(OperatorMatch(item.operator_id, None, "UNMATCHED", {}))
        return tuple(result)
