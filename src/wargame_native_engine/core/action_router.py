from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.world import WorldState


@dataclass(frozen=True, slots=True)
class RoutedAction:
    action_sequence: int
    action: EngineAction


class ActionRouter:
    """Normalize and common-validate one source-compatible action list."""

    supported_types = frozenset({
        ActionType.MOVE, ActionType.SHOOT, ActionType.STOP_MOVE,
        ActionType.CHANGE_STATE, ActionType.WEAPON_LOCK,
        ActionType.WEAPON_UNFOLD, ActionType.WAIT,
    })

    def normalize(self, actions: Mapping[str, EngineAction | None]
                  | Sequence[EngineAction], *, side_order: Sequence[str],
                  start_sequence: int) -> tuple[RoutedAction, ...]:
        values: list[EngineAction] = []
        if isinstance(actions, Mapping):
            for side in side_order:
                item = actions.get(side)
                values.append(item if item is not None else
                              EngineAction(side, None, ActionType.WAIT))
        elif isinstance(actions, Sequence) and not isinstance(actions, (str, bytes)):
            for item in actions:
                if not isinstance(item, EngineAction):
                    raise TypeError("source-compatible actions must be EngineAction values")
                values.append(item)
        else:
            raise TypeError("actions must be a side mapping or ordered action sequence")
        return tuple(RoutedAction(start_sequence + index, item)
                     for index, item in enumerate(values))

    def common_error(self, routed: RoutedAction, world: WorldState,
                     side_order: Sequence[str]) -> str | None:
        action = routed.action
        if action.side not in side_order:
            return "UNKNOWN_SIDE"
        if action.action_type not in self.supported_types:
            return "UNSUPPORTED_ACTION_TYPE"
        if action.action_type is ActionType.WAIT and action.actor_id is None:
            return None
        if action.actor_id is None:
            return "ACTOR_REQUIRED"
        operator = world.operators.get(action.actor_id)
        if operator is None:
            return "ACTOR_NOT_FOUND"
        if operator.side != action.side:
            return "ACTOR_NOT_CONTROLLED"
        if not operator.alive:
            return "ACTOR_DESTROYED"
        if not operator.deployed:
            return "ACTOR_NOT_DEPLOYED"
        return None
