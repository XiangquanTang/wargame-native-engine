"""Versioned, non-executable Native guidance authority facts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.core.fire_intent import FireMode
from wargame_native_engine.io.serialization import canonical_hash


GUIDANCE_PROFILE_SCHEMA_VERSION = "native-guidance-profiles/1.0"
DIRECT_ONLY_FIRE_MODES = frozenset({FireMode.DIRECT})


class GuidanceScope(str, Enum):
    """The only relationship scopes authorized by the Native guidance facts."""

    AFFILIATED_SHOOTER = "AFFILIATED_SHOOTER"
    SIDE_WIDE = "SIDE_WIDE"


@dataclass(frozen=True, slots=True)
class GuidanceProfile:
    profile_id: str
    guidance_scope: GuidanceScope
    requires_stopped: bool | None
    requires_unfolded: bool | None
    prohibited_while_suppressed: bool | None
    recovery_seconds: int

    def __post_init__(self) -> None:
        _nonempty(self.profile_id, "profile_id")
        if not isinstance(self.guidance_scope, GuidanceScope):
            raise TypeError("guidance_scope must be GuidanceScope")
        for name in (
            "requires_stopped",
            "requires_unfolded",
            "prohibited_while_suppressed",
        ):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise TypeError(f"{name} must be an exact bool or None")
        if type(self.recovery_seconds) is not int or self.recovery_seconds < 0:
            raise ValueError("recovery_seconds must be a non-negative exact int")


@dataclass(frozen=True, slots=True)
class GuidanceAffiliationEvidence:
    """Detached provenance for an explicitly authorized affiliation.

    Merely constructing evidence does not infer or grant an affiliation.  The
    authoritative ``guidance_affiliation_id`` must be supplied independently by
    a trusted Native scenario/supplement mapping.
    """

    source_field: str
    source_value: int | str
    mapping_version: str

    def __post_init__(self) -> None:
        _nonempty(self.source_field, "source_field")
        _nonempty(self.mapping_version, "mapping_version")
        if type(self.source_value) is int:
            if self.source_value < 0:
                raise ValueError("source_value integer must be non-negative")
        elif isinstance(self.source_value, str):
            _nonempty(self.source_value, "source_value")
        else:
            raise TypeError("source_value must be an exact int or string")

    def to_dict(self) -> dict[str, int | str]:
        return {
            "source_field": self.source_field,
            "source_value": self.source_value,
            "mapping_version": self.mapping_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GuidanceAffiliationEvidence":
        if not isinstance(value, Mapping):
            raise TypeError("guidance affiliation evidence must be an object")
        allowed = {"source_field", "source_value", "mapping_version"}
        if set(value) != allowed:
            raise ValueError("guidance affiliation evidence fields mismatch")
        return cls(
            source_field=str(value["source_field"]),
            source_value=value["source_value"],
            mapping_version=str(value["mapping_version"]),
        )


@dataclass(frozen=True, slots=True)
class GuidanceAuthority:
    config_id: str
    config_version: str
    mapping_version: str
    profiles: tuple[GuidanceProfile, ...]
    source_operator_profiles: tuple[tuple[int, int, str], ...]
    weapon_fire_modes: tuple[tuple[int, frozenset[FireMode]], ...]
    content_hash: str

    def profile(self, profile_id: str) -> GuidanceProfile | None:
        return next(
            (item for item in self.profiles if item.profile_id == profile_id),
            None,
        )

    def profile_for_source_codes(
        self,
        type_code: int,
        subtype_code: int,
    ) -> GuidanceProfile | None:
        if type(type_code) is not int or type(subtype_code) is not int:
            return None
        profile_id = next(
            (
                profile
                for mapped_type, mapped_subtype, profile in self.source_operator_profiles
                if mapped_type == type_code and mapped_subtype == subtype_code
            ),
            None,
        )
        return None if profile_id is None else self.profile(profile_id)

    def supported_fire_modes(self, weapon_id: int) -> frozenset[FireMode]:
        if type(weapon_id) is not int or weapon_id < 0:
            return DIRECT_ONLY_FIRE_MODES
        return next(
            (
                modes
                for mapped_weapon_id, modes in self.weapon_fire_modes
                if mapped_weapon_id == weapon_id
            ),
            DIRECT_ONLY_FIRE_MODES,
        )


def default_guidance_profiles_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "config/rulesets/guidance_profiles_v1.json"
    )


def load_guidance_authority(
    path: str | Path | None = None,
) -> GuidanceAuthority:
    source = default_guidance_profiles_path() if path is None else Path(path)
    try:
        root = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid guidance configuration {source}: {exc}") from exc
    if not isinstance(root, Mapping):
        raise ValueError("guidance configuration root must be an object")
    required = {
        "schema_version",
        "config_id",
        "config_version",
        "mapping_version",
        "unknown_policy",
        "profiles",
        "source_operator_profiles",
        "weapon_fire_modes",
    }
    if set(root) != required:
        raise ValueError("guidance configuration fields mismatch")
    if root["schema_version"] != GUIDANCE_PROFILE_SCHEMA_VERSION:
        raise ValueError("unsupported guidance profile schema")
    if root["unknown_policy"] != "UNKNOWN":
        raise ValueError("guidance unknown policy must remain UNKNOWN")

    profile_values = _mapping(root["profiles"], "profiles")
    profiles = tuple(
        GuidanceProfile(
            profile_id=_nonempty(profile_id, "profile_id"),
            guidance_scope=GuidanceScope(str(_mapping(value, profile_id)[
                "guidance_scope"])),
            requires_stopped=_optional_exact_bool(value.get("requires_stopped"),
                                                  "requires_stopped"),
            requires_unfolded=_optional_exact_bool(value.get("requires_unfolded"),
                                                   "requires_unfolded"),
            prohibited_while_suppressed=_optional_exact_bool(
                value.get("prohibited_while_suppressed"),
                "prohibited_while_suppressed",
            ),
            recovery_seconds=_exact_nonnegative_int(
                value.get("recovery_seconds"), "recovery_seconds"
            ),
        )
        for profile_id, value in sorted(profile_values.items())
    )
    profile_ids = {item.profile_id for item in profiles}

    mappings: list[tuple[int, int, str]] = []
    for key, raw_profile_id in sorted(
        _mapping(root["source_operator_profiles"], "source_operator_profiles").items()
    ):
        try:
            type_text, subtype_text = str(key).split("_", 1)
            type_code, subtype_code = int(type_text), int(subtype_text)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid Source operator identity {key!r}") from exc
        if type_code < 0 or subtype_code < 0:
            raise ValueError("Source operator identity must be non-negative")
        profile_id = str(raw_profile_id)
        if profile_id not in profile_ids:
            raise ValueError(f"unknown guidance profile {profile_id!r}")
        mappings.append((type_code, subtype_code, profile_id))

    weapon_modes: list[tuple[int, frozenset[FireMode]]] = []
    for raw_weapon_id, raw_modes in sorted(
        _mapping(root["weapon_fire_modes"], "weapon_fire_modes").items(),
        key=lambda item: int(item[0]),
    ):
        weapon_id = _exact_nonnegative_int(int(raw_weapon_id), "weapon_id")
        if not isinstance(raw_modes, list) or not raw_modes:
            raise ValueError("weapon fire modes must be a non-empty array")
        modes = frozenset(FireMode(str(item)) for item in raw_modes)
        if FireMode.DIRECT not in modes:
            raise ValueError("configured weapons must preserve DIRECT compatibility")
        weapon_modes.append((weapon_id, modes))

    return GuidanceAuthority(
        config_id=_nonempty(root["config_id"], "config_id"),
        config_version=_nonempty(root["config_version"], "config_version"),
        mapping_version=_nonempty(root["mapping_version"], "mapping_version"),
        profiles=profiles,
        source_operator_profiles=tuple(mappings),
        weapon_fire_modes=tuple(weapon_modes),
        content_hash=canonical_hash(root),
    )


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _nonempty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_exact_bool(value: Any, name: str) -> bool | None:
    if value is None:
        return None
    if type(value) is not bool:
        raise TypeError(f"{name} must be an exact bool or null")
    return value


def _exact_nonnegative_int(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative exact int")
    return value
