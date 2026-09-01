"""Versioned deterministic seed authority for future Native learners."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import random
from typing import Any, Mapping

from wargame_native_engine.io.serialization import canonical_hash, canonical_json
from wargame_native_engine.training.learner_backend import (
    LearnerDependencyError,
    require_pytorch,
)


TRAINING_SEED_CONFIG_SCHEMA_VERSION = "native-training-seed-config/1.0"
TRAINING_SEED_CONFIG_ID = "native-training-seeds"
TRAINING_SEED_CONFIG_VERSION = "1.0.0"
TRAINING_SEED_DERIVATION_ALGORITHM = (
    "SHA256_CANONICAL_JSON_FIRST_63_BITS_V1"
)
MAX_TRAINING_SEED = (1 << 63) - 1


class TrainingSeedContractError(ValueError):
    """A seed config or derivation request violates the stable contract."""


class TrainingSeedStream(str, Enum):
    ENVIRONMENT = "ENVIRONMENT"
    POLICY_INITIALIZATION = "POLICY_INITIALIZATION"
    POLICY_SAMPLING = "POLICY_SAMPLING"
    MINIBATCH_SHUFFLE = "MINIBATCH_SHUFFLE"
    TORCH_CPU = "TORCH_CPU"
    TORCH_CUDA = "TORCH_CUDA"
    WORKER = "WORKER"
    EVALUATION = "EVALUATION"


TRAINING_SEED_STREAM_ORDER = tuple(TrainingSeedStream)


@dataclass(frozen=True, slots=True)
class TrainingSeedIdentity:
    config_id: str
    config_version: str
    config_hash: str

    def __post_init__(self) -> None:
        _nonempty(self.config_id, "seed config id")
        _nonempty(self.config_version, "seed config version")
        _sha256(self.config_hash, "seed config hash")

    def to_dict(self) -> dict[str, str]:
        return {
            "config_id": self.config_id,
            "config_version": self.config_version,
            "config_hash": self.config_hash,
        }


@dataclass(frozen=True, slots=True)
class TrainingSeedConfig:
    schema_version: str
    config_id: str
    config_version: str
    root_seed: int
    derivation_algorithm: str
    stream_namespaces: tuple[TrainingSeedStream, ...]
    content_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != TRAINING_SEED_CONFIG_SCHEMA_VERSION:
            raise TrainingSeedContractError("unsupported training seed schema")
        if self.config_id != TRAINING_SEED_CONFIG_ID or (
            self.config_version != TRAINING_SEED_CONFIG_VERSION
        ):
            raise TrainingSeedContractError("unsupported training seed identity")
        _seed(self.root_seed, "root_seed")
        if self.derivation_algorithm != TRAINING_SEED_DERIVATION_ALGORITHM:
            raise TrainingSeedContractError(
                "unsupported training seed derivation algorithm"
            )
        if self.stream_namespaces != TRAINING_SEED_STREAM_ORDER:
            raise TrainingSeedContractError(
                "seed stream namespaces must use the fixed v1 order"
            )
        expected = canonical_hash(self.semantic_payload())
        if self.content_hash != expected:
            raise TrainingSeedContractError("training seed config hash mismatch")

    @property
    def identity(self) -> TrainingSeedIdentity:
        return TrainingSeedIdentity(
            self.config_id, self.config_version, self.content_hash
        )

    def semantic_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "config_id": self.config_id,
            "config_version": self.config_version,
            "root_seed": self.root_seed,
            "derivation_algorithm": self.derivation_algorithm,
            "stream_namespaces": [item.value for item in self.stream_namespaces],
        }

    def to_dict(self) -> dict[str, object]:
        value = self.semantic_payload()
        value["config_hash"] = self.content_hash
        return value

    def derive_seed(
        self,
        stream: TrainingSeedStream | str,
        *,
        worker_index: int = 0,
        env_index: int = 0,
        episode_index: int = 0,
        stream_index: int = 0,
    ) -> int:
        namespace = (
            stream
            if isinstance(stream, TrainingSeedStream)
            else TrainingSeedStream(str(stream))
        )
        payload = {
            "derivation_algorithm": self.derivation_algorithm,
            "root_seed": self.root_seed,
            "namespace": namespace.value,
            "worker_index": _index(worker_index, "worker_index"),
            "env_index": _index(env_index, "env_index"),
            "episode_index": _index(episode_index, "episode_index"),
            "stream_index": _index(stream_index, "stream_index"),
        }
        digest = hashlib.sha256(
            canonical_json(payload).encode("utf-8")
        ).digest()
        return int.from_bytes(digest[:8], "big") & MAX_TRAINING_SEED


def build_training_seed_config(
    *,
    root_seed: int,
    config_id: str = TRAINING_SEED_CONFIG_ID,
    config_version: str = TRAINING_SEED_CONFIG_VERSION,
    derivation_algorithm: str = TRAINING_SEED_DERIVATION_ALGORITHM,
    stream_namespaces: tuple[TrainingSeedStream, ...] = (
        TRAINING_SEED_STREAM_ORDER
    ),
) -> TrainingSeedConfig:
    normalized = tuple(stream_namespaces)
    payload = {
        "schema_version": TRAINING_SEED_CONFIG_SCHEMA_VERSION,
        "config_id": _nonempty(config_id, "seed config id"),
        "config_version": _nonempty(config_version, "seed config version"),
        "root_seed": _seed(root_seed, "root_seed"),
        "derivation_algorithm": _nonempty(
            derivation_algorithm, "derivation_algorithm"
        ),
        "stream_namespaces": [item.value for item in normalized],
    }
    return TrainingSeedConfig(
        schema_version=TRAINING_SEED_CONFIG_SCHEMA_VERSION,
        config_id=payload["config_id"],
        config_version=payload["config_version"],
        root_seed=payload["root_seed"],
        derivation_algorithm=payload["derivation_algorithm"],
        stream_namespaces=normalized,
        content_hash=canonical_hash(payload),
    )


@lru_cache(maxsize=4)
def load_training_seed_config(
    path: str | Path | None = None,
) -> TrainingSeedConfig:
    source = (
        Path(path)
        if path is not None
        else Path(__file__).resolve().parents[3]
        / "config/training/native_training_seed_v1.json"
    )
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TrainingSeedContractError(
            f"invalid training seed config {source}: {exc}"
        ) from exc
    if not isinstance(value, Mapping):
        raise TrainingSeedContractError("training seed config must be an object")
    expected = {
        "schema_version", "config_id", "config_version", "root_seed",
        "derivation_algorithm", "stream_namespaces",
    }
    if set(value) != expected:
        raise TrainingSeedContractError(
            "training seed config fields are missing or unknown"
        )
    namespaces = value["stream_namespaces"]
    if not isinstance(namespaces, list):
        raise TrainingSeedContractError("stream_namespaces must be an array")
    try:
        streams = tuple(TrainingSeedStream(str(item)) for item in namespaces)
    except ValueError as exc:
        raise TrainingSeedContractError("unknown training seed namespace") from exc
    return build_training_seed_config(
        root_seed=value["root_seed"],
        config_id=str(value["config_id"]),
        config_version=str(value["config_version"]),
        derivation_algorithm=str(value["derivation_algorithm"]),
        stream_namespaces=streams,
    )


def python_random_for_stream(
    config: TrainingSeedConfig,
    stream: TrainingSeedStream | str,
    **coordinates: int,
) -> random.Random:
    """Return an isolated RNG; never modify Python's global random state."""

    if not isinstance(config, TrainingSeedConfig):
        raise TypeError("config must be TrainingSeedConfig")
    return random.Random(config.derive_seed(stream, **coordinates))


def seed_torch_cpu(
    config: TrainingSeedConfig,
    **coordinates: int,
) -> int:
    """Explicitly seed PyTorch's CPU default generator and return the seed."""

    if not isinstance(config, TrainingSeedConfig):
        raise TypeError("config must be TrainingSeedConfig")
    seed = config.derive_seed(TrainingSeedStream.TORCH_CPU, **coordinates)
    torch = require_pytorch()
    torch.random.default_generator.manual_seed(seed)
    return seed


def seed_torch_cuda(
    config: TrainingSeedConfig,
    **coordinates: int,
) -> int:
    """Explicit CUDA seed helper; it is never called during module import."""

    if not isinstance(config, TrainingSeedConfig):
        raise TypeError("config must be TrainingSeedConfig")
    torch = require_pytorch()
    if not bool(torch.cuda.is_available()):
        raise LearnerDependencyError(
            "CUSTOM_PYTORCH_PPO CUDA seed requested but CUDA is unavailable"
        )
    seed = config.derive_seed(TrainingSeedStream.TORCH_CUDA, **coordinates)
    torch.cuda.manual_seed_all(seed)
    return seed


def _seed(value: object, name: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_TRAINING_SEED:
        raise TrainingSeedContractError(
            f"{name} must be an exact int in [0, {MAX_TRAINING_SEED}]"
        )
    return value


def _index(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise TrainingSeedContractError(
            f"{name} must be a non-negative exact int"
        )
    return value


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise TrainingSeedContractError(
            f"{name} must be non-empty trimmed text"
        )
    return value


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        item not in "0123456789abcdef" for item in value
    ):
        raise TrainingSeedContractError(f"{name} must be lowercase SHA-256")
    return value


__all__ = [
    "MAX_TRAINING_SEED",
    "TRAINING_SEED_CONFIG_ID",
    "TRAINING_SEED_CONFIG_SCHEMA_VERSION",
    "TRAINING_SEED_CONFIG_VERSION",
    "TRAINING_SEED_DERIVATION_ALGORITHM",
    "TRAINING_SEED_STREAM_ORDER",
    "TrainingSeedConfig",
    "TrainingSeedContractError",
    "TrainingSeedIdentity",
    "TrainingSeedStream",
    "build_training_seed_config",
    "load_training_seed_config",
    "python_random_for_stream",
    "seed_torch_cpu",
    "seed_torch_cuda",
]
