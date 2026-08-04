from __future__ import annotations

import pytest

from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.io.mapping import (
    UnsupportedActionError, action_from_compatible_payload,
)


def test_action_compatibility_mapping_is_explicit() -> None:
    action = action_from_compatible_payload(
        "red", {"type": 1, "actor": 101, "move_path": [[2.0, 3.0]]})
    assert action.action_type is ActionType.MOVE
    assert action.target_position == (2.0, 3.0)
    assert action.parameters["compatible_payload"]["type"] == 1


def test_unsupported_action_is_rejected() -> None:
    with pytest.raises(UnsupportedActionError, match="unsupported_action:11"):
        action_from_compatible_payload("red", {"type": 11, "actor": 101})


def test_engine_action_identity_is_canonical() -> None:
    left = EngineAction("red", 1, ActionType.ATTACK, target_id=2,
                        parameters={"weapon_id": 10, "mode": "direct"})
    right = EngineAction("red", 1, ActionType.ATTACK, target_id=2,
                         parameters={"mode": "direct", "weapon_id": 10})
    assert left.action_id == right.action_id
