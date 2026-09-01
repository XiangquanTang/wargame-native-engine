"""Versioned synchronous multiprocessing Native PPO runner authority."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import math
from pathlib import Path
from typing import Mapping

from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.training.ppo_config import (
    PPOAlgorithmConfig, PPOAuthorityReference, load_ppo_algorithm_config,
)
from wargame_native_engine.training.ppo_parallel_protocol import (
    PPO_PARALLEL_WORKER_PROTOCOL_HASH,
    PPO_PARALLEL_WORKER_PROTOCOL_ID,
    PPO_PARALLEL_WORKER_PROTOCOL_VERSION,
)
from wargame_native_engine.training.ppo_runner_config import PPORewardMode
from wargame_native_engine.training.ppo_vector_runner_config import (
    PPOVectorRunnerConfig, load_ppo_vector_runner_config,
)
from wargame_native_engine.training.training_seed import (
    TrainingSeedConfig, load_training_seed_config,
)


PPO_PARALLEL_RUNNER_CONFIG_SCHEMA_VERSION = "native-ppo-parallel-runner-config/1.0"
PPO_PARALLEL_RUNNER_ID = "native-parallel-ppo-runner"
PPO_PARALLEL_RUNNER_VERSION = "1.0.0"


class PPOParallelRunnerConfigError(ValueError):
    """Parallel execution config is invalid or references drifted authority."""


@dataclass(frozen=True, slots=True)
class PPOParallelRunnerConfig:
    schema_version: str
    runner_id: str
    runner_version: str
    purpose: str
    base_vector_runner: PPOAuthorityReference
    worker_protocol: PPOAuthorityReference
    num_envs: int
    num_workers: int
    execution_mode: str
    start_method: str
    trained_side: str
    reward_mode: PPORewardMode
    ppo_config: PPOAuthorityReference
    seed_config: PPOAuthorityReference
    rollout_target_semantics: str
    aggregation_order: str
    failure_policy: str
    auto_restart: bool
    worker_timeout_seconds: float
    content_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != PPO_PARALLEL_RUNNER_CONFIG_SCHEMA_VERSION:
            raise PPOParallelRunnerConfigError("unsupported parallel runner schema")
        if (self.runner_id, self.runner_version) != (
            PPO_PARALLEL_RUNNER_ID, PPO_PARALLEL_RUNNER_VERSION
        ):
            raise PPOParallelRunnerConfigError("unsupported parallel runner identity")
        if self.purpose not in {"BASELINE_V1", "TEST_SMOKE"}:
            raise PPOParallelRunnerConfigError("parallel purpose is invalid")
        if type(self.num_envs) is not int or self.num_envs < 1 or (
            self.num_workers != self.num_envs
        ):
            raise PPOParallelRunnerConfigError("v1 requires positive num_workers == num_envs")
        expected = (
            "SYNCHRONOUS_MULTIPROCESS", "SPAWN",
            "TOTAL_TRANSITIONS_PER_UPDATE", "STABLE_ENV_INDEX",
            "FAIL_WHOLE_ROLLOUT", False,
        )
        actual = (
            self.execution_mode, self.start_method,
            self.rollout_target_semantics, self.aggregation_order,
            self.failure_policy, self.auto_restart,
        )
        if actual != expected:
            raise PPOParallelRunnerConfigError("unsupported parallel v1 semantics")
        if self.trained_side not in {"RED", "BLUE"}:
            raise PPOParallelRunnerConfigError("trained side is invalid")
        if not isinstance(self.reward_mode, PPORewardMode):
            raise TypeError("reward_mode must be PPORewardMode")
        if isinstance(self.worker_timeout_seconds, bool) or not isinstance(
            self.worker_timeout_seconds, (int, float)
        ) or not math.isfinite(self.worker_timeout_seconds) or self.worker_timeout_seconds <= 0:
            raise PPOParallelRunnerConfigError("worker timeout must be positive finite")
        if self.content_hash != canonical_hash(self.semantic_payload()):
            raise PPOParallelRunnerConfigError("parallel runner hash mismatch")

    def semantic_payload(self) -> dict[str, object]:
        """Learning/execution semantics; operational timeout is intentionally excluded."""

        return {
            "schema_version": self.schema_version,
            "runner_id": self.runner_id,
            "runner_version": self.runner_version,
            "purpose": self.purpose,
            "base_vector_runner_ref": self.base_vector_runner.to_dict(),
            "worker_protocol_ref": self.worker_protocol.to_dict(),
            "num_envs": self.num_envs,
            "num_workers": self.num_workers,
            "execution_mode": self.execution_mode,
            "start_method": self.start_method,
            "trained_side": self.trained_side,
            "reward_mode": self.reward_mode.value,
            "ppo_config_ref": self.ppo_config.to_dict(),
            "seed_config_ref": self.seed_config.to_dict(),
            "rollout_target_semantics": self.rollout_target_semantics,
            "aggregation_order": self.aggregation_order,
            "failure_policy": self.failure_policy,
            "auto_restart": self.auto_restart,
        }

    def to_dict(self) -> dict[str, object]:
        value = self.semantic_payload()
        value["operational"] = {"worker_timeout_seconds": self.worker_timeout_seconds}
        value["config_hash"] = self.content_hash
        return value


def build_ppo_parallel_runner_config(
    value: Mapping[str, object], *,
    vector: PPOVectorRunnerConfig | None = None,
    ppo: PPOAlgorithmConfig | None = None,
    seed: TrainingSeedConfig | None = None,
) -> PPOParallelRunnerConfig:
    if not isinstance(value, Mapping):
        raise TypeError("parallel config must be a mapping")
    expected = {
        "schema_version", "runner_id", "runner_version", "purpose",
        "base_vector_runner_ref", "worker_protocol_ref", "num_envs",
        "num_workers", "execution_mode", "start_method", "trained_side",
        "reward_mode", "ppo_config_ref", "seed_config_ref",
        "rollout_target_semantics", "aggregation_order", "failure_policy",
        "auto_restart", "operational",
    }
    if set(value) != expected:
        raise PPOParallelRunnerConfigError("parallel config fields missing or unknown")
    actual_vector = load_ppo_vector_runner_config() if vector is None else vector
    actual_ppo = load_ppo_algorithm_config() if ppo is None else ppo
    actual_seed = load_training_seed_config() if seed is None else seed
    vector_ref = _ref(value["base_vector_runner_ref"])
    protocol_ref = _ref(value["worker_protocol_ref"])
    ppo_ref = _ref(value["ppo_config_ref"])
    seed_ref = _ref(value["seed_config_ref"])
    if vector_ref != PPOAuthorityReference(
        actual_vector.runner_id, actual_vector.runner_version, actual_vector.content_hash
    ):
        raise PPOParallelRunnerConfigError("base vector reference mismatch")
    if protocol_ref != PPOAuthorityReference(
        PPO_PARALLEL_WORKER_PROTOCOL_ID, PPO_PARALLEL_WORKER_PROTOCOL_VERSION,
        PPO_PARALLEL_WORKER_PROTOCOL_HASH,
    ):
        raise PPOParallelRunnerConfigError("worker protocol reference mismatch")
    if ppo_ref != PPOAuthorityReference(
        actual_ppo.algorithm_id, actual_ppo.algorithm_version, actual_ppo.content_hash
    ) or ppo_ref != actual_vector.ppo_config:
        raise PPOParallelRunnerConfigError("parallel PPO reference mismatch")
    if seed_ref != PPOAuthorityReference(
        actual_seed.config_id, actual_seed.config_version, actual_seed.content_hash
    ) or seed_ref != actual_vector.seed_config:
        raise PPOParallelRunnerConfigError("parallel seed reference mismatch")
    operational = _mapping(value["operational"])
    if set(operational) != {"worker_timeout_seconds"}:
        raise PPOParallelRunnerConfigError("parallel operational fields invalid")
    common = dict(
        schema_version=str(value["schema_version"]), runner_id=str(value["runner_id"]),
        runner_version=str(value["runner_version"]), purpose=str(value["purpose"]),
        base_vector_runner=vector_ref, worker_protocol=protocol_ref,
        num_envs=_positive(value["num_envs"]), num_workers=_positive(value["num_workers"]),
        execution_mode=str(value["execution_mode"]), start_method=str(value["start_method"]),
        trained_side=str(value["trained_side"]),
        reward_mode=PPORewardMode(str(value["reward_mode"])),
        ppo_config=ppo_ref, seed_config=seed_ref,
        rollout_target_semantics=str(value["rollout_target_semantics"]),
        aggregation_order=str(value["aggregation_order"]),
        failure_policy=str(value["failure_policy"]),
        auto_restart=_bool(value["auto_restart"]),
        worker_timeout_seconds=_positive_float(operational["worker_timeout_seconds"]),
    )
    if common["num_envs"] != actual_vector.num_envs or (
        common["trained_side"] != actual_vector.trained_side
    ) or common["reward_mode"] is not actual_vector.reward_mode:
        raise PPOParallelRunnerConfigError("parallel/vector semantic mismatch")
    temporary = object.__new__(PPOParallelRunnerConfig)
    for name, item in common.items():
        object.__setattr__(temporary, name, item)
    return PPOParallelRunnerConfig(
        **common, content_hash=canonical_hash(temporary.semantic_payload())
    )


@lru_cache(maxsize=8)
def load_ppo_parallel_runner_config(path: str | Path | None = None) -> PPOParallelRunnerConfig:
    source = Path(path) if path is not None else (
        Path(__file__).resolve().parents[3] / "config/training/native_ppo_parallel_runner_v1.json"
    )
    return build_ppo_parallel_runner_config(json.loads(source.read_text(encoding="utf-8")))


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PPOParallelRunnerConfigError("expected object")
    return value


def _ref(value: object) -> PPOAuthorityReference:
    item = _mapping(value)
    if set(item) != {"id", "version", "hash"}:
        raise PPOParallelRunnerConfigError("authority reference fields invalid")
    return PPOAuthorityReference(str(item["id"]), str(item["version"]), str(item["hash"]))


def _positive(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise PPOParallelRunnerConfigError("expected positive integer")
    return value


def _positive_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise PPOParallelRunnerConfigError("expected positive finite number")
    return float(value)


def _bool(value: object) -> bool:
    if type(value) is not bool:
        raise TypeError("expected exact bool")
    return value


__all__ = [
    "PPO_PARALLEL_RUNNER_CONFIG_SCHEMA_VERSION", "PPO_PARALLEL_RUNNER_ID",
    "PPO_PARALLEL_RUNNER_VERSION", "PPOParallelRunnerConfig",
    "PPOParallelRunnerConfigError", "build_ppo_parallel_runner_config",
    "load_ppo_parallel_runner_config",
]
