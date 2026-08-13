"""Detached pure-Python records for the minimum Native training boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

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
