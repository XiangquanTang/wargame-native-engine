"""Versioned authority for the Native masked PPO v1 baseline."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import math
from pathlib import Path
from typing import Mapping

from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.training.decision_encoder import (
    DECISION_ACTION_ENCODER_SCHEMA_VERSION,
    DecisionActionEncoder,
)
from wargame_native_engine.training.discount import (
    SharedTrainingDiscountConfig,
    load_shared_training_discount_config,
)
from wargame_native_engine.training.learner_backend import (
    load_learner_backend_authority,
)
from wargame_native_engine.training.learner_preprocessor import (
    load_learner_observation_preprocessor_spec_for_identity,
)
from wargame_native_engine.training.training_seed import (
    TrainingSeedConfig,
    load_training_seed_config,
)


PPO_CONFIG_SCHEMA_VERSION = "native-ppo-config/1.0"
PPO_ALGORITHM_ID = "native-masked-ppo"
PPO_ALGORITHM_VERSION = "1.0.0"
PPO_ACTION_CAPACITY = 256


class PPOConfigContractError(ValueError):
    """The PPO algorithm config violates its stable machine contract."""


@dataclass(frozen=True, slots=True)
class PPOAuthorityReference:
    identity_id: str
    identity_version: str
    identity_hash: str

    def __post_init__(self) -> None:
        _text(self.identity_id, "authority id")
        _text(self.identity_version, "authority version")
        _sha256(self.identity_hash, "authority hash")

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.identity_id,
            "version": self.identity_version,
            "hash": self.identity_hash,
        }


@dataclass(frozen=True, slots=True)
class PPOActionEncoderReference:
    schema_version: str
    contract_hash: str
    capacity: int

    def __post_init__(self) -> None:
        if self.schema_version != DECISION_ACTION_ENCODER_SCHEMA_VERSION:
            raise PPOConfigContractError("action encoder schema mismatch")
        _sha256(self.contract_hash, "action encoder hash")
        if type(self.capacity) is not int or self.capacity <= 0:
            raise PPOConfigContractError(
                "action capacity must be a positive exact int"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "hash": self.contract_hash,
            "capacity": self.capacity,
        }


@dataclass(frozen=True, slots=True)
class PPOAlgorithmConfig:
    schema_version: str
    algorithm_id: str
    algorithm_version: str
    backend: PPOAuthorityReference
    observation_preprocessor: PPOAuthorityReference
    action_encoder: PPOActionEncoderReference
    discount: PPOAuthorityReference
    seed: PPOAuthorityReference
    gae_lambda: float
    clip_epsilon: float
    learning_rate: float
    entropy_coefficient: float
    value_coefficient: float
    max_grad_norm: float
    rollout_length: int
    minibatch_size: int
    update_epochs: int
    advantage_normalization: str
    advantage_normalization_epsilon: float
    advantage_normalization_scope: str
    network_architecture: str
    hidden_sizes: tuple[int, ...]
    activation: str
    shared_backbone: bool
    initialization: str
    hidden_gain: float
    policy_head_gain: float
    value_head_gain: float
    action_masking_enabled: bool
    action_mask_authority: str
    action_mask_application: str
    all_false_mask_policy: str
    optimizer_type: str
    adam_betas: tuple[float, float]
    adam_epsilon: float
    adam_weight_decay: float
    learning_rate_schedule: str
    device_policy: str
    evaluation_action_selection: str
    observation_normalization: str
    reward_normalization: str
    reward_clipping: str
    value_clipping: str
    mixed_precision: str
    target_kl: str
    approx_kl_definition: str
    clip_fraction_definition: str
    content_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != PPO_CONFIG_SCHEMA_VERSION:
            raise PPOConfigContractError("unsupported PPO config schema")
        if self.algorithm_id != PPO_ALGORITHM_ID or (
            self.algorithm_version != PPO_ALGORITHM_VERSION
        ):
            raise PPOConfigContractError("unsupported PPO algorithm identity")
        for value in (
            self.backend, self.observation_preprocessor, self.discount, self.seed
        ):
            if not isinstance(value, PPOAuthorityReference):
                raise TypeError("PPO authority refs must be typed")
        if not isinstance(self.action_encoder, PPOActionEncoderReference):
            raise TypeError("action_encoder must be PPOActionEncoderReference")
        _bounded(self.gae_lambda, "gae_lambda", lower=0.0, upper=1.0,
                 lower_open=True)
        _bounded(self.clip_epsilon, "clip_epsilon", lower=0.0, upper=1.0,
                 lower_open=True, upper_open=True)
        for name in (
            "learning_rate", "entropy_coefficient", "value_coefficient"
        ):
            _bounded(getattr(self, name), name, lower=0.0)
        _bounded(self.max_grad_norm, "max_grad_norm", lower=0.0,
                 lower_open=True)
        for name in ("rollout_length", "minibatch_size", "update_epochs"):
            _positive_int(getattr(self, name), name)
        if self.minibatch_size > self.rollout_length:
            raise PPOConfigContractError(
                "minibatch_size cannot exceed rollout_length"
            )
        if not self.hidden_sizes or any(
            type(value) is not int or value <= 0 for value in self.hidden_sizes
        ):
            raise PPOConfigContractError("hidden sizes must be positive exact ints")
        for name in (
            "advantage_normalization_epsilon", "hidden_gain",
            "policy_head_gain", "value_head_gain", "adam_epsilon",
        ):
            _bounded(getattr(self, name), name, lower=0.0, lower_open=True)
        if self.adam_betas != (0.9, 0.999) or self.adam_weight_decay != 0.0:
            raise PPOConfigContractError("unsupported Adam v1 semantics")
        expected = {
            "advantage_normalization": "ENABLED",
            "advantage_normalization_scope": "UPDATE_BATCH",
            "network_architecture": "SHARED_MLP",
            "activation": "TANH",
            "shared_backbone": True,
            "initialization": "ORTHOGONAL_V1",
            "action_masking_enabled": True,
            "action_mask_authority": "ACTION_CATALOG_SNAPSHOT",
            "action_mask_application": "LOGITS_BEFORE_NORMALIZATION",
            "all_false_mask_policy": "FAIL_CLOSED",
            "optimizer_type": "ADAM",
            "learning_rate_schedule": "CONSTANT",
            "device_policy": "CPU",
            "evaluation_action_selection": "MASKED_ARGMAX",
            "observation_normalization": "NONE",
            "reward_normalization": "NONE",
            "reward_clipping": "NONE",
            "value_clipping": "NONE",
            "mixed_precision": "NONE",
            "target_kl": "NONE",
            "approx_kl_definition": "MEAN_OLD_LOG_PROB_MINUS_NEW_LOG_PROB",
            "clip_fraction_definition": "ABS_RATIO_MINUS_ONE_GT_CLIP_EPSILON",
        }
        for name, value in expected.items():
            if getattr(self, name) != value:
                raise PPOConfigContractError(f"PPO v1 field mismatch: {name}")
        if self.content_hash != canonical_hash(self.semantic_payload()):
            raise PPOConfigContractError("PPO config hash mismatch")

    def semantic_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "algorithm_id": self.algorithm_id,
            "algorithm_version": self.algorithm_version,
            "backend_ref": self.backend.to_dict(),
            "observation_preprocessor_ref": self.observation_preprocessor.to_dict(),
            "action_encoder_ref": self.action_encoder.to_dict(),
            "discount_config_ref": self.discount.to_dict(),
            "seed_config_ref": self.seed.to_dict(),
            "gae_lambda": self.gae_lambda,
            "clip_epsilon": self.clip_epsilon,
            "learning_rate": self.learning_rate,
            "entropy_coefficient": self.entropy_coefficient,
            "value_coefficient": self.value_coefficient,
            "max_grad_norm": self.max_grad_norm,
            "rollout_length": self.rollout_length,
            "minibatch_size": self.minibatch_size,
            "update_epochs": self.update_epochs,
            "advantage_normalization": {
                "mode": self.advantage_normalization,
                "epsilon": self.advantage_normalization_epsilon,
                "scope": self.advantage_normalization_scope,
            },
            "network": {
                "architecture": self.network_architecture,
                "hidden_sizes": list(self.hidden_sizes),
                "activation": self.activation,
                "shared_backbone": self.shared_backbone,
                "initialization": self.initialization,
                "hidden_gain": self.hidden_gain,
                "policy_head_gain": self.policy_head_gain,
                "value_head_gain": self.value_head_gain,
            },
            "action_masking": {
                "enabled": self.action_masking_enabled,
                "authority": self.action_mask_authority,
                "application": self.action_mask_application,
                "all_false": self.all_false_mask_policy,
            },
            "optimizer": {
                "type": self.optimizer_type,
                "betas": list(self.adam_betas),
                "epsilon": self.adam_epsilon,
                "weight_decay": self.adam_weight_decay,
                "learning_rate_schedule": self.learning_rate_schedule,
            },
            "device_policy": self.device_policy,
            "evaluation_action_selection": self.evaluation_action_selection,
            "observation_normalization": self.observation_normalization,
            "reward_normalization": self.reward_normalization,
            "reward_clipping": self.reward_clipping,
            "value_clipping": self.value_clipping,
            "mixed_precision": self.mixed_precision,
            "target_kl": self.target_kl,
            "approx_kl_definition": self.approx_kl_definition,
            "clip_fraction_definition": self.clip_fraction_definition,
        }

    def to_dict(self) -> dict[str, object]:
        result = self.semantic_payload()
        result["config_hash"] = self.content_hash
        return result


def build_ppo_algorithm_config(
    value: Mapping[str, object], *, validate_runtime: bool = True,
) -> PPOAlgorithmConfig:
    root = _object(value, "PPO config")
    expected = {
        "schema_version", "algorithm_id", "algorithm_version", "backend_ref",
        "observation_preprocessor_ref", "action_encoder_ref",
        "discount_config_ref", "seed_config_ref", "gae_lambda",
        "clip_epsilon", "learning_rate", "entropy_coefficient",
        "value_coefficient", "max_grad_norm", "rollout_length",
        "minibatch_size", "update_epochs", "advantage_normalization",
        "network", "action_masking", "optimizer", "device_policy",
        "evaluation_action_selection", "observation_normalization",
        "reward_normalization", "reward_clipping", "value_clipping",
        "mixed_precision", "target_kl", "approx_kl_definition",
        "clip_fraction_definition",
    }
    _exact_fields(root, expected, "PPO config")
    advantage = _object(root["advantage_normalization"], "advantage_normalization")
    network = _object(root["network"], "network")
    masking = _object(root["action_masking"], "action_masking")
    optimizer = _object(root["optimizer"], "optimizer")
    _exact_fields(advantage, {"mode", "epsilon", "scope"}, "advantage_normalization")
    _exact_fields(network, {
        "architecture", "hidden_sizes", "activation", "shared_backbone",
        "initialization", "hidden_gain", "policy_head_gain", "value_head_gain",
    }, "network")
    _exact_fields(masking, {"enabled", "authority", "application", "all_false"}, "action_masking")
    _exact_fields(optimizer, {"type", "betas", "epsilon", "weight_decay", "learning_rate_schedule"}, "optimizer")
    hidden = _array(network["hidden_sizes"], "hidden_sizes")
    betas = _array(optimizer["betas"], "optimizer betas")
    if len(betas) != 2:
        raise PPOConfigContractError("optimizer betas must have two values")
    common = {
        "schema_version": _text(root["schema_version"], "schema_version"),
        "algorithm_id": _text(root["algorithm_id"], "algorithm_id"),
        "algorithm_version": _text(root["algorithm_version"], "algorithm_version"),
        "backend": _authority(root["backend_ref"], "backend_ref"),
        "observation_preprocessor": _authority(root["observation_preprocessor_ref"], "observation_preprocessor_ref"),
        "action_encoder": _action_authority(root["action_encoder_ref"]),
        "discount": _authority(root["discount_config_ref"], "discount_config_ref"),
        "seed": _authority(root["seed_config_ref"], "seed_config_ref"),
        "gae_lambda": _number(root["gae_lambda"], "gae_lambda"),
        "clip_epsilon": _number(root["clip_epsilon"], "clip_epsilon"),
        "learning_rate": _number(root["learning_rate"], "learning_rate"),
        "entropy_coefficient": _number(root["entropy_coefficient"], "entropy_coefficient"),
        "value_coefficient": _number(root["value_coefficient"], "value_coefficient"),
        "max_grad_norm": _number(root["max_grad_norm"], "max_grad_norm"),
        "rollout_length": _positive_int(root["rollout_length"], "rollout_length"),
        "minibatch_size": _positive_int(root["minibatch_size"], "minibatch_size"),
        "update_epochs": _positive_int(root["update_epochs"], "update_epochs"),
        "advantage_normalization": _text(advantage["mode"], "advantage mode"),
        "advantage_normalization_epsilon": _number(advantage["epsilon"], "advantage epsilon"),
        "advantage_normalization_scope": _text(advantage["scope"], "advantage scope"),
        "network_architecture": _text(network["architecture"], "network architecture"),
        "hidden_sizes": tuple(_positive_int(item, "hidden size") for item in hidden),
        "activation": _text(network["activation"], "activation"),
        "shared_backbone": _exact_bool(network["shared_backbone"], "shared_backbone"),
        "initialization": _text(network["initialization"], "initialization"),
        "hidden_gain": _number(network["hidden_gain"], "hidden_gain"),
        "policy_head_gain": _number(network["policy_head_gain"], "policy_head_gain"),
        "value_head_gain": _number(network["value_head_gain"], "value_head_gain"),
        "action_masking_enabled": _exact_bool(masking["enabled"], "mask enabled"),
        "action_mask_authority": _text(masking["authority"], "mask authority"),
        "action_mask_application": _text(masking["application"], "mask application"),
        "all_false_mask_policy": _text(masking["all_false"], "all false mask policy"),
        "optimizer_type": _text(optimizer["type"], "optimizer type"),
        "adam_betas": (_number(betas[0], "adam beta1"), _number(betas[1], "adam beta2")),
        "adam_epsilon": _number(optimizer["epsilon"], "adam epsilon"),
        "adam_weight_decay": _number(optimizer["weight_decay"], "adam weight decay"),
        "learning_rate_schedule": _text(optimizer["learning_rate_schedule"], "learning rate schedule"),
        "device_policy": _text(root["device_policy"], "device policy"),
        "evaluation_action_selection": _text(root["evaluation_action_selection"], "evaluation selection"),
        "observation_normalization": _text(root["observation_normalization"], "observation normalization"),
        "reward_normalization": _text(root["reward_normalization"], "reward normalization"),
        "reward_clipping": _text(root["reward_clipping"], "reward clipping"),
        "value_clipping": _text(root["value_clipping"], "value clipping"),
        "mixed_precision": _text(root["mixed_precision"], "mixed precision"),
        "target_kl": _text(root["target_kl"], "target kl"),
        "approx_kl_definition": _text(root["approx_kl_definition"], "approx kl definition"),
        "clip_fraction_definition": _text(root["clip_fraction_definition"], "clip fraction definition"),
    }
    temporary = PPOAlgorithmConfig(
        **common, content_hash=canonical_hash(_payload_from_common(common))
    )
    if validate_runtime:
        require_ppo_runtime_authorities(temporary)
    return temporary


@lru_cache(maxsize=8)
def load_ppo_algorithm_config(path: str | Path | None = None) -> PPOAlgorithmConfig:
    source = Path(path) if path is not None else (
        Path(__file__).resolve().parents[3]
        / "config/training/native_masked_ppo_v1.json"
    )
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PPOConfigContractError(f"invalid PPO config {source}: {exc}") from exc
    return build_ppo_algorithm_config(value)


def require_ppo_runtime_authorities(
    config: PPOAlgorithmConfig,
    *,
    discount: SharedTrainingDiscountConfig | None = None,
    seed: TrainingSeedConfig | None = None,
) -> None:
    if not isinstance(config, PPOAlgorithmConfig):
        raise TypeError("config must be PPOAlgorithmConfig")
    backend = load_learner_backend_authority()
    preprocessor = load_learner_observation_preprocessor_spec_for_identity(
        config.observation_preprocessor.identity_id,
        config.observation_preprocessor.identity_version,
    )
    action = DecisionActionEncoder(config.action_encoder.capacity)
    actual_discount = load_shared_training_discount_config() if discount is None else discount
    actual_seed = load_training_seed_config() if seed is None else seed
    expected = (
        (config.backend, backend.backend_id, backend.backend_version, backend.semantic_hash, "backend"),
        (config.observation_preprocessor, preprocessor.preprocessor_id, preprocessor.preprocessor_version, preprocessor.semantic_hash, "preprocessor"),
        (config.discount, actual_discount.config_id, actual_discount.config_version, actual_discount.content_hash, "discount"),
        (config.seed, actual_seed.config_id, actual_seed.config_version, actual_seed.content_hash, "seed"),
    )
    for reference, identity_id, version, value_hash, name in expected:
        if reference != PPOAuthorityReference(identity_id, version, value_hash):
            raise PPOConfigContractError(f"PPO {name} authority mismatch")
    if config.action_encoder != PPOActionEncoderReference(
        DECISION_ACTION_ENCODER_SCHEMA_VERSION,
        action.contract_hash,
        action.max_actions,
    ):
        raise PPOConfigContractError("PPO action encoder authority mismatch")


def _payload_from_common(value: Mapping[str, object]) -> dict[str, object]:
    placeholder = object.__new__(PPOAlgorithmConfig)
    for name, item in value.items():
        object.__setattr__(placeholder, name, item)
    return PPOAlgorithmConfig.semantic_payload(placeholder)


def _authority(value: object, name: str) -> PPOAuthorityReference:
    root = _object(value, name)
    _exact_fields(root, {"id", "version", "hash"}, name)
    return PPOAuthorityReference(
        _text(root["id"], f"{name} id"),
        _text(root["version"], f"{name} version"),
        _sha256(root["hash"], f"{name} hash"),
    )


def _action_authority(value: object) -> PPOActionEncoderReference:
    root = _object(value, "action_encoder_ref")
    _exact_fields(root, {"schema_version", "hash", "capacity"}, "action_encoder_ref")
    return PPOActionEncoderReference(
        _text(root["schema_version"], "action encoder schema"),
        _sha256(root["hash"], "action encoder hash"),
        _positive_int(root["capacity"], "action capacity"),
    )


def _object(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PPOConfigContractError(f"{name} must be an object")
    return value


def _array(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise PPOConfigContractError(f"{name} must be an array")
    return value


def _exact_fields(value: Mapping[str, object], fields: set[str], name: str) -> None:
    if set(value) != fields:
        raise PPOConfigContractError(f"{name} fields are missing or unknown")


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PPOConfigContractError(f"{name} must be non-empty trimmed text")
    return value


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        item not in "0123456789abcdef" for item in value
    ):
        raise PPOConfigContractError(f"{name} must be lowercase SHA-256")
    return value


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite number (not bool)")
    result = float(value)
    if not math.isfinite(result):
        raise PPOConfigContractError(f"{name} must be finite")
    return result


def _bounded(
    value: object, name: str, *, lower: float | None = None,
    upper: float | None = None, lower_open: bool = False,
    upper_open: bool = False,
) -> float:
    result = _number(value, name)
    if lower is not None and (result <= lower if lower_open else result < lower):
        raise PPOConfigContractError(f"{name} is below its allowed range")
    if upper is not None and (result >= upper if upper_open else result > upper):
        raise PPOConfigContractError(f"{name} exceeds its allowed range")
    return result


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise PPOConfigContractError(f"{name} must be a positive exact int")
    return value


def _exact_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be an exact bool")
    return value


__all__ = [
    "PPO_ACTION_CAPACITY", "PPO_ALGORITHM_ID", "PPO_ALGORITHM_VERSION",
    "PPO_CONFIG_SCHEMA_VERSION", "PPOActionEncoderReference",
    "PPOAlgorithmConfig", "PPOAuthorityReference", "PPOConfigContractError",
    "build_ppo_algorithm_config", "load_ppo_algorithm_config",
    "require_ppo_runtime_authorities",
]
