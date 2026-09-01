"""Deterministic integer encoding of current Native valid actions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from wargame_native_engine.io.serialization import json_clone
from wargame_native_engine.training.models import EncodedActionSpace


class NativeActionEncoder:
    """Legacy valid-action compatibility wrapper.

    New training code uses ``DecisionActionEncoder`` over ActionCatalogSnapshot.
    """

    def __init__(self, max_actions: int = 256) -> None:
        if type(max_actions) is not int or max_actions <= 0:
            raise ValueError("max_actions must be a positive exact int")
        self.max_actions = max_actions

    def encode(
        self,
        valid_actions: Sequence[Mapping[str, Any]],
    ) -> EncodedActionSpace:
        if len(valid_actions) > self.max_actions:
            raise ValueError(
                f"current valid actions exceed max_actions={self.max_actions}"
            )
        values = tuple(json_clone(item) for item in valid_actions)
        action_ids = tuple(_action_id(item) for item in values)
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("current valid action ids must be unique")
        return EncodedActionSpace(
            action_ids=action_ids,
            actions=values,
            mask=(True,) * len(values)
            + (False,) * (self.max_actions - len(values)),
        )

    @staticmethod
    def resolve(space: EncodedActionSpace, action_id: int | None) -> str | None:
        if action_id is None:
            return None
        if type(action_id) is not int:
            raise TypeError("action_id must be an exact int or None")
        if action_id < 0 or action_id >= len(space.mask) or not space.mask[action_id]:
            raise ValueError(f"action_id {action_id!r} is not currently valid")
        return space.action_ids[action_id]

    @classmethod
    def resolve_batch(
        cls,
        space: EncodedActionSpace,
        action_ids: Sequence[int],
    ) -> tuple[str, ...]:
        """Resolve one ordered decision-window batch from the current mask."""

        if isinstance(action_ids, (str, bytes)) or not isinstance(
            action_ids, Sequence
        ):
            raise TypeError("action_ids must be a sequence of exact ints")
        resolved: list[str] = []
        seen: set[int] = set()
        for action_id in action_ids:
            if type(action_id) is not int:
                raise TypeError("action_ids must contain exact ints")
            if action_id in seen:
                raise ValueError(
                    f"action_id {action_id!r} occurs more than once"
                )
            seen.add(action_id)
            value = cls.resolve(space, action_id)
            if value is None:  # pragma: no cover - exact ints cannot resolve None
                raise RuntimeError("encoded action unexpectedly resolved to NoOp")
            resolved.append(value)
        return tuple(resolved)


def _action_id(value: Mapping[str, Any]) -> str:
    action_id = value.get("action_id")
    if not isinstance(action_id, str) or not action_id:
        raise ValueError("valid action record requires a non-empty action_id")
    return action_id
