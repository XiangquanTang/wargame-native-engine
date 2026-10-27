"""Minimal tensor-only masked PPO core.

This explicit learner module loads the optional PyTorch dependency.  It has no
environment, World, GREEN, Gateway, Decision, catalog-query, or reward-derivation
imports.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import math
from typing import Mapping, Sequence

from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.training.discount import (
    SharedTrainingDiscountConfig,
    load_shared_training_discount_config,
)
from wargame_native_engine.training.learner_backend import require_pytorch
from wargame_native_engine.training.learner_preprocessor import (
    load_learner_observation_preprocessor_spec_for_identity,
)
from wargame_native_engine.training.ppo_config import (
    PPOAlgorithmConfig,
    PPOAuthorityReference,
    load_ppo_algorithm_config,
    require_ppo_runtime_authorities,
)
from wargame_native_engine.training.training_seed import (
    TrainingSeedConfig,
    TrainingSeedStream,
    load_training_seed_config,
)


torch = require_pytorch()
nn = torch.nn

PPO_LEARNER_TRANSITION_SCHEMA_VERSION = "native-ppo-learner-transition/1.0"
PPO_ROLLOUT_BATCH_SCHEMA_VERSION = "native-ppo-rollout-batch/1.0"
PPO_CHECKPOINT_SCHEMA_VERSION = "native-ppo-checkpoint/1.0"


class PPOCoreError(RuntimeError):
    """The tensor-only PPO contract failed closed."""


class AllActionsMaskedError(PPOCoreError):
    """No legal catalog entry exists for at least one active policy row."""


class InvalidMaskedActionError(PPOCoreError):
    """A selected action is not legal under the stored catalog mask."""


class PPOCheckpointError(PPOCoreError):
    """Checkpoint state or semantic authority does not match runtime."""


class MaskedCategorical:
    """Stable categorical distribution over exactly the supplied legal mask."""

    def __init__(self, logits, mask) -> None:
        _tensor(logits, "logits", dtype=torch.float32, min_ndim=1)
        _tensor(mask, "mask", dtype=torch.bool, min_ndim=1)
        if logits.shape != mask.shape:
            raise PPOCoreError("logits and mask shapes must match")
        if not bool(torch.isfinite(logits).all()):
            raise PPOCoreError("masked categorical logits must be finite")
        if bool((~mask.any(dim=-1)).any()):
            raise AllActionsMaskedError("all actions are masked")
        self.mask = mask.detach().clone()
        self.masked_logits = logits.masked_fill(~mask, -torch.inf)
        self.log_probs = torch.log_softmax(self.masked_logits, dim=-1)
        self.probs = torch.exp(self.log_probs)
        if bool((self.probs.masked_select(~self.mask) != 0.0).any()):
            raise PPOCoreError("invalid action probability is not exact zero")
        sums = self.probs.sum(dim=-1)
        if not bool(torch.allclose(
            sums, torch.ones_like(sums), rtol=1e-6, atol=1e-7
        )):
            raise PPOCoreError("legal action probabilities do not sum to one")

    def sample(self, *, generator) -> object:
        if not isinstance(generator, torch.Generator):
            raise TypeError("sampling requires an explicit torch.Generator")
        flat = self.probs.reshape(-1, self.probs.shape[-1])
        result = torch.multinomial(
            flat, 1, replacement=True, generator=generator
        ).squeeze(-1)
        return result.reshape(self.probs.shape[:-1])

    def log_prob(self, actions):
        _tensor(actions, "actions", dtype=torch.long, min_ndim=0)
        if tuple(actions.shape) != tuple(self.probs.shape[:-1]):
            raise PPOCoreError("action shape does not match distribution batch")
        if bool(((actions < 0) | (actions >= self.probs.shape[-1])).any()):
            raise InvalidMaskedActionError("action index is out of range")
        chosen_mask = self.mask.gather(-1, actions.unsqueeze(-1)).squeeze(-1)
        if not bool(chosen_mask.all()):
            raise InvalidMaskedActionError("masked action cannot have a log_prob")
        return self.log_probs.gather(-1, actions.unsqueeze(-1)).squeeze(-1)

    def entropy(self):
        safe_log_probs = torch.where(
            self.mask, self.log_probs, torch.zeros_like(self.log_probs)
        )
        terms = torch.where(
            self.mask,
            self.probs * safe_log_probs,
            torch.zeros_like(self.probs),
        )
        result = -terms.sum(dim=-1)
        if not bool(torch.isfinite(result).all()):
            raise PPOCoreError("masked entropy is not finite")
        return result


def masked_argmax(logits, mask):
    distribution = MaskedCategorical(logits, mask)
    return torch.argmax(distribution.masked_logits, dim=-1)


class SharedActorCriticMLP(nn.Module):
    """Config-driven shared Tanh MLP with separate policy and value heads."""

    def __init__(
        self,
        config: PPOAlgorithmConfig,
        *,
        initialization_generator,
        observation_size: int | None = None,
    ) -> None:
        super().__init__()
        if not isinstance(config, PPOAlgorithmConfig):
            raise TypeError("config must be PPOAlgorithmConfig")
        if not isinstance(initialization_generator, torch.Generator):
            raise TypeError("initialization_generator must be torch.Generator")
        if observation_size is None:
            observation_size = load_learner_observation_preprocessor_spec_for_identity(
                config.observation_preprocessor.identity_id,
                config.observation_preprocessor.identity_version,
            ).output_shape[0]
        self.config_hash = config.content_hash
        self.observation_size = observation_size
        self.action_capacity = config.action_encoder.capacity
        layers: list[object] = []
        width = observation_size
        linear_layers = []
        for hidden in config.hidden_sizes:
            layer = nn.Linear(width, hidden)
            layers.extend((layer, nn.Tanh()))
            linear_layers.append(layer)
            width = hidden
        self.backbone = nn.Sequential(*layers)
        self.policy_head = nn.Linear(width, self.action_capacity)
        self.value_head = nn.Linear(width, 1)
        for layer in linear_layers:
            _orthogonal(layer, config.hidden_gain, initialization_generator)
        _orthogonal(
            self.policy_head, config.policy_head_gain, initialization_generator
        )
        _orthogonal(
            self.value_head, config.value_head_gain, initialization_generator
        )
        self.to(dtype=torch.float32, device="cpu")

    def forward(self, observations):
        _tensor(observations, "observations", dtype=torch.float32, min_ndim=1)
        if observations.shape[-1] != self.observation_size:
            raise PPOCoreError("actor-critic observation shape mismatch")
        if observations.device != next(self.parameters()).device:
            raise PPOCoreError("actor-critic model and observation devices must match")
        if not bool(torch.isfinite(observations).all()):
            raise PPOCoreError("actor-critic observation contains NaN or Inf")
        hidden = self.backbone(observations)
        logits = self.policy_head(hidden)
        values = self.value_head(hidden).squeeze(-1)
        if not bool(torch.isfinite(logits).all()) or not bool(
            torch.isfinite(values).all()
        ):
            raise PPOCoreError("actor-critic output contains NaN or Inf")
        return logits, values


@dataclass(frozen=True, slots=True)
class PPOActionOutput:
    action_index: object
    log_prob: object
    value_prediction: object


@dataclass(frozen=True, slots=True)
class LearnerTransition:
    schema_version: str
    episode_id: str
    run_namespace: str
    side: str
    observation: tuple[float, ...]
    action_mask: tuple[bool, ...]
    decision_identity: str
    catalog_hash: str
    action_encoder_hash: str
    action_index: int
    action_id: str
    old_log_prob: float
    value_prediction: float
    reward: float
    reward_record_hash: str
    base_reward_amount: float
    shaping_reward_amount: float
    terminated: bool
    truncated: bool
    truncation_source: str
    effective_gamma: float
    next_observation: tuple[float, ...]
    next_decision_identity: str
    next_value_prediction: float
    bootstrap_allowed: bool
    trace_continues: bool
    preprocessor_hash: str
    task_reward_amount: float = 0.0
    training_reward_mode: str = "BASE_OR_COMPOSITE"

    def __post_init__(self) -> None:
        if self.schema_version != PPO_LEARNER_TRANSITION_SCHEMA_VERSION:
            raise PPOCoreError("unsupported learner transition schema")
        for name in ("episode_id", "run_namespace", "action_id", "truncation_source"):
            _text(getattr(self, name), name)
        if self.side not in {"red", "blue"}:
            raise PPOCoreError("learner transition side must be red or blue")
        for name in (
            "decision_identity", "catalog_hash", "action_encoder_hash",
            "reward_record_hash", "next_decision_identity", "preprocessor_hash",
        ):
            _sha256(getattr(self, name), name)
        if not self.observation or len(self.observation) != len(self.next_observation):
            raise PPOCoreError("learner transition observation shape mismatch")
        _finite_sequence(self.observation, "observation")
        _finite_sequence(self.next_observation, "next_observation")
        if not self.action_mask or any(type(item) is not bool for item in self.action_mask):
            raise PPOCoreError("learner transition action mask must contain bools")
        if type(self.action_index) is not int or not 0 <= self.action_index < len(self.action_mask):
            raise PPOCoreError("learner transition action index is invalid")
        if not self.action_mask[self.action_index]:
            raise InvalidMaskedActionError("stored action is masked")
        for name in (
            "old_log_prob", "value_prediction", "reward",
            "base_reward_amount", "shaping_reward_amount",
            "effective_gamma", "next_value_prediction",
        ):
            _finite(getattr(self, name), name)
        _finite(self.task_reward_amount, "task_reward_amount")
        if self.training_reward_mode not in {
            "BASE_OR_COMPOSITE", "TASK_OBJECTIVE_CAPTURE"
        }:
            raise PPOCoreError("unsupported training reward mode")
        expected_reward = (
            self.task_reward_amount
            if self.training_reward_mode == "TASK_OBJECTIVE_CAPTURE"
            else self.base_reward_amount + self.shaping_reward_amount
        )
        if not math.isclose(
            self.reward, expected_reward, rel_tol=0.0, abs_tol=1e-7,
        ):
            raise PPOCoreError("selected reward component does not equal stored reward")
        if not 0.0 <= self.effective_gamma <= 1.0:
            raise PPOCoreError("effective_gamma must be in [0, 1]")
        for name in (
            "terminated", "truncated", "bootstrap_allowed", "trace_continues"
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be an exact bool")
        if self.terminated and self.truncated:
            raise PPOCoreError("transition cannot be terminated and truncated")
        if self.terminated and (self.bootstrap_allowed or self.trace_continues):
            raise PPOCoreError("terminated transition cannot bootstrap or trace")
        if self.truncated and (
            not self.bootstrap_allowed or self.trace_continues
        ):
            raise PPOCoreError("truncated transition must bootstrap and stop trace")


@dataclass(frozen=True, slots=True)
class GAEResult:
    advantages: object
    value_targets: object


def compute_gae(
    *, rewards, values, next_values, effective_gammas,
    bootstrap_allowed, trace_continues, gae_lambda: float,
) -> GAEResult:
    tensors = {
        "rewards": (rewards, torch.float32),
        "values": (values, torch.float32),
        "next_values": (next_values, torch.float32),
        "effective_gammas": (effective_gammas, torch.float32),
        "bootstrap_allowed": (bootstrap_allowed, torch.bool),
        "trace_continues": (trace_continues, torch.bool),
    }
    length = None
    for name, (value, dtype) in tensors.items():
        _tensor(value, name, dtype=dtype, min_ndim=1, exact_ndim=1)
        length = value.shape[0] if length is None else length
        if value.shape[0] != length:
            raise PPOCoreError("GAE vectors must have identical lengths")
        if dtype is torch.float32 and not bool(torch.isfinite(value).all()):
            raise PPOCoreError(f"{name} contains NaN or Inf")
    normalized_lambda = _finite(gae_lambda, "gae_lambda")
    if not 0.0 < normalized_lambda <= 1.0:
        raise PPOCoreError("gae_lambda must be in (0, 1]")
    if bool(((effective_gammas < 0.0) | (effective_gammas > 1.0)).any()):
        raise PPOCoreError("effective gammas must be in [0, 1]")
    advantages = torch.zeros_like(rewards)
    next_advantage = torch.zeros((), dtype=torch.float32, device=rewards.device)
    for index in range(rewards.shape[0] - 1, -1, -1):
        bootstrap = bootstrap_allowed[index].to(torch.float32)
        trace = trace_continues[index].to(torch.float32)
        delta = (
            rewards[index]
            + effective_gammas[index] * bootstrap * next_values[index]
            - values[index]
        )
        next_advantage = (
            delta
            + effective_gammas[index]
            * normalized_lambda
            * trace
            * next_advantage
        )
        advantages[index] = next_advantage
    targets = advantages + values
    if not bool(torch.isfinite(advantages).all()) or not bool(
        torch.isfinite(targets).all()
    ):
        raise PPOCoreError("GAE produced NaN or Inf")
    return GAEResult(advantages.detach(), targets.detach())


@dataclass(frozen=True, slots=True)
class PPORolloutBatch:
    schema_version: str
    ppo_config_hash: str
    observations: object
    action_masks: object
    actions: object
    old_log_probs: object
    old_values: object
    rewards: object
    terminated: object
    truncated: object
    effective_gammas: object
    next_values: object
    bootstrap_allowed: object
    trace_continues: object
    advantages: object
    value_targets: object
    provenance: tuple[LearnerTransition, ...]

    def __post_init__(self) -> None:
        if self.schema_version != PPO_ROLLOUT_BATCH_SCHEMA_VERSION:
            raise PPOCoreError("unsupported PPO rollout batch schema")
        _sha256(self.ppo_config_hash, "PPO config hash")
        length = self.observations.shape[0]
        observation_width = self.observations.shape[1]
        action_width = self.action_masks.shape[1]
        expected = {
            "observations": (torch.float32, (length, observation_width)),
            "action_masks": (torch.bool, (length, action_width)),
            "actions": (torch.long, (length,)),
            "old_log_probs": (torch.float32, (length,)),
            "old_values": (torch.float32, (length,)),
            "rewards": (torch.float32, (length,)),
            "terminated": (torch.bool, (length,)),
            "truncated": (torch.bool, (length,)),
            "effective_gammas": (torch.float32, (length,)),
            "next_values": (torch.float32, (length,)),
            "bootstrap_allowed": (torch.bool, (length,)),
            "trace_continues": (torch.bool, (length,)),
            "advantages": (torch.float32, (length,)),
            "value_targets": (torch.float32, (length,)),
        }
        if length <= 0:
            raise PPOCoreError("rollout batch cannot be empty")
        if observation_width <= 0 or action_width <= 0:
            raise PPOCoreError("rollout tensor widths must be positive")
        for name, (dtype, shape) in expected.items():
            value = getattr(self, name)
            _tensor(value, name, dtype=dtype, min_ndim=len(shape), exact_ndim=len(shape))
            if tuple(value.shape) != shape:
                raise PPOCoreError(f"{name} has an invalid rollout shape")
            if dtype is not torch.bool and dtype is not torch.long and not bool(
                torch.isfinite(value).all()
            ):
                raise PPOCoreError(f"{name} contains NaN or Inf")
            object.__setattr__(self, name, value.detach().clone())
        if len(self.provenance) != length or any(
            not isinstance(item, LearnerTransition) for item in self.provenance
        ):
            raise PPOCoreError("rollout provenance length/type mismatch")
        if bool((~self.action_masks.any(dim=-1)).any()):
            raise AllActionsMaskedError("rollout contains an all-false mask")
        selected = self.action_masks.gather(-1, self.actions.unsqueeze(-1)).squeeze(-1)
        if not bool(selected.all()):
            raise InvalidMaskedActionError("rollout contains a masked action")

    @classmethod
    def from_transitions(
        cls, transitions: Sequence[LearnerTransition], config: PPOAlgorithmConfig,
    ) -> "PPORolloutBatch":
        values = tuple(transitions)
        if not values:
            raise PPOCoreError("rollout transitions cannot be empty")
        if len(values) > config.rollout_length:
            raise PPOCoreError("rollout exceeds configured rollout_length")
        preprocessor = load_learner_observation_preprocessor_spec_for_identity(
            config.observation_preprocessor.identity_id,
            config.observation_preprocessor.identity_version,
        )
        expected_width = preprocessor.output_shape[0]
        if any(
            len(item.observation) != expected_width
            or len(item.next_observation) != expected_width
            or len(item.action_mask) != config.action_encoder.capacity
            or item.preprocessor_hash != preprocessor.semantic_hash
            for item in values
        ):
            raise PPOCoreError("rollout transition/runtime shape authority mismatch")
        def floats(name: str):
            return torch.tensor(
                [getattr(item, name) for item in values], dtype=torch.float32
            )
        rewards = floats("reward")
        old_values = floats("value_prediction")
        next_values = floats("next_value_prediction")
        gammas = floats("effective_gamma")
        bootstrap = torch.tensor(
            [item.bootstrap_allowed for item in values], dtype=torch.bool
        )
        trace = torch.tensor(
            [item.trace_continues for item in values], dtype=torch.bool
        )
        gae = compute_gae(
            rewards=rewards, values=old_values, next_values=next_values,
            effective_gammas=gammas, bootstrap_allowed=bootstrap,
            trace_continues=trace, gae_lambda=config.gae_lambda,
        )
        return cls(
            schema_version=PPO_ROLLOUT_BATCH_SCHEMA_VERSION,
            ppo_config_hash=config.content_hash,
            observations=torch.tensor(
                [item.observation for item in values], dtype=torch.float32
            ),
            action_masks=torch.tensor(
                [item.action_mask for item in values], dtype=torch.bool
            ),
            actions=torch.tensor(
                [item.action_index for item in values], dtype=torch.long
            ),
            old_log_probs=floats("old_log_prob"), old_values=old_values,
            rewards=rewards,
            terminated=torch.tensor([item.terminated for item in values], dtype=torch.bool),
            truncated=torch.tensor([item.truncated for item in values], dtype=torch.bool),
            effective_gammas=gammas, next_values=next_values,
            bootstrap_allowed=bootstrap, trace_continues=trace,
            advantages=gae.advantages, value_targets=gae.value_targets,
            provenance=values,
        )

    def select(self, indices) -> "PPORolloutBatch":
        _tensor(indices, "indices", dtype=torch.long, min_ndim=1, exact_ndim=1)
        names = _batch_tensor_names()
        provenance = tuple(self.provenance[int(index)] for index in indices.tolist())
        return PPORolloutBatch(
            schema_version=self.schema_version,
            ppo_config_hash=self.ppo_config_hash,
            provenance=provenance,
            **{name: getattr(self, name).index_select(0, indices) for name in names},
        )

    def to(self, device: str) -> "PPORolloutBatch":
        target = torch.device(device)
        return PPORolloutBatch(
            schema_version=self.schema_version,
            ppo_config_hash=self.ppo_config_hash,
            provenance=self.provenance,
            **{name: getattr(self, name).to(target) for name in _batch_tensor_names()},
        )


def normalize_advantages(values, *, epsilon: float):
    _tensor(values, "advantages", dtype=torch.float32, min_ndim=1, exact_ndim=1)
    normalized_epsilon = _finite(epsilon, "advantage normalization epsilon")
    if normalized_epsilon <= 0.0:
        raise PPOCoreError("advantage normalization epsilon must be positive")
    result = (values - values.mean()) / (
        values.std(unbiased=False) + normalized_epsilon
    )
    if not bool(torch.isfinite(result).all()):
        raise PPOCoreError("normalized advantages contain NaN or Inf")
    return result


@dataclass(frozen=True, slots=True)
class PPOLoss:
    policy_loss: object
    value_loss: object
    entropy_mean: object
    total_loss: object
    approx_kl: object
    clip_fraction: object


def ppo_loss(
    *, new_log_probs, old_log_probs, advantages, new_values,
    value_targets, entropy, config: PPOAlgorithmConfig,
) -> PPOLoss:
    for name, value in (
        ("new_log_probs", new_log_probs), ("old_log_probs", old_log_probs),
        ("advantages", advantages), ("new_values", new_values),
        ("value_targets", value_targets), ("entropy", entropy),
    ):
        _tensor(value, name, dtype=torch.float32, min_ndim=1, exact_ndim=1)
        if not bool(torch.isfinite(value).all()):
            raise PPOCoreError(f"{name} contains NaN or Inf")
    shapes = {tuple(value.shape) for value in (
        new_log_probs, old_log_probs, advantages, new_values,
        value_targets, entropy,
    )}
    if len(shapes) != 1:
        raise PPOCoreError("PPO loss vectors must have the same shape")
    ratio = torch.exp(new_log_probs - old_log_probs)
    unclipped = ratio * advantages
    clipped = torch.clamp(
        ratio, 1.0 - config.clip_epsilon, 1.0 + config.clip_epsilon
    ) * advantages
    policy = -torch.minimum(unclipped, clipped).mean()
    value = 0.5 * torch.square(new_values - value_targets).mean()
    entropy_mean = entropy.mean()
    total = (
        policy
        + config.value_coefficient * value
        - config.entropy_coefficient * entropy_mean
    )
    approx_kl = (old_log_probs - new_log_probs).mean()
    clip_fraction = (
        torch.abs(ratio - 1.0) > config.clip_epsilon
    ).to(torch.float32).mean()
    for name, result in (
        ("policy loss", policy), ("value loss", value),
        ("entropy", entropy_mean), ("total loss", total),
        ("approx KL", approx_kl), ("clip fraction", clip_fraction),
    ):
        if not bool(torch.isfinite(result)):
            raise PPOCoreError(f"{name} is not finite")
    return PPOLoss(policy, value, entropy_mean, total, approx_kl, clip_fraction)


@dataclass(frozen=True, slots=True)
class PPOUpdateMetrics:
    policy_loss: float
    value_loss: float
    entropy: float
    total_loss: float
    approx_kl: float
    clip_fraction: float
    grad_norm: float
    clipped_grad_norm: float
    optimizer_steps: int
    update_step: int


class NativeMaskedPPO:
    """Pure tensor PPO mathematics; never owns or calls an environment."""

    def __init__(
        self,
        config: PPOAlgorithmConfig | None = None,
        *,
        discount: SharedTrainingDiscountConfig | None = None,
        seed: TrainingSeedConfig | None = None,
        seed_stream_index: int = 0,
    ) -> None:
        self.config = load_ppo_algorithm_config() if config is None else config
        self.discount = load_shared_training_discount_config() if discount is None else discount
        self.seed = load_training_seed_config() if seed is None else seed
        require_ppo_runtime_authorities(
            self.config, discount=self.discount, seed=self.seed
        )
        if type(seed_stream_index) is not int or seed_stream_index < 0:
            raise PPOCoreError("seed_stream_index must be a non-negative integer")
        self.seed_stream_index = seed_stream_index
        self.initialization_generator = torch.Generator(device="cpu")
        self.initialization_generator.manual_seed(self.seed.derive_seed(
            TrainingSeedStream.POLICY_INITIALIZATION,
            stream_index=self.seed_stream_index,
        ))
        self.policy_sampling_generator = torch.Generator(device="cpu")
        self.policy_sampling_generator.manual_seed(self.seed.derive_seed(
            TrainingSeedStream.POLICY_SAMPLING,
            stream_index=self.seed_stream_index,
        ))
        self.minibatch_shuffle_generator = torch.Generator(device="cpu")
        self.minibatch_shuffle_generator.manual_seed(self.seed.derive_seed(
            TrainingSeedStream.MINIBATCH_SHUFFLE,
            stream_index=self.seed_stream_index,
        ))
        self.preprocessor_spec = load_learner_observation_preprocessor_spec_for_identity(
            self.config.observation_preprocessor.identity_id,
            self.config.observation_preprocessor.identity_version,
        )
        self.model = SharedActorCriticMLP(
            self.config,
            initialization_generator=self.initialization_generator,
            observation_size=self.preprocessor_spec.output_shape[0],
        )
        self.optimizer = torch.optim.Adam(
            self.model.parameters(),
            lr=self.config.learning_rate,
            betas=self.config.adam_betas,
            eps=self.config.adam_epsilon,
            weight_decay=self.config.adam_weight_decay,
        )
        self.update_step = 0
        self.sample_step = 0

    def evaluate_actions(self, observations, action_masks, actions):
        logits, values = self.model(observations)
        distribution = MaskedCategorical(logits, action_masks)
        return distribution.log_prob(actions), values, distribution.entropy()

    def set_policy_sampling_stream(
        self,
        *,
        worker_index: int = 0,
        env_index: int = 0,
        episode_index: int = 0,
        stream_index: int = 0,
    ) -> int:
        """Bind stochastic rollout sampling to explicit seed coordinates."""

        derived = self.seed.derive_seed(
            TrainingSeedStream.POLICY_SAMPLING,
            worker_index=worker_index,
            env_index=env_index,
            episode_index=episode_index,
            stream_index=stream_index,
        )
        self.policy_sampling_generator.manual_seed(derived)
        return derived

    def predict_value(self, observation):
        """Return a detached value estimate without sampling or RNG effects."""

        was_training = self.model.training
        self.model.eval()
        try:
            with torch.no_grad():
                _logits, value = self.model(observation)
            return value.detach()
        finally:
            self.model.train(was_training)

    def parameter_content_hash(self) -> str:
        """Stable debug identity for current weights, not a semantic config hash."""

        digest = hashlib.sha256()
        for name, value in sorted(self.model.state_dict().items()):
            tensor = value.detach().cpu().contiguous()
            digest.update(name.encode("utf-8"))
            digest.update(str(tensor.dtype).encode("ascii"))
            digest.update(str(tuple(tensor.shape)).encode("ascii"))
            digest.update(tensor.numpy().tobytes(order="C"))
        return digest.hexdigest()

    def act(
        self, observation, action_mask, *, evaluation: bool = False,
        evaluation_generator=None,
    ) -> PPOActionOutput:
        if evaluation_generator is not None and not evaluation:
            raise ValueError("evaluation_generator requires evaluation=True")
        if evaluation_generator is not None and not isinstance(
            evaluation_generator, torch.Generator
        ):
            raise TypeError("evaluation_generator must be a torch.Generator")
        device = next(self.model.parameters()).device
        if device.type != "cpu" and (not evaluation or evaluation_generator is not None):
            raise PPOCoreError("CUDA core supports only optimizer-free argmax evaluation")
        _tensor(observation, "observation", dtype=torch.float32, min_ndim=1)
        _tensor(action_mask, "action_mask", dtype=torch.bool, min_ndim=1)
        observation = observation.to(device)
        action_mask = action_mask.to(device)
        self.last_action_devices = {"input_device": str(observation.device), "mask_device": str(action_mask.device), "model_device": str(device)}
        was_training = self.model.training
        self.model.eval() if evaluation else self.model.train()
        try:
            with torch.no_grad():
                logits, value = self.model(observation)
                distribution = MaskedCategorical(logits, action_mask)
                action = (
                    (
                        masked_argmax(logits, action_mask)
                        if evaluation_generator is None
                        else distribution.sample(generator=evaluation_generator)
                    )
                    if evaluation
                    else distribution.sample(generator=self.policy_sampling_generator)
                )
                log_prob = distribution.log_prob(action)
            if not evaluation:
                self.sample_step += int(action.numel())
            return PPOActionOutput(
                action.detach(), log_prob.detach(), value.detach()
            )
        finally:
            self.model.train(was_training)

    def update(self, batch: PPORolloutBatch) -> PPOUpdateMetrics:
        if next(self.model.parameters()).device.type != "cpu":
            raise PPOCoreError("CUDA PPO optimizer updates are not supported")
        if not isinstance(batch, PPORolloutBatch):
            raise TypeError("batch must be PPORolloutBatch")
        if batch.ppo_config_hash != self.config.content_hash:
            raise PPOCoreError("rollout PPO config identity mismatch")
        self.model.train()
        advantages = normalize_advantages(
            batch.advantages,
            epsilon=self.config.advantage_normalization_epsilon,
        )
        metrics: list[tuple[PPOLoss, float, float]] = []
        optimizer_steps = 0
        length = batch.observations.shape[0]
        for _epoch in range(self.config.update_epochs):
            permutation = torch.randperm(
                length, generator=self.minibatch_shuffle_generator
            )
            for start in range(0, length, self.config.minibatch_size):
                indices = permutation[start:start + self.config.minibatch_size]
                selected = batch.select(indices)
                new_log_prob, new_values, entropy = self.evaluate_actions(
                    selected.observations, selected.action_masks, selected.actions
                )
                loss = ppo_loss(
                    new_log_probs=new_log_prob,
                    old_log_probs=selected.old_log_probs,
                    advantages=advantages.index_select(0, indices),
                    new_values=new_values,
                    value_targets=selected.value_targets,
                    entropy=entropy,
                    config=self.config,
                )
                self.optimizer.zero_grad(set_to_none=True)
                loss.total_loss.backward()
                _require_finite_gradients(self.model)
                pre_clip = torch.nn.utils.clip_grad_norm_(
                    self.model.parameters(), self.config.max_grad_norm
                )
                if not bool(torch.isfinite(pre_clip)):
                    raise PPOCoreError("gradient norm is not finite")
                _require_finite_gradients(self.model)
                post_clip = min(float(pre_clip), self.config.max_grad_norm)
                self.optimizer.step()
                _require_finite_parameters(self.model)
                metrics.append((loss, float(pre_clip), post_clip))
                optimizer_steps += 1
        self.update_step += 1
        return PPOUpdateMetrics(
            policy_loss=_mean(item[0].policy_loss for item in metrics),
            value_loss=_mean(item[0].value_loss for item in metrics),
            entropy=_mean(item[0].entropy_mean for item in metrics),
            total_loss=_mean(item[0].total_loss for item in metrics),
            approx_kl=_mean(item[0].approx_kl for item in metrics),
            clip_fraction=_mean(item[0].clip_fraction for item in metrics),
            grad_norm=sum(item[1] for item in metrics) / len(metrics),
            clipped_grad_norm=sum(item[2] for item in metrics) / len(metrics),
            optimizer_steps=optimizer_steps,
            update_step=self.update_step,
        )

    def checkpoint_payload(self, reward_identity: "PPORewardIdentity") -> dict[str, object]:
        manifest = PPOCheckpointManifest.from_core(self, reward_identity)
        return {
            "manifest": manifest.to_dict(),
            "model_state_dict": deepcopy(self.model.state_dict()),
            "optimizer_state_dict": deepcopy(self.optimizer.state_dict()),
            "torch_cpu_rng_state": torch.random.get_rng_state().clone(),
            "policy_sampling_rng_state": self.policy_sampling_generator.get_state().clone(),
            "minibatch_shuffle_rng_state": self.minibatch_shuffle_generator.get_state().clone(),
        }

    def restore_checkpoint(
        self, payload: Mapping[str, object], *, reward_identity: "PPORewardIdentity",
    ) -> None:
        if not isinstance(payload, Mapping) or set(payload) != {
            "manifest", "model_state_dict", "optimizer_state_dict",
            "torch_cpu_rng_state", "policy_sampling_rng_state",
            "minibatch_shuffle_rng_state",
        }:
            raise PPOCheckpointError("checkpoint payload fields are invalid")
        manifest = PPOCheckpointManifest.from_dict(payload["manifest"])
        manifest.require_runtime(self, reward_identity)
        try:
            self.model.load_state_dict(payload["model_state_dict"], strict=True)
            self.optimizer.load_state_dict(payload["optimizer_state_dict"])
            torch.random.set_rng_state(payload["torch_cpu_rng_state"])
            self.policy_sampling_generator.set_state(payload["policy_sampling_rng_state"])
            self.minibatch_shuffle_generator.set_state(payload["minibatch_shuffle_rng_state"])
        except (TypeError, ValueError, RuntimeError, KeyError) as exc:
            raise PPOCheckpointError(f"checkpoint binary state is invalid: {exc}") from exc
        self.update_step = manifest.update_step
        self.sample_step = manifest.sample_step


@dataclass(frozen=True, slots=True)
class PPORewardIdentity:
    policy_id: str
    policy_version: str
    policy_hash: str

    def __post_init__(self) -> None:
        _text(self.policy_id, "reward policy id")
        _text(self.policy_version, "reward policy version")
        _sha256(self.policy_hash, "reward policy hash")

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.policy_id,
            "version": self.policy_version,
            "hash": self.policy_hash,
        }


@dataclass(frozen=True, slots=True)
class PPOCheckpointManifest:
    schema_version: str
    ppo_config_hash: str
    backend: PPOAuthorityReference
    preprocessor: PPOAuthorityReference
    action_encoder_hash: str
    action_capacity: int
    discount: PPOAuthorityReference
    seed: PPOAuthorityReference
    reward: PPORewardIdentity
    update_step: int
    sample_step: int
    semantic_hash: str

    @classmethod
    def from_core(
        cls, core: NativeMaskedPPO, reward: PPORewardIdentity,
    ) -> "PPOCheckpointManifest":
        values = {
            "schema_version": PPO_CHECKPOINT_SCHEMA_VERSION,
            "ppo_config_hash": core.config.content_hash,
            "backend": core.config.backend,
            "preprocessor": core.config.observation_preprocessor,
            "action_encoder_hash": core.config.action_encoder.contract_hash,
            "action_capacity": core.config.action_encoder.capacity,
            "discount": core.config.discount,
            "seed": core.config.seed,
            "reward": reward,
            "update_step": core.update_step,
            "sample_step": core.sample_step,
        }
        return cls(**values, semantic_hash=canonical_hash(_manifest_payload(values)))

    @classmethod
    def from_dict(cls, value: object) -> "PPOCheckpointManifest":
        if not isinstance(value, Mapping):
            raise PPOCheckpointError("checkpoint manifest must be an object")
        expected = {
            "schema_version", "ppo_config_hash", "backend", "preprocessor",
            "action_encoder_hash", "action_capacity", "discount", "seed",
            "reward", "update_step", "sample_step", "semantic_hash",
        }
        if set(value) != expected:
            raise PPOCheckpointError("checkpoint manifest fields are invalid")
        def reference(name: str) -> PPOAuthorityReference:
            item = value[name]
            if not isinstance(item, Mapping) or set(item) != {"id", "version", "hash"}:
                raise PPOCheckpointError(f"checkpoint {name} identity is invalid")
            return PPOAuthorityReference(item["id"], item["version"], item["hash"])
        reward_value = value["reward"]
        if not isinstance(reward_value, Mapping) or set(reward_value) != {"id", "version", "hash"}:
            raise PPOCheckpointError("checkpoint reward identity is invalid")
        return cls(
            schema_version=value["schema_version"],
            ppo_config_hash=value["ppo_config_hash"],
            backend=reference("backend"),
            preprocessor=reference("preprocessor"),
            action_encoder_hash=value["action_encoder_hash"],
            action_capacity=value["action_capacity"],
            discount=reference("discount"), seed=reference("seed"),
            reward=PPORewardIdentity(
                reward_value["id"], reward_value["version"], reward_value["hash"]
            ),
            update_step=value["update_step"], sample_step=value["sample_step"],
            semantic_hash=value["semantic_hash"],
        )

    def __post_init__(self) -> None:
        if self.schema_version != PPO_CHECKPOINT_SCHEMA_VERSION:
            raise PPOCheckpointError("unsupported PPO checkpoint schema")
        _sha256(self.ppo_config_hash, "checkpoint PPO config hash")
        _sha256(self.action_encoder_hash, "checkpoint action encoder hash")
        if type(self.action_capacity) is not int or self.action_capacity <= 0:
            raise PPOCheckpointError("checkpoint action capacity is invalid")
        for name in ("update_step", "sample_step"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise PPOCheckpointError(f"checkpoint {name} is invalid")
        expected = canonical_hash(_manifest_payload({
            "schema_version": self.schema_version,
            "ppo_config_hash": self.ppo_config_hash,
            "backend": self.backend,
            "preprocessor": self.preprocessor,
            "action_encoder_hash": self.action_encoder_hash,
            "action_capacity": self.action_capacity,
            "discount": self.discount,
            "seed": self.seed,
            "reward": self.reward,
            "update_step": self.update_step,
            "sample_step": self.sample_step,
        }))
        if self.semantic_hash != expected:
            raise PPOCheckpointError("checkpoint manifest hash mismatch")

    def to_dict(self) -> dict[str, object]:
        result = _manifest_payload({
            "schema_version": self.schema_version,
            "ppo_config_hash": self.ppo_config_hash,
            "backend": self.backend,
            "preprocessor": self.preprocessor,
            "action_encoder_hash": self.action_encoder_hash,
            "action_capacity": self.action_capacity,
            "discount": self.discount,
            "seed": self.seed,
            "reward": self.reward,
            "update_step": self.update_step,
            "sample_step": self.sample_step,
        })
        result["semantic_hash"] = self.semantic_hash
        return result

    def require_runtime(
        self, core: NativeMaskedPPO, reward: PPORewardIdentity,
    ) -> None:
        expected = PPOCheckpointManifest.from_core(core, reward)
        static_names = (
            "ppo_config_hash", "backend", "preprocessor", "action_encoder_hash",
            "action_capacity", "discount", "seed", "reward",
        )
        if any(getattr(self, name) != getattr(expected, name) for name in static_names):
            raise PPOCheckpointError("checkpoint/runtime authority mismatch")


def _manifest_payload(value: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": value["schema_version"],
        "ppo_config_hash": value["ppo_config_hash"],
        "backend": value["backend"].to_dict(),
        "preprocessor": value["preprocessor"].to_dict(),
        "action_encoder_hash": value["action_encoder_hash"],
        "action_capacity": value["action_capacity"],
        "discount": value["discount"].to_dict(),
        "seed": value["seed"].to_dict(),
        "reward": value["reward"].to_dict(),
        "update_step": value["update_step"],
        "sample_step": value["sample_step"],
    }


def _orthogonal(layer, gain: float, generator) -> None:
    nn.init.orthogonal_(layer.weight, gain=gain, generator=generator)
    nn.init.constant_(layer.bias, 0.0)


def _tensor(value, name: str, *, dtype, min_ndim: int, exact_ndim: int | None = None) -> None:
    if not isinstance(value, torch.Tensor):
        raise TypeError(f"{name} must be a torch.Tensor")
    if value.dtype is not dtype:
        raise PPOCoreError(f"{name} dtype must be {dtype}")
    if value.ndim < min_ndim or (exact_ndim is not None and value.ndim != exact_ndim):
        raise PPOCoreError(f"{name} rank is invalid")


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite number (not bool)")
    result = float(value)
    if not math.isfinite(result):
        raise PPOCoreError(f"{name} must be finite")
    return result


def _finite_sequence(value: Sequence[object], name: str) -> None:
    for item in value:
        _finite(item, name)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PPOCoreError(f"{name} must be non-empty trimmed text")
    return value


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        item not in "0123456789abcdef" for item in value
    ):
        raise PPOCoreError(f"{name} must be lowercase SHA-256")
    return value


def _require_finite_gradients(model) -> None:
    for parameter in model.parameters():
        if parameter.grad is not None and not bool(torch.isfinite(parameter.grad).all()):
            raise PPOCoreError("model gradient contains NaN or Inf")


def _require_finite_parameters(model) -> None:
    for parameter in model.parameters():
        if not bool(torch.isfinite(parameter).all()):
            raise PPOCoreError("model parameter contains NaN or Inf")


def _mean(values) -> float:
    converted = [float(value.detach()) for value in values]
    return sum(converted) / len(converted)


def _batch_tensor_names() -> tuple[str, ...]:
    return (
        "observations", "action_masks", "actions", "old_log_probs",
        "old_values", "rewards", "terminated", "truncated",
        "effective_gammas", "next_values", "bootstrap_allowed",
        "trace_continues", "advantages", "value_targets",
    )


__all__ = [
    "AllActionsMaskedError", "GAEResult", "InvalidMaskedActionError",
    "LearnerTransition", "MaskedCategorical", "NativeMaskedPPO",
    "PPOActionOutput", "PPOCheckpointError", "PPOCheckpointManifest",
    "PPOCoreError", "PPOLoss", "PPORolloutBatch", "PPORewardIdentity",
    "PPOUpdateMetrics", "PPO_CHECKPOINT_SCHEMA_VERSION",
    "PPO_LEARNER_TRANSITION_SCHEMA_VERSION", "PPO_ROLLOUT_BATCH_SCHEMA_VERSION",
    "SharedActorCriticMLP", "compute_gae", "masked_argmax",
    "normalize_advantages", "ppo_loss",
]
