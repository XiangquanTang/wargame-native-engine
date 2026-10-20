"""Detached pure-Python records for the minimum Native training boundary."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from typing import Any, Mapping

from wargame_native_engine.agents.seat import (
    SeatActionCapabilities,
    SeatDecision,
)
from wargame_native_engine.io.serialization import json_clone
from wargame_native_engine.training.decision_encoder import (
    EncodedDecisionActionSpace,
)
from wargame_native_engine.training.reward import (
    JointTrainingRewardRecord,
    TrainingRewardRecord,
)
from wargame_native_engine.training.objective_task import (
    ObjectiveTaskTransitionRecord,
)


@dataclass(frozen=True, slots=True)
class EncodedActionSpace:
    """One fixed-capacity mask over current Native ``valid_actions``."""

    action_ids: tuple[str, ...]
    actions: tuple[Mapping[str, Any], ...]
    mask: tuple[bool, ...]

    def to_record(self) -> dict[str, object]:
        return {
            "action_ids": list(self.action_ids),
            "actions": json_clone(self.actions),
            "mask": list(self.mask),
        }


@dataclass(frozen=True, slots=True)
class CatalogActionSelection:
    """Exact window-bound action identities selected by a training policy."""

    action_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        values = tuple(self.action_ids)
        if any(not isinstance(value, str) or not value for value in values):
            raise ValueError("catalog action_ids must be non-empty strings")
        if len(values) != len(set(values)):
            raise ValueError("catalog action_ids must be unique")
        object.__setattr__(self, "action_ids", values)

    def to_record(self) -> dict[str, object]:
        return {
            "selection_type": "ACTION_CATALOG_IDS",
            "action_ids": list(self.action_ids),
        }


@dataclass(frozen=True, slots=True)
class TrainingReset:
    observation: Mapping[str, Any]
    action_space: EncodedActionSpace
    info: Mapping[str, Any]

    def to_record(self) -> dict[str, object]:
        return {
            "observation": json_clone(self.observation),
            "action_space": self.action_space.to_record(),
            "info": json_clone(self.info),
        }


@dataclass(frozen=True, slots=True)
class TrainingStep:
    observation: Mapping[str, Any]
    reward: float
    terminated: bool
    truncated: bool
    info: Mapping[str, Any]
    action_space: EncodedActionSpace
    reward_record: TrainingRewardRecord
    task_reward_record: ObjectiveTaskTransitionRecord | None = None

    def __post_init__(self) -> None:
        _exact_bool(self.terminated, "terminated")
        _exact_bool(self.truncated, "truncated")
        if not isinstance(self.reward_record, TrainingRewardRecord):
            raise TypeError("reward_record must be TrainingRewardRecord")
        task = self.task_reward_record
        if task is None:
            if float(self.reward) != self.reward_record.total_reward:
                raise ValueError("reward scalar/record mismatch")
            if self.terminated != self.reward_record.terminated or (
                self.truncated != self.reward_record.truncated
            ):
                raise ValueError("reward record terminal flags mismatch")
        else:
            if not isinstance(task, ObjectiveTaskTransitionRecord):
                raise TypeError("task_reward_record has invalid type")
            if float(self.reward) != task.task_reward:
                raise ValueError("task reward scalar/record mismatch")
            if self.terminated != task.task_terminated or (
                self.truncated != task.task_truncated
            ):
                raise ValueError("task reward terminal flags mismatch")
            if (
                task.joint_transition_id
                != self.reward_record.transition_identity.joint_transition_id
            ):
                raise ValueError("base/task transition identity mismatch")

    def to_record(self) -> dict[str, object]:
        return {
            "observation": json_clone(self.observation),
            "reward": self.reward,
            "terminated": self.terminated,
            "truncated": self.truncated,
            "info": json_clone(self.info),
            "action_space": self.action_space.to_record(),
            "reward_record": self.reward_record.to_dict(),
            "task_reward_record": (
                None
                if self.task_reward_record is None
                else self.task_reward_record.to_dict()
            ),
        }


@dataclass(frozen=True, slots=True)
class AgentResetResult:
    episode_id: str
    run_namespace: str
    observations: Mapping[str, Mapping[str, Any]]
    capabilities: Mapping[str, SeatActionCapabilities]
    action_spaces: Mapping[str, EncodedDecisionActionSpace]
    decision_index: int
    fact_frame_index: int
    sim_time_ms: int
    seed: int
    scenario_id: str | None
    scenario_hash: str | None
    map_id: str | None

    def __post_init__(self) -> None:
        _episode(self.episode_id)
        _namespace(self.run_namespace)
        _exact_nonnegative(self.decision_index, "decision_index")
        _exact_nonnegative(self.fact_frame_index, "fact_frame_index")
        _exact_nonnegative(self.sim_time_ms, "sim_time_ms")
        if type(self.seed) is not int:
            raise TypeError("seed must be an exact int")
        object.__setattr__(self, "observations", json_clone(self.observations))
        object.__setattr__(self, "capabilities", _capabilities(self.capabilities))
        object.__setattr__(self, "action_spaces", _action_spaces(self.action_spaces))

    def to_record(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "run_namespace": self.run_namespace,
            "observations": json_clone(self.observations),
            "capabilities": {
                side: value.to_record()
                for side, value in sorted(self.capabilities.items())
            },
            "action_spaces": {
                side: value.to_record()
                for side, value in sorted(self.action_spaces.items())
            },
            "decision_index": self.decision_index,
            "fact_frame_index": self.fact_frame_index,
            "sim_time_ms": self.sim_time_ms,
            "seed": self.seed,
            "scenario_id": self.scenario_id,
            "scenario_hash": self.scenario_hash,
            "map_id": self.map_id,
        }


@dataclass(frozen=True, slots=True)
class DecisionTransition:
    episode_id: str
    run_namespace: str
    decision_index: int
    side: str
    observation: Mapping[str, Any]
    capabilities: SeatActionCapabilities
    decision: SeatDecision | CatalogActionSelection
    reward: float
    next_observation: Mapping[str, Any]
    next_capabilities: SeatActionCapabilities
    terminated: bool
    truncated: bool
    seed: int
    scenario_id: str | None
    scenario_hash: str | None
    map_id: str | None
    fact_frame_start: int
    fact_frame_end: int
    sim_time_start_ms: int
    sim_time_end_ms: int
    reward_record: TrainingRewardRecord

    def __post_init__(self) -> None:
        _episode(self.episode_id)
        _namespace(self.run_namespace)
        _exact_nonnegative(self.decision_index, "decision_index")
        if self.side not in {"red", "blue"}:
            raise ValueError("side must be RED or BLUE")
        if isinstance(self.reward, bool) or not isinstance(
            self.reward, (int, float)
        ):
            raise TypeError("reward must be a number")
        if type(self.terminated) is not bool or type(self.truncated) is not bool:
            raise TypeError("terminated and truncated must be exact bool values")
        if self.terminated and self.truncated:
            raise ValueError("a transition cannot be terminated and truncated")
        if type(self.seed) is not int:
            raise TypeError("seed must be an exact int")
        if not isinstance(self.decision, (SeatDecision, CatalogActionSelection)):
            raise TypeError("decision must be SeatDecision or CatalogActionSelection")
        start = _exact_nonnegative(self.fact_frame_start, "fact_frame_start")
        end = _exact_nonnegative(self.fact_frame_end, "fact_frame_end")
        if end < start:
            raise ValueError("fact frame range must be a non-empty closed interval")
        sim_start = _exact_nonnegative(
            self.sim_time_start_ms, "sim_time_start_ms"
        )
        sim_end = _exact_nonnegative(self.sim_time_end_ms, "sim_time_end_ms")
        if sim_end < sim_start:
            raise ValueError("simulation time must not move backwards")
        object.__setattr__(self, "observation", json_clone(self.observation))
        object.__setattr__(self, "capabilities", deepcopy(self.capabilities))
        object.__setattr__(self, "decision", deepcopy(self.decision))
        object.__setattr__(
            self, "next_observation", json_clone(self.next_observation)
        )
        object.__setattr__(
            self, "next_capabilities", deepcopy(self.next_capabilities)
        )
        object.__setattr__(self, "reward", float(self.reward))
        if not isinstance(self.reward_record, TrainingRewardRecord):
            raise TypeError("reward_record must be TrainingRewardRecord")
        if self.reward_record.side.value != self.side:
            raise ValueError("transition reward side mismatch")
        if math.isfinite(self.reward) and (
            self.reward_record.total_reward != self.reward
        ):
            raise ValueError("transition reward scalar/record mismatch")
        if self.reward_record.terminated != self.terminated or (
            self.reward_record.truncated != self.truncated
        ):
            raise ValueError("transition reward terminal flags mismatch")
        identity = self.reward_record.transition_identity
        if (
            identity.episode_id != self.episode_id
            or identity.run_namespace != self.run_namespace
            or identity.decision_window_index != self.decision_index
            or identity.fact_frame_start != self.fact_frame_start
            or identity.fact_frame_end != self.fact_frame_end
            or identity.sim_time_before_ms != self.sim_time_start_ms
            or identity.sim_time_after_ms != self.sim_time_end_ms
        ):
            raise ValueError("transition/reward identity mismatch")

    def to_record(self) -> dict[str, object]:
        return {
            "transition_schema": "native-decision-transition/1.0",
            "episode_id": self.episode_id,
            "run_namespace": self.run_namespace,
            "decision_index": self.decision_index,
            "side": self.side,
            "observation": json_clone(self.observation),
            "capabilities": self.capabilities.to_record(),
            "decision": self.decision.to_record(),
            "reward": self.reward,
            "next_observation": json_clone(self.next_observation),
            "next_capabilities": self.next_capabilities.to_record(),
            "terminated": self.terminated,
            "truncated": self.truncated,
            "seed": self.seed,
            "scenario_id": self.scenario_id,
            "scenario_hash": self.scenario_hash,
            "map_id": self.map_id,
            "fact_frame_start": self.fact_frame_start,
            "fact_frame_end": self.fact_frame_end,
            "fact_frame_range_contract": "closed",
            "sim_time_start_ms": self.sim_time_start_ms,
            "sim_time_end_ms": self.sim_time_end_ms,
            "reward_record": self.reward_record.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class AgentStepResult:
    episode_id: str
    run_namespace: str
    observations: Mapping[str, Mapping[str, Any]]
    capabilities: Mapping[str, SeatActionCapabilities]
    action_spaces: Mapping[str, EncodedDecisionActionSpace]
    rewards: Mapping[str, float]
    terminated: bool
    truncated: bool
    decision_index: int
    next_decision_index: int
    fact_frame_start: int
    fact_frame_end: int
    sim_time_start_ms: int
    sim_time_end_ms: int
    winner: str | None
    outcome: str | None
    transitions: tuple[DecisionTransition, ...]
    info: Mapping[str, Any]
    joint_reward_record: JointTrainingRewardRecord

    def __post_init__(self) -> None:
        _episode(self.episode_id)
        _namespace(self.run_namespace)
        if type(self.terminated) is not bool or type(self.truncated) is not bool:
            raise TypeError("terminated and truncated must be exact bool values")
        if self.terminated and self.truncated:
            raise ValueError("a step cannot be terminated and truncated")
        _exact_nonnegative(self.decision_index, "decision_index")
        _exact_nonnegative(self.next_decision_index, "next_decision_index")
        object.__setattr__(self, "observations", json_clone(self.observations))
        object.__setattr__(self, "capabilities", _capabilities(self.capabilities))
        object.__setattr__(self, "action_spaces", _action_spaces(self.action_spaces))
        object.__setattr__(
            self,
            "rewards",
            {side: float(value) for side, value in self.rewards.items()},
        )
        object.__setattr__(self, "transitions", deepcopy(tuple(self.transitions)))
        object.__setattr__(self, "info", json_clone(self.info))
        if not isinstance(self.joint_reward_record, JointTrainingRewardRecord):
            raise TypeError("joint_reward_record must be JointTrainingRewardRecord")
        expected_rewards = {
            "red": self.joint_reward_record.red.total_reward,
            "blue": self.joint_reward_record.blue.total_reward,
        }
        if dict(self.rewards) != expected_rewards:
            raise ValueError("joint reward scalar/record mismatch")

    def to_record(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "run_namespace": self.run_namespace,
            "observations": json_clone(self.observations),
            "capabilities": {
                side: value.to_record()
                for side, value in sorted(self.capabilities.items())
            },
            "action_spaces": {
                side: value.to_record()
                for side, value in sorted(self.action_spaces.items())
            },
            "rewards": dict(self.rewards),
            "terminated": self.terminated,
            "truncated": self.truncated,
            "decision_index": self.decision_index,
            "next_decision_index": self.next_decision_index,
            "fact_frame_start": self.fact_frame_start,
            "fact_frame_end": self.fact_frame_end,
            "sim_time_start_ms": self.sim_time_start_ms,
            "sim_time_end_ms": self.sim_time_end_ms,
            "winner": self.winner,
            "outcome": self.outcome,
            "transitions": [item.to_record() for item in self.transitions],
            "info": json_clone(self.info),
            "joint_reward_record": self.joint_reward_record.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class SingleSeatResetResult:
    observation: Mapping[str, Any]
    capabilities: SeatActionCapabilities
    action_space: EncodedDecisionActionSpace
    info: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SingleSeatStepResult:
    observation: Mapping[str, Any]
    capabilities: SeatActionCapabilities
    action_space: EncodedDecisionActionSpace
    reward: float
    terminated: bool
    truncated: bool
    info: Mapping[str, Any]
    transition: DecisionTransition
    reward_record: TrainingRewardRecord

    def __post_init__(self) -> None:
        _exact_bool(self.terminated, "terminated")
        _exact_bool(self.truncated, "truncated")
        if not isinstance(self.reward_record, TrainingRewardRecord):
            raise TypeError("reward_record must be TrainingRewardRecord")
        if self.transition.reward_record != self.reward_record:
            raise ValueError("single-seat transition/reward record mismatch")
        if float(self.reward) != self.reward_record.total_reward:
            raise ValueError("single-seat reward scalar/record mismatch")


def _episode(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("episode_id must be non-empty")
    return value


def _namespace(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("run_namespace must be a non-empty string")
    if value != value.strip():
        raise ValueError("run_namespace must not contain surrounding whitespace")
    return value


def _exact_bool(value: bool, field: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{field} must be an exact bool")
    return value


def _exact_nonnegative(value: int, field: str) -> int:
    if type(value) is not int or value < 0:
        raise TypeError(f"{field} must be a non-negative exact int")
    return value


def _capabilities(
    values: Mapping[str, SeatActionCapabilities],
) -> dict[str, SeatActionCapabilities]:
    if set(values) != {"red", "blue"}:
        raise ValueError("capabilities must contain exactly RED and BLUE")
    result = {side: deepcopy(value) for side, value in values.items()}
    if any(result[side].side != side for side in result):
        raise ValueError("capability side mismatch")
    return result


def _action_spaces(
    values: Mapping[str, EncodedDecisionActionSpace],
) -> dict[str, EncodedDecisionActionSpace]:
    if set(values) != {"red", "blue"}:
        raise ValueError("action_spaces must contain exactly RED and BLUE")
    result = {side: deepcopy(value) for side, value in values.items()}
    if any(result[side].side.value != side for side in result):
        raise ValueError("action space side mismatch")
    return result
