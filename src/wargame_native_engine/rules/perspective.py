"""Versioned side-observation and Source perspective field policy."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.io.serialization import canonical_hash, json_clone


SOURCE_PERSPECTIVE_POLICY_SCHEMA_VERSION = "source-perspective-policy/1.1"


@dataclass(frozen=True, slots=True)
class PerspectivePolicy:
    """One fail-closed policy shared by every normal side representation."""

    document: Mapping[str, Any]
    content_hash: str

    @property
    def policy_id(self) -> str:
        return str(self.document["policy_id"])

    @property
    def policy_version(self) -> str:
        return str(self.document["policy_version"])

    @property
    def relation_id_fields(self) -> frozenset[str]:
        return frozenset(
            str(value) for value in self.document["relation_id_fields"]
        )

    @property
    def side_extra_allowlist(self) -> frozenset[str]:
        return frozenset(
            str(value)
            for value in self.document["side_extra_native_allowlist"]
        )

    @property
    def enemy_observation_allowlist(self) -> frozenset[str]:
        return frozenset(
            str(value)
            for value in self.document["visible_enemy_observation_fields"]
        )

    @property
    def enemy_hidden_fields(self) -> frozenset[str]:
        return frozenset(
            str(value)
            for value in self.document["visible_enemy_hidden_fields"]
        )

    @property
    def own_lifecycle_fields(self) -> frozenset[str]:
        return frozenset(
            str(value) for value in self.document["own_lifecycle_fields"]
        )

    def enemy_motion(self, legacy_state: object) -> str:
        mapping = self.document["visible_enemy_motion_mapping"]
        if not isinstance(mapping, Mapping):
            raise RuntimeError("invalid visible enemy motion mapping")
        normalized = str(legacy_state).strip().upper()
        value = mapping.get(normalized, mapping.get("*", "UNKNOWN"))
        result = str(value).upper()
        if result not in {"MOVING", "STATIONARY", "UNKNOWN"}:
            raise RuntimeError("unsafe visible enemy motion mapping")
        return result

    def to_provenance(self) -> dict[str, str]:
        return {
            "perspective_policy_id": self.policy_id,
            "perspective_policy_version": self.policy_version,
            "perspective_policy_hash": self.content_hash,
        }


@lru_cache(maxsize=4)
def load_perspective_policy(path: str | Path | None = None) -> PerspectivePolicy:
    source = (
        Path(path)
        if path is not None
        else Path(__file__).resolve().parents[3]
        / "config/source_compat/source_perspective_policy_v1.json"
    )
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("perspective policy root must be an object")
    required = {
        "policy_id",
        "policy_version",
        "schema_version",
        "perspectives",
        "own_unit_field_policy",
        "visible_enemy_field_policy",
        "actions_visibility",
        "judge_info_visibility",
        "communication_visibility",
        "scores_visibility",
        "cities_visibility",
        "landmark_visibility",
        "jm_point_visibility",
        "role_and_grouping_visibility",
        "native_evidence_visibility",
        "unknown_field_policy",
        "relation_id_fields",
        "side_extra_native_allowlist",
        "visible_enemy_safe_fields",
        "visible_enemy_observation_fields",
        "visible_enemy_hidden_fields",
        "visible_enemy_motion_mapping",
        "own_lifecycle_fields",
        "own_integrity_visibility",
    }
    missing = sorted(required - set(value))
    if missing:
        raise ValueError(f"perspective policy missing fields: {missing}")
    if value["schema_version"] != SOURCE_PERSPECTIVE_POLICY_SCHEMA_VERSION:
        raise ValueError("unsupported perspective policy schema")
    if value["perspectives"] != {"RED": 0, "BLUE": 1, "GREEN": -1}:
        raise ValueError("perspective numeric contract mismatch")
    if value["unknown_field_policy"] != "UNKNOWN_DO_NOT_COPY_FROM_GREEN":
        raise ValueError("unsafe perspective unknown-field policy")
    if value["visible_enemy_field_policy"] != (
        "ALLOWLIST_FROM_SIDE_OBSERVATION_EVIDENCE_ONLY"
    ):
        raise ValueError("unsafe visible enemy field policy")
    if value["own_integrity_visibility"] != "GREEN_ONLY":
        raise ValueError("unsafe own integrity visibility")
    document = json_clone(value)
    return PerspectivePolicy(document, canonical_hash(document))


__all__ = [
    "PerspectivePolicy",
    "SOURCE_PERSPECTIVE_POLICY_SCHEMA_VERSION",
    "load_perspective_policy",
]
