"""Versioned, fail-closed authority for the minimal boarding contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import sys

from wargame_native_engine.io.serialization import canonical_hash


BOARDING_PROFILE_SCHEMA = "native-boarding-profiles/v1.0"
BOARDING_AUTHORITY_VERSION = "native-boarding-authority/1.0"
BOARDING_ASSIGNMENT_POLICY = "EXPLICIT_OPERATOR_PROFILE_ONLY"
BOARDING_TYPE_ASSIGNMENT_SCHEMA = "native-boarding-type-assignments/v1.0"
BOARDING_TYPE_ASSIGNMENT_AUTHORITY_VERSION = (
    "native-boarding-type-assignment-authority/1.0"
)
BOARDING_TYPE_ASSIGNMENT_POLICY = "EXPLICIT_PROFILE_THEN_EXACT_OPERATOR_TYPE"
BOARDING_EXPLICIT_DISABLED = "DISABLED"


def _default_boarding_config_path(filename: str) -> Path:
    """Locate a boarding authority in a source tree or installed wheel."""

    source_tree = (
        Path(__file__).resolve().parents[3] / "config/rulesets" / filename
    )
    if source_tree.is_file():
        return source_tree
    installed = Path(sys.prefix) / "config/rulesets" / filename
    if installed.is_file():
        return installed
    return source_tree


def default_boarding_profiles_path() -> Path:
    return _default_boarding_config_path("boarding_profiles_v1.json")


def default_boarding_type_assignments_path() -> Path:
    return _default_boarding_config_path("boarding_type_assignments_v1.json")


@dataclass(frozen=True, slots=True)
class BoardingProfile:
    profile_id: str
    passenger_domains: frozenset[str]
    carrier_domains: frozenset[str]
    transition_duration_ms: int
    single_passenger: bool

    def __post_init__(self) -> None:
        if not self.profile_id:
            raise ValueError("boarding profile_id must be non-empty")
        if not self.passenger_domains or not self.carrier_domains:
            raise ValueError("boarding domains must be non-empty")
        if self.transition_duration_ms != 75_000:
            raise ValueError("minimal boarding transition must be 75000ms")
        if self.single_passenger is not True:
            raise ValueError("minimal boarding profile must be single-passenger")

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "passenger_domains": sorted(self.passenger_domains),
            "carrier_domains": sorted(self.carrier_domains),
            "transition_duration_ms": self.transition_duration_ms,
            "single_passenger": self.single_passenger,
        }


@dataclass(frozen=True, slots=True)
class BoardingAuthority:
    profiles: tuple[BoardingProfile, ...]
    content_hash: str
    authority_version: str = BOARDING_AUTHORITY_VERSION
    assignment_policy: str = BOARDING_ASSIGNMENT_POLICY

    def profile(self, profile_id: str) -> BoardingProfile | None:
        return next(
            (item for item in self.profiles if item.profile_id == profile_id),
            None,
        )


@dataclass(frozen=True, slots=True)
class BoardingTypeAssignment:
    operator_type: str
    subtype: str
    role: str
    profile_id: str

    def __post_init__(self) -> None:
        if not self.operator_type or self.operator_type != self.operator_type.upper():
            raise ValueError("boarding assignment operator_type must be uppercase")
        if not self.subtype:
            raise ValueError("boarding assignment subtype must be non-empty")
        if self.role not in {"CARRIER", "PASSENGER"}:
            raise ValueError("boarding assignment role must be CARRIER or PASSENGER")
        if not self.profile_id:
            raise ValueError("boarding assignment profile_id must be non-empty")

    def to_dict(self) -> dict[str, str]:
        return {
            "operator_type": self.operator_type,
            "subtype": self.subtype,
            "role": self.role,
            "profile_id": self.profile_id,
        }


@dataclass(frozen=True, slots=True)
class BoardingTypeAssignmentAuthority:
    assignments: tuple[BoardingTypeAssignment, ...]
    content_hash: str
    authority_version: str = BOARDING_TYPE_ASSIGNMENT_AUTHORITY_VERSION
    assignment_policy: str = BOARDING_TYPE_ASSIGNMENT_POLICY

    def profile_id_for(
        self,
        operator_type: str,
        subtype: str | None,
        role: str,
    ) -> str | None:
        if subtype is None:
            return None
        key = (str(operator_type).upper(), str(subtype), str(role).upper())
        return next(
            (
                item.profile_id
                for item in self.assignments
                if (item.operator_type, item.subtype, item.role) == key
            ),
            None,
        )

    def identity_record(self) -> dict[str, str]:
        return {
            "authority_version": self.authority_version,
            "authority_hash": self.content_hash,
            "assignment_policy": self.assignment_policy,
        }


def load_boarding_authority(path: str | Path | None = None) -> BoardingAuthority:
    source = (
        Path(path)
        if path is not None
        else default_boarding_profiles_path()
    )
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("boarding authority must be an object")
    if value.get("schema") != BOARDING_PROFILE_SCHEMA:
        raise ValueError("unsupported boarding profile schema")
    if value.get("authority_version") != BOARDING_AUTHORITY_VERSION:
        raise ValueError("unsupported boarding authority version")
    if value.get("assignment_policy") != BOARDING_ASSIGNMENT_POLICY:
        raise ValueError("boarding authority must require explicit assignment")
    raw_profiles = value.get("profiles")
    if not isinstance(raw_profiles, list):
        raise ValueError("boarding profiles must be an array")
    profiles = tuple(_profile(item) for item in raw_profiles)
    if tuple(item.profile_id for item in profiles) != tuple(sorted(
        item.profile_id for item in profiles
    )):
        raise ValueError("boarding profiles must be deterministically sorted")
    if len({item.profile_id for item in profiles}) != len(profiles):
        raise ValueError("boarding profile ids must be unique")
    return BoardingAuthority(profiles, canonical_hash(value))


def load_boarding_type_assignment_authority(
    path: str | Path | None = None,
) -> BoardingTypeAssignmentAuthority:
    source = (
        Path(path)
        if path is not None
        else default_boarding_type_assignments_path()
    )
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("boarding type assignment authority must be an object")
    if value.get("schema") != BOARDING_TYPE_ASSIGNMENT_SCHEMA:
        raise ValueError("unsupported boarding type assignment schema")
    if (
        value.get("authority_version")
        != BOARDING_TYPE_ASSIGNMENT_AUTHORITY_VERSION
    ):
        raise ValueError("unsupported boarding type assignment authority version")
    if value.get("assignment_policy") != BOARDING_TYPE_ASSIGNMENT_POLICY:
        raise ValueError("unsupported boarding type assignment policy")
    if value.get("unknown_policy") != "REPORT_UNSUPPORTED_WITHOUT_NAME_INFERENCE":
        raise ValueError("boarding type assignment must fail closed")
    raw_assignments = value.get("assignments")
    if not isinstance(raw_assignments, list):
        raise ValueError("boarding type assignments must be an array")
    assignments = tuple(_type_assignment(item) for item in raw_assignments)
    keys = tuple(
        (item.operator_type, item.subtype, item.role, item.profile_id)
        for item in assignments
    )
    if keys != tuple(sorted(set(keys))):
        raise ValueError("boarding type assignments must be unique and sorted")
    profiles = load_boarding_authority()
    known_profile_ids = {item.profile_id for item in profiles.profiles}
    if any(item.profile_id not in known_profile_ids for item in assignments):
        raise ValueError("boarding type assignment references an unknown profile")
    return BoardingTypeAssignmentAuthority(assignments, canonical_hash(value))


def _profile(value: object) -> BoardingProfile:
    if not isinstance(value, Mapping):
        raise ValueError("boarding profile must be an object")
    passenger = _domains(value.get("passenger_domains"), "passenger_domains")
    carrier = _domains(value.get("carrier_domains"), "carrier_domains")
    duration = value.get("transition_duration_ms")
    if type(duration) is not int:
        raise ValueError("transition_duration_ms must be an exact integer")
    if type(value.get("single_passenger")) is not bool:
        raise ValueError("single_passenger must be boolean")
    return BoardingProfile(
        profile_id=_text(value.get("profile_id"), "profile_id"),
        passenger_domains=frozenset(passenger),
        carrier_domains=frozenset(carrier),
        transition_duration_ms=duration,
        single_passenger=value["single_passenger"],
    )


def _type_assignment(value: object) -> BoardingTypeAssignment:
    if not isinstance(value, Mapping):
        raise ValueError("boarding type assignment must be an object")
    return BoardingTypeAssignment(
        operator_type=_text(value.get("operator_type"), "operator_type").upper(),
        subtype=_text(value.get("subtype"), "subtype"),
        role=_text(value.get("role"), "role").upper(),
        profile_id=_text(value.get("profile_id"), "profile_id"),
    )


def _domains(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty array")
    domains = tuple(_text(item, field).upper() for item in value)
    if domains != tuple(sorted(set(domains))):
        raise ValueError(f"{field} must be unique and sorted")
    return domains


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value.strip()


__all__ = [
    "BOARDING_ASSIGNMENT_POLICY",
    "BOARDING_AUTHORITY_VERSION",
    "BOARDING_EXPLICIT_DISABLED",
    "BOARDING_PROFILE_SCHEMA",
    "BOARDING_TYPE_ASSIGNMENT_AUTHORITY_VERSION",
    "BOARDING_TYPE_ASSIGNMENT_POLICY",
    "BOARDING_TYPE_ASSIGNMENT_SCHEMA",
    "BoardingAuthority",
    "BoardingProfile",
    "BoardingTypeAssignment",
    "BoardingTypeAssignmentAuthority",
    "default_boarding_profiles_path",
    "default_boarding_type_assignments_path",
    "load_boarding_authority",
    "load_boarding_type_assignment_authority",
]
