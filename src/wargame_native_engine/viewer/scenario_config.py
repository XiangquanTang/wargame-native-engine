"""Read-only presentation adapter for one explicitly selected Source scenario.

Only static display facts are retained. Dynamic unit state always comes from
the replay frame, and this module never turns historical scenario fields into
Native adjudication inputs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path


class ViewerScenarioConfigError(ValueError):
    """Raised when an explicit presentation scenario is malformed."""


@dataclass(frozen=True, slots=True)
class ViewerScenarioFeature:
    feature_id: str
    kind: str
    row: int
    column: int
    position_id: str
    name: str
    side: str
    value: float | None

    def to_record(self) -> dict[str, object]:
        return {
            "feature_id": self.feature_id,
            "kind": self.kind,
            "row": self.row,
            "column": self.column,
            "position_id": self.position_id,
            "name": self.name,
            "side": self.side,
            "value": self.value,
        }


@dataclass(frozen=True, slots=True)
class ViewerScenarioConfig:
    scenario_id: str
    bound_map_id: str | None
    features: tuple[ViewerScenarioFeature, ...]
    source_name: str
    source_sha256: str
    initial_unit_count: int

    def to_record(self) -> dict[str, object]:
        return {
            "scenario_id": self.scenario_id,
            "bound_map_id": self.bound_map_id,
            "binding_source": "explicit_replay_identity",
            "features": [feature.to_record() for feature in self.features],
            "initial_unit_count": self.initial_unit_count,
            "dynamic_units_source": "replay_frames",
            "source": {
                "name": self.source_name,
                "sha256": self.source_sha256,
            },
        }


def load_viewer_scenario_config(
    path: str | Path,
    *,
    expected_scenario_id: str,
    expected_map_id: str | None,
) -> ViewerScenarioConfig:
    """Load safe static display facts from one explicit scenario JSON."""

    source = Path(path)
    if not source.is_file():
        raise ViewerScenarioConfigError(
            f"scenario config is not a file: {source}"
        )
    payload = source.read_bytes()
    try:
        value = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ViewerScenarioConfigError(
            f"invalid scenario config JSON: {source}"
        ) from exc
    if not isinstance(value, Mapping):
        raise ViewerScenarioConfigError("scenario config root must be an object")
    raw_scenario_id = value.get("scenario_id")
    if isinstance(raw_scenario_id, bool) or not isinstance(
        raw_scenario_id, (str, int)
    ):
        raise ViewerScenarioConfigError(
            "scenario config scenario_id must be a string or int"
        )
    scenario_id = str(raw_scenario_id)
    if scenario_id != str(expected_scenario_id):
        raise ViewerScenarioConfigError(
            "scenario config scenario_id does not match replay: "
            f"config={scenario_id!r}, replay={str(expected_scenario_id)!r}"
        )

    features: list[ViewerScenarioFeature] = []
    cities = value.get("cities", [])
    if not _is_array(cities):
        raise ViewerScenarioConfigError("scenario config cities must be an array")
    for index, item in enumerate(cities):
        if not isinstance(item, Mapping):
            raise ViewerScenarioConfigError(
                f"scenario config cities[{index}] must be an object"
            )
        features.append(_mapping_feature(item, kind="city", index=index))

    landmarks = value.get("landmarks")
    if landmarks is not None:
        if not isinstance(landmarks, Mapping):
            raise ViewerScenarioConfigError(
                "scenario config landmarks must be an object or null"
            )
        for raw_kind, items in sorted(
            landmarks.items(), key=lambda item: str(item[0])
        ):
            kind = str(raw_kind)
            if not _is_array(items):
                raise ViewerScenarioConfigError(
                    f"scenario config landmarks.{kind} must be an array"
                )
            for index, item in enumerate(items):
                if isinstance(item, Mapping):
                    feature = _mapping_feature(item, kind=kind, index=index)
                else:
                    row, column, position_id = _position(item)
                    feature = ViewerScenarioFeature(
                        feature_id=f"{kind}:{index}",
                        kind=kind,
                        row=row,
                        column=column,
                        position_id=position_id,
                        name=f"{kind} {index + 1}",
                        side="neutral",
                        value=None,
                    )
                features.append(feature)

    operators = value.get("operators", [])
    if not _is_array(operators):
        raise ViewerScenarioConfigError(
            "scenario config operators must be an array"
        )
    features.sort(key=lambda item: (item.row, item.column, item.kind, item.feature_id))
    return ViewerScenarioConfig(
        scenario_id=scenario_id,
        bound_map_id=None if expected_map_id is None else str(expected_map_id),
        features=tuple(features),
        source_name=source.name,
        source_sha256=hashlib.sha256(payload).hexdigest(),
        initial_unit_count=len(operators),
    )


def _mapping_feature(
    value: Mapping[str, object],
    *,
    kind: str,
    index: int,
) -> ViewerScenarioFeature:
    raw_position = value.get(
        "coord",
        value.get("cur_hex", value.get("pos", value.get("position"))),
    )
    row, column, position_id = _position(raw_position)
    raw_name = value.get("name")
    name = (
        f"{kind} {index + 1}"
        if raw_name is None
        else str(raw_name)
    )
    raw_flag = value.get("flag", value.get("color"))
    side = "red" if raw_flag == 0 else "blue" if raw_flag == 1 else "neutral"
    raw_value = value.get("value")
    if raw_value is None:
        numeric_value = None
    elif isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        raise ViewerScenarioConfigError(
            f"scenario feature {kind}[{index}].value must be a number or null"
        )
    else:
        numeric_value = float(raw_value)
    raw_id = value.get("id", value.get("obj_id"))
    feature_id = f"{kind}:{index}" if raw_id is None else f"{kind}:{raw_id}"
    return ViewerScenarioFeature(
        feature_id=feature_id,
        kind=kind,
        row=row,
        column=column,
        position_id=position_id,
        name=name,
        side=side,
        value=numeric_value,
    )


def _position(value: object) -> tuple[int, int, str]:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ViewerScenarioConfigError(
            "scenario feature position must be an int or digit string"
        )
    text = str(value)
    if not text.isdigit():
        raise ViewerScenarioConfigError(
            "scenario feature position must contain only digits"
        )
    numeric = int(text)
    return numeric // 100, numeric % 100, text


def _is_array(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))
