from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias


class FireMode(str, Enum):
    """Stable fire-mode identities for the internal fire contract."""

    DIRECT = "DIRECT"
    GUIDED = "GUIDED"
    INDIRECT = "INDIRECT"


def _require_identifier(value: object, field_name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field_name} must be a non-negative exact int")


@dataclass(frozen=True, slots=True)
class UnitTarget:
    unit_id: int

    def __post_init__(self) -> None:
        _require_identifier(self.unit_id, "unit_id")


@dataclass(frozen=True, slots=True)
class HexTarget:
    hex_id: int

    def __post_init__(self) -> None:
        _require_identifier(self.hex_id, "hex_id")


FireTarget: TypeAlias = UnitTarget | HexTarget


@dataclass(frozen=True, slots=True)
class FireIntent:
    """Typed execution-time fire intent detached from action serialization.

    This contract deliberately has no free-form metadata.  It does not replace
    ``EngineAction`` in replay evidence; legacy ``SHOOT`` actions are converted
    only when the authoritative execution path begins adjudication.
    """

    mode: FireMode
    shooter_id: int
    weapon_id: int
    target: FireTarget
    guider_id: int | None = None
    observer_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, FireMode):
            raise TypeError("mode must be FireMode")
        _require_identifier(self.shooter_id, "shooter_id")
        _require_identifier(self.weapon_id, "weapon_id")
        if not isinstance(self.target, (UnitTarget, HexTarget)):
            raise TypeError("target must be UnitTarget or HexTarget")
        if self.guider_id is not None:
            _require_identifier(self.guider_id, "guider_id")
        if self.observer_id is not None:
            _require_identifier(self.observer_id, "observer_id")


@dataclass(frozen=True, slots=True, init=False)
class IndirectFireIntent:
    """Authority-bound execution contract created only by ``NativeEngine``.

    ``FireIntent`` is the untrusted transport normalization.  This second
    contract exists only after the Engine has rebound that input to one sealed
    pre-state, one current capability, and the exact versioned authorities that
    will later be required by strict replay.
    """

    tick: int
    shooter_id: int
    weapon_id: int
    target: HexTarget
    execution_policy_id: str
    execution_policy_hash: str
    authority_version: str
    authority_hash: str
    ammo_mapping_hash: str
    observation_policy_hash: str
    damage_policy_hash: str

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("indirect_fire_intent_requires_engine_validation")


def _build_validated_indirect_fire_intent(
    *,
    tick: int,
    shooter_id: int,
    weapon_id: int,
    target: HexTarget,
    execution_policy_id: str,
    execution_policy_hash: str,
    authority_version: str,
    authority_hash: str,
    ammo_mapping_hash: str,
    observation_policy_hash: str,
    damage_policy_hash: str,
) -> IndirectFireIntent:
    values = {
        "tick": tick,
        "shooter_id": shooter_id,
        "weapon_id": weapon_id,
        "target": target,
        "execution_policy_id": execution_policy_id,
        "execution_policy_hash": execution_policy_hash,
        "authority_version": authority_version,
        "authority_hash": authority_hash,
        "ammo_mapping_hash": ammo_mapping_hash,
        "observation_policy_hash": observation_policy_hash,
        "damage_policy_hash": damage_policy_hash,
    }
    _require_identifier(tick, "tick")
    _require_identifier(shooter_id, "shooter_id")
    _require_identifier(weapon_id, "weapon_id")
    if not isinstance(target, HexTarget):
        raise TypeError("target must be HexTarget")
    for name in ("execution_policy_id", "authority_version"):
        value = values[name]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
    for name in (
        "execution_policy_hash",
        "authority_hash",
        "ammo_mapping_hash",
        "observation_policy_hash",
        "damage_policy_hash",
    ):
        value = values[name]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError(f"{name} must be lowercase SHA-256")
    instance = object.__new__(IndirectFireIntent)
    for name, value in values.items():
        object.__setattr__(instance, name, value)
    return instance


__all__ = [
    "FireIntent",
    "FireMode",
    "FireTarget",
    "HexTarget",
    "IndirectFireIntent",
    "UnitTarget",
]
