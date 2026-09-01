"""Versioned synchronous-vector Native PPO runner authority."""

from __future__ import annotations

from dataclasses import dataclass
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
from wargame_native_engine.training.ppo_runner_config import (
    PPORunnerConfig,
    PPORewardMode,
    load_ppo_runner_config,
)
from wargame_native_engine.training.training_seed import (
    TrainingSeedConfig,
    load_training_seed_config,
)


PPO_VECTOR_RUNNER_CONFIG_SCHEMA_VERSION = "native-ppo-vector-runner-config/1.0"
PPO_VECTOR_RUNNER_ID = "native-synchronous-vector-ppo-runner"
PPO_VECTOR_RUNNER_VERSION = "1.0.0"


class PPOVectorRunnerConfigError(ValueError):
    """Vector collection authority is incomplete or inconsistent."""


@dataclass(frozen=True, slots=True)
class PPOVectorRunnerConfig:
    schema_version: str
    runner_id: str
    runner_version: str
    purpose: str
    base_runner_config: PPOAuthorityReference
    num_envs: int
    trained_side: str
    reward_mode: PPORewardMode
    ppo_config: PPOAuthorityReference
    seed_config: PPOAuthorityReference
    rollout_target_source: str
    rollout_target_semantics: str
    transitions_per_update: int
    collection_mode: str
    reset_policy: str
    scenario_assignment: str
    flatten_order: str
    error_policy: str
    reward_verification: str
    device: str
    content_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != PPO_VECTOR_RUNNER_CONFIG_SCHEMA_VERSION:
            raise PPOVectorRunnerConfigError("unsupported vector runner schema")
        if (self.runner_id, self.runner_version) != (
            PPO_VECTOR_RUNNER_ID, PPO_VECTOR_RUNNER_VERSION
        ):
            raise PPOVectorRunnerConfigError("unsupported vector runner identity")
        if self.purpose not in {"BASELINE_V1", "TEST_SMOKE"}:
            raise PPOVectorRunnerConfigError("vector runner purpose is invalid")
        if type(self.num_envs) is not int or self.num_envs < 1:
            raise PPOVectorRunnerConfigError("num_envs must be positive")
        if self.trained_side not in {"RED", "BLUE"}:
            raise PPOVectorRunnerConfigError("trained_side must be RED or BLUE")
        if not isinstance(self.reward_mode, PPORewardMode):
            raise TypeError("reward_mode must be PPORewardMode")
        if self.rollout_target_source != "PPO_CONFIG" or (
            self.rollout_target_semantics != "TOTAL_TRANSITIONS_PER_UPDATE"
        ):
            raise PPOVectorRunnerConfigError("unsupported rollout target authority")
        if type(self.transitions_per_update) is not int or (
            self.transitions_per_update <= 0
        ):
            raise PPOVectorRunnerConfigError("transitions_per_update must be positive")
        expected = (
            "SYNCHRONOUS", "AUTO_RESET_COMPLETED_ENV",
            "SAME_SCENARIO_PER_ENV_DIFFERENT_DERIVED_SEEDS",
            "ENV_MAJOR_THEN_COLLECTION_ORDER", "FAIL_WHOLE_VECTOR",
            "VERIFY_EVERY_COMPLETED_EPISODE", "CPU",
        )
        actual = (
            self.collection_mode, self.reset_policy, self.scenario_assignment,
            self.flatten_order, self.error_policy, self.reward_verification,
            self.device,
        )
        if actual != expected:
            raise PPOVectorRunnerConfigError("unsupported vector runner v1 semantics")
        if self.content_hash != canonical_hash(self.semantic_payload()):
            raise PPOVectorRunnerConfigError("vector runner config hash mismatch")

    def semantic_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "runner_id": self.runner_id,
            "runner_version": self.runner_version,
            "purpose": self.purpose,
            "base_runner_config_ref": self.base_runner_config.to_dict(),
            "num_envs": self.num_envs,
            "trained_side": self.trained_side,
            "reward_mode": self.reward_mode.value,
            "ppo_config_ref": self.ppo_config.to_dict(),
            "seed_config_ref": self.seed_config.to_dict(),
            "rollout_target": {
                "source": self.rollout_target_source,
                "semantics": self.rollout_target_semantics,
                "transitions_per_update": self.transitions_per_update,
            },
            "collection_mode": self.collection_mode,
            "reset_policy": self.reset_policy,
            "scenario_assignment": self.scenario_assignment,
            "flatten_order": self.flatten_order,
            "error_policy": self.error_policy,
            "reward_verification": self.reward_verification,
            "device": self.device,
        }

    def to_dict(self) -> dict[str, object]:
        value = self.semantic_payload()
        value["config_hash"] = self.content_hash
        return value


def build_ppo_vector_runner_config(
    value: Mapping[str, object],
    *,
    base_runner: PPORunnerConfig | None = None,
    ppo: PPOAlgorithmConfig | None = None,
    seed: TrainingSeedConfig | None = None,
) -> PPOVectorRunnerConfig:
    if not isinstance(value, Mapping):
        raise TypeError("vector runner config must be a mapping")
    expected = {
        "schema_version", "runner_id", "runner_version", "purpose",
        "base_runner_config_ref", "num_envs", "trained_side", "reward_mode",
        "ppo_config_ref", "seed_config_ref", "rollout_target",
        "collection_mode", "reset_policy", "scenario_assignment",
        "flatten_order", "error_policy", "reward_verification", "device",
    }
    if set(value) != expected:
        raise PPOVectorRunnerConfigError(
            "vector runner config fields are missing or unknown"
        )
    actual_base = load_ppo_runner_config() if base_runner is None else base_runner
    actual_ppo = load_ppo_algorithm_config() if ppo is None else ppo
    actual_seed = load_training_seed_config() if seed is None else seed
    base_ref = _reference(value["base_runner_config_ref"], "base_runner_config_ref")
    ppo_ref = _reference(value["ppo_config_ref"], "ppo_config_ref")
    seed_ref = _reference(value["seed_config_ref"], "seed_config_ref")
    if base_ref != PPOAuthorityReference(
        actual_base.runner_id, actual_base.runner_version, actual_base.content_hash
    ):
        raise PPOVectorRunnerConfigError("base runner reference mismatch")
    if ppo_ref != PPOAuthorityReference(
        actual_ppo.algorithm_id, actual_ppo.algorithm_version,
        actual_ppo.content_hash,
    ) or ppo_ref != actual_base.ppo_config:
        raise PPOVectorRunnerConfigError("PPO reference mismatch")
    if seed_ref != PPOAuthorityReference(
        actual_seed.config_id, actual_seed.config_version,
        actual_seed.content_hash,
    ) or seed_ref != actual_base.seed_config:
        raise PPOVectorRunnerConfigError("seed reference mismatch")
    rollout = _mapping(value["rollout_target"], "rollout_target")
    if set(rollout) != {"source", "semantics", "transitions_per_update"}:
        raise PPOVectorRunnerConfigError("rollout target fields are invalid")
    common = dict(
        schema_version=str(value["schema_version"]),
        runner_id=str(value["runner_id"]),
        runner_version=str(value["runner_version"]),
        purpose=str(value["purpose"]),
        base_runner_config=base_ref,
        num_envs=_positive(value["num_envs"], "num_envs"),
        trained_side=str(value["trained_side"]),
        reward_mode=PPORewardMode(str(value["reward_mode"])),
        ppo_config=ppo_ref,
        seed_config=seed_ref,
        rollout_target_source=str(rollout["source"]),
        rollout_target_semantics=str(rollout["semantics"]),
        transitions_per_update=_positive(
            rollout["transitions_per_update"], "transitions_per_update"
        ),
        collection_mode=str(value["collection_mode"]),
        reset_policy=str(value["reset_policy"]),
        scenario_assignment=str(value["scenario_assignment"]),
        flatten_order=str(value["flatten_order"]),
        error_policy=str(value["error_policy"]),
        reward_verification=str(value["reward_verification"]),
        device=str(value["device"]),
    )
    if common["trained_side"] != actual_base.trained_side or (
        common["reward_mode"] is not actual_base.reward_mode
    ):
        raise PPOVectorRunnerConfigError("vector/base runner policy mismatch")
    if common["transitions_per_update"] != actual_ppo.rollout_length:
        raise PPOVectorRunnerConfigError(
            "vector transition target must equal PPO rollout_length"
        )
    temporary = object.__new__(PPOVectorRunnerConfig)
    for name, item in common.items():
        object.__setattr__(temporary, name, item)
    return PPOVectorRunnerConfig(
        **common, content_hash=canonical_hash(temporary.semantic_payload())
    )


@lru_cache(maxsize=8)
def load_ppo_vector_runner_config(
    path: str | Path | None = None,
) -> PPOVectorRunnerConfig:
    source = Path(path) if path is not None else (
        Path(__file__).resolve().parents[3]
        / "config/training/native_ppo_vector_runner_v1.json"
    )
    return build_ppo_vector_runner_config(
        json.loads(source.read_text(encoding="utf-8"))
    )


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PPOVectorRunnerConfigError(f"{name} must be an object")
    return value


def _reference(value: object, name: str) -> PPOAuthorityReference:
    item = _mapping(value, name)
    if set(item) != {"id", "version", "hash"}:
        raise PPOVectorRunnerConfigError(f"{name} fields are invalid")
    return PPOAuthorityReference(str(item["id"]), str(item["version"]), str(item["hash"]))


def _positive(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise PPOVectorRunnerConfigError(f"{name} must be positive")
    return value


__all__ = [
    "PPO_VECTOR_RUNNER_CONFIG_SCHEMA_VERSION", "PPO_VECTOR_RUNNER_ID",
    "PPO_VECTOR_RUNNER_VERSION", "PPOVectorRunnerConfig",
    "PPOVectorRunnerConfigError", "build_ppo_vector_runner_config",
    "load_ppo_vector_runner_config",
]
