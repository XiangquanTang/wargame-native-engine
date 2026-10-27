"""Bounded hypothetical-target visibility queries over a frozen own-unit view.

A rectangle uses Source odd-r coordinates: x is column, y is row, and a cell's
ID is y * 100 + x. The result describes coverage for one explicit target
profile, not whether an actual enemy occupies any of those cells.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from wargame_native_engine.compat.source_frame.models import UnitDomain
from wargame_native_engine.core.empty_fire import EMPTY_FIRE_EXPOSURE_FRAMES
from wargame_native_engine.visibility.line_of_sight import (
    HexMapProfile,
    LineOfSightEvidence,
    hex_distance,
)
from wargame_native_engine.visibility.observation import evaluate_visibility
from wargame_native_engine.visibility.profiles import ObservationProfiles
from wargame_native_engine.visibility.result import VisibilityResult


MAX_REGION_CELLS = 4096
TARGET_CLASSES = (
    "PERSONNEL", "VEHICLE", "HELICOPTER", "DRONE", "LOITERING_MUNITION",
)
CELL_FIELDS = (
    "hex_id", "observable", "line_of_sight", "distance",
    "observation_limit", "reason",
)


class RegionVisibilityQueryError(ValueError):
    """A stable, non-adjudication query rejection."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _record(value: object, required: set[str], optional: set[str] | None = None
            ) -> Mapping[str, Any]:
    if (not isinstance(value, Mapping)
            or not required <= value.keys()
            or value.keys() - required - (optional or set())):
        raise RegionVisibilityQueryError(
            "invalid_arguments", "missing or unsupported query fields"
        )
    return value


def _integer(value: object, field: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise RegionVisibilityQueryError(
            "invalid_arguments", f"{field} must be an integer >= {minimum}"
        )
    return value


@dataclass(frozen=True, slots=True)
class RegionRectangle:
    x_min: int
    y_min: int
    width: int
    height: int

    def __post_init__(self) -> None:
        _integer(self.x_min, "x_min")
        _integer(self.y_min, "y_min")
        _integer(self.width, "width", 1)
        _integer(self.height, "height", 1)
        if self.x_min + self.width > 100:
            raise RegionVisibilityQueryError(
                "invalid_arguments", "Source columns must be in [0, 99]"
            )
        if self.width * self.height > MAX_REGION_CELLS:
            raise RegionVisibilityQueryError(
                "invalid_arguments", f"region must contain at most {MAX_REGION_CELLS} cells"
            )

    @classmethod
    def from_dict(cls, value: object) -> "RegionRectangle":
        return cls(**_record(value, {"x_min", "y_min", "width", "height"}))

    def hex_ids(self) -> tuple[int, ...]:
        return tuple(
            y * 100 + x
            for y in range(self.y_min, self.y_min + self.height)
            for x in range(self.x_min, self.x_min + self.width)
        )

    def to_dict(self) -> dict[str, int]:
        return {"x_min": self.x_min, "y_min": self.y_min,
                "width": self.width, "height": self.height}


@dataclass(frozen=True, slots=True)
class VisibilityTargetProfile:
    target_class: str
    concealed: bool
    height_m: int
    empty_fire_exposure_frames: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.target_class, str) or self.target_class not in TARGET_CLASSES:
            raise RegionVisibilityQueryError("invalid_arguments", "unknown target_class")
        if type(self.concealed) is not bool:
            raise RegionVisibilityQueryError("invalid_arguments", "concealed must be boolean")
        if type(self.height_m) is not int or self.height_m not in {0, 20, 200, 500}:
            raise RegionVisibilityQueryError(
                "invalid_arguments", "height_m must be 0, 20, 200 or 500"
            )
        if self.domain in {UnitDomain.PERSONNEL, UnitDomain.VEHICLE} and self.height_m != 0:
            raise RegionVisibilityQueryError("invalid_arguments", "ground targets require height_m=0")
        if self.target_class == "HELICOPTER" and self.height_m == 0:
            raise RegionVisibilityQueryError("invalid_arguments", "helicopters require an air height")
        if self.is_drone and self.height_m != 200:
            raise RegionVisibilityQueryError("invalid_arguments", "drone profiles require height_m=200")
        _integer(self.empty_fire_exposure_frames, "empty_fire_exposure_frames")
        if self.empty_fire_exposure_frames > EMPTY_FIRE_EXPOSURE_FRAMES:
            raise RegionVisibilityQueryError(
                "invalid_arguments",
                f"empty_fire_exposure_frames must be <= {EMPTY_FIRE_EXPOSURE_FRAMES}",
            )

    @property
    def domain(self) -> UnitDomain:
        return (UnitDomain(self.target_class)
                if self.target_class in {"PERSONNEL", "VEHICLE"} else UnitDomain.AIR)

    @property
    def is_drone(self) -> bool:
        return self.target_class in {"DRONE", "LOITERING_MUNITION"}

    @classmethod
    def from_dict(cls, value: object) -> "VisibilityTargetProfile":
        return cls(**_record(
            value, {"target_class", "concealed", "height_m"},
            {"empty_fire_exposure_frames"},
        ))

    def to_dict(self) -> dict[str, object]:
        return {"target_class": self.target_class, "concealed": self.concealed,
                "height_m": self.height_m,
                "empty_fire_exposure_frames": self.empty_fire_exposure_frames}


@dataclass(frozen=True, slots=True)
class ActorAssertion:
    """Optional expectations about an own actor, never state overrides."""

    fields: tuple[tuple[str, object], ...] = ()

    def __post_init__(self) -> None:
        if type(self.fields) is not tuple or any(
            type(pair) is not tuple or len(pair) != 2 for pair in self.fields
        ):
            raise RegionVisibilityQueryError("invalid_arguments", "invalid actor assertion")
        value = dict(self.fields)
        _record(value, set(), {"current_hex", "operator_type", "subtype"})
        if len(value) != len(self.fields):
            raise RegionVisibilityQueryError("invalid_arguments", "duplicate actor assertion")
        for key, field in self.fields:
            if key == "current_hex":
                _integer(field, key)
            elif key == "subtype" and field is None:
                continue
            elif not isinstance(field, str) or not field or len(field) > 128:
                raise RegionVisibilityQueryError("invalid_arguments", f"invalid actor {key}")

    @classmethod
    def from_dict(cls, value: object) -> "ActorAssertion":
        record = _record(value, set(), {"current_hex", "operator_type", "subtype"})
        return cls(tuple(record.items()))

    def validate(self, actor: Mapping[str, object]) -> None:
        for key, expected in self.fields:
            if actor.get(key) != expected:
                raise RegionVisibilityQueryError(
                    "actor_assertion_mismatch", f"actor {key} differs from the frozen observation"
                )


@dataclass(frozen=True, slots=True)
class RegionVisibilityQuery:
    region: RegionRectangle
    target_profile: VisibilityTargetProfile
    actor_assertion: ActorAssertion = ActorAssertion()

    def __post_init__(self) -> None:
        if (not isinstance(self.region, RegionRectangle)
                or not isinstance(self.target_profile, VisibilityTargetProfile)
                or not isinstance(self.actor_assertion, ActorAssertion)):
            raise RegionVisibilityQueryError("invalid_arguments", "invalid visibility query records")

    @classmethod
    def from_dict(cls, value: object) -> "RegionVisibilityQuery":
        record = _record(value, {"region", "target_profile"}, {"actor_assertion"})
        return cls(
            RegionRectangle.from_dict(record["region"]),
            VisibilityTargetProfile.from_dict(record["target_profile"]),
            ActorAssertion.from_dict(record.get("actor_assertion", {})),
        )


@dataclass(frozen=True, slots=True)
class RegionVisibilityCell:
    """Nullable ordinary-rule evidence distinguishes a signal from clear LOS."""

    observable: bool
    line_of_sight: bool | None
    distance: int
    observation_limit: float | None
    reason: str

    @classmethod
    def from_visibility(cls, result: VisibilityResult) -> "RegionVisibilityCell":
        return cls(result.observable, result.line_of_sight, result.distance,
                   result.observation_limit, result.reason)


@dataclass(frozen=True, slots=True)
class RegionVisibilityResult:
    actor: tuple[tuple[str, object], ...]
    region: RegionRectangle
    target_profile: VisibilityTargetProfile
    map_id: str
    los_authority: str
    cells: tuple[tuple[int, RegionVisibilityCell], ...]

    def to_dict(self) -> dict[str, object]:
        visible_count = sum(result.observable for _, result in self.cells)
        cell_count = len(self.cells)
        return {
            "semantics": "hypothetical_target_visibility",
            "actor": dict(self.actor),
            "map_id": self.map_id,
            "coordinate_system": "oddr_column_row",
            "region": self.region.to_dict(),
            "target_profile": self.target_profile.to_dict(),
            "target_assumptions": {"alive": True, "deployed": True},
            "los_authority": self.los_authority,
            "coverage": ("all" if visible_count == cell_count
                         else "some" if visible_count else "none"),
            "all_visible": visible_count == cell_count,
            "any_visible": bool(visible_count),
            "visible_count": visible_count,
            "cell_count": cell_count,
            "coverage_fraction": visible_count / cell_count,
            "cell_fields": list(CELL_FIELDS),
            "cells": [
                [hex_id, result.observable, result.line_of_sight, result.distance,
                 result.observation_limit, result.reason]
                for hex_id, result in self.cells
            ],
        }


def evaluate_region_visibility(
    *,
    query: RegionVisibilityQuery,
    actor: Mapping[str, object],
    observer_domain: UnitDomain,
    observer_is_drone: bool,
    profile: HexMapProfile,
    observation_profiles: ObservationProfiles,
    terrain_at: Callable[[int], str],
    line_of_sight: Callable[[int], LineOfSightEvidence],
    map_id: str,
    los_authority: str,
) -> RegionVisibilityResult:
    """Evaluate only public map cells and the caller's frozen friendly actor."""

    query.actor_assertion.validate(actor)
    hex_ids = query.region.hex_ids()
    if any(hex_id not in profile.cells for hex_id in hex_ids):
        raise RegionVisibilityQueryError(
            "region_out_of_map", "every requested cell must belong to the current map"
        )
    observer_hex = _integer(actor.get("current_hex"), "actor current_hex")
    if observer_hex not in profile.cells:
        raise RegionVisibilityQueryError("actor_unavailable", "actor is outside the hex map")
    target = query.target_profile
    if target.empty_fire_exposure_frames:
        # The authoritative extension returns before category, range and LOS
        # evaluation. A location signal is not evidence of a clear firing line.
        return RegionVisibilityResult(
            tuple(actor.items()), query.region, target, map_id,
            "not_evaluated_empty_fire_exposure",
            tuple((hex_id, RegionVisibilityCell(
                observable=True,
                line_of_sight=None,
                distance=hex_distance(observer_hex, hex_id),
                observation_limit=None,
                reason="empty_fire_exposure",
            )) for hex_id in hex_ids),
        )
    cells = []
    for hex_id in hex_ids:
        result = evaluate_visibility(
            profile,
            observer_hex=observer_hex,
            target_hex=hex_id,
            observer_domain=observer_domain,
            target_domain=target.domain,
            observer_is_drone=observer_is_drone,
            target_is_drone=target.is_drone,
            target_concealed=target.concealed,
            target_terrain=terrain_at(hex_id),
            observation_profiles=observation_profiles,
            line_of_sight_evidence=line_of_sight(hex_id),
        )
        cells.append((hex_id, RegionVisibilityCell.from_visibility(result)))
    return RegionVisibilityResult(
        tuple(actor.items()), query.region, target, map_id, los_authority, tuple(cells)
    )


def region_visibility_query_schema() -> dict[str, object]:
    """Query-only JSON schema; the seat adapter adds actor/version fields."""

    integer = {"type": "integer", "minimum": 0}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["region", "target_profile"],
        "properties": {
            "region": {
                "type": "object", "additionalProperties": False,
                "description": "Odd-r columns/rows, at most 4096 cells; every cell must be on the map.",
                "required": ["x_min", "y_min", "width", "height"],
                "properties": {
                    "x_min": {**integer, "maximum": 99},
                    "y_min": dict(integer),
                    "width": {"type": "integer", "minimum": 1, "maximum": 100},
                    "height": {"type": "integer", "minimum": 1, "maximum": MAX_REGION_CELLS},
                },
            },
            "target_profile": {
                "type": "object", "additionalProperties": False,
                "description": "Hypothetical target. Ground height=0; drones=200; helicopters=20/200/500.",
                "required": ["target_class", "concealed", "height_m"],
                "properties": {
                    "target_class": {"type": "string", "enum": list(TARGET_CLASSES)},
                    "concealed": {"type": "boolean"},
                    "height_m": {"type": "integer", "enum": [0, 20, 200, 500]},
                    "empty_fire_exposure_frames": {
                        "type": "integer", "minimum": 0,
                        "maximum": EMPTY_FIRE_EXPOSURE_FRAMES, "default": 0,
                        "description": "Hypothetical remaining result frames of the empty-fire location signal. Positive values bypass ordinary visibility; LOS and limit are not evaluated and return null.",
                    },
                },
            },
            "actor_assertion": {
                "type": "object", "additionalProperties": False,
                "description": "Optional checks against the own-unit snapshot, never overrides.",
                "properties": {
                    "current_hex": dict(integer),
                    "operator_type": {"type": "string", "minLength": 1, "maxLength": 128},
                    "subtype": {"type": ["string", "null"], "minLength": 1, "maxLength": 128},
                },
            },
        },
    }
