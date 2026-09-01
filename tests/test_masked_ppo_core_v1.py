from __future__ import annotations

import ast
from copy import deepcopy
import json
import math
from pathlib import Path

import pytest
import torch

from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.training.decision_encoder import DecisionActionEncoder
from wargame_native_engine.training.discount import (
    TrainingDiscountBasis,
    build_shared_training_discount_config,
    load_shared_training_discount_config,
)
from wargame_native_engine.training.eight_factor_shaping_contract import (
    load_eight_factor_shaping_policy,
)
from wargame_native_engine.training.learner_preprocessor import (
    load_learner_observation_preprocessor_spec,
)
from wargame_native_engine.training.ppo_config import (
    PPO_CONFIG_SCHEMA_VERSION,
    PPOConfigContractError,
    build_ppo_algorithm_config,
    load_ppo_algorithm_config,
    require_ppo_runtime_authorities,
)
from wargame_native_engine.training.ppo_core import (
    AllActionsMaskedError,
    InvalidMaskedActionError,
    LearnerTransition,
    MaskedCategorical,
    NativeMaskedPPO,
    PPOCheckpointError,
    PPOCheckpointManifest,
    PPOCoreError,
    PPORolloutBatch,
    PPORewardIdentity,
    PPO_CHECKPOINT_SCHEMA_VERSION,
    PPO_LEARNER_TRANSITION_SCHEMA_VERSION,
    SharedActorCriticMLP,
    compute_gae,
    masked_argmax,
    normalize_advantages,
    ppo_loss,
)
from wargame_native_engine.training.reward import load_reward_policy
from wargame_native_engine.training.training_seed import load_training_seed_config


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config/training/native_masked_ppo_v1.json"
PRODUCTION_CONFIG_HASH = (
    "d0b08a8295b22b316d8082272fa3763f491961d07934f2ac2c2b84c951fa622d"
)


def _config(*, lr: float = 3e-4, grad: float = 0.5, epochs: int = 2):
    value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    value["network"]["hidden_sizes"] = [16, 16]
    value["rollout_length"] = 8
    value["minibatch_size"] = 4
    value["update_epochs"] = epochs
    value["learning_rate"] = lr
    value["max_grad_norm"] = grad
    return build_ppo_algorithm_config(value)


def _reward_identity() -> PPORewardIdentity:
    value = load_reward_policy()
    return PPORewardIdentity(
        value.policy_id, value.policy_version, value.content_hash
    )


def _observations(length: int = 8):
    values = torch.zeros((length, 8302), dtype=torch.float32)
    for index in range(length):
        values[index, 0] = index / 10.0
        values[index, 1] = (index % 3) / 10.0
    return values


def _masks(length: int = 8):
    value = torch.zeros((length, 256), dtype=torch.bool)
    value[:, :3] = True
    return value


def _transitions(core: NativeMaskedPPO, length: int = 8):
    observations = _observations(length)
    next_observations = observations + 0.01
    masks = _masks(length)
    actions = torch.tensor([index % 3 for index in range(length)], dtype=torch.long)
    with torch.no_grad():
        logits, values = core.model(observations)
        distribution = MaskedCategorical(logits, masks)
        old_log_probs = distribution.log_prob(actions)
        _, next_values = core.model(next_observations)
    result = []
    for index in range(length):
        reward = 0.1 if index % 2 == 0 else -0.05
        result.append(LearnerTransition(
            schema_version=PPO_LEARNER_TRANSITION_SCHEMA_VERSION,
            episode_id="fixture-episode",
            run_namespace="ppo-core-test",
            side="red",
            observation=tuple(float(item) for item in observations[index].tolist()),
            action_mask=tuple(bool(item) for item in masks[index].tolist()),
            decision_identity=canonical_hash({"decision": index}),
            catalog_hash=canonical_hash({"catalog": index}),
            action_encoder_hash=DecisionActionEncoder().contract_hash,
            action_index=int(actions[index]),
            action_id=f"fixture-action-{index}",
            old_log_prob=float(old_log_probs[index]),
            value_prediction=float(values[index]),
            reward=reward,
            reward_record_hash=canonical_hash({"reward": index}),
            base_reward_amount=reward,
            shaping_reward_amount=0.0,
            terminated=False,
            truncated=False,
            truncation_source="NONE",
            effective_gamma=core.discount.gamma,
            next_observation=tuple(
                float(item) for item in next_observations[index].tolist()
            ),
            next_decision_identity=canonical_hash({"decision": index + 1}),
            next_value_prediction=float(next_values[index]),
            bootstrap_allowed=True,
            trace_continues=index != length - 1,
            preprocessor_hash=core.config.observation_preprocessor.identity_hash,
        ))
    return tuple(result)


def _batch(core: NativeMaskedPPO) -> PPORolloutBatch:
    return PPORolloutBatch.from_transitions(_transitions(core), core.config)


def test_algorithm_config_schema_hash_and_frozen_baseline() -> None:
    config = load_ppo_algorithm_config()
    schema = json.loads((
        ROOT / "config/schemas/native_ppo_config.schema.json"
    ).read_text(encoding="utf-8"))
    assert config.schema_version == PPO_CONFIG_SCHEMA_VERSION
    assert config.algorithm_id == "native-masked-ppo"
    assert config.algorithm_version == "1.0.0"
    assert config.content_hash == PRODUCTION_CONFIG_HASH
    assert config.content_hash == canonical_hash(config.semantic_payload())
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(config.semantic_payload())
    assert config.gae_lambda == 0.95
    assert config.clip_epsilon == 0.2
    assert config.learning_rate == 3e-4
    assert config.entropy_coefficient == 0.01
    assert config.value_coefficient == 0.5
    assert config.max_grad_norm == 0.5
    assert config.rollout_length == 2048
    assert config.minibatch_size == 64
    assert config.update_epochs == 10
    assert config.hidden_sizes == (256, 256)
    assert config.action_encoder.capacity == 256


def test_algorithm_config_validation_references_and_lambda_isolation() -> None:
    original = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    shaping_hash = load_eight_factor_shaping_policy().content_hash
    changed = deepcopy(original)
    changed["gae_lambda"] = 0.8
    changed_config = build_ppo_algorithm_config(changed)
    assert changed_config.content_hash != load_ppo_algorithm_config().content_hash
    assert load_eight_factor_shaping_policy().content_hash == shaping_hash
    assert "lambda" not in original
    assert "gamma" not in original
    for field, value in (
        ("gae_lambda", 0.0), ("clip_epsilon", 1.0),
        ("learning_rate", float("nan")), ("max_grad_norm", True),
        ("rollout_length", 0),
    ):
        broken = deepcopy(original)
        broken[field] = value
        with pytest.raises((PPOConfigContractError, TypeError)):
            build_ppo_algorithm_config(broken)
    broken = deepcopy(original)
    broken["observation_preprocessor_ref"]["hash"] = "f" * 64
    with pytest.raises(PPOConfigContractError, match="preprocessor"):
        build_ppo_algorithm_config(broken)
    broken = deepcopy(original)
    broken["action_encoder_ref"]["capacity"] = 128
    with pytest.raises(PPOConfigContractError):
        build_ppo_algorithm_config(broken)


def test_l1_l5_pc1_pc4_masked_distribution_contract() -> None:
    logits = torch.tensor([
        [1e20, -1e20, 3.0, -7.0],
        [-1e20, 1e20, -3.0, 7.0],
    ], dtype=torch.float32)
    mask = torch.tensor([
        [False, False, True, True],
        [True, False, True, False],
    ], dtype=torch.bool)
    distribution = MaskedCategorical(logits, mask)
    assert torch.isfinite(distribution.probs).all()
    assert torch.equal(distribution.probs.masked_select(~mask), torch.zeros(4))
    assert torch.allclose(distribution.probs.sum(-1), torch.ones(2))
    changed = logits.clone()
    changed[~mask] = torch.tensor([9e20, -9e20, -8e20, 8e20])
    assert torch.equal(
        MaskedCategorical(changed, mask).probs,
        distribution.probs,
    )
    one = MaskedCategorical(
        torch.tensor([9.0, -4.0], dtype=torch.float32),
        torch.tensor([False, True]),
    )
    assert torch.equal(one.probs, torch.tensor([0.0, 1.0]))
    assert float(one.entropy()) == 0.0
    with pytest.raises(AllActionsMaskedError):
        MaskedCategorical(torch.zeros(256), torch.zeros(256, dtype=torch.bool))


def test_sampling_log_prob_entropy_and_masked_argmax_are_legal() -> None:
    logits = torch.tensor([3.0, 3.0, 1000.0, -5.0], dtype=torch.float32)
    mask = torch.tensor([True, True, False, False])
    distribution = MaskedCategorical(logits, mask)
    first = torch.Generator().manual_seed(123)
    second = torch.Generator().manual_seed(123)
    samples_a = [int(distribution.sample(generator=first)) for _ in range(30)]
    samples_b = [int(distribution.sample(generator=second)) for _ in range(30)]
    assert samples_a == samples_b
    assert set(samples_a) <= {0, 1}
    assert int(masked_argmax(logits, mask)) == 0
    assert math.isclose(float(distribution.entropy()), math.log(2), rel_tol=1e-6)
    assert torch.isfinite(distribution.log_prob(torch.tensor(0)))
    with pytest.raises(InvalidMaskedActionError):
        distribution.log_prob(torch.tensor(2))


def test_pc15_pc17_actor_critic_shapes_dtype_and_validation() -> None:
    config = _config()
    generator = torch.Generator().manual_seed(7)
    model = SharedActorCriticMLP(config, initialization_generator=generator)
    logits, values = model(torch.zeros((3, 8302), dtype=torch.float32))
    assert logits.shape == (3, 256)
    assert values.shape == (3,)
    assert logits.dtype is values.dtype is torch.float32
    assert not any(isinstance(item, (torch.nn.Dropout, torch.nn.BatchNorm1d)) for item in model.modules())
    with pytest.raises(PPOCoreError, match="shape"):
        model(torch.zeros((3, 8301), dtype=torch.float32))
    with pytest.raises(PPOCoreError, match="dtype"):
        model(torch.zeros((3, 8302), dtype=torch.float64))
    bad = torch.zeros((1, 8302), dtype=torch.float32)
    bad[0, 0] = float("nan")
    with pytest.raises(PPOCoreError, match="NaN"):
        model(bad)
    with pytest.raises(PPOCoreError, match="shapes"):
        MaskedCategorical(torch.zeros(256), torch.ones(255, dtype=torch.bool))


def test_rollout_transition_batch_detachment_and_provenance() -> None:
    core = NativeMaskedPPO(_config())
    transitions = _transitions(core)
    batch = PPORolloutBatch.from_transitions(transitions, core.config)
    assert batch.observations.shape == (8, 8302)
    assert batch.action_masks.shape == (8, 256)
    assert batch.observations.dtype is torch.float32
    assert batch.action_masks.dtype is torch.bool
    assert batch.actions.dtype is torch.long
    assert not batch.old_log_probs.requires_grad
    assert not batch.old_values.requires_grad
    assert batch.provenance == transitions
    selected = batch.select(torch.tensor([7, 1, 3], dtype=torch.long))
    assert selected.observations.shape == (3, 8302)
    assert tuple(item.action_id for item in selected.provenance) == (
        "fixture-action-7", "fixture-action-1", "fixture-action-3"
    )
    assert selected.to("cpu").observations.device.type == "cpu"


def test_l12_l16_gae_known_vector_terminal_truncation_and_cutoff() -> None:
    result = compute_gae(
        rewards=torch.tensor([1.0, 2.0, 3.0]),
        values=torch.tensor([0.5, 0.6, 0.7]),
        next_values=torch.tensor([0.6, 0.7, 9.0]),
        effective_gammas=torch.tensor([0.5, 0.9, 0.1]),
        bootstrap_allowed=torch.tensor([True, True, False]),
        trace_continues=torch.tensor([True, False, False]),
        gae_lambda=0.95,
    )
    assert torch.allclose(
        result.advantages, torch.tensor([1.76425, 2.03, 2.3]), atol=1e-5
    )
    assert torch.allclose(
        result.value_targets, torch.tensor([2.26425, 2.63, 3.0]), atol=1e-5
    )
    cutoff = compute_gae(
        rewards=torch.tensor([0.0]), values=torch.tensor([1.0]),
        next_values=torch.tensor([2.0]), effective_gammas=torch.tensor([0.5]),
        bootstrap_allowed=torch.tensor([True]),
        trace_continues=torch.tensor([False]), gae_lambda=0.95,
    )
    assert float(cutoff.advantages) == 0.0
    with pytest.raises(PPOCoreError):
        compute_gae(
            rewards=torch.tensor([float("inf")]), values=torch.tensor([0.0]),
            next_values=torch.tensor([0.0]), effective_gammas=torch.tensor([0.9]),
            bootstrap_allowed=torch.tensor([True]),
            trace_continues=torch.tensor([False]), gae_lambda=0.95,
        )


def test_advantage_normalization_constant_batch_is_finite() -> None:
    value = normalize_advantages(torch.ones(1), epsilon=1e-8)
    assert torch.equal(value, torch.zeros(1))
    value = normalize_advantages(torch.ones(8), epsilon=1e-8)
    assert torch.equal(value, torch.zeros(8))


def test_pc6_pc11_policy_value_entropy_loss_known_values() -> None:
    config = _config()
    zeros = torch.zeros(2)
    loss = ppo_loss(
        new_log_probs=zeros, old_log_probs=zeros,
        advantages=torch.tensor([1.0, -1.0]),
        new_values=torch.tensor([2.0, 0.0]),
        value_targets=torch.tensor([1.0, 1.0]),
        entropy=torch.tensor([0.5, 0.5]), config=config,
    )
    assert float(loss.policy_loss) == 0.0
    assert float(loss.value_loss) == 0.5
    assert math.isclose(float(loss.total_loss), 0.245, abs_tol=1e-6)
    assert float(loss.approx_kl) == 0.0
    assert float(loss.clip_fraction) == 0.0
    positive = ppo_loss(
        new_log_probs=torch.tensor([math.log(1.5)]), old_log_probs=torch.zeros(1),
        advantages=torch.ones(1), new_values=torch.zeros(1),
        value_targets=torch.zeros(1), entropy=torch.zeros(1), config=config,
    )
    negative = ppo_loss(
        new_log_probs=torch.tensor([math.log(0.5)]), old_log_probs=torch.zeros(1),
        advantages=-torch.ones(1), new_values=torch.zeros(1),
        value_targets=torch.zeros(1), entropy=torch.zeros(1), config=config,
    )
    assert math.isclose(float(positive.policy_loss), -1.2, abs_tol=1e-6)
    assert math.isclose(float(negative.policy_loss), 0.8, abs_tol=1e-6)

    document = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    document["network"]["hidden_sizes"] = [16, 16]
    document["rollout_length"] = 8
    document["minibatch_size"] = 4
    document["update_epochs"] = 1
    document["entropy_coefficient"] = 0.0
    no_entropy = build_ppo_algorithm_config(document)
    entropy_free = ppo_loss(
        new_log_probs=zeros, old_log_probs=zeros,
        advantages=torch.tensor([1.0, -1.0]), new_values=torch.ones(2),
        value_targets=torch.zeros(2), entropy=torch.ones(2), config=no_entropy,
    )
    assert math.isclose(
        float(entropy_free.total_loss),
        float(entropy_free.policy_loss + 0.5 * entropy_free.value_loss),
        abs_tol=1e-7,
    )
    document["value_coefficient"] = 0.0
    no_value_or_entropy = build_ppo_algorithm_config(document)
    coefficient_free = ppo_loss(
        new_log_probs=zeros, old_log_probs=zeros,
        advantages=torch.tensor([1.0, -1.0]), new_values=torch.ones(2),
        value_targets=torch.zeros(2), entropy=torch.ones(2),
        config=no_value_or_entropy,
    )
    assert float(coefficient_free.total_loss) == float(
        coefficient_free.policy_loss
    )


def test_l20_l23_pc12_pc14_update_finite_clipped_and_nonmutating() -> None:
    core = NativeMaskedPPO(_config(grad=1e-6, epochs=2))
    batch = _batch(core)
    before_params = tuple(item.detach().clone() for item in core.model.parameters())
    before_observations = batch.observations.clone()
    before_masks = batch.action_masks.clone()
    metrics = core.update(batch)
    assert all(math.isfinite(value) for value in (
        metrics.policy_loss, metrics.value_loss, metrics.entropy,
        metrics.total_loss, metrics.approx_kl, metrics.clip_fraction,
        metrics.grad_norm, metrics.clipped_grad_norm,
    ))
    assert metrics.optimizer_steps == 4
    assert metrics.update_step == 1
    assert metrics.grad_norm > metrics.clipped_grad_norm
    assert metrics.clipped_grad_norm <= 1e-6
    assert any(not torch.equal(a, b) for a, b in zip(before_params, core.model.parameters()))
    assert torch.equal(before_observations, batch.observations)
    assert torch.equal(before_masks, batch.action_masks)


def test_zero_learning_rate_leaves_parameters_unchanged() -> None:
    core = NativeMaskedPPO(_config(lr=0.0, epochs=1))
    batch = _batch(core)
    before = tuple(item.detach().clone() for item in core.model.parameters())
    core.update(batch)
    assert all(torch.equal(a, b) for a, b in zip(before, core.model.parameters()))


def test_cpu_initialization_sampling_minibatch_and_update_determinism() -> None:
    config = _config(epochs=1)
    first = NativeMaskedPPO(config)
    second = NativeMaskedPPO(config)
    assert all(torch.equal(a, b) for a, b in zip(first.model.parameters(), second.model.parameters()))
    observation = torch.zeros(8302, dtype=torch.float32)
    mask = torch.zeros(256, dtype=torch.bool)
    mask[:4] = True
    samples_a = [int(first.act(observation, mask).action_index) for _ in range(10)]
    samples_b = [int(second.act(observation, mask).action_index) for _ in range(10)]
    assert samples_a == samples_b
    batch = _batch(first)
    metrics_a = first.update(batch)
    metrics_b = second.update(batch)
    assert metrics_a == metrics_b
    assert all(torch.equal(a, b) for a, b in zip(first.model.parameters(), second.model.parameters()))


def test_checkpoint_save_load_outputs_rng_and_identity_validation() -> None:
    config = _config(epochs=1)
    first = NativeMaskedPPO(config)
    observation = torch.zeros(8302, dtype=torch.float32)
    mask = torch.zeros(256, dtype=torch.bool)
    mask[:3] = True
    first.act(observation, mask)
    payload = first.checkpoint_payload(_reward_identity())
    manifest = PPOCheckpointManifest.from_dict(payload["manifest"])
    assert manifest.schema_version == PPO_CHECKPOINT_SCHEMA_VERSION
    assert manifest.semantic_hash == canonical_hash({
        key: value for key, value in manifest.to_dict().items()
        if key != "semantic_hash"
    })
    expected_next = first.act(observation, mask)
    restored = NativeMaskedPPO(config)
    restored.restore_checkpoint(payload, reward_identity=_reward_identity())
    actual_next = restored.act(observation, mask)
    assert torch.equal(expected_next.action_index, actual_next.action_index)
    assert torch.equal(expected_next.log_prob, actual_next.log_prob)
    assert torch.equal(expected_next.value_prediction, actual_next.value_prediction)
    assert restored.sample_step == first.sample_step
    wrong_reward = PPORewardIdentity("other", "1.0", "f" * 64)
    with pytest.raises(PPOCheckpointError, match="authority"):
        NativeMaskedPPO(config).restore_checkpoint(
            payload, reward_identity=wrong_reward
        )
    with pytest.raises(PPOCheckpointError, match="authority"):
        NativeMaskedPPO(_config(lr=0.0, epochs=1)).restore_checkpoint(
            payload, reward_identity=_reward_identity()
        )


def test_discount_mismatch_fails_and_gamma_is_transition_data() -> None:
    config = load_ppo_algorithm_config()
    other = build_shared_training_discount_config(
        config_id="fixture-discount", config_version="1.0", gamma=0.5,
        basis=TrainingDiscountBasis.PER_DECISION_TRANSITION,
    )
    with pytest.raises(PPOConfigContractError, match="discount"):
        require_ppo_runtime_authorities(config, discount=other)
    assert "gamma" not in config.semantic_payload()
    assert load_shared_training_discount_config().gamma == 0.99


def test_actor_core_static_purity_and_no_reward_recomputation() -> None:
    source = (ROOT / "src/wargame_native_engine/training/ppo_core.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any("training.reward" in value for value in imports)
    assert not any("core.engine" in value or "core.world" in value for value in imports)
    assert not any(
        value.startswith("wargame_native_engine.gateway")
        or value.startswith("wargame_native_engine.analysis")
        or value.startswith("wargame_native_engine.projection")
        for value in imports
    )


def test_checkpoint_schema_and_current_authority_hashes() -> None:
    schema = json.loads((
        ROOT / "config/schemas/native_ppo_checkpoint.schema.json"
    ).read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert load_learner_observation_preprocessor_spec().semantic_hash == (
        "8f0520cd00d03d4346146f22b4cc8a35290d5868b1281419e4a5baa500ef1554"
    )
    assert load_training_seed_config().content_hash == (
        "0bf9ddec709ab6048855f6bf86e2338fe5f3b8ef15fdca99a98e9fdf092aa1f3"
    )
    assert load_shared_training_discount_config().content_hash == (
        "f35a748f9d0232506135b7e2438fa64f5bcc601a7aeb1fc631b182f396e4f06e"
    )
