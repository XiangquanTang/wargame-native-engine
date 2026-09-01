"""Versioned shared discount authority for learners and reward shaping.

No learner is implemented here.  This module only defines and loads the one
discount contract that future learner and shaping consumers must reference.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
import json
import math
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.io.serialization import canonical_hash


TRAINING_DISCOUNT_CONFIG_SCHEMA_VERSION = (
    "native-training-discount-config/1.0"
)


class TrainingDiscountContractError(ValueError):
    """A shared training discount config violates its machine contract."""


class TrainingDiscountBasis(str, Enum):
    PER_DECISION_TRANSITION = "PER_DECISION_TRANSITION"
    PER_SIMULATION_TIME_REFERENCE = "PER_SIMULATION_TIME_REFERENCE"


@dataclass(frozen=True, slots=True)
class TrainingDiscountIdentity:
    config_id: str
    config_version: str
    config_hash: str

    def __post_init__(self) -> None:
        _nonempty(self.config_id, "discount config id")
        _nonempty(self.config_version, "discount config version")
        _sha256(self.config_hash, "discount config hash")

    def to_dict(self) -> dict[str, str]:
        return {
            "config_id": self.config_id,
            "config_version": self.config_version,
            "config_hash": self.config_hash,
        }


@dataclass(frozen=True, slots=True)
class SharedTrainingDiscountConfig:
    schema_version: str
    config_id: str
    config_version: str
    gamma: float
    basis: TrainingDiscountBasis
    reference_interval_ms: int | None
    content_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != TRAINING_DISCOUNT_CONFIG_SCHEMA_VERSION:
            raise TrainingDiscountContractError(
                "unsupported training discount config schema"
            )
        _nonempty(self.config_id, "discount config id")
        _nonempty(self.config_version, "discount config version")
        gamma = _gamma(self.gamma)
        object.__setattr__(self, "gamma", gamma)
        if not isinstance(self.basis, TrainingDiscountBasis):
            raise TypeError("basis must be TrainingDiscountBasis")
        if self.basis is TrainingDiscountBasis.PER_DECISION_TRANSITION:
            if self.reference_interval_ms is not None:
                raise TrainingDiscountContractError(
                    "per-decision discount cannot define a reference interval"
                )
        else:
            if type(self.reference_interval_ms) is not int or (
                self.reference_interval_ms <= 0
            ):
                raise TrainingDiscountContractError(
                    "simulation-time discount requires a positive exact "
                    "reference_interval_ms"
                )
        expected = canonical_hash(self.semantic_payload())
        if self.content_hash != expected:
            raise TrainingDiscountContractError(
                "training discount config hash mismatch"
            )

    @property
    def identity(self) -> TrainingDiscountIdentity:
        return TrainingDiscountIdentity(
            self.config_id, self.config_version, self.content_hash
        )

    def semantic_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "config_id": self.config_id,
            "config_version": self.config_version,
            "gamma": self.gamma,
            "basis": self.basis.value,
            "reference_interval_ms": self.reference_interval_ms,
        }

    def to_dict(self) -> dict[str, object]:
        result = self.semantic_payload()
        result["config_hash"] = self.content_hash
        return result

    def effective_gamma(self, *, delta_time_ms: int) -> float:
        """Resolve the exact discount for one committed policy transition."""

        if type(delta_time_ms) is not int or delta_time_ms <= 0:
            raise TrainingDiscountContractError(
                "delta_time_ms must be a positive exact int"
            )
        if self.basis is TrainingDiscountBasis.PER_DECISION_TRANSITION:
            return self.gamma
        assert self.reference_interval_ms is not None
        result = self.gamma ** (delta_time_ms / self.reference_interval_ms)
        if not math.isfinite(result):
            raise TrainingDiscountContractError(
                "effective transition gamma must be finite"
            )
        return result


def build_shared_training_discount_config(
    *,
    config_id: str,
    config_version: str,
    gamma: int | float,
    basis: TrainingDiscountBasis | str,
    reference_interval_ms: int | None = None,
) -> SharedTrainingDiscountConfig:
    normalized_basis = (
        basis if isinstance(basis, TrainingDiscountBasis)
        else TrainingDiscountBasis(str(basis))
    )
    normalized_gamma = _gamma(gamma)
    payload = {
        "schema_version": TRAINING_DISCOUNT_CONFIG_SCHEMA_VERSION,
        "config_id": _nonempty(config_id, "discount config id"),
        "config_version": _nonempty(
            config_version, "discount config version"
        ),
        "gamma": normalized_gamma,
        "basis": normalized_basis.value,
        "reference_interval_ms": reference_interval_ms,
    }
    return SharedTrainingDiscountConfig(
        schema_version=TRAINING_DISCOUNT_CONFIG_SCHEMA_VERSION,
        config_id=payload["config_id"],
        config_version=payload["config_version"],
        gamma=normalized_gamma,
        basis=normalized_basis,
        reference_interval_ms=reference_interval_ms,
        content_hash=canonical_hash(payload),
    )


@lru_cache(maxsize=8)
def load_shared_training_discount_config(
    path: str | Path | None = None,
) -> SharedTrainingDiscountConfig:
    source = (
        Path(path)
        if path is not None
        else Path(__file__).resolve().parents[3]
        / "config/training/native_training_discount_v1.json"
    )
    root = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(root, Mapping):
        raise TrainingDiscountContractError(
            "training discount config root must be an object"
        )
    expected = {
        "schema_version", "config_id", "config_version", "gamma", "basis",
        "reference_interval_ms",
    }
    if set(root) != expected:
        raise TrainingDiscountContractError(
            "training discount config fields are missing or unknown"
        )
    if root["schema_version"] != TRAINING_DISCOUNT_CONFIG_SCHEMA_VERSION:
        raise TrainingDiscountContractError(
            "unsupported training discount config schema"
        )
    return build_shared_training_discount_config(
        config_id=root["config_id"],
        config_version=root["config_version"],
        gamma=root["gamma"],
        basis=root["basis"],
        reference_interval_ms=root["reference_interval_ms"],
    )


def require_discount_identity(
    config: SharedTrainingDiscountConfig,
    expected: TrainingDiscountIdentity,
) -> None:
    """Fail closed when a consumer and authority reference different configs."""

    if not isinstance(config, SharedTrainingDiscountConfig):
        raise TypeError("config must be SharedTrainingDiscountConfig")
    if not isinstance(expected, TrainingDiscountIdentity):
        raise TypeError("expected must be TrainingDiscountIdentity")
    if config.identity != expected:
        raise TrainingDiscountContractError(
            "training discount authority identity mismatch"
        )


def _gamma(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("gamma must be a finite number (not bool)")
    result = float(value)
    if not math.isfinite(result):
        raise TrainingDiscountContractError("gamma must be finite")
    if result < 0.0 or result > 1.0:
        raise TrainingDiscountContractError("gamma must be in [0, 1]")
    return result


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise TrainingDiscountContractError(
            f"{name} must be a non-empty trimmed string"
        )
    return value


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise TrainingDiscountContractError(
            f"{name} must be lowercase SHA-256"
        )
    return value


__all__ = [
    "TRAINING_DISCOUNT_CONFIG_SCHEMA_VERSION",
    "SharedTrainingDiscountConfig",
    "TrainingDiscountBasis",
    "TrainingDiscountContractError",
    "TrainingDiscountIdentity",
    "build_shared_training_discount_config",
    "load_shared_training_discount_config",
    "require_discount_identity",
]
