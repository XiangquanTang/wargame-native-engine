"""Versioned single-environment Native PPO runner authority."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
import json
from pathlib import Path
from typing import Mapping

from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.training.ppo_config import (
    PPOAlgorithmConfig,
    PPOAuthorityReference,
    load_ppo_algorithm_config,
)
from wargame_native_engine.training.training_seed import (
    TrainingSeedConfig,
    load_training_seed_config,
)


PPO_RUNNER_CONFIG_SCHEMA_VERSION = "native-ppo-runner-config/1.0"
PPO_RUNNER_ID = "native-single-seat-ppo-runner"
PPO_RUNNER_VERSION = "1.0.0"


class PPORunnerConfigError(ValueError):
    """Runner config is incomplete or references incompatible authority."""


class PPORewardMode(str, Enum):
    BASE_ONLY = "BASE_ONLY"
    COMPOSITE_EIGHT_FACTOR = "COMPOSITE_EIGHT_FACTOR"


@dataclass(frozen=True, slots=True)
class PPORunLimits:
    max_updates: int | None
    max_environment_steps: int | None
    max_episodes: int | None

    def __post_init__(self) -> None:
        for name in ("max_updates", "max_environment_steps", "max_episodes"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value <= 0):
                raise PPORunnerConfigError(f"{name} must be positive or null")
        if all(value is None for value in (
            self.max_updates, self.max_environment_steps, self.max_episodes
        )):
            raise PPORunnerConfigError("at least one runner stop limit is required")

    def to_dict(self) -> dict[str, object]:
        return {
            "max_updates": self.max_updates,
            "max_environment_steps": self.max_environment_steps,
            "max_episodes": self.max_episodes,
        }


@dataclass(frozen=True, slots=True)
class PPORunnerConfig:
    schema_version: str
    runner_id: str
    runner_version: str
    purpose: str
    mode: str
    trained_side: str
    opponent_kind: str
    opponent_id: str
    opponent_deterministic: bool
    reward_mode: PPORewardMode
    ppo_config: PPOAuthorityReference
    seed_config: PPOAuthorityReference
    worker_index: int
    env_index: int
    run_limits: PPORunLimits
    checkpoint_every_updates: int | None
    evaluation_episodes: int
    rollout_model_mode: str
    device: str
    content_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != PPO_RUNNER_CONFIG_SCHEMA_VERSION:
            raise PPORunnerConfigError("unsupported PPO runner config schema")
        if (self.runner_id, self.runner_version) != (PPO_RUNNER_ID, PPO_RUNNER_VERSION):
            raise PPORunnerConfigError("unsupported PPO runner identity")
        if self.purpose not in {"BASELINE_V1", "TEST_SMOKE"}:
            raise PPORunnerConfigError("runner purpose is invalid")
        if self.mode != "TRAINED_SIDE_VS_FIXED_OPPONENT":
            raise PPORunnerConfigError("runner v1 requires fixed-opponent mode")
        if self.trained_side not in {"RED", "BLUE"}:
            raise PPORunnerConfigError("trained_side must be RED or BLUE")
        if (self.opponent_kind, self.opponent_id, self.opponent_deterministic) != (
            "NO_OP_AGENT", "native-no-op-fixed-opponent-v1", True
        ):
            raise PPORunnerConfigError("runner v1 requires deterministic NoOpAgent")
        if not isinstance(self.reward_mode, PPORewardMode):
            raise TypeError("reward_mode must be PPORewardMode")
        if not isinstance(self.ppo_config, PPOAuthorityReference) or not isinstance(
            self.seed_config, PPOAuthorityReference
        ):
            raise TypeError("runner authority references must be typed")
        for name in ("worker_index", "env_index"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise PPORunnerConfigError(f"{name} must be non-negative")
        if not isinstance(self.run_limits, PPORunLimits):
            raise TypeError("run_limits must be PPORunLimits")
        if self.checkpoint_every_updates is not None and (
            type(self.checkpoint_every_updates) is not int
            or self.checkpoint_every_updates <= 0
        ):
            raise PPORunnerConfigError("checkpoint cadence must be positive or null")
        if type(self.evaluation_episodes) is not int or self.evaluation_episodes <= 0:
            raise PPORunnerConfigError("evaluation_episodes must be positive")
        if self.rollout_model_mode != "EVAL_NO_GRAD" or self.device != "CPU":
            raise PPORunnerConfigError("runner v1 requires EVAL_NO_GRAD on CPU")
        if self.content_hash != canonical_hash(self.semantic_payload()):
            raise PPORunnerConfigError("PPO runner config hash mismatch")

    def semantic_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "runner_id": self.runner_id,
            "runner_version": self.runner_version,
            "purpose": self.purpose,
            "mode": self.mode,
            "trained_side": self.trained_side,
            "fixed_opponent": {
                "kind": self.opponent_kind,
                "id": self.opponent_id,
                "deterministic": self.opponent_deterministic,
            },
            "reward_mode": self.reward_mode.value,
            "ppo_config_ref": self.ppo_config.to_dict(),
            "seed_config_ref": self.seed_config.to_dict(),
            "worker_index": self.worker_index,
            "env_index": self.env_index,
            "run_limits": self.run_limits.to_dict(),
            "checkpoint_every_updates": self.checkpoint_every_updates,
            "evaluation_episodes": self.evaluation_episodes,
            "rollout_model_mode": self.rollout_model_mode,
            "device": self.device,
        }

    def to_dict(self) -> dict[str, object]:
        result = self.semantic_payload()
        result["config_hash"] = self.content_hash
        return result


def build_ppo_runner_config(
    value: Mapping[str, object],
    *,
    ppo: PPOAlgorithmConfig | None = None,
    seed: TrainingSeedConfig | None = None,
) -> PPORunnerConfig:
    if not isinstance(value, Mapping):
        raise TypeError("runner config must be a mapping")
    expected = {
        "schema_version", "runner_id", "runner_version", "purpose", "mode",
        "trained_side", "fixed_opponent", "reward_mode", "ppo_config_ref",
        "seed_config_ref", "worker_index", "env_index", "run_limits",
        "checkpoint_every_updates", "evaluation_episodes",
        "rollout_model_mode", "device",
    }
    if set(value) != expected:
        raise PPORunnerConfigError("runner config fields are missing or unknown")
    opponent = _mapping(value["fixed_opponent"], "fixed_opponent")
    limits = _mapping(value["run_limits"], "run_limits")
    actual_ppo = load_ppo_algorithm_config() if ppo is None else ppo
    actual_seed = load_training_seed_config() if seed is None else seed
    ppo_ref = _reference(value["ppo_config_ref"], "ppo_config_ref")
    seed_ref = _reference(value["seed_config_ref"], "seed_config_ref")
    expected_ppo = PPOAuthorityReference(
        actual_ppo.algorithm_id, actual_ppo.algorithm_version, actual_ppo.content_hash
    )
    expected_seed = PPOAuthorityReference(
        actual_seed.config_id, actual_seed.config_version, actual_seed.content_hash
    )
    if ppo_ref != expected_ppo or seed_ref != expected_seed:
        raise PPORunnerConfigError("runner referenced authority mismatch")
    common = dict(
        schema_version=str(value["schema_version"]),
        runner_id=str(value["runner_id"]), runner_version=str(value["runner_version"]),
        purpose=str(value["purpose"]), mode=str(value["mode"]),
        trained_side=str(value["trained_side"]),
        opponent_kind=str(opponent["kind"]), opponent_id=str(opponent["id"]),
        opponent_deterministic=_bool(opponent["deterministic"], "opponent deterministic"),
        reward_mode=PPORewardMode(str(value["reward_mode"])),
        ppo_config=ppo_ref, seed_config=seed_ref,
        worker_index=_nonnegative(value["worker_index"], "worker_index"),
        env_index=_nonnegative(value["env_index"], "env_index"),
        run_limits=PPORunLimits(
            _optional_positive(limits["max_updates"], "max_updates"),
            _optional_positive(limits["max_environment_steps"], "max_environment_steps"),
            _optional_positive(limits["max_episodes"], "max_episodes"),
        ),
        checkpoint_every_updates=_optional_positive(
            value["checkpoint_every_updates"], "checkpoint_every_updates"
        ),
        evaluation_episodes=_positive(value["evaluation_episodes"], "evaluation_episodes"),
        rollout_model_mode=str(value["rollout_model_mode"]), device=str(value["device"]),
    )
    temporary = object.__new__(PPORunnerConfig)
    for name, item in common.items():
        object.__setattr__(temporary, name, item)
    return PPORunnerConfig(
        **common, content_hash=canonical_hash(temporary.semantic_payload())
    )


@lru_cache(maxsize=8)
def load_ppo_runner_config(path: str | Path | None = None) -> PPORunnerConfig:
    source = Path(path) if path is not None else (
        Path(__file__).resolve().parents[3]
        / "config/training/native_ppo_runner_v1.json"
    )
    return build_ppo_runner_config(json.loads(source.read_text(encoding="utf-8")))


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PPORunnerConfigError(f"{name} must be an object")
    return value


def _reference(value: object, name: str) -> PPOAuthorityReference:
    item = _mapping(value, name)
    if set(item) != {"id", "version", "hash"}:
        raise PPORunnerConfigError(f"{name} fields are invalid")
    return PPOAuthorityReference(str(item["id"]), str(item["version"]), str(item["hash"]))


def _bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be an exact bool")
    return value


def _nonnegative(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise PPORunnerConfigError(f"{name} must be non-negative")
    return value


def _positive(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise PPORunnerConfigError(f"{name} must be positive")
    return value


def _optional_positive(value: object, name: str) -> int | None:
    return None if value is None else _positive(value, name)
