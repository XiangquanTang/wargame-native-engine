"""Immutable Source-compatible operator descriptions for presentation clients.

This module is deliberately outside the authoritative Engine.  It resolves an
exact ``(type_code, subtype_code)`` identity into detached display metadata; it
does not infer identity from names and does not authorize any adjudication rule.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

from wargame_native_engine.weapon_names import weapon_display_name


OPERATOR_REGISTRY_SCHEMA = "operator-registry/v1"
OPERATOR_DESCRIPTOR_SCHEMA = "operator-descriptor/v1"


class OperatorRegistryError(ValueError):
    """Raised when an operator registry violates its public data contract."""


@dataclass(frozen=True, slots=True)
class OperatorIdentity:
    """One exact Source operator identity."""

    type_code: int
    subtype_code: int

    @property
    def key(self) -> str:
        return f"{self.type_code}_{self.subtype_code}"

    def to_record(self) -> dict[str, int]:
        return {
            "type_code": self.type_code,
            "subtype_code": self.subtype_code,
        }


@dataclass(frozen=True, slots=True)
class OperatorSymbol:
    """Presentation-only SVG base and composable modifiers."""

    base: str
    modifiers: tuple[str, ...]

    def to_record(self) -> dict[str, object]:
        return {"base": self.base, "modifiers": list(self.modifiers)}


@dataclass(frozen=True, slots=True)
class WeaponDescriptor:
    """Descriptive weapon metadata, never a substitute for Engine profiles."""

    weapon_id: int
    name: str
    weapon_class: str
    maximum_range: int | None
    target_domains: tuple[str, ...]
    requires_unfolded: bool | None
    can_fire_while_moving: bool | None
    status: str

    def to_record(self) -> dict[str, object]:
        return {
            "weapon_id": self.weapon_id,
            "name": self.name,
            "weapon_class": self.weapon_class,
            "range": self.maximum_range,
            "target_domain": list(self.target_domains),
            "requires_unfolded": self.requires_unfolded,
            "can_fire_while_moving": self.can_fire_while_moving,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class OperatorDescriptor:
    """Detached semantic description for one exact operator identity."""

    identity: OperatorIdentity
    display_name: str
    category: str
    symbol: OperatorSymbol
    weapons: tuple[WeaponDescriptor, ...]
    status: str

    @property
    def key(self) -> str:
        return self.identity.key

    def to_record(self) -> dict[str, object]:
        return {
            "schema": OPERATOR_DESCRIPTOR_SCHEMA,
            "key": self.key,
            "identity": self.identity.to_record(),
            "display_name": self.display_name,
            "category": self.category,
            "symbol": self.symbol.to_record(),
            "weapons": [weapon.to_record() for weapon in self.weapons],
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class OperatorRegistry:
    """Validated immutable registry keyed only by exact numeric identity."""

    registry_id: str
    registry_version: str
    operators: Mapping[str, OperatorDescriptor]
    weapon_profiles: Mapping[int, WeaponDescriptor]
    provenance: Mapping[str, str]

    def resolve_operator(
        self, type_code: int, subtype_code: int,
    ) -> OperatorDescriptor:
        """Resolve exact numeric codes or return a non-inferential fallback."""

        _exact_int(type_code, "type_code")
        _exact_int(subtype_code, "subtype_code")
        identity = OperatorIdentity(type_code, subtype_code)
        mapped = self.operators.get(identity.key)
        if mapped is not None:
            return mapped
        return OperatorDescriptor(
            identity=identity,
            display_name="未知算子",
            category="unknown",
            symbol=OperatorSymbol(base="unknown", modifiers=()),
            weapons=(),
            status="UNKNOWN",
        )

    def resolve_viewer_unit(
        self, viewer_unit: Mapping[str, object],
    ) -> OperatorDescriptor:
        """Resolve old/new Viewer records without reading names or icon keys."""

        if not isinstance(viewer_unit, Mapping):
            raise TypeError("viewer_unit must be a mapping")
        raw_type = viewer_unit.get("type_code", viewer_unit.get("type"))
        raw_subtype = viewer_unit.get(
            "subtype_code",
            viewer_unit.get("sub_type_code", viewer_unit.get("sub_type")),
        )
        if type(raw_type) is not int or type(raw_subtype) is not int:
            return self.resolve_operator(-1, -1)
        return self.resolve_operator(raw_type, raw_subtype)

    def to_record(self) -> dict[str, object]:
        """Return a detached, JSON-compatible representation."""

        return {
            "schema": OPERATOR_REGISTRY_SCHEMA,
            "registry_id": self.registry_id,
            "registry_version": self.registry_version,
            "provenance": dict(self.provenance),
            "weapon_profiles": {
                str(weapon_id): profile.to_record()
                for weapon_id, profile in self.weapon_profiles.items()
            },
            "operators": {
                key: descriptor.to_record()
                for key, descriptor in self.operators.items()
            },
        }


def default_operator_registry_path() -> Path:
    """Return the tracked source-release registry path."""

    return (
        Path(__file__).resolve().parents[3]
        / "config/source_compat/operator_registry_v1.json"
    )


def load_operator_registry(path: str | Path | None = None) -> OperatorRegistry:
    """Load and validate a tracked Source-compatible operator registry."""

    resolved = (
        default_operator_registry_path()
        if path is None
        else Path(path)
    ).resolve()
    return _load_operator_registry(resolved)


def resolve_operator(
    type_code: int,
    subtype_code: int,
    *,
    registry: OperatorRegistry | None = None,
) -> OperatorDescriptor:
    """Resolve through the default registry for simple Viewer integrations."""

    active = registry if registry is not None else load_operator_registry()
    return active.resolve_operator(type_code, subtype_code)


@lru_cache(maxsize=8)
def _load_operator_registry(path: Path) -> OperatorRegistry:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OperatorRegistryError(
            f"failed to load operator registry {path}: {exc}"
        ) from exc
    document = _mapping(raw, "registry")
    if document.get("schema") != OPERATOR_REGISTRY_SCHEMA:
        raise OperatorRegistryError(
            f"operator registry schema must be {OPERATOR_REGISTRY_SCHEMA}"
        )

    profiles_raw = _mapping(document.get("weapon_profiles"), "weapon_profiles")
    profiles: dict[int, WeaponDescriptor] = {}
    for raw_key, raw_profile in profiles_raw.items():
        if not isinstance(raw_key, str) or not raw_key.isdecimal():
            raise OperatorRegistryError("weapon profile keys must be decimal strings")
        profile = _weapon_descriptor(
            _mapping(raw_profile, f"weapon_profiles.{raw_key}"),
            f"weapon_profiles.{raw_key}",
        )
        if profile.weapon_id != int(raw_key):
            raise OperatorRegistryError(
                f"weapon profile key {raw_key} does not match weapon_id"
            )
        if profile.weapon_id in profiles:
            raise OperatorRegistryError(
                f"duplicate weapon profile {profile.weapon_id}"
            )
        profiles[profile.weapon_id] = profile

    operators_raw = _mapping(document.get("operators"), "operators")
    operators: dict[str, OperatorDescriptor] = {}
    for key, raw_operator in operators_raw.items():
        if not isinstance(key, str):
            raise OperatorRegistryError("operator keys must be strings")
        operator = _operator_descriptor(
            key,
            _mapping(raw_operator, f"operators.{key}"),
            profiles,
        )
        if operator.key != key:
            raise OperatorRegistryError(
                f"operator key {key!r} does not match identity {operator.key!r}"
            )
        if key in operators:
            raise OperatorRegistryError(f"duplicate operator key {key}")
        operators[key] = operator

    provenance_raw = _mapping(document.get("provenance"), "provenance")
    provenance = {
        _nonempty_string(key, "provenance key"): _nonempty_string(
            value, f"provenance.{key}",
        )
        for key, value in provenance_raw.items()
    }
    return OperatorRegistry(
        registry_id=_nonempty_string(document.get("registry_id"), "registry_id"),
        registry_version=_nonempty_string(
            document.get("registry_version"), "registry_version",
        ),
        operators=MappingProxyType(operators),
        weapon_profiles=MappingProxyType(profiles),
        provenance=MappingProxyType(provenance),
    )


def _operator_descriptor(
    key: str,
    value: Mapping[str, Any],
    profiles: Mapping[int, WeaponDescriptor],
) -> OperatorDescriptor:
    identity_raw = _mapping(value.get("identity"), f"operators.{key}.identity")
    identity = OperatorIdentity(
        _nonnegative_exact_int(
            identity_raw.get("type_code"), f"operators.{key}.type_code",
        ),
        _nonnegative_exact_int(
            identity_raw.get("subtype_code"), f"operators.{key}.subtype_code",
        ),
    )
    symbol_raw = _mapping(value.get("symbol"), f"operators.{key}.symbol")
    modifiers = _string_tuple(
        symbol_raw.get("modifiers"), f"operators.{key}.symbol.modifiers",
    )
    if len(set(modifiers)) != len(modifiers):
        raise OperatorRegistryError(
            f"operators.{key}.symbol.modifiers must be unique"
        )
    weapon_refs = _sequence(value.get("weapons"), f"operators.{key}.weapons")
    weapons: list[WeaponDescriptor] = []
    seen_weapon_ids: set[int] = set()
    for index, raw_reference in enumerate(weapon_refs):
        reference = _mapping(
            raw_reference, f"operators.{key}.weapons[{index}]",
        )
        weapon_id = _nonnegative_exact_int(
            reference.get("weapon_id"),
            f"operators.{key}.weapons[{index}].weapon_id",
        )
        if weapon_id in seen_weapon_ids:
            raise OperatorRegistryError(
                f"operators.{key}.weapons contains duplicate {weapon_id}"
            )
        try:
            weapons.append(profiles[weapon_id])
        except KeyError as exc:
            raise OperatorRegistryError(
                f"operators.{key} references missing weapon profile {weapon_id}"
            ) from exc
        seen_weapon_ids.add(weapon_id)
    return OperatorDescriptor(
        identity=identity,
        display_name=_nonempty_string(
            value.get("display_name"), f"operators.{key}.display_name",
        ),
        category=_nonempty_string(
            value.get("category"), f"operators.{key}.category",
        ),
        symbol=OperatorSymbol(
            base=_nonempty_string(
                symbol_raw.get("base"), f"operators.{key}.symbol.base",
            ),
            modifiers=modifiers,
        ),
        weapons=tuple(weapons),
        status="MAPPED",
    )


def _weapon_descriptor(
    value: Mapping[str, Any], field: str,
) -> WeaponDescriptor:
    raw_range = value.get("range")
    maximum_range = (
        None
        if raw_range is None
        else _nonnegative_exact_int(raw_range, f"{field}.range")
    )
    target_domains = _string_tuple(
        value.get("target_domain"), f"{field}.target_domain",
    )
    if tuple(sorted(set(target_domains))) != target_domains:
        raise OperatorRegistryError(
            f"{field}.target_domain must be unique and sorted"
        )
    return WeaponDescriptor(
        weapon_id=_nonnegative_exact_int(
            value.get("weapon_id"), f"{field}.weapon_id",
        ),
        name=weapon_display_name(
            _nonnegative_exact_int(value.get("weapon_id"), f"{field}.weapon_id"),
            _nonempty_string(value.get("name"), f"{field}.name"),
        ),
        weapon_class=_nonempty_string(
            value.get("weapon_class"), f"{field}.weapon_class",
        ),
        maximum_range=maximum_range,
        target_domains=target_domains,
        requires_unfolded=_optional_exact_bool(
            value.get("requires_unfolded"), f"{field}.requires_unfolded",
        ),
        can_fire_while_moving=_optional_exact_bool(
            value.get("can_fire_while_moving"),
            f"{field}.can_fire_while_moving",
        ),
        status=_nonempty_string(value.get("status"), f"{field}.status"),
    )


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OperatorRegistryError(f"{field} must be an object")
    return value  # type: ignore[return-value]


def _sequence(value: object, field: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise OperatorRegistryError(f"{field} must be an array")
    return value


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    result = tuple(
        _nonempty_string(item, f"{field}[]")
        for item in _sequence(value, field)
    )
    return result


def _nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OperatorRegistryError(f"{field} must be a non-empty string")
    return value


def _exact_int(value: object, field: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{field} must be an exact int")
    return value


def _nonnegative_exact_int(value: object, field: str) -> int:
    result = _exact_int(value, field)
    if result < 0:
        raise OperatorRegistryError(f"{field} must be non-negative")
    return result


def _optional_exact_bool(value: object, field: str) -> bool | None:
    if value is None:
        return None
    if type(value) is not bool:
        raise OperatorRegistryError(f"{field} must be an exact bool or null")
    return value
