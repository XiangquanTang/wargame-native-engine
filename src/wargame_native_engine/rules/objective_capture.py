"""Versioned Native simplified objective-capture rules.

This module is read-only.  It computes regions and eligibility from a sealed
WorldState; only NativeEngine commits a successful ownership change.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import math
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.compat.source_frame.models import UnitDomain
from wargame_native_engine.compat.source_frame.mapping import (
    SourceMappings,
    source_identity_codes,
)
from wargame_native_engine.core.world import ObjectiveState, WorldState
from wargame_native_engine.io.serialization import canonical_hash, json_clone
from wargame_native_engine.movement.map_adapter import StaticMovementMapReader


CAPTURE_POLICY_SCHEMA_VERSION = "native-objective-capture-policy/1.0"
LEGACY_CAPTURE_MODE = "LEGACY_AUTOMATIC_EUCLIDEAN_RADIUS"
EXPLICIT_CAPTURE_MODE = "EXPLICIT_CENTER_AND_MAP_NEIGHBORS"
OBJECTIVE_CAPTURE_POLICY_ID = "native-objective-capture"

_POLICY_VARIANTS = {
    "1.0.0": ("v1", LEGACY_CAPTURE_MODE),
    "2.0.0": ("v2", EXPLICIT_CAPTURE_MODE),
}
_POLICY_VERSION_ALIASES = {
    "1": "1.0.0",
    "1.0.0": "1.0.0",
    "legacy": "1.0.0",
    "v1": "1.0.0",
    "2": "2.0.0",
    "2.0.0": "2.0.0",
    "v2": "2.0.0",
}


@dataclass(frozen=True, slots=True)
class ObjectiveCapturePolicy:
    policy_id: str
    policy_version: str
    mode: str
    description: str
    _content: tuple[tuple[str, Any], ...]

    @property
    def content_hash(self) -> str:
        return canonical_hash(dict(self._content))

    def identity_record(self) -> dict[str, str]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy_hash": self.content_hash,
            "mode": self.mode,
        }


@dataclass(frozen=True, slots=True)
class CaptureAssessment:
    allowed: bool
    reason: str
    objective_hex: int | None
    region: tuple[int, ...]


class ObjectiveCaptureService:
    """Bounded evaluator over one immutable static map reader."""

    def __init__(
        self,
        map_reader: StaticMovementMapReader,
        policy: ObjectiveCapturePolicy,
    ) -> None:
        if policy.mode != EXPLICIT_CAPTURE_MODE:
            raise ValueError("explicit capture service requires v2 policy")
        self._map = map_reader
        self.policy = policy
        self._region_cache: dict[tuple[int, float, float], tuple[int, ...]] = {}

    def objective_hex(self, objective: ObjectiveState) -> int | None:
        """Convert exact integral Native column/row coordinates to hex_id."""

        column = _exact_integral_coordinate(objective.x)
        row = _exact_integral_coordinate(objective.y)
        # Source hex ids encode ``row * 100 + column``.  Check each component
        # before encoding so an invalid column cannot alias another map cell.
        if (
            column is None
            or row is None
            or column < 0
            or column >= 100
            or row < 0
        ):
            return None
        hex_id = row * 100 + column
        return hex_id if self._map.contains(hex_id) else None

    def region(self, objective: ObjectiveState) -> tuple[int, ...]:
        key = (objective.objective_id, objective.x, objective.y)
        cached = self._region_cache.get(key)
        if cached is not None:
            return cached
        center = self.objective_hex(objective)
        if center is None:
            return ()
        values = {center}
        values.update(
            neighbor
            for neighbor in self._map.neighbors(center)
            if neighbor >= 0 and self._map.contains(neighbor)
        )
        result = tuple(sorted(values))
        self._region_cache[key] = result
        return result

    def assess(
        self,
        world: WorldState,
        *,
        side: str,
        actor_id: int | None,
        objective_id: int | None,
    ) -> CaptureAssessment:
        objective = (
            None if objective_id is None else world.objectives.get(objective_id)
        )
        if objective is None:
            return CaptureAssessment(False, "OBJECTIVE_NOT_FOUND", None, ())
        center = self.objective_hex(objective)
        region = self.region(objective)
        if center is None or not region:
            return CaptureAssessment(
                False, "OBJECTIVE_HEX_INVALID", center, region
            )
        actor = None if actor_id is None else world.operators.get(actor_id)
        if actor is None:
            return CaptureAssessment(False, "ACTOR_NOT_FOUND", center, region)
        if actor.side != side:
            return CaptureAssessment(
                False, "ACTOR_NOT_CONTROLLED", center, region
            )
        actor_reason = ground_presence_error(actor)
        if actor_reason is not None:
            return CaptureAssessment(False, actor_reason, center, region)
        if actor.current_hex not in region:
            return CaptureAssessment(
                False, "ACTOR_OUTSIDE_CAPTURE_REGION", center, region
            )
        if objective.controlling_side == side:
            return CaptureAssessment(
                False, "OBJECTIVE_ALREADY_CONTROLLED", center, region
            )
        if any(
            operator.side != side
            and ground_presence_error(operator) is None
            and operator.current_hex in region
            for operator in world.operators.values()
        ):
            # Deliberately side-safe: do not disclose blocker identity/count/hex.
            return CaptureAssessment(
                False, "OBJECTIVE_CONTESTED_BY_GROUND_PRESENCE", center, region
            )
        return CaptureAssessment(True, "CAPTURE_AVAILABLE", center, region)


def ground_presence_error(operator: Any) -> str | None:
    """Return a stable eligibility code without using names or fuzzy labels."""

    if not bool(getattr(operator, "alive", False)):
        return "ACTOR_DESTROYED"
    if not bool(getattr(operator, "deployed", False)):
        return "ACTOR_NOT_DEPLOYED"
    if getattr(operator, "mounted_on", None) is not None:
        return "ACTOR_ON_BOARD"
    if getattr(operator, "current_hex", None) is None:
        return "ACTOR_NOT_ON_MAP"
    try:
        domain = UnitDomain(str(getattr(operator, "operator_type", "")).upper())
    except ValueError:
        domain = UnitDomain.UNKNOWN
    source_codes = source_identity_codes(getattr(operator, "subtype", None))
    if source_codes is not None:
        mapped = _source_mappings().domain(source_codes[0])
        if (
            mapped.status != "DIRECT"
            or str(mapped.native_effective_value).upper() != domain.value
        ):
            domain = UnitDomain.UNKNOWN
    if domain is UnitDomain.AIR:
        return "AIR_UNIT_CANNOT_CAPTURE"
    if domain not in {UnitDomain.PERSONNEL, UnitDomain.VEHICLE}:
        return "UNKNOWN_UNIT_DOMAIN"
    return None


def load_objective_capture_policy(
    version: str = "2",
) -> ObjectiveCapturePolicy:
    normalized = _POLICY_VERSION_ALIASES.get(str(version))
    if normalized is None:
        raise ValueError(
            f"unsupported objective capture policy version: {version!r}"
        )
    suffix, expected_mode = _POLICY_VARIANTS[normalized]
    path = (
        Path(__file__).resolve().parents[3]
        / f"config/rulesets/objective_capture_policy_{suffix}.json"
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("objective capture policy root must be an object")
    if value.get("schema_version") != CAPTURE_POLICY_SCHEMA_VERSION:
        raise ValueError("unsupported objective capture policy schema")
    if value.get("policy_id") != OBJECTIVE_CAPTURE_POLICY_ID:
        raise ValueError("unsupported objective capture policy id")
    if value.get("policy_version") != normalized:
        raise ValueError("objective capture policy version/content mismatch")
    mode = str(value.get("mode"))
    if mode != expected_mode:
        raise ValueError("objective capture policy mode/version mismatch")
    content = json_clone(value)
    return ObjectiveCapturePolicy(
        policy_id=str(value["policy_id"]),
        policy_version=str(value["policy_version"]),
        mode=mode,
        description=str(value["description"]),
        _content=tuple(sorted(content.items())),
    )


def resolve_objective_capture_policy_identity(
    identity: object,
) -> ObjectiveCapturePolicy:
    """Resolve only an exact identity for one frozen supported policy."""

    if not isinstance(identity, Mapping):
        raise ValueError("objective capture policy identity must be an object")
    version = identity.get("policy_version")
    if not isinstance(version, str):
        raise ValueError("objective capture policy version must be a string")
    policy = load_objective_capture_policy(version)
    if dict(identity) != policy.identity_record():
        raise ValueError("objective capture policy identity mismatch")
    return policy


@lru_cache(maxsize=1)
def _source_mappings() -> SourceMappings:
    return SourceMappings()


def _exact_integral_coordinate(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    integer = int(value)
    return integer if float(value) == float(integer) else None


__all__ = [
    "CaptureAssessment",
    "EXPLICIT_CAPTURE_MODE",
    "LEGACY_CAPTURE_MODE",
    "OBJECTIVE_CAPTURE_POLICY_ID",
    "ObjectiveCapturePolicy",
    "ObjectiveCaptureService",
    "ground_presence_error",
    "load_objective_capture_policy",
    "resolve_objective_capture_policy_identity",
]
