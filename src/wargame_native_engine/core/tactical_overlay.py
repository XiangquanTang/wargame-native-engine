"""Detached read-only tactical-space query records.

These records describe presentation data only.  They deliberately contain no
``WorldState`` reference and cannot be submitted as actions.
"""

from __future__ import annotations

from dataclasses import dataclass


TACTICAL_OVERLAY_SCHEMA = "native-tactical-overlay/1.0"
VISIBILITY_FIELD_SEMANTICS = "observable_unconcealed_ground_target"
WEAPON_ENVELOPE_SEMANTICS = "theoretical_direct_fire_envelope"
FIRE_TARGETS_SEMANTICS = "current_legal_shoot_targets"


def _hex_ids(values: tuple[int, ...], field: str) -> None:
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError(f"{field} must contain non-negative exact ints")
    if tuple(sorted(set(values))) != values:
        raise ValueError(f"{field} must be unique and sorted")


@dataclass(frozen=True, slots=True)
class VisibilityDomainField:
    target_domain: str
    hex_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        if self.target_domain not in {"PERSONNEL", "VEHICLE"}:
            raise ValueError("visibility target_domain must be PERSONNEL or VEHICLE")
        _hex_ids(self.hex_ids, "visibility hex_ids")

    def to_record(self) -> list[int]:
        return list(self.hex_ids)


@dataclass(frozen=True, slots=True)
class VisibilityField:
    by_target_domain: tuple[VisibilityDomainField, ...]
    semantics: str = VISIBILITY_FIELD_SEMANTICS

    def __post_init__(self) -> None:
        if self.semantics != VISIBILITY_FIELD_SEMANTICS:
            raise ValueError("unsupported visibility-field semantics")
        domains = tuple(value.target_domain for value in self.by_target_domain)
        if domains != tuple(sorted(set(domains))):
            raise ValueError("visibility domains must be unique and sorted")

    @property
    def hex_ids(self) -> tuple[int, ...]:
        return tuple(sorted({
            hex_id
            for value in self.by_target_domain
            for hex_id in value.hex_ids
        }))

    def to_record(self) -> dict[str, object]:
        return {
            "semantics": self.semantics,
            "hex_ids": list(self.hex_ids),
            "by_target_domain": {
                value.target_domain: value.to_record()
                for value in self.by_target_domain
            },
        }


@dataclass(frozen=True, slots=True)
class WeaponEnvelopeEntry:
    weapon_id: int
    maximum_range: int
    target_domains: tuple[str, ...]
    hex_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        if type(self.weapon_id) is not int or self.weapon_id < 0:
            raise ValueError("weapon_id must be a non-negative exact int")
        if type(self.maximum_range) is not int or self.maximum_range < 0:
            raise ValueError("maximum_range must be a non-negative exact int")
        if self.target_domains != tuple(sorted(set(self.target_domains))):
            raise ValueError("target_domains must be unique and sorted")
        if any(value not in {"PERSONNEL", "VEHICLE"}
               for value in self.target_domains):
            raise ValueError("weapon envelope v1 supports ground domains only")
        _hex_ids(self.hex_ids, "weapon envelope hex_ids")

    def to_record(self) -> dict[str, object]:
        return {
            "weapon_id": self.weapon_id,
            "maximum_range": self.maximum_range,
            "target_domains": list(self.target_domains),
            "hex_ids": list(self.hex_ids),
        }


@dataclass(frozen=True, slots=True)
class WeaponEnvelope:
    weapons: tuple[WeaponEnvelopeEntry, ...]
    semantics: str = WEAPON_ENVELOPE_SEMANTICS

    def __post_init__(self) -> None:
        if self.semantics != WEAPON_ENVELOPE_SEMANTICS:
            raise ValueError("unsupported weapon-envelope semantics")
        ids = tuple(value.weapon_id for value in self.weapons)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("weapon envelope entries must be unique and sorted")

    @property
    def hex_ids(self) -> tuple[int, ...]:
        return tuple(sorted({
            hex_id for weapon in self.weapons for hex_id in weapon.hex_ids
        }))

    def to_record(self) -> dict[str, object]:
        return {
            "semantics": self.semantics,
            "hex_ids": list(self.hex_ids),
            "weapons": [value.to_record() for value in self.weapons],
        }


@dataclass(frozen=True, slots=True)
class FireTarget:
    target_id: int
    hex_id: int

    def __post_init__(self) -> None:
        if type(self.target_id) is not int or self.target_id < 0:
            raise ValueError("target_id must be a non-negative exact int")
        if type(self.hex_id) is not int or self.hex_id < 0:
            raise ValueError("fire target hex_id must be a non-negative exact int")

    def to_record(self) -> dict[str, int]:
        return {"target_id": self.target_id, "hex_id": self.hex_id}


@dataclass(frozen=True, slots=True)
class FireTargets:
    targets: tuple[FireTarget, ...]
    semantics: str = FIRE_TARGETS_SEMANTICS

    def __post_init__(self) -> None:
        if self.semantics != FIRE_TARGETS_SEMANTICS:
            raise ValueError("unsupported fire-target semantics")
        keys = tuple((value.target_id, value.hex_id) for value in self.targets)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("fire targets must be unique and sorted")

    @property
    def hex_ids(self) -> tuple[int, ...]:
        return tuple(sorted({value.hex_id for value in self.targets}))

    def to_record(self) -> dict[str, object]:
        return {
            "semantics": self.semantics,
            "hex_ids": list(self.hex_ids),
            "targets": [value.to_record() for value in self.targets],
        }


@dataclass(frozen=True, slots=True)
class UnitTacticalOverlay:
    unit_id: int
    visibility_field: VisibilityField
    weapon_envelope: WeaponEnvelope
    fire_targets: FireTargets
    schema: str = TACTICAL_OVERLAY_SCHEMA

    def __post_init__(self) -> None:
        if type(self.unit_id) is not int or self.unit_id < 0:
            raise ValueError("unit_id must be a non-negative exact int")
        if self.schema != TACTICAL_OVERLAY_SCHEMA:
            raise ValueError("unsupported tactical-overlay schema")

    def to_record(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "unit_id": self.unit_id,
            "visibility_field": self.visibility_field.to_record(),
            "weapon_envelope": self.weapon_envelope.to_record(),
            "fire_targets": self.fire_targets.to_record(),
        }
