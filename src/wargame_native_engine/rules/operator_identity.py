"""Read-only canonical identity resolution shared by rules and projections."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import Any, Protocol

from wargame_native_engine.compat.source_frame.mapping import (
    SourceMappings,
    source_identity_codes,
)


class CanonicalIdentitySource(str, Enum):
    LIFECYCLE_PROFILE = "LIFECYCLE_PROFILE"
    SOURCE_MAPPING = "SOURCE_MAPPING"
    COMPATIBILITY_RAW = "COMPATIBILITY_RAW"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class CanonicalIdentityResolution:
    canonical_identity: str | None
    source: CanonicalIdentitySource


class _LifecycleAuthority(Protocol):
    def profile(self, profile_id: str) -> Any | None: ...


def resolve_operator_canonical_identity(
    operator: Any,
    authority: _LifecycleAuthority,
    *,
    allow_compatibility_fallback: bool,
) -> CanonicalIdentityResolution:
    """Resolve typed/profile identity before exact versioned Source mapping.

    The optional compatibility fallback preserves the existing Engine boundary.
    Read-only semantic projections disable it so raw metadata cannot become a
    second authority.
    """

    profile_id = getattr(operator, "lifecycle_profile_id", None)
    if profile_id is not None:
        profile = authority.profile(str(profile_id))
        if profile is not None:
            return CanonicalIdentityResolution(
                str(profile.canonical_identity).upper(),
                CanonicalIdentitySource.LIFECYCLE_PROFILE,
            )

    source_codes = source_identity_codes(getattr(operator, "subtype", None))
    if source_codes is not None:
        mappings = _source_identity_mappings()
        mapped = mappings.canonical_identity(*source_codes)
        mapped_domain = mappings.domain(source_codes[0])
        if (
            mapped.status == "DIRECT"
            and mapped_domain.status == "DIRECT"
            and str(mapped_domain.native_effective_value).upper()
            == str(getattr(operator, "operator_type", "")).upper()
        ):
            return CanonicalIdentityResolution(
                str(mapped.native_effective_value).upper(),
                CanonicalIdentitySource.SOURCE_MAPPING,
            )

    if allow_compatibility_fallback:
        raw = getattr(operator, "raw_attributes", {})
        value = str(raw.get("canonical_type", "")).upper()
        if value:
            return CanonicalIdentityResolution(
                value,
                CanonicalIdentitySource.COMPATIBILITY_RAW,
            )
    return CanonicalIdentityResolution(None, CanonicalIdentitySource.UNKNOWN)


@lru_cache(maxsize=1)
def _source_identity_mappings() -> SourceMappings:
    return SourceMappings()


__all__ = [
    "CanonicalIdentityResolution",
    "CanonicalIdentitySource",
    "resolve_operator_canonical_identity",
]
