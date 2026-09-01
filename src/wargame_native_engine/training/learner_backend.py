"""Optional PyTorch backend authority for future Native learners.

Importing this module never imports PyTorch.  Only an explicit learner runtime
request crosses the optional dependency boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import importlib
import json
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.io.serialization import canonical_hash


LEARNER_BACKEND_SCHEMA_VERSION = "native-learner-backend-authority/1.0"
CUSTOM_PYTORCH_BACKEND_ID = "custom-pytorch-ppo"
CUSTOM_PYTORCH_BACKEND_VERSION = "1.0.0"
CUSTOM_PYTORCH_BACKEND_KIND = "CUSTOM_PYTORCH"
PYTORCH_FRAMEWORK_COMPATIBILITY = ">=2.2,<3"


class LearnerBackendContractError(ValueError):
    """The configured learner backend violates its stable contract."""


class LearnerDependencyError(RuntimeError):
    """The explicitly selected optional learner dependency is unavailable."""


@dataclass(frozen=True, slots=True)
class LearnerBackendAuthority:
    schema_version: str
    backend_id: str
    backend_version: str
    backend_kind: str
    framework: str
    framework_compatibility: str
    future_algorithm: str
    semantic_hash: str

    def __post_init__(self) -> None:
        expected = {
            "schema_version": LEARNER_BACKEND_SCHEMA_VERSION,
            "backend_id": CUSTOM_PYTORCH_BACKEND_ID,
            "backend_version": CUSTOM_PYTORCH_BACKEND_VERSION,
            "backend_kind": CUSTOM_PYTORCH_BACKEND_KIND,
            "framework": "pytorch",
            "framework_compatibility": PYTORCH_FRAMEWORK_COMPATIBILITY,
            "future_algorithm": "PPO",
        }
        if self.semantic_payload() != expected:
            raise LearnerBackendContractError(
                "unsupported Native learner backend authority"
            )
        if self.semantic_hash != canonical_hash(expected):
            raise LearnerBackendContractError(
                "learner backend semantic hash mismatch"
            )

    def semantic_payload(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "backend_id": self.backend_id,
            "backend_version": self.backend_version,
            "backend_kind": self.backend_kind,
            "framework": self.framework,
            "framework_compatibility": self.framework_compatibility,
            "future_algorithm": self.future_algorithm,
        }

    def to_dict(self) -> dict[str, str]:
        value = self.semantic_payload()
        value["semantic_hash"] = self.semantic_hash
        return value


@dataclass(frozen=True, slots=True)
class LearnerBackendRuntimeProvenance:
    """Runtime framework version, deliberately outside semantic identity."""

    backend_semantic_hash: str
    framework: str
    framework_version: str

    def __post_init__(self) -> None:
        _sha256(self.backend_semantic_hash, "backend semantic hash")
        _nonempty(self.framework, "framework")
        _nonempty(self.framework_version, "framework version")

    def to_dict(self) -> dict[str, str]:
        return {
            "backend_semantic_hash": self.backend_semantic_hash,
            "framework": self.framework,
            "framework_version": self.framework_version,
        }


def build_learner_backend_authority(
    value: Mapping[str, object],
) -> LearnerBackendAuthority:
    if not isinstance(value, Mapping):
        raise TypeError("learner backend config must be a Mapping")
    expected_fields = {
        "schema_version",
        "backend_id",
        "backend_version",
        "backend_kind",
        "framework",
        "framework_compatibility",
        "future_algorithm",
    }
    if set(value) != expected_fields:
        raise LearnerBackendContractError(
            "learner backend fields are missing or unknown"
        )
    payload = {name: _nonempty(value[name], name) for name in sorted(value)}
    return LearnerBackendAuthority(
        schema_version=payload["schema_version"],
        backend_id=payload["backend_id"],
        backend_version=payload["backend_version"],
        backend_kind=payload["backend_kind"],
        framework=payload["framework"],
        framework_compatibility=payload["framework_compatibility"],
        future_algorithm=payload["future_algorithm"],
        semantic_hash=canonical_hash(payload),
    )


@lru_cache(maxsize=4)
def load_learner_backend_authority(
    path: str | Path | None = None,
) -> LearnerBackendAuthority:
    source = (
        Path(path)
        if path is not None
        else Path(__file__).resolve().parents[3]
        / "config/training/native_learner_backend_v1.json"
    )
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LearnerBackendContractError(
            f"invalid learner backend authority {source}: {exc}"
        ) from exc
    return build_learner_backend_authority(value)


def require_pytorch(
    authority: LearnerBackendAuthority | None = None,
) -> Any:
    """Load the optional framework only after an explicit learner request."""

    expected = load_learner_backend_authority() if authority is None else authority
    if not isinstance(expected, LearnerBackendAuthority):
        raise TypeError("authority must be LearnerBackendAuthority")
    try:
        torch = importlib.import_module("torch")
    except ModuleNotFoundError as exc:
        raise LearnerDependencyError(
            "CUSTOM_PYTORCH_PPO requires the optional learner dependency; "
            "install with `pip install -e '.[learner]'`"
        ) from exc
    version = getattr(torch, "__version__", None)
    if not isinstance(version, str) or not _compatible_torch_version(version):
        raise LearnerDependencyError(
            "CUSTOM_PYTORCH_PPO requires PyTorch >=2.2,<3; "
            f"found {version!r}"
        )
    return torch


def learner_backend_runtime_provenance(
    authority: LearnerBackendAuthority | None = None,
) -> LearnerBackendRuntimeProvenance:
    expected = load_learner_backend_authority() if authority is None else authority
    torch = require_pytorch(expected)
    return LearnerBackendRuntimeProvenance(
        expected.semantic_hash,
        expected.framework,
        str(torch.__version__),
    )


def _compatible_torch_version(value: str) -> bool:
    core = value.split("+", 1)[0]
    parts = core.split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1])
    except (IndexError, ValueError):
        return False
    return major == 2 and minor >= 2


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise LearnerBackendContractError(
            f"{name} must be non-empty trimmed text"
        )
    return value


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        item not in "0123456789abcdef" for item in value
    ):
        raise LearnerBackendContractError(f"{name} must be lowercase SHA-256")
    return value


__all__ = [
    "CUSTOM_PYTORCH_BACKEND_ID",
    "CUSTOM_PYTORCH_BACKEND_KIND",
    "CUSTOM_PYTORCH_BACKEND_VERSION",
    "LEARNER_BACKEND_SCHEMA_VERSION",
    "PYTORCH_FRAMEWORK_COMPATIBILITY",
    "LearnerBackendAuthority",
    "LearnerBackendContractError",
    "LearnerBackendRuntimeProvenance",
    "LearnerDependencyError",
    "build_learner_backend_authority",
    "learner_backend_runtime_provenance",
    "load_learner_backend_authority",
    "require_pytorch",
]
