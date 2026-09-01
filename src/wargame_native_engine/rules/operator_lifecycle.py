"""Versioned authority for finite-lived, predeployed Native operators.

This deliberately small contract does not schedule arbitrary effects and does
not replace other timers.  A profile only authorizes an explicitly assigned
operator to exist for a bounded amount of authoritative engine time and defines
what happens at expiry.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path

from wargame_native_engine.io.serialization import canonical_hash


OPERATOR_LIFECYCLE_PROFILE_SCHEMA = "native-operator-lifecycle-profiles/1.0"
OPERATOR_LIFECYCLE_AUTHORITY_VERSION = (
    "native-operator-lifecycle-authority/1.0"
)
OPERATOR_LIFECYCLE_ASSIGNMENT_POLICY = "EXPLICIT_OPERATOR_PROFILE_ONLY"


class LifecycleExpiryBehavior(str, Enum):
    DESTROY_OPERATOR = "DESTROY_OPERATOR"


@dataclass(frozen=True, slots=True)
class OperatorLifecycleProfile:
    profile_id: str
    canonical_identity: str
    operator_domain: str
    source_type_code: int
    source_subtype_code: int
    maximum_lifetime_ms: int
    expiry_behavior: LifecycleExpiryBehavior
    source_rules: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("profile_id", "canonical_identity", "operator_domain"):
            if not isinstance(getattr(self, name), str) or not getattr(
                self, name
            ).strip():
                raise ValueError(f"{name} must be non-empty text")
        for name in ("source_type_code", "source_subtype_code"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative exact int")
        if type(self.maximum_lifetime_ms) is not int or (
            self.maximum_lifetime_ms <= 0
        ):
            raise ValueError("maximum_lifetime_ms must be a positive exact int")
        if not isinstance(self.expiry_behavior, LifecycleExpiryBehavior):
            raise TypeError("expiry_behavior must be LifecycleExpiryBehavior")
        if not self.source_rules or self.source_rules != tuple(
            sorted(set(self.source_rules))
        ):
            raise ValueError("source_rules must be non-empty, unique and sorted")

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "canonical_identity": self.canonical_identity,
            "operator_domain": self.operator_domain,
            "source_type_code": self.source_type_code,
            "source_subtype_code": self.source_subtype_code,
            "maximum_lifetime_ms": self.maximum_lifetime_ms,
            "expiry_behavior": self.expiry_behavior.value,
            "source_rules": list(self.source_rules),
        }


@dataclass(frozen=True, slots=True)
class OperatorLifecycleAuthority:
    profiles: tuple[OperatorLifecycleProfile, ...]
    content_hash: str
    authority_version: str = OPERATOR_LIFECYCLE_AUTHORITY_VERSION
    assignment_policy: str = OPERATOR_LIFECYCLE_ASSIGNMENT_POLICY

    def profile(self, profile_id: str) -> OperatorLifecycleProfile | None:
        return next(
            (item for item in self.profiles if item.profile_id == profile_id),
            None,
        )

    def profile_for_source_codes(
        self,
        type_code: int,
        subtype_code: int,
    ) -> OperatorLifecycleProfile | None:
        if type(type_code) is not int or type(subtype_code) is not int:
            return None
        return next(
            (
                item
                for item in self.profiles
                if item.source_type_code == type_code
                and item.source_subtype_code == subtype_code
            ),
            None,
        )

    def identity_record(self) -> dict[str, str]:
        return {
            "authority_version": self.authority_version,
            "authority_hash": self.content_hash,
        }


def default_operator_lifecycle_profiles_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "config/rulesets/operator_lifecycle_profiles_v1.json"
    )


def load_operator_lifecycle_authority(
    path: str | Path | None = None,
) -> OperatorLifecycleAuthority:
    source = (
        default_operator_lifecycle_profiles_path()
        if path is None
        else Path(path)
    )
    try:
        root = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid operator lifecycle authority {source}: {exc}") from exc
    if not isinstance(root, Mapping):
        raise ValueError("operator lifecycle authority must be an object")
    if set(root) != {"schema", "authority_version", "assignment_policy", "profiles"}:
        raise ValueError("operator lifecycle authority fields mismatch")
    if root.get("schema") != OPERATOR_LIFECYCLE_PROFILE_SCHEMA:
        raise ValueError("unsupported operator lifecycle profile schema")
    if root.get("authority_version") != OPERATOR_LIFECYCLE_AUTHORITY_VERSION:
        raise ValueError("unsupported operator lifecycle authority version")
    if root.get("assignment_policy") != OPERATOR_LIFECYCLE_ASSIGNMENT_POLICY:
        raise ValueError("operator lifecycle profiles require explicit assignment")
    raw_profiles = root.get("profiles")
    if not isinstance(raw_profiles, list):
        raise ValueError("operator lifecycle profiles must be an array")
    profiles = tuple(_profile(value) for value in raw_profiles)
    if tuple(item.profile_id for item in profiles) != tuple(
        sorted(item.profile_id for item in profiles)
    ):
        raise ValueError("operator lifecycle profiles must be sorted")
    if len({item.profile_id for item in profiles}) != len(profiles):
        raise ValueError("operator lifecycle profile ids must be unique")
    identities = tuple(
        (item.source_type_code, item.source_subtype_code) for item in profiles
    )
    if len(set(identities)) != len(identities):
        raise ValueError("operator lifecycle Source identities must be unique")
    return OperatorLifecycleAuthority(profiles, canonical_hash(root))


def _profile(value: object) -> OperatorLifecycleProfile:
    if not isinstance(value, Mapping):
        raise ValueError("operator lifecycle profile must be an object")
    required = {
        "profile_id",
        "canonical_identity",
        "operator_domain",
        "source_type_code",
        "source_subtype_code",
        "maximum_lifetime_ms",
        "expiry_behavior",
        "source_rules",
    }
    if set(value) != required:
        raise ValueError("operator lifecycle profile fields mismatch")
    rules = value.get("source_rules")
    if not isinstance(rules, list) or not all(
        isinstance(item, str) and item.strip() for item in rules
    ):
        raise ValueError("source_rules must be a string array")
    return OperatorLifecycleProfile(
        profile_id=str(value["profile_id"]),
        canonical_identity=str(value["canonical_identity"]).upper(),
        operator_domain=str(value["operator_domain"]).upper(),
        source_type_code=value["source_type_code"],
        source_subtype_code=value["source_subtype_code"],
        maximum_lifetime_ms=value["maximum_lifetime_ms"],
        expiry_behavior=LifecycleExpiryBehavior(str(value["expiry_behavior"])),
        source_rules=tuple(rules),
    )


__all__ = [
    "LifecycleExpiryBehavior",
    "OPERATOR_LIFECYCLE_ASSIGNMENT_POLICY",
    "OPERATOR_LIFECYCLE_AUTHORITY_VERSION",
    "OPERATOR_LIFECYCLE_PROFILE_SCHEMA",
    "OperatorLifecycleAuthority",
    "OperatorLifecycleProfile",
    "default_operator_lifecycle_profiles_path",
    "load_operator_lifecycle_authority",
]
