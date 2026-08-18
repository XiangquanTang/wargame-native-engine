from __future__ import annotations

from wargame_native_engine.agents import (
    AgentDecisionContext,
    AgentResetContext,
    RandomProductSeatAgent,
    SeatActionCapabilities,
    SeatDecision,
)


def _context() -> AgentDecisionContext:
    actions = (
        {
            "action_id": "move-1",
            "priority": 1,
            "source": "test",
            "action": {
                "side": "red",
                "actor_id": 1,
                "action_type": "MOVE",
                "target_id": None,
                "target_position": None,
                "parameters": {
                    "path": [101, 102],
                    "movement_mode": "NORMAL",
                },
            },
        },
        {
            "action_id": "shoot-2-9",
            "priority": 0,
            "source": "test",
            "action": {
                "side": "red",
                "actor_id": 2,
                "action_type": "SHOOT",
                "target_id": 9,
                "target_position": None,
                "parameters": {"weapon_id": 7},
            },
        },
        {
            "action_id": "wait-1",
            "priority": 99,
            "source": "test",
            "action": {
                "side": "red",
                "actor_id": 1,
                "action_type": "WAIT",
                "target_id": None,
                "target_position": None,
                "parameters": {},
            },
        },
    )
    capabilities = SeatActionCapabilities(
        "red",
        0,
        (1, 2),
        ("MOVE", "SHOOT", "WAIT"),
        actions,
        {
            "operations": {},
            "selected_count": 2,
            "selected_units": [],
            "shoot_target_ids": [9],
        },
    )
    return AgentDecisionContext(
        "red",
        0,
        0,
        0,
        {
            "side": "red",
            "friendly_operators": [],
            "visible_enemies": [],
            "legal_action_ids": ["move-1", "shoot-2-9", "wait-1"],
        },
        capabilities,
    )


def _reset(agent: RandomProductSeatAgent) -> None:
    agent.reset(AgentResetContext(
        "session-1", "scenario-1", "red", 42, "rules-v1", agent.agent_id,
    ))


def test_random_product_seat_agent_is_seeded_and_returns_product_operations() -> None:
    first = RandomProductSeatAgent("random-red", seed=17)
    second = RandomProductSeatAgent("random-red-copy", seed=17)
    _reset(first)
    _reset(second)

    first_values = tuple(first.act(context=_context()) for _ in range(8))
    second_values = tuple(second.act(context=_context()) for _ in range(8))
    assert first_values == second_values
    assert {value.operations[0].operation_type for value in first_values} == {
        "MOVE", "SHOOT",
    }
    for decision in first_values:
        assert isinstance(decision, SeatDecision)
        record = decision.to_record()
        assert "action_id" not in str(record)
        assert "path" not in str(record)


def test_random_product_seat_agent_rebuilds_move_and_shoot_parameters() -> None:
    agent = RandomProductSeatAgent(seed=1)
    _reset(agent)
    decisions = tuple(agent.act(context=_context()) for _ in range(20))
    operations = [value.operations[0] for value in decisions]
    move = next(value for value in operations if value.operation_type == "MOVE")
    shoot = next(value for value in operations if value.operation_type == "SHOOT")
    assert move.actor_ids == (1,)
    assert move.destination_hex == 102
    assert shoot.actor_ids == (2,)
    assert shoot.target_id == 9
    assert shoot.weapon_choices == ((2, 7),)


def test_random_product_seat_agent_does_not_cross_the_seat_boundary() -> None:
    context = _context()
    bad = dict(context.operation_capabilities.frozen_actions[0])
    bad["action"] = dict(bad["action"], side="blue")
    capabilities = SeatActionCapabilities(
        "red", 0, (1,), ("MOVE",), (bad,), {
            "operations": {}, "selected_count": 1,
            "selected_units": [], "shoot_target_ids": [],
        },
    )
    value = AgentDecisionContext(
        "red", 0, 0, 0,
        {"legal_action_ids": ["move-1"]}, capabilities,
    )
    agent = RandomProductSeatAgent(seed=1)
    _reset(agent)
    assert agent.act(context=value) == SeatDecision()
