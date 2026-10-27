"""Explicit presentation labels, not movement or scenario normalization.

The integer table follows config/source_compat/source_movement_policy_v1.json.
NativeHex also permits the named TerrainType values accepted by that policy.
The basic-map loader deliberately keeps its separate integer/null contract.
Unknown Native generic terrain (for example ``rough`` or ``blocked``) is not
silently translated into a Source terrain code. No input facts are rewritten.
"""

from __future__ import annotations


_SOURCE_TERRAIN_NAMES = {
    0: "open",
    1: "forest",
    2: "settlement",
    3: "soft_ground",
    4: "water",
    5: "roadblock",
}

_NATIVE_TERRAIN_NAMES = {
    "NORMAL": "open",
    "FOREST": "forest",
    "SETTLEMENT": "settlement",
    "SOFT_GROUND": "soft_ground",
    "LARGE_RIVER": "water",
    "SMALL_RIVER": "small_river",
    "ROADBLOCK": "roadblock",
    "UNKNOWN": "unknown",
}


def terrain_display_semantic(
    value: object, *, allow_native_strings: bool = False,
) -> str:
    """Return a known renderer label without coercing bools or guessing types.

    Callers explicitly opt into Native strings; this does not detect or expand
    another map format. Preserve the raw value separately in the presentation.
    Numeric Native strings use the same documented integer table as the policy.
    """
    if type(value) is int:
        return _SOURCE_TERRAIN_NAMES.get(value, "unknown")
    if allow_native_strings and isinstance(value, str):
        digits = value[1:] if value.startswith("-") else value
        if digits.isdigit():
            try:
                return _SOURCE_TERRAIN_NAMES.get(int(value), "unknown")
            except ValueError:
                # Python may reject extremely long integer strings.
                return "unknown"
        return _NATIVE_TERRAIN_NAMES.get(value.upper(), "unknown")
    return "unknown"
