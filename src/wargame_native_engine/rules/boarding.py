"""Versioned, fail-closed authority for the minimal boarding contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path

from wargame_native_engine.io.serialization import canonical_hash


BOARDING_PROFILE_SCHEMA = "native-boarding-profiles/v1.0"
BOARDING_AUTHORITY_VERSION = "native-boarding-authority/1.0"
BOARDING_ASSIGNMENT_POLICY = "EXPLICIT_OPERATOR_PROFILE_ONLY"


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


def load_boarding_authority(path: str | Path | None = None) -> BoardingAuthority:
    source = (
        Path(path)
        if path is not None
        else Path(__file__).resolve().parents[3]
        / "config/rulesets/boarding_profiles_v1.json"
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
    "BOARDING_PROFILE_SCHEMA",
    "BoardingAuthority",
    "BoardingProfile",
    "load_boarding_authority",
]
