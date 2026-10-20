"""Versioned, perspective-safe objective-control training task contract.

The evaluator consumes detached Decision projections produced by the Engine.  It
does not decide capture legality, inspect ``WorldState``, or infer success from
an accepted action: success is only an observed ownership change for the exact
configured objective and side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Mapping, Sequence

from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.training.reward import RewardPolicyIdentity


OBJECTIVE_TASK_SCHEMA_VERSION = "native-objective-control-task/1.0"
OBJECTIVE_TASK_REWARD_SCHEMA_VERSION = "native-objective-task-reward-policy/1.0"
OBJECTIVE_TASK_RECORD_SCHEMA_VERSION = "native-objective-task-transition/1.0"


class ObjectiveTaskContractError(ValueError):
    """Task configuration or detached transition evidence is inconsistent."""


@dataclass(frozen=True, slots=True)
class ObjectiveTaskRewardPolicy:
    schema_version: str
    policy_id: str
    policy_version: str
    mode: str
    first_success_reward: float
    otherwise_reward: float
    repeat_semantics: str
    content_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != OBJECTIVE_TASK_REWARD_SCHEMA_VERSION:
            raise ObjectiveTaskContractError("unsupported task reward schema")
        if (self.policy_id, self.policy_version) != (
            "native-objective-first-control", "1.0.0"
        ):
            raise ObjectiveTaskContractError("unsupported task reward identity")
        if self.mode != "TASK_ONLY":
            raise ObjectiveTaskContractError("task reward mode must be TASK_ONLY")
        if (self.first_success_reward, self.otherwise_reward) != (1.0, 0.0):
            raise ObjectiveTaskContractError("unsupported task reward mapping")
        if self.repeat_semantics != "FIRST_SUCCESS_ONCE_PER_EPISODE":
            raise ObjectiveTaskContractError("unsupported repeat semantics")
        _sha256(self.content_hash, "task reward hash")

    @property
    def identity(self) -> RewardPolicyIdentity:
        return RewardPolicyIdentity(
            self.policy_id, self.policy_version, self.content_hash
        )

    def identity_record(self) -> dict[str, str]:
        return self.identity.to_dict()


@dataclass(frozen=True, slots=True)
class ObjectiveTaskConfig:
    schema_version: str
    task_id: str
    task_version: str
    controlled_side: str
    objective_id: int
    success_predicate: str
    require_initial_not_controlled: bool
    require_initial_capture_unavailable: bool
    training_horizon_decisions: int
    evaluation_horizon_decisions: int
    horizon_boundary: str
    goal_hexes: tuple[int, ...]
    scenario_id: str
    scenario_hash: str
    map_id: str
    map_hash: str
    provider_id: str
    provider_version: str
    provider_hash: str
    encoder_schema: str
    encoder_hash: str
    delta_time_seconds: int
    reward_policy: ObjectiveTaskRewardPolicy
    content_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != OBJECTIVE_TASK_SCHEMA_VERSION:
            raise ObjectiveTaskContractError("unsupported objective task schema")
        if (self.task_id, self.task_version) != (
            "public-full-action-objective-1", "1.0.0"
        ):
            raise ObjectiveTaskContractError("unsupported objective task identity")
        if self.controlled_side not in {"red", "blue"}:
            raise ObjectiveTaskContractError("task side must be RED or BLUE")
        if type(self.objective_id) is not int or self.objective_id < 0:
            raise ObjectiveTaskContractError("objective_id must be non-negative")
        if self.success_predicate != "OBJECTIVE_CONTROLLED_BY_CONFIGURED_SIDE":
            raise ObjectiveTaskContractError("unsupported success predicate")
        if self.require_initial_not_controlled is not True or (
            self.require_initial_capture_unavailable is not True
        ):
            raise ObjectiveTaskContractError("task start conditions are incomplete")
        for name in (
            "training_horizon_decisions", "evaluation_horizon_decisions",
            "delta_time_seconds",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ObjectiveTaskContractError(f"{name} must be positive")
        if self.horizon_boundary != "TIME_LIMIT_BOOTSTRAP":
            raise ObjectiveTaskContractError("unsupported horizon boundary")
        if not self.goal_hexes or tuple(sorted(set(self.goal_hexes))) != self.goal_hexes:
            raise ObjectiveTaskContractError("goal_hexes must be sorted and unique")
        for name in (
            "scenario_hash", "map_hash", "provider_hash", "encoder_hash",
            "content_hash",
        ):
            _sha256(getattr(self, name), name)

    def identity_record(self) -> dict[str, str]:
        return {
            "task_id": self.task_id,
            "task_version": self.task_version,
            "task_hash": self.content_hash,
        }

    def authority_record(self) -> dict[str, object]:
        return {
            **self.identity_record(),
            "controlled_side": self.controlled_side,
            "objective_id": self.objective_id,
            "success_predicate": self.success_predicate,
            "training_horizon_decisions": self.training_horizon_decisions,
            "evaluation_horizon_decisions": self.evaluation_horizon_decisions,
            "horizon_boundary": self.horizon_boundary,
            "scenario_id": self.scenario_id,
            "scenario_hash": self.scenario_hash,
            "map_id": self.map_id,
            "map_hash": self.map_hash,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "provider_hash": self.provider_hash,
            "encoder_schema": self.encoder_schema,
            "encoder_hash": self.encoder_hash,
            "delta_time_seconds": self.delta_time_seconds,
            "reward": self.reward_policy.identity_record(),
            "fixed_target_experiment": True,
            "target_conditioned_input": False,
        }


@dataclass(frozen=True, slots=True)
class ObjectiveTaskTransitionRecord:
    schema_version: str
    task_identity: Mapping[str, str]
    reward_policy: RewardPolicyIdentity
    joint_transition_id: str
    controlled_side: str
    objective_id: int
    controlling_side_before: str | None
    controlling_side_after: str | None
    task_success: bool
    first_success: bool
    task_reward: float
    task_terminated: bool
    task_truncated: bool
    termination_reason: str | None
    engine_terminated: bool
    engine_truncated: bool
    engine_winner: str | None
    frame_after: int
    decision_after: int
    sim_time_after_ms: int
    semantic_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != OBJECTIVE_TASK_RECORD_SCHEMA_VERSION:
            raise ObjectiveTaskContractError("unsupported task transition schema")
        if set(self.task_identity) != {"task_id", "task_version", "task_hash"}:
            raise ObjectiveTaskContractError("task transition identity is incomplete")
        _sha256(str(self.task_identity["task_hash"]), "task hash")
        if not isinstance(self.reward_policy, RewardPolicyIdentity):
            raise TypeError("reward_policy must be RewardPolicyIdentity")
        _sha256(self.joint_transition_id, "joint transition id")
        if self.controlled_side not in {"red", "blue"}:
            raise ObjectiveTaskContractError("task transition side is invalid")
        if self.task_terminated and self.task_truncated:
            raise ObjectiveTaskContractError("task boundary cannot terminate and truncate")
        if self.first_success != (self.task_success and self.task_reward == 1.0):
            raise ObjectiveTaskContractError("first-success reward mismatch")
        if self.task_reward not in {0.0, 1.0}:
            raise ObjectiveTaskContractError("task reward must be zero or one")
        if self.first_success and (
            self.controlling_side_before == self.controlled_side
            or self.controlling_side_after != self.controlled_side
            or not self.task_terminated
            or self.termination_reason != "TASK_SUCCESS"
        ):
            raise ObjectiveTaskContractError("success lacks objective-control evidence")
        if self.task_truncated and self.termination_reason != "TASK_HORIZON":
            raise ObjectiveTaskContractError("task truncation reason mismatch")
        object.__setattr__(self, "task_identity", dict(self.task_identity))
        object.__setattr__(self, "semantic_hash", canonical_hash(self._payload()))

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "task_identity": dict(self.task_identity),
            "reward_policy": self.reward_policy.to_dict(),
            "joint_transition_id": self.joint_transition_id,
            "controlled_side": self.controlled_side,
            "objective_id": self.objective_id,
            "controlling_side_before": self.controlling_side_before,
            "controlling_side_after": self.controlling_side_after,
            "task_success": self.task_success,
            "first_success": self.first_success,
            "task_reward": self.task_reward,
            "task_terminated": self.task_terminated,
            "task_truncated": self.task_truncated,
            "termination_reason": self.termination_reason,
            "engine_terminated": self.engine_terminated,
            "engine_truncated": self.engine_truncated,
            "engine_winner": self.engine_winner,
            "frame_after": self.frame_after,
            "decision_after": self.decision_after,
            "sim_time_after_ms": self.sim_time_after_ms,
        }

    def to_dict(self) -> dict[str, object]:
        value = self._payload()
        value["semantic_hash"] = self.semantic_hash
        return value


@dataclass(frozen=True, slots=True)
class ObjectiveTaskSequenceVerification:
    record_count: int
    expected_sequence_hash: str
    rederived_sequence_hash: str
    matches: bool


class ObjectiveTaskEvaluator:
    """Stateful once-per-episode scorer over detached Engine projections."""

    def __init__(self, config: ObjectiveTaskConfig) -> None:
        if not isinstance(config, ObjectiveTaskConfig):
            raise TypeError("config must be ObjectiveTaskConfig")
        self.config = config
        self._started = False
        self._succeeded = False
        self._records: list[ObjectiveTaskTransitionRecord] = []

    @property
    def records(self) -> tuple[ObjectiveTaskTransitionRecord, ...]:
        return tuple(self._records)

    def reset(self, view: object) -> None:
        objective = _objective(view, self.config.objective_id)
        if _side(view) != self.config.controlled_side:
            raise ObjectiveTaskContractError("task/view controlled side mismatch")
        if objective.controlling_side == self.config.controlled_side:
            raise ObjectiveTaskContractError("task_start_objective_already_controlled")
        catalog = getattr(view, "action_catalog", None)
        if catalog is None:
            raise ObjectiveTaskContractError("task start lacks action catalog")
        if any(
            entry.action_type == "CAPTURE"
            and entry.target_reference == self.config.objective_id
            for entry in catalog.entries
        ):
            raise ObjectiveTaskContractError(
                "task_start_capture_available_without_required_movement"
            )
        self._started = True
        self._succeeded = False
        self._records.clear()

    def success_observed(self, before: object, after: object) -> bool:
        """Return the authoritative configured ownership-change predicate."""

        return (
            not self._succeeded
            and _objective(before, self.config.objective_id).controlling_side
            != self.config.controlled_side
            and _objective(after, self.config.objective_id).controlling_side
            == self.config.controlled_side
        )

    def evaluate(
        self,
        *,
        before: object,
        after: object,
        joint_transition_id: str,
        engine_terminated: bool,
        engine_truncated: bool,
        engine_winner: str | None,
        horizon_reached: bool,
    ) -> ObjectiveTaskTransitionRecord:
        if not self._started:
            raise ObjectiveTaskContractError("task evaluator reset required")
        before_control = _objective(
            before, self.config.objective_id
        ).controlling_side
        after_control = _objective(
            after, self.config.objective_id
        ).controlling_side
        first_success = self.success_observed(before, after)
        if first_success:
            self._succeeded = True
        # Explicit precedence: an authoritative success on the horizon step is
        # a task terminal, never a horizon truncation.
        task_terminated = self._succeeded or engine_terminated
        task_truncated = not task_terminated and (engine_truncated or horizon_reached)
        reason = (
            "TASK_SUCCESS" if self._succeeded
            else "ENGINE_TERMINATED" if engine_terminated
            else "TASK_HORIZON" if task_truncated
            else None
        )
        decision = getattr(after, "decision_semantic", None)
        if decision is None:
            raise ObjectiveTaskContractError("task after projection is missing")
        snapshot = decision.snapshot
        record = ObjectiveTaskTransitionRecord(
            schema_version=OBJECTIVE_TASK_RECORD_SCHEMA_VERSION,
            task_identity=self.config.identity_record(),
            reward_policy=self.config.reward_policy.identity,
            joint_transition_id=joint_transition_id,
            controlled_side=self.config.controlled_side,
            objective_id=self.config.objective_id,
            controlling_side_before=before_control,
            controlling_side_after=after_control,
            task_success=self._succeeded,
            first_success=first_success,
            task_reward=(
                self.config.reward_policy.first_success_reward
                if first_success else self.config.reward_policy.otherwise_reward
            ),
            task_terminated=task_terminated,
            task_truncated=task_truncated,
            termination_reason=reason,
            engine_terminated=engine_terminated,
            engine_truncated=engine_truncated,
            engine_winner=engine_winner,
            frame_after=snapshot.frame_index,
            decision_after=snapshot.decision_window_index,
            sim_time_after_ms=snapshot.sim_time_ms,
        )
        self._records.append(record)
        return record

    def verify(self) -> ObjectiveTaskSequenceVerification:
        rederived: list[ObjectiveTaskTransitionRecord] = []
        succeeded = False
        for stored in self._records:
            first = (
                not succeeded
                and stored.controlling_side_before != stored.controlled_side
                and stored.controlling_side_after == stored.controlled_side
            )
            succeeded = succeeded or first
            terminated = succeeded or stored.engine_terminated
            truncated = stored.task_truncated and not terminated
            reason = (
                "TASK_SUCCESS" if succeeded
                else "ENGINE_TERMINATED" if stored.engine_terminated
                else "TASK_HORIZON" if truncated
                else None
            )
            rederived.append(ObjectiveTaskTransitionRecord(
                schema_version=stored.schema_version,
                task_identity=stored.task_identity,
                reward_policy=stored.reward_policy,
                joint_transition_id=stored.joint_transition_id,
                controlled_side=stored.controlled_side,
                objective_id=stored.objective_id,
                controlling_side_before=stored.controlling_side_before,
                controlling_side_after=stored.controlling_side_after,
                task_success=succeeded,
                first_success=first,
                task_reward=1.0 if first else 0.0,
                task_terminated=terminated,
                task_truncated=truncated,
                termination_reason=reason,
                engine_terminated=stored.engine_terminated,
                engine_truncated=stored.engine_truncated,
                engine_winner=stored.engine_winner,
                frame_after=stored.frame_after,
                decision_after=stored.decision_after,
                sim_time_after_ms=stored.sim_time_after_ms,
            ))
        expected = canonical_hash([item.to_dict() for item in self._records])
        actual = canonical_hash([item.to_dict() for item in rederived])
        return ObjectiveTaskSequenceVerification(
            len(rederived), expected, actual,
            tuple(rederived) == tuple(self._records) and expected == actual,
        )


def load_objective_task_reward_policy(
    path: str | Path,
) -> ObjectiveTaskRewardPolicy:
    source = Path(path)
    value = _mapping(json.loads(source.read_text(encoding="utf-8")), "reward")
    expected = {
        "schema_version", "policy_id", "policy_version", "mode", "mapping",
        "repeat_semantics",
    }
    if set(value) != expected:
        raise ObjectiveTaskContractError("task reward fields are missing or unknown")
    mapping = _mapping(value["mapping"], "mapping")
    content_hash = canonical_hash(value)
    return ObjectiveTaskRewardPolicy(
        str(value["schema_version"]), str(value["policy_id"]),
        str(value["policy_version"]), str(value["mode"]),
        float(mapping["first_success"]), float(mapping["otherwise"]),
        str(value["repeat_semantics"]), content_hash,
    )


def load_objective_task_config(
    path: str | Path,
    *,
    reward_policy_path: str | Path,
) -> ObjectiveTaskConfig:
    value = _mapping(
        json.loads(Path(path).read_text(encoding="utf-8")), "task"
    )
    expected = {
        "schema_version", "task_id", "task_version", "controlled_side",
        "objective_ref", "success_predicate", "start_conditions", "horizons",
        "goal_hexes", "environment", "reward_ref",
    }
    if set(value) != expected:
        raise ObjectiveTaskContractError("task fields are missing or unknown")
    objective = _mapping(value["objective_ref"], "objective_ref")
    start = _mapping(value["start_conditions"], "start_conditions")
    horizons = _mapping(value["horizons"], "horizons")
    environment = _mapping(value["environment"], "environment")
    scenario = _mapping(environment["scenario"], "scenario")
    native_map = _mapping(environment["map"], "map")
    provider = _mapping(environment["spatial_candidate_provider"], "provider")
    encoder = _mapping(environment["encoder"], "encoder")
    reward_ref = _mapping(value["reward_ref"], "reward_ref")
    reward = load_objective_task_reward_policy(reward_policy_path)
    if dict(reward_ref) != reward.identity_record():
        raise ObjectiveTaskContractError("task reward reference mismatch")
    content_hash = canonical_hash(value)
    return ObjectiveTaskConfig(
        str(value["schema_version"]), str(value["task_id"]),
        str(value["task_version"]), str(value["controlled_side"]).lower(),
        _exact_int(objective["objective_id"], "objective_id"),
        str(value["success_predicate"]),
        _exact_bool(start["objective_not_controlled_by_side"]),
        _exact_bool(start["capture_action_unavailable"]),
        _positive(horizons["training_decisions"], "training horizon"),
        _positive(horizons["evaluation_decisions"], "evaluation horizon"),
        str(horizons["boundary"]),
        tuple(_exact_int(item, "goal hex") for item in value["goal_hexes"]),
        str(scenario["id"]), str(scenario["hash"]),
        str(native_map["id"]), str(native_map["hash"]),
        str(provider["id"]), str(provider["version"]), str(provider["hash"]),
        str(encoder["schema"]), str(encoder["hash"]),
        _positive(environment["delta_time_seconds"], "delta time"),
        reward, content_hash,
    )


def write_task_records_jsonl(
    path: str | Path, records: Sequence[ObjectiveTaskTransitionRecord],
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "".join(
            json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n"
            for item in records
        ),
        encoding="utf-8",
    )
    return target


def _objective(view: object, objective_id: int):
    decision = getattr(view, "decision_semantic", None)
    if decision is None:
        raise ObjectiveTaskContractError("task view lacks Decision projection")
    matches = tuple(
        item for item in decision.side_context.objectives
        if item.objective_id == objective_id
    )
    if len(matches) != 1:
        raise ObjectiveTaskContractError("configured objective is missing or duplicate")
    return matches[0]


def _side(view: object) -> str:
    return str(getattr(view, "perspective", "")).lower()


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ObjectiveTaskContractError(f"{name} must be an object")
    return value


def _exact_bool(value: object) -> bool:
    if type(value) is not bool:
        raise TypeError("task boolean must be exact bool")
    return value


def _exact_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an exact int")
    return value


def _positive(value: object, name: str) -> int:
    result = _exact_int(value, name)
    if result <= 0:
        raise ObjectiveTaskContractError(f"{name} must be positive")
    return result


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ObjectiveTaskContractError(f"{name} must be sha256")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ObjectiveTaskContractError(f"{name} must be sha256") from exc
    return value


__all__ = [
    "OBJECTIVE_TASK_RECORD_SCHEMA_VERSION",
    "OBJECTIVE_TASK_REWARD_SCHEMA_VERSION",
    "OBJECTIVE_TASK_SCHEMA_VERSION",
    "ObjectiveTaskConfig",
    "ObjectiveTaskContractError",
    "ObjectiveTaskEvaluator",
    "ObjectiveTaskRewardPolicy",
    "ObjectiveTaskSequenceVerification",
    "ObjectiveTaskTransitionRecord",
    "load_objective_task_config",
    "load_objective_task_reward_policy",
    "write_task_records_jsonl",
]
