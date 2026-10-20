"""Small, deterministic masked behavior-cloning utilities.

This module is tensor-only. It never owns an environment, reads WorldState, or
changes PPO counters. The caller is responsible for collecting frozen
observation/catalog bindings through the normal Native training boundary.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Mapping, Sequence

from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.training.learner_backend import require_pytorch
from wargame_native_engine.training.ppo_core import MaskedCategorical


torch = require_pytorch()

BC_CONFIG_SCHEMA_VERSION = "native-behavior-cloning-config/1.0"
BC_TRAINING_METHOD = "MASKED_CROSS_ENTROPY_SINGLE_EXACT_EXPERT_LABEL"


class BehaviorCloningError(RuntimeError):
    """A frozen sample or bounded BC update failed closed."""


@dataclass(frozen=True, slots=True)
class BehaviorCloningConfig:
    config_id: str
    config_version: str
    seed: int
    learning_rate: float
    betas: tuple[float, float]
    epsilon: float
    weight_decay: float
    maximum_optimizer_steps: int
    maximum_mean_loss: float
    content_hash: str
    trainable_parameters: str = "SHARED_BACKBONE_AND_POLICY_HEAD_ONLY"


@dataclass(frozen=True, slots=True)
class BehaviorCloningSample:
    """One exact label bound to the model input and legal action mask."""

    phase: str
    observation: tuple[float, ...]
    action_mask: tuple[bool, ...]
    action_index: int
    learner_tensor_hash: str
    action_mask_hash: str
    decision_hash: str
    catalog_hash: str
    action_semantics_hash: str

    def __post_init__(self) -> None:
        if not self.phase or self.phase != self.phase.strip():
            raise BehaviorCloningError("sample phase must be non-empty text")
        from wargame_native_engine.training.demonstrations import validate_input_label
        try:
            validate_input_label(self.observation, self.action_mask, self.action_index)
        except ValueError as exc:
            raise BehaviorCloningError(str(exc)) from exc
        for name in (
            "learner_tensor_hash", "action_mask_hash", "decision_hash",
            "catalog_hash", "action_semantics_hash",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 64:
                raise BehaviorCloningError(f"sample {name} must be sha256")
        if canonical_hash(list(self.observation)) != self.learner_tensor_hash:
            raise BehaviorCloningError("sample learner tensor hash mismatch")
        if canonical_hash(list(self.action_mask)) != self.action_mask_hash:
            raise BehaviorCloningError("sample action mask hash mismatch")

    @property
    def deduplication_key(self) -> tuple[str, str]:
        return self.learner_tensor_hash, self.action_mask_hash


@dataclass(frozen=True, slots=True)
class BehaviorCloningFit:
    optimizer_steps: int
    stopped_early: bool
    stop_reason: str
    parameter_hash_before: str
    parameter_hash_after: str
    parameter_l2_delta: float
    parameter_max_abs_delta: float
    final_gradient_l2_norm: float
    loss_history: tuple[float, ...]
    parameter_scope: Mapping[str, object]
    before: Mapping[str, object]
    after: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "optimizer_steps": self.optimizer_steps,
            "stopped_early": self.stopped_early,
            "stop_reason": self.stop_reason,
            "parameter_hash_before": self.parameter_hash_before,
            "parameter_hash_after": self.parameter_hash_after,
            "parameter_l2_delta": self.parameter_l2_delta,
            "parameter_max_abs_delta": self.parameter_max_abs_delta,
            "final_gradient_l2_norm": self.final_gradient_l2_norm,
            "loss_history": list(self.loss_history),
            "parameter_scope": dict(self.parameter_scope),
            "before": dict(self.before),
            "after": dict(self.after),
        }


def load_behavior_cloning_config(path: str | Path) -> BehaviorCloningConfig:
    source = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(source, Mapping):
        raise BehaviorCloningError("BC config must be an object")
    expected = {
        "schema_version", "config_id", "config_version", "training_method",
        "seed", "optimizer", "batch", "maximum_optimizer_steps", "stop",
        "trainable_parameters", "equivalent_label_policy", "checkpoint_selection",
    }
    if set(source) != expected:
        raise BehaviorCloningError("BC config fields are invalid")
    if source["schema_version"] != BC_CONFIG_SCHEMA_VERSION:
        raise BehaviorCloningError("unsupported BC config schema")
    if source["training_method"] != BC_TRAINING_METHOD:
        raise BehaviorCloningError("unsupported BC training method")
    if source["batch"] != "ALL_DEDUPLICATED_SAMPLES_CANONICAL_ORDER":
        raise BehaviorCloningError("BC batch order must be deterministic")
    if source["trainable_parameters"] not in {
        "SHARED_BACKBONE_AND_POLICY_HEAD_ONLY", "POLICY_HEAD_ONLY",
    }:
        raise BehaviorCloningError("BC may only train the shared policy path")
    if source["equivalent_label_policy"] != "NONE_SINGLE_EXACT_RULE_LABEL":
        raise BehaviorCloningError("unsupported equivalent-label policy")
    if source["checkpoint_selection"] != "FINAL_STEP_ONLY_NO_EVALUATION_SELECTION":
        raise BehaviorCloningError("BC checkpoint selection must be final-only")
    optimizer = source["optimizer"]
    stop = source["stop"]
    if not isinstance(optimizer, Mapping) or set(optimizer) != {
        "type", "learning_rate", "betas", "epsilon", "weight_decay",
    } or optimizer["type"] != "ADAM":
        raise BehaviorCloningError("BC optimizer config is invalid")
    if not isinstance(stop, Mapping) or set(stop) != {
        "all_expert_labels_top1", "mean_loss_at_most",
    } or stop["all_expert_labels_top1"] is not True:
        raise BehaviorCloningError("BC stop config is invalid")
    betas = optimizer["betas"]
    if not isinstance(betas, list) or len(betas) != 2:
        raise BehaviorCloningError("BC Adam betas are invalid")
    seed = _nonnegative_int(source["seed"], "BC seed")
    steps = _positive_int(source["maximum_optimizer_steps"], "BC maximum steps")
    if steps > 256:
        raise BehaviorCloningError("BC maximum steps exceed the task limit")
    learning_rate = _positive_finite(optimizer["learning_rate"], "learning rate")
    beta_values = tuple(_unit_interval(v, "Adam beta") for v in betas)
    epsilon = _positive_finite(optimizer["epsilon"], "Adam epsilon")
    weight_decay = _nonnegative_finite(optimizer["weight_decay"], "weight decay")
    maximum_loss = _positive_finite(stop["mean_loss_at_most"], "maximum loss")
    config_id = _text(source["config_id"], "config id")
    config_version = _text(source["config_version"], "config version")
    return BehaviorCloningConfig(
        config_id=config_id,
        config_version=config_version,
        seed=seed,
        learning_rate=learning_rate,
        betas=(beta_values[0], beta_values[1]),
        epsilon=epsilon,
        weight_decay=weight_decay,
        maximum_optimizer_steps=steps,
        maximum_mean_loss=maximum_loss,
        content_hash=canonical_hash(source),
        trainable_parameters=source["trainable_parameters"],
    )


def validate_behavior_cloning_provenance(value: object) -> Mapping[str, object]:
    """Validate the inference-only BC sidecar attached to a PPO checkpoint."""

    if not isinstance(value, Mapping):
        raise BehaviorCloningError("BC provenance must be an object")
    expected = {
        "schema_version", "training_method", "inference_restore_only",
        "seamless_optimizer_resume_validated", "starting_parameter_hash",
        "final_parameter_hash", "dataset_sha256", "protocol_sha256",
        "bc_config_hash", "optimizer", "ppo_update_steps", "ppo_sample_steps",
        "runner_checkpoint_identity", "runner_checkpoint_state_hash",
        "source_replay_hashes", "semantic_hash",
    }
    version = value.get("schema_version")
    if version in {"native-behavior-cloning-provenance/2.0", "native-behavior-cloning-provenance/3.0"}:
        expected |= {"parameter_scope"}
    if version == "native-behavior-cloning-provenance/3.0":
        expected |= {"execution"}
        execution = value.get("execution")
        if not isinstance(execution, Mapping) or execution.get("requested_device") not in {"cpu", "cuda:0"}:
            raise BehaviorCloningError("BC provenance device is invalid")
        target = execution["requested_device"]
        if any(execution.get(k) != target for k in ("input_device", "mask_device", "label_device", "loss_device")) or any(
            execution.get(k) != [target] for k in ("model_devices", "gradient_devices", "optimizer_tensor_devices")
        ) or execution.get("model_dtypes") != ["torch.float32"]:
            raise BehaviorCloningError("BC provenance execution devices disagree")
    if set(value) != expected:
        raise BehaviorCloningError("BC provenance fields are invalid")
    if version not in {"native-behavior-cloning-provenance/1.0",
                       "native-behavior-cloning-provenance/2.0",
                       "native-behavior-cloning-provenance/3.0"} or (
        value["training_method"] != BC_TRAINING_METHOD
    ):
        raise BehaviorCloningError("unsupported BC provenance identity")
    if value["inference_restore_only"] is not True or (
        value["seamless_optimizer_resume_validated"] is not False
    ):
        raise BehaviorCloningError("BC provenance resume claim is invalid")
    if value["ppo_update_steps"] != 0 or value["ppo_sample_steps"] != 0:
        raise BehaviorCloningError("BC provenance must not claim PPO steps")
    for name in (
        "starting_parameter_hash", "final_parameter_hash", "dataset_sha256",
        "protocol_sha256", "bc_config_hash", "runner_checkpoint_identity",
        "runner_checkpoint_state_hash",
    ):
        item = value[name]
        if not isinstance(item, str) or len(item) != 64:
            raise BehaviorCloningError(f"BC provenance {name} must be sha256")
    replay_hashes = value["source_replay_hashes"]
    if not isinstance(replay_hashes, list) or replay_hashes != sorted(replay_hashes) or (
        any(not isinstance(item, str) or len(item) != 64 for item in replay_hashes)
    ):
        raise BehaviorCloningError("BC provenance replay hashes are invalid")
    optimizer = value["optimizer"]
    if not isinstance(optimizer, Mapping) or optimizer.get("type") != "ADAM" or (
        type(optimizer.get("optimizer_steps")) is not int
        or not 0 < optimizer["optimizer_steps"] <= 256
    ):
        raise BehaviorCloningError("BC provenance optimizer is invalid")
    if version in {"native-behavior-cloning-provenance/2.0", "native-behavior-cloning-provenance/3.0"}:
        scope = value["parameter_scope"]
        if not isinstance(scope, Mapping) or scope.get("mode") not in {
            "SHARED_BACKBONE_AND_POLICY_HEAD_ONLY", "POLICY_HEAD_ONLY",
        } or scope.get("value_head_unchanged") is not True:
            raise BehaviorCloningError("BC provenance parameter scope is invalid")
        expected_scope = {
            "mode", "trainable_parameter_names", "frozen_parameter_names", "modules",
            "backbone_unchanged", "value_head_unchanged", "hidden_outputs_unchanged", "policy_head_updated",
        }
        if set(scope) != expected_scope:
            raise BehaviorCloningError("BC provenance parameter scope fields are invalid")
        for key in ("trainable_parameter_names", "frozen_parameter_names"):
            names = scope[key]
            if not isinstance(names, list) or any(not isinstance(n, str) or not n for n in names) or names != sorted(set(names)):
                raise BehaviorCloningError("BC provenance parameter names are invalid")
        if not scope["trainable_parameter_names"] or set(scope["trainable_parameter_names"]) & set(scope["frozen_parameter_names"]):
            raise BehaviorCloningError("BC provenance parameter partition is invalid")
        modules = scope["modules"]
        if not isinstance(modules, Mapping) or set(modules) != {"backbone", "policy_head", "value_head"}:
            raise BehaviorCloningError("BC provenance modules are invalid")
        for hashes in modules.values():
            if not isinstance(hashes, Mapping) or set(hashes) != {"before_hash", "after_hash"} or any(
                not isinstance(h, str) or len(h) != 64 for h in hashes.values()
            ):
                raise BehaviorCloningError("BC provenance module hashes are invalid")
        for module, unchanged in (("backbone", "backbone_unchanged"), ("value_head", "value_head_unchanged")):
            if scope[unchanged] != (modules[module]["before_hash"] == modules[module]["after_hash"]):
                raise BehaviorCloningError("BC provenance frozen module hashes disagree")
        if scope["policy_head_updated"] != (modules["policy_head"]["before_hash"] != modules["policy_head"]["after_hash"]):
            raise BehaviorCloningError("BC provenance policy update hashes disagree")
        if scope["mode"] == "POLICY_HEAD_ONLY" and not (
            scope.get("backbone_unchanged") is True
            and scope.get("hidden_outputs_unchanged") is True
            and scope.get("policy_head_updated") is True
        ):
            raise BehaviorCloningError("BC provenance frozen backbone claim is invalid")
    payload = dict(value)
    claimed = payload.pop("semantic_hash")
    if claimed != canonical_hash(payload):
        raise BehaviorCloningError("BC provenance semantic hash mismatch")
    return dict(value)


def deduplicate_behavior_cloning_samples(
    samples: Sequence[BehaviorCloningSample],
) -> tuple[tuple[BehaviorCloningSample, ...], Mapping[tuple[str, str], int]]:
    """Deduplicate exact inputs and reject ambiguous expert semantics."""

    if not samples:
        raise BehaviorCloningError("BC requires at least one sample")
    chosen: dict[tuple[str, str], BehaviorCloningSample] = {}
    counts: dict[tuple[str, str], int] = {}
    for sample in samples:
        if not isinstance(sample, BehaviorCloningSample):
            raise TypeError("samples must contain BehaviorCloningSample values")
        key = sample.deduplication_key
        previous = chosen.get(key)
        if previous is not None and (
            previous.action_index != sample.action_index
            or previous.action_semantics_hash != sample.action_semantics_hash
        ):
            raise BehaviorCloningError(
                "identical learner tensor/mask has conflicting expert labels"
            )
        chosen.setdefault(key, sample)
        counts[key] = counts.get(key, 0) + 1
    ordered = tuple(chosen[key] for key in sorted(chosen))
    return ordered, counts


def masked_cross_entropy(model, samples: Sequence[BehaviorCloningSample]):
    observations, masks, actions = _batch(samples, device=next(model.parameters()).device)
    logits, _values = model(observations)
    distribution = MaskedCategorical(logits, masks)
    return -distribution.log_prob(actions).mean()


def evaluate_behavior_cloning(
    model, samples: Sequence[BehaviorCloningSample],
) -> Mapping[str, object]:
    observations, masks, actions = _batch(samples, device=next(model.parameters()).device)
    was_training = model.training
    model.eval()
    try:
        with torch.no_grad():
            logits, _values = model(observations)
            distribution = MaskedCategorical(logits, masks)
            losses = -distribution.log_prob(actions)
            probabilities = distribution.probs
    finally:
        model.train(was_training)
    records = []
    for row, sample in enumerate(samples):
        legal = [index for index, allowed in enumerate(sample.action_mask) if allowed]
        order = sorted(
            legal,
            key=lambda index: (-float(probabilities[row, index]), index),
        )
        rank = order.index(sample.action_index) + 1
        records.append({
            "phase": sample.phase,
            "learner_tensor_hash": sample.learner_tensor_hash,
            "action_mask_hash": sample.action_mask_hash,
            "expert_action_index": sample.action_index,
            "expert_action_semantics_hash": sample.action_semantics_hash,
            "legal_action_count": len(legal),
            "loss": float(losses[row]),
            "expert_probability": float(probabilities[row, sample.action_index]),
            "expert_rank": rank,
            "top1_index": order[0],
            "top1_matches": rank == 1,
        })
    return {
        "mean_loss": sum(float(item["loss"]) for item in records) / len(records),
        "all_expert_labels_top1": all(bool(item["top1_matches"]) for item in records),
        "samples": records,
    }


def single_sample_update_probe(
    model, sample: BehaviorCloningSample, config: BehaviorCloningConfig,
) -> Mapping[str, object]:
    """Run one disposable update to prove the policy gradient path."""

    probe = deepcopy(model)
    before = model_parameter_hash(probe)
    before_loss = float(masked_cross_entropy(probe, (sample,)).detach())
    optimizer = _optimizer(probe, config)
    optimizer.zero_grad(set_to_none=True)
    loss = masked_cross_entropy(probe, (sample,))
    loss.backward()
    gradient = _gradient_norm(probe)
    optimizer.step()
    _require_finite_parameters(probe)
    after = model_parameter_hash(probe)
    after_loss = float(masked_cross_entropy(probe, (sample,)).detach())
    return {
        "sample_tensor_hash": sample.learner_tensor_hash,
        "loss_before": before_loss,
        "loss_after": after_loss,
        "policy_gradient_l2_norm": gradient,
        "parameter_hash_before": before,
        "parameter_hash_after": after,
        "parameters_updated": before != after,
        "finite": all(math.isfinite(v) for v in (before_loss, after_loss, gradient)),
    }


def fit_behavior_cloning(
    model,
    samples: Sequence[BehaviorCloningSample],
    config: BehaviorCloningConfig,
) -> BehaviorCloningFit:
    """Fit the fixed sample set once, with no evaluation-based selection."""

    unique, _counts = deduplicate_behavior_cloning_samples(samples)
    torch.manual_seed(config.seed)
    before_state = {
        name: value.detach().clone() for name, value in model.state_dict().items()
    }
    before_hash = model_parameter_hash(model)
    before_metrics = evaluate_behavior_cloning(model, unique)
    optimizer = _optimizer(model, config)
    losses: list[float] = []
    final_gradient = 0.0
    stop_reason = "MAXIMUM_OPTIMIZER_STEPS"
    flags = {name: parameter.requires_grad for name, parameter in model.named_parameters()}
    trainable = set(_trainable_names(model, config.trainable_parameters))
    for name, parameter in model.named_parameters():
        parameter.requires_grad_(name in trainable)
        parameter.grad = None
    try:
        hidden_before = _hidden(model, unique)
        model.train()
        for step in range(1, config.maximum_optimizer_steps + 1):
            optimizer.zero_grad(set_to_none=True)
            loss = masked_cross_entropy(model, unique)
            if not bool(torch.isfinite(loss)):
                raise BehaviorCloningError("BC loss is not finite")
            loss.backward()
            final_gradient = _gradient_norm(model)
            optimizer.step()
            _require_finite_parameters(model)
            losses.append(float(loss.detach()))
            metrics = evaluate_behavior_cloning(model, unique)
            if metrics["all_expert_labels_top1"] and (
                float(metrics["mean_loss"]) <= config.maximum_mean_loss
            ):
                stop_reason = "DECLARED_FIT_THRESHOLD_REACHED"
                break
        after_metrics = dict(evaluate_behavior_cloning(model, unique))
        from wargame_native_engine.training.bc_device import device_evidence
        obs, masks, labels = _batch(unique, device=next(model.parameters()).device)
        after_metrics["execution"] = {
            **device_evidence(obs.device, model), "input_device": str(obs.device),
            "mask_device": str(masks.device), "label_device": str(labels.device),
            "loss_device": str(loss.device),
            "input_dtype": str(obs.dtype), "mask_dtype": str(masks.dtype),
            "label_dtype": str(labels.dtype), "loss_dtype": str(loss.dtype),
            "gradient_devices": sorted({str(p.grad.device) for p in model.parameters() if p.grad is not None}),
            "optimizer_tensor_devices": sorted({str(v.device) for st in optimizer.state.values()
                                                for k, v in st.items() if isinstance(v, torch.Tensor) and k != "step"}),
            "optimizer_step_devices": sorted({str(st["step"].device) for st in optimizer.state.values() if "step" in st}),
        }
        after_hash = model_parameter_hash(model)
        l2 = 0.0
        maximum = 0.0
        for name, value in model.state_dict().items():
            delta = value.detach() - before_state[name]
            l2 += float(torch.square(delta).sum())
            if delta.numel():
                maximum = max(maximum, float(torch.abs(delta).max()))
        scope = {
            "mode": config.trainable_parameters,
            "trainable_parameter_names": sorted(trainable),
            "frozen_parameter_names": sorted(set(flags) - trainable),
            "modules": {
                name: {"before_hash": state_hash(before_state, name + "."),
                       "after_hash": state_hash(model.state_dict(), name + ".")}
                for name in ("backbone", "policy_head", "value_head")
            },
            "backbone_unchanged": all(torch.equal(value, before_state[name])
                                      for name, value in model.state_dict().items()
                                      if name.startswith("backbone.")),
            "value_head_unchanged": all(torch.equal(value, before_state[name])
                                        for name, value in model.state_dict().items()
                                        if name.startswith("value_head.")),
            "hidden_outputs_unchanged": torch.equal(hidden_before, _hidden(model, unique)),
            "policy_head_updated": any(not torch.equal(value, before_state[name])
                                       for name, value in model.state_dict().items()
                                       if name.startswith("policy_head.")),
        }
        return BehaviorCloningFit(
            parameter_scope=scope,
            optimizer_steps=len(losses),
            stopped_early=len(losses) < config.maximum_optimizer_steps,
            stop_reason=stop_reason,
            parameter_hash_before=before_hash,
            parameter_hash_after=after_hash,
            parameter_l2_delta=math.sqrt(l2),
            parameter_max_abs_delta=maximum,
            final_gradient_l2_norm=final_gradient,
            loss_history=tuple(losses),
            before=before_metrics,
            after=after_metrics,
        )
    finally:
        for name, parameter in model.named_parameters():
            parameter.requires_grad_(flags[name])


def activation_diagnostics(model, observations: Sequence[Sequence[float]]) -> Mapping[str, object]:
    """Aggregate finite input and Tanh saturation diagnostics."""

    tensor = torch.tensor(tuple(tuple(row) for row in observations), dtype=torch.float32, device=next(model.parameters()).device)
    if tensor.ndim != 2 or tensor.shape[0] == 0:
        raise BehaviorCloningError("diagnostics require a non-empty observation batch")
    current = tensor
    tanh_layers = []
    with torch.no_grad():
        for index, layer in enumerate(model.backbone):
            current = layer(current)
            if isinstance(layer, torch.nn.Tanh):
                absolute = torch.abs(current)
                tanh_layers.append({
                    "backbone_layer_index": index,
                    "unit_count": current.numel(),
                    "abs_ge_0_99_fraction": float((absolute >= 0.99).to(torch.float32).mean()),
                    "abs_ge_0_999_fraction": float((absolute >= 0.999).to(torch.float32).mean()),
                    "minimum": float(current.min()),
                    "maximum": float(current.max()),
                })
    return {
        "input_shape": list(tensor.shape),
        "input_minimum": float(tensor.min()),
        "input_maximum": float(tensor.max()),
        "input_nonfinite_count": int((~torch.isfinite(tensor)).sum()),
        "tanh_layers": tanh_layers,
        "dtype": str(tensor.dtype),
        "hidden_pairwise_l2": [
            {"left": left, "right": right,
             "distance": float(torch.linalg.vector_norm(current[left] - current[right]))}
            for left in range(len(tensor)) for right in range(left + 1, len(tensor))
        ],
    }


def model_parameter_hash(model) -> str:
    return state_hash(model.state_dict())


def state_hash(state, prefix: str = "") -> str:
    """Hash parameters and buffers, including names, dtype and shape."""
    digest = hashlib.sha256()
    for name, value in sorted(state.items()):
        if not name.startswith(prefix):
            continue
        tensor = value.detach().cpu().contiguous()
        digest.update(name.encode("utf-8"))
        digest.update(str(tensor.dtype).encode("ascii"))
        digest.update(str(tuple(tensor.shape)).encode("ascii"))
        digest.update(tensor.numpy().tobytes(order="C"))
    return digest.hexdigest()


def _hidden(model, samples):
    with torch.no_grad():
        return model.backbone(_batch(samples, device=next(model.parameters()).device)[0]).detach().clone()


def _trainable_names(model, mode):
    if mode not in {"POLICY_HEAD_ONLY", "SHARED_BACKBONE_AND_POLICY_HEAD_ONLY"}:
        raise BehaviorCloningError("unsupported BC parameter scope")
    prefixes = (("policy_head.",) if mode == "POLICY_HEAD_ONLY"
                else ("backbone.", "policy_head."))
    return [name for name, _ in model.named_parameters() if name.startswith(prefixes)]


def validate_bc_checkpoint(checkpoint, manifest, model=None):
    """Validate optional inference provenance against the actual checkpoint."""
    path = Path(checkpoint) / "bc_provenance.json"
    if not path.exists():
        return
    value = validate_behavior_cloning_provenance(json.loads(path.read_text()))
    if any(value[key] != manifest[field] for key, field in (
        ("runner_checkpoint_identity", "semantic_hash"),
        ("runner_checkpoint_state_hash", "state_hash"),
        ("final_parameter_hash", "parameter_hash"),
    )):
        raise BehaviorCloningError("BC provenance/checkpoint identity mismatch")
    if model is not None and "parameter_scope" in value:
        scope = value["parameter_scope"]
        for module, hashes in scope["modules"].items():
            if state_hash(model.state_dict(), module + ".") != hashes["after_hash"]:
                raise BehaviorCloningError("BC provenance/module identity mismatch")
        if sorted(_trainable_names(model, scope["mode"])) != scope["trainable_parameter_names"]:
            raise BehaviorCloningError("BC provenance/trainable names mismatch")


def _batch(samples: Sequence[BehaviorCloningSample], *, device="cpu"):
    if not samples:
        raise BehaviorCloningError("BC batch must not be empty")
    observations = torch.tensor(
        tuple(sample.observation for sample in samples), dtype=torch.float32, device=device
    )
    masks = torch.tensor(
        tuple(sample.action_mask for sample in samples), dtype=torch.bool, device=device
    )
    actions = torch.tensor(
        tuple(sample.action_index for sample in samples), dtype=torch.long, device=device
    )
    if masks.shape[0] != observations.shape[0] or actions.shape != (len(samples),):
        raise BehaviorCloningError("BC batch shape mismatch")
    return observations, masks, actions


def _optimizer(model, config: BehaviorCloningConfig):
    names = set(_trainable_names(model, config.trainable_parameters))
    parameters = [value for name, value in model.named_parameters() if name in names]
    return torch.optim.Adam(
        parameters,
        lr=config.learning_rate,
        betas=config.betas,
        eps=config.epsilon,
        weight_decay=config.weight_decay,
    )


def _gradient_norm(model) -> float:
    total = 0.0
    found = False
    for parameter in list(model.backbone.parameters()) + list(model.policy_head.parameters()):
        if parameter.grad is None:
            continue
        if not bool(torch.isfinite(parameter.grad).all()):
            raise BehaviorCloningError("BC policy gradient is not finite")
        total += float(torch.square(parameter.grad).sum())
        found = True
    if not found:
        raise BehaviorCloningError("BC policy gradient is missing")
    return math.sqrt(total)


def _require_finite_parameters(model) -> None:
    if any(not bool(torch.isfinite(value).all()) for value in model.parameters()):
        raise BehaviorCloningError("BC produced non-finite parameters")


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise BehaviorCloningError(f"{name} must be non-empty text")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise BehaviorCloningError(f"{name} must be a non-negative integer")
    return value


def _positive_int(value: object, name: str) -> int:
    result = _nonnegative_int(value, name)
    if result == 0:
        raise BehaviorCloningError(f"{name} must be positive")
    return result


def _positive_finite(value: object, name: str) -> float:
    result = _nonnegative_finite(value, name)
    if result <= 0.0:
        raise BehaviorCloningError(f"{name} must be positive")
    return result


def _nonnegative_finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BehaviorCloningError(f"{name} must be numerical")
    result = float(value)
    if not math.isfinite(result) or result < 0.0:
        raise BehaviorCloningError(f"{name} must be finite and non-negative")
    return result


def _unit_interval(value: object, name: str) -> float:
    result = _nonnegative_finite(value, name)
    if result >= 1.0:
        raise BehaviorCloningError(f"{name} must be below one")
    return result


__all__ = [
    "BC_CONFIG_SCHEMA_VERSION", "BC_TRAINING_METHOD", "BehaviorCloningConfig",
    "BehaviorCloningError", "BehaviorCloningFit", "BehaviorCloningSample",
    "activation_diagnostics", "deduplicate_behavior_cloning_samples",
    "evaluate_behavior_cloning", "fit_behavior_cloning",
    "load_behavior_cloning_config", "masked_cross_entropy",
    "model_parameter_hash", "single_sample_update_probe",
    "validate_behavior_cloning_provenance", "validate_bc_checkpoint", "state_hash",
]
