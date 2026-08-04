from __future__ import annotations

from wargame_native_engine.core.operator import OperatorState


def test_operator_state_preserves_raw_attributes() -> None:
    raw = {"legacy_blood": "40", "unresolved_flag": {"value": 7}}
    operator = OperatorState(
        1, "red", "ground", None, 0, 0, 0, 0, 40, 40,
        0, 1, 1, 5, "ready", True, [10], raw,
        attack_range=2, attack_damage=5,
    )
    raw["legacy_blood"] = "changed"
    assert operator.raw_attributes["legacy_blood"] == "40"
    assert OperatorState.from_dict(operator.to_dict()).raw_attributes == {
        "legacy_blood": "40", "unresolved_flag": {"value": 7}}
