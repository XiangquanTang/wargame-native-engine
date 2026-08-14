"""Immutable authorization snapshots for untrusted Agent selections."""

from __future__ import annotations

from copy import deepcopy
from types import MappingProxyType
from typing import Mapping, Sequence

from wargame_native_engine.agents.protocol import AgentAction
from wargame_native_engine.core.action import EngineAction, ValidAction


def freeze_valid_actions(
    side: str,
    values: Sequence[ValidAction],
) -> Mapping[str, EngineAction]:
    """Return a detached action-id map that callers cannot mutate.

    The map is built before any valid-action objects are exposed to an Agent.
    Resolution always returns another detached copy from this snapshot; Agent
    supplied ``EngineAction`` content is never treated as authorization.
    """

    frozen: dict[str, EngineAction] = {}
    for value in values:
        action = deepcopy(value.action)
        if action.side != side:
            raise ValueError(f"valid_action_side_mismatch:{side}")
        action_id = action.action_id
        if action_id in frozen:
            raise ValueError(f"duplicate_valid_action_id:{action_id}")
        frozen[action_id] = action
    return MappingProxyType(frozen)


def resolve_agent_selection(
    side: str,
    selected: AgentAction | None,
    frozen: Mapping[str, EngineAction],
) -> EngineAction | None:
    """Resolve only the selected ID against a pre-exposure snapshot."""

    if selected is None:
        return None
    if not isinstance(selected, AgentAction):
        raise TypeError("Agent.act must return AgentAction or None")
    action_id = selected.valid_action_id
    if not isinstance(action_id, str) or not action_id:
        raise ValueError("agent valid-action identity mismatch")
    if selected.action.side != side:
        raise ValueError(f"agent_action_side_mismatch:{side}")
    action = frozen.get(action_id)
    if action is None:
        if selected.action.action_id != action_id:
            raise ValueError("agent valid-action identity mismatch")
        raise ValueError(f"agent_action_not_valid:{side}")
    if action.side != side:
        raise ValueError(f"agent_action_side_mismatch:{side}")
    return deepcopy(action)
