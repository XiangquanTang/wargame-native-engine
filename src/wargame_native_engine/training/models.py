"""Detached pure-Python records for the minimum Native training boundary."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

from wargame_native_engine.agents.seat import (
    SeatActionCapabilities,
    SeatDecision,
)
from wargame_native_engine.io.serialization import json_clone


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

    def to_record(self) -> dict[str, object]:
        return {
            "observation": json_clone(self.observation),
            "reward": self.reward,
            "terminated": self.terminated,
            "truncated": self.truncated,
            "info": json_clone(self.info),
            "action_space": self.action_space.to_record(),
        }


@dataclass(frozen=True, slots=True)
class AgentResetResult:
    episode_id: str
    observations: Mapping[str, Mapping[str, Any]]
    capabilities: Mapping[str, SeatActionCapabilities]
    decision_index: int
    fact_frame_index: int
    sim_time_ms: int
    seed: int
    scenario_id: str | None
    scenario_hash: str | None
    map_id: str | None

    def __post_init__(self) -> None:
        _episode(self.episode_id)
        _exact_nonnegative(self.decision_index, "decision_index")
        _exact_nonnegative(self.fact_frame_index, "fact_frame_index")
        _exact_nonnegative(self.sim_time_ms, "sim_time_ms")
        if type(self.seed) is not int:
            raise TypeError("seed must be an exact int")
        object.__setattr__(self, "observations", json_clone(self.observations))
        object.__setattr__(self, "capabilities", _capabilities(self.capabilities))

    def to_record(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "observations": json_clone(self.observations),
            "capabilities": {
                side: value.to_record()
                for side, value in sorted(self.capabilities.items())
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
    decision_index: int
    side: str
    observation: Mapping[str, Any]
    capabilities: SeatActionCapabilities
    decision: SeatDecision
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

    def __post_init__(self) -> None:
        _episode(self.episode_id)
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

    def to_record(self) -> dict[str, object]:
        return {
            "transition_schema": "native-decision-transition/1.0",
            "episode_id": self.episode_id,
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
        }


@dataclass(frozen=True, slots=True)
class AgentStepResult:
    episode_id: str
    observations: Mapping[str, Mapping[str, Any]]
    capabilities: Mapping[str, SeatActionCapabilities]
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

    def __post_init__(self) -> None:
        _episode(self.episode_id)
        if type(self.terminated) is not bool or type(self.truncated) is not bool:
            raise TypeError("terminated and truncated must be exact bool values")
        if self.terminated and self.truncated:
            raise ValueError("a step cannot be terminated and truncated")
        _exact_nonnegative(self.decision_index, "decision_index")
        _exact_nonnegative(self.next_decision_index, "next_decision_index")
        object.__setattr__(self, "observations", json_clone(self.observations))
        object.__setattr__(self, "capabilities", _capabilities(self.capabilities))
        object.__setattr__(
            self,
            "rewards",
            {side: float(value) for side, value in self.rewards.items()},
        )
        object.__setattr__(self, "transitions", deepcopy(tuple(self.transitions)))
        object.__setattr__(self, "info", json_clone(self.info))

    def to_record(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "observations": json_clone(self.observations),
            "capabilities": {
                side: value.to_record()
                for side, value in sorted(self.capabilities.items())
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
        }


@dataclass(frozen=True, slots=True)
class SingleSeatResetResult:
    observation: Mapping[str, Any]
    capabilities: SeatActionCapabilities
    info: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SingleSeatStepResult:
    observation: Mapping[str, Any]
    capabilities: SeatActionCapabilities
    reward: float
    terminated: bool
    truncated: bool
    info: Mapping[str, Any]
    transition: DecisionTransition


def _episode(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("episode_id must be non-empty")
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
