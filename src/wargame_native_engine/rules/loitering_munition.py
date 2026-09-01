"""Versioned Native simplified authority for predeployed loitering attacks.

The policy is intentionally narrow.  It authorizes one explicit lifecycle
profile and weapon pair to reuse direct-fire target adjudication while treating
the attacking operator itself as the one-use resource.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path

from wargame_native_engine.io.serialization import canonical_hash


LOITERING_MUNITION_ATTACK_POLICY_SCHEMA = (
    "native-loitering-munition-attack-policies/1.0"
)
LOITERING_MUNITION_ATTACK_AUTHORITY_VERSION = (
    "native-loitering-munition-attack-authority/1.0"
)
LOITERING_MUNITION_ATTACK_ASSIGNMENT_POLICY = (
    "EXPLICIT_LIFECYCLE_PROFILE_AND_WEAPON"
)
SELF_AS_MUNITION = "SELF_AS_MUNITION"
ATTACK_RESOLUTION_COMMITTED = "ATTACK_RESOLUTION_COMMITTED"


@dataclass(frozen=True, slots=True)
class LoiteringMunitionAttackPolicy:
    policy_id: str
    canonical_identity: str
    lifecycle_profile_id: str
    weapon_id: int
    weapon_class: str
    maximum_range: int
    target_domains: tuple[str, ...]
    requires_observable_target: bool
    can_interrupt_movement_route: bool
    resource_policy: str
    consume_condition: str
    destruction_reason_code: str
    source_rules: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            "policy_id",
            "canonical_identity",
            "lifecycle_profile_id",
            "weapon_class",
            "destruction_reason_code",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(
                self, name
            ).strip():
                raise ValueError(f"{name} must be non-empty text")
        if type(self.weapon_id) is not int or self.weapon_id < 0:
            raise ValueError("weapon_id must be a non-negative exact int")
        if type(self.maximum_range) is not int or self.maximum_range < 0:
            raise ValueError("maximum_range must be a non-negative exact int")
        if self.target_domains != tuple(sorted(set(self.target_domains))):
            raise ValueError("target_domains must be unique and sorted")
        if self.resource_policy != SELF_AS_MUNITION:
            raise ValueError("unsupported loitering resource policy")
        if self.consume_condition != ATTACK_RESOLUTION_COMMITTED:
            raise ValueError("unsupported loitering consumption condition")
        if self.source_rules != tuple(sorted(set(self.source_rules))):
            raise ValueError("source_rules must be unique and sorted")

    def to_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "canonical_identity": self.canonical_identity,
            "lifecycle_profile_id": self.lifecycle_profile_id,
            "weapon_id": self.weapon_id,
            "weapon_class": self.weapon_class,
            "maximum_range": self.maximum_range,
            "target_domains": list(self.target_domains),
            "requires_observable_target": self.requires_observable_target,
            "can_interrupt_movement_route": self.can_interrupt_movement_route,
            "resource_policy": self.resource_policy,
            "consume_condition": self.consume_condition,
            "destruction_reason_code": self.destruction_reason_code,
            "source_rules": list(self.source_rules),
        }


@dataclass(frozen=True, slots=True)
class LoiteringMunitionAttackAuthority:
    policies: tuple[LoiteringMunitionAttackPolicy, ...]
    content_hash: str
    authority_version: str = LOITERING_MUNITION_ATTACK_AUTHORITY_VERSION
    assignment_policy: str = LOITERING_MUNITION_ATTACK_ASSIGNMENT_POLICY

    def policy_for(
        self,
        *,
        lifecycle_profile_id: str | None,
        weapon_id: int,
    ) -> LoiteringMunitionAttackPolicy | None:
        if lifecycle_profile_id is None or type(weapon_id) is not int:
            return None
        return next(
            (
                item
                for item in self.policies
                if item.lifecycle_profile_id == lifecycle_profile_id
                and item.weapon_id == weapon_id
            ),
            None,
        )

    def identity_record(self) -> dict[str, str]:
        return {
            "authority_version": self.authority_version,
            "authority_hash": self.content_hash,
        }


def default_loitering_munition_attack_policy_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "config/rulesets/loitering_munition_attack_policy_v1.json"
    )


def load_loitering_munition_attack_authority(
    path: str | Path | None = None,
) -> LoiteringMunitionAttackAuthority:
    source = (
        default_loitering_munition_attack_policy_path()
        if path is None
        else Path(path)
    )
    try:
        root = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"invalid loitering-munition attack authority {source}: {exc}"
        ) from exc
    if not isinstance(root, Mapping):
        raise ValueError("loitering-munition attack authority must be an object")
    if set(root) != {
        "schema", "authority_version", "assignment_policy", "policies"
    }:
        raise ValueError("loitering-munition attack authority fields mismatch")
    if root.get("schema") != LOITERING_MUNITION_ATTACK_POLICY_SCHEMA:
        raise ValueError("unsupported loitering-munition attack policy schema")
    if root.get("authority_version") != LOITERING_MUNITION_ATTACK_AUTHORITY_VERSION:
        raise ValueError("unsupported loitering-munition attack authority version")
    if root.get("assignment_policy") != (
        LOITERING_MUNITION_ATTACK_ASSIGNMENT_POLICY
    ):
        raise ValueError("unsupported loitering-munition assignment policy")
    raw = root.get("policies")
    if not isinstance(raw, list):
        raise ValueError("loitering-munition attack policies must be an array")
    policies = tuple(_policy(value) for value in raw)
    if tuple(item.policy_id for item in policies) != tuple(
        sorted(item.policy_id for item in policies)
    ):
        raise ValueError("loitering-munition attack policies must be sorted")
    if len({item.policy_id for item in policies}) != len(policies):
        raise ValueError("loitering-munition attack policy ids must be unique")
    assignments = tuple(
        (item.lifecycle_profile_id, item.weapon_id) for item in policies
    )
    if len(set(assignments)) != len(assignments):
        raise ValueError("loitering-munition attack assignments must be unique")
    return LoiteringMunitionAttackAuthority(policies, canonical_hash(root))


def _policy(value: object) -> LoiteringMunitionAttackPolicy:
    if not isinstance(value, Mapping):
        raise ValueError("loitering-munition attack policy must be an object")
    required = {
        "policy_id",
        "canonical_identity",
        "lifecycle_profile_id",
        "weapon_id",
        "weapon_class",
        "maximum_range",
        "target_domains",
        "requires_observable_target",
        "can_interrupt_movement_route",
        "resource_policy",
        "consume_condition",
        "destruction_reason_code",
        "source_rules",
    }
    if set(value) != required:
        raise ValueError("loitering-munition attack policy fields mismatch")
    target_domains = value.get("target_domains")
    source_rules = value.get("source_rules")
    if not isinstance(target_domains, list) or not all(
        isinstance(item, str) and item.strip() for item in target_domains
    ):
        raise ValueError("target_domains must be a string array")
    if not isinstance(source_rules, list) or not all(
        isinstance(item, str) and item.strip() for item in source_rules
    ):
        raise ValueError("source_rules must be a string array")
    for field_name in (
        "requires_observable_target", "can_interrupt_movement_route"
    ):
        if type(value.get(field_name)) is not bool:
            raise ValueError(f"{field_name} must be a boolean")
    return LoiteringMunitionAttackPolicy(
        policy_id=str(value["policy_id"]),
        canonical_identity=str(value["canonical_identity"]).upper(),
        lifecycle_profile_id=str(value["lifecycle_profile_id"]),
        weapon_id=value["weapon_id"],
        weapon_class=str(value["weapon_class"]),
        maximum_range=value["maximum_range"],
        target_domains=tuple(str(item).upper() for item in target_domains),
        requires_observable_target=value["requires_observable_target"],
        can_interrupt_movement_route=value["can_interrupt_movement_route"],
        resource_policy=str(value["resource_policy"]),
        consume_condition=str(value["consume_condition"]),
        destruction_reason_code=str(value["destruction_reason_code"]),
        source_rules=tuple(source_rules),
    )


__all__ = [
    "ATTACK_RESOLUTION_COMMITTED",
    "LOITERING_MUNITION_ATTACK_ASSIGNMENT_POLICY",
    "LOITERING_MUNITION_ATTACK_AUTHORITY_VERSION",
    "LOITERING_MUNITION_ATTACK_POLICY_SCHEMA",
    "SELF_AS_MUNITION",
    "LoiteringMunitionAttackAuthority",
    "LoiteringMunitionAttackPolicy",
    "default_loitering_munition_attack_policy_path",
    "load_loitering_munition_attack_authority",
]
