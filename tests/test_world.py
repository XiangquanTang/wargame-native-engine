from __future__ import annotations

from wargame_native_engine.core.concealment import ConcealmentPosture
from wargame_native_engine.core.operator import (
    MovementTaskPauseReason,
    OperatorState,
)


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


def test_off_map_operator_roundtrip_preserves_nondefault_typed_state() -> None:
    operator = OperatorState(
        1, "red", "PERSONNEL", "fixture", 0, 0, 0, 0, 40, 40,
        0, 36, 1, 5, "ON_BOARD", True, [10], {"concealed": True},
        deployed=False,
        basic_speed=24,
        current_hex=None,
        cur_pos_millionths=250_000,
        move_path=[101, 102],
        movement_mode="MARCHING",
        stopped=False,
        move_to_stop_remaining_ms=2_000,
        fatigue_level=2,
        fatigue_recovery_elapsed_ms=3_000,
        movement_action_id="move-1",
        movement_action_sequence=4,
        movement_stop_requested=True,
        movement_blocked_reason="FIXTURE_BLOCKER",
        movement_task_pause_reason=MovementTaskPauseReason.SUPPRESSION,
        segment_from_hex=101,
        segment_to_hex=102,
        segment_elapsed_ms=1_000,
        segment_traversal_ms=4_000,
        segment_event_id="segment-1",
        transition_kind="FIXTURE_TRANSITION",
        transition_remaining_ms=5_000,
        weapon_locked=True,
        weapon_unfolded=False,
        weapon_cooldown_remaining_ms=6_000,
        suppression_remaining_ms=7_000,
        guided_fire_recovery_remaining_ms=8_000,
        indirect_fire_planned_cooldown_remaining_ms=9_000,
        mounted_on=2,
        concealment_posture=ConcealmentPosture.CONCEALED,
        lifecycle_profile_id="FINITE_FIXTURE_V1",
        lifecycle_remaining_ms=10_000,
    )

    serialized = operator.to_dict()
    restored = OperatorState.from_dict(serialized)

    assert "current_hex" not in serialized
    for field_name in (
        "basic_speed",
        "cur_pos_millionths",
        "move_path",
        "movement_mode",
        "stopped",
        "move_to_stop_remaining_ms",
        "fatigue_level",
        "fatigue_recovery_elapsed_ms",
        "movement_action_id",
        "movement_action_sequence",
        "movement_stop_requested",
        "movement_blocked_reason",
        "movement_task_pause_reason",
        "segment_from_hex",
        "segment_to_hex",
        "segment_elapsed_ms",
        "segment_traversal_ms",
        "segment_event_id",
        "transition_kind",
        "transition_remaining_ms",
        "weapon_locked",
        "weapon_unfolded",
        "weapon_cooldown_remaining_ms",
        "suppression_remaining_ms",
        "guided_fire_recovery_remaining_ms",
        "indirect_fire_planned_cooldown_remaining_ms",
        "concealment_posture",
        "lifecycle_profile_id",
        "lifecycle_remaining_ms",
    ):
        assert getattr(restored, field_name) == getattr(operator, field_name)
    assert restored.to_dict() == serialized


def test_off_map_default_runtime_fields_keep_legacy_omission_shape() -> None:
    operator = OperatorState(
        1, "red", "PERSONNEL", None, 0, 0, 0, 0, 40, 40,
        0, 36, 1, 5, "ready", True, [], {}, current_hex=None,
    )

    serialized = operator.to_dict()

    for field_name in (
        "basic_speed",
        "current_hex",
        "cur_pos_millionths",
        "move_path",
        "movement_mode",
        "stopped",
        "fatigue_level",
        "weapon_cooldown_remaining_ms",
        "suppression_remaining_ms",
        "guided_fire_recovery_remaining_ms",
        "indirect_fire_planned_cooldown_remaining_ms",
    ):
        assert field_name not in serialized
    assert OperatorState.from_dict(serialized).to_dict() == serialized
