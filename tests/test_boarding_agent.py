from __future__ import annotations

from pathlib import Path

import pytest

from scripts.run_demo import boarding_basic_agent_validator
from tests.test_realtime_dual_side_control import Fixture
from wargame_native_engine.agents import CommandStatus
from wargame_native_engine.data import ScenarioBundle


SCENARIO = Path("examples/demos/boarding_basic/scenario.json")


def test_agent_consumes_board_and_unload_from_detached_seat_view(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path, bundle=ScenarioBundle.from_path(SCENARIO))
    try:
        board = boarding_basic_agent_validator(
            fixture.reset.red,
            operation_type="BOARD",
            actor_id=101,
            target_id=102,
        )
        board_receipt = fixture.session.submit_operation(
            fixture.reset.red_binding,
            observation_version=fixture.reset.red.observation_version,
            operation=board,
        )
        current = fixture.session.advance_tick().red
        assert board_receipt.accepted is True
        assert fixture.session.command_events[-1].status is CommandStatus.APPLIED

        for _ in range(74):
            current = fixture.session.advance_tick().red
        unload = boarding_basic_agent_validator(
            current,
            operation_type="UNLOAD",
            actor_id=102,
            target_id=101,
        )
        unload_receipt = fixture.session.submit_operation(
            fixture.reset.red_binding,
            observation_version=current.observation_version,
            operation=unload,
        )
        current = fixture.session.advance_tick().red
        assert unload_receipt.accepted is True
        assert fixture.session.command_events[-1].status is CommandStatus.APPLIED

        for _ in range(74):
            current = fixture.session.advance_tick().red
        passenger = next(
            value for value in current.observation["friendly_operators"]
            if value["operator_id"] == 101
        )
        assert passenger["state"] == "ACTIVE"
        assert passenger.get("mounted_on") is None
        assert any(
            value.get("action", {}).get("actor_id") == 101
            for value in current.capabilities.frozen_actions
        )
    finally:
        fixture.close()


def test_agent_validator_fails_closed_for_forged_target(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, bundle=ScenarioBundle.from_path(SCENARIO))
    try:
        with pytest.raises(RuntimeError, match="Engine-issued BOARD"):
            boarding_basic_agent_validator(
                fixture.reset.red,
                operation_type="BOARD",
                actor_id=101,
                target_id=201,
            )
        assert fixture.session.command_events == ()
    finally:
        fixture.close()


def test_agent_validator_has_no_authoritative_state_dependency() -> None:
    source = Path("scripts/run_demo.py").read_text(encoding="utf-8")
    start = source.index("def boarding_basic_agent_validator(")
    body = source[start:source.index("\n\ndef ", start + 10)]

    assert "frozen_actions" in body
    assert "NativeEngine" not in body
    assert "WorldState" not in body
    assert "._world" not in body
