from __future__ import annotations

from copy import deepcopy

import pytest

from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.core.world import Stage
from wargame_native_engine.io.scenario import (
    load_scenario,
    scenario_from_dict,
)
from wargame_native_engine.session import (
    DeploymentError,
    DeploymentPlacement,
    DeploymentPlan,
    DeploymentSession,
)
from wargame_native_engine.session.game_session import GameSession


def _scenario():
    document = load_scenario("examples/minimal_scenario.json").to_dict()
    document["metadata"]["native_deployment"] = {
        "allowed_positions": {
            "red": {"101": [[1, 3], [2, 2]]},
            "blue": {"201": [[6, 3], [4, 2], [2, 2]]},
        },
    }
    return scenario_from_dict(document)


def _session():
    return DeploymentSession(
        _scenario(),
        session_id="deployment-1",
        red_controller_id="red-controller",
        blue_controller_id="blue-controller",
    )


def _plan(side: str, operator_id: int, position) -> DeploymentPlan:
    return DeploymentPlan(
        side,
        (DeploymentPlacement(operator_id, position),),
    )


def test_side_views_hide_opponent_positions_and_finalization_is_immutable() -> None:
    session = _session()
    red_plan = _plan("red", 101, (2, 2))
    red = session.submit("red-controller", red_plan)
    blue_before = session.view("blue-controller")

    assert red.own_plan == red_plan
    assert blue_before.own_plan is None
    assert blue_before.opponent_ready is True
    assert not hasattr(blue_before, "opponent_plan")
    session.submit("blue-controller", _plan("blue", 201, (4, 2)))
    finalized = session.finalized

    assert session.view("red-controller").state == "Running"
    assert finalized.source_scenario_hash != finalized.final_scenario_hash
    assert finalized.scenario.scenario_hash == finalized.final_scenario_hash
    assert finalized.scenario.deployment_positions["red"][101] == (2.0, 2.0)
    assert finalized.scenario.deployment_positions["blue"][201] == (4.0, 2.0)
    assert all(item.deployed for item in finalized.scenario.operators)
    changed = finalized.scenario.to_dict()
    changed["operators"][0]["x"] = 999
    assert session.finalized.scenario.operators[0].x == 2.0
    with pytest.raises(DeploymentError, match="deployment_finalized"):
        session.submit("red-controller", red_plan)


def test_same_plans_in_different_input_order_have_same_bytes_and_hashes() -> None:
    first = _session()
    first.submit("red-controller", _plan("red", 101, (2, 2)))
    first.submit("blue-controller", _plan("blue", 201, (4, 2)))
    second = _session()
    second.submit("blue-controller", _plan("blue", 201, [4, 2]))
    second.submit("red-controller", _plan("red", 101, [2, 2]))

    assert first.finalized.scenario_bytes == second.finalized.scenario_bytes
    assert first.finalized.deployment_hash == second.finalized.deployment_hash
    assert first.finalized.final_scenario_hash == second.finalized.final_scenario_hash
    assert first.finalized.scenario_bytes.endswith(b"\n")


def test_plan_rejects_wrong_side_units_duplicates_and_unapproved_positions() -> None:
    with pytest.raises(DeploymentError, match="deployment_side_mismatch"):
        _session().submit(
            "red-controller", _plan("blue", 201, (6, 3)),
        )
    with pytest.raises(DeploymentError, match="deployment_unit_set_mismatch"):
        _session().submit(
            "red-controller", DeploymentPlan("red", ()),
        )
    duplicate = DeploymentPlan("red", (
        DeploymentPlacement(101, (1, 3)),
        DeploymentPlacement(101, (2, 2)),
    ))
    with pytest.raises(DeploymentError, match="duplicate_deployment_unit"):
        _session().submit("red-controller", duplicate)
    with pytest.raises(DeploymentError, match="deployment_position_not_allowed"):
        _session().submit(
            "red-controller", _plan("red", 101, (9, 5)),
        )
    with pytest.raises(DeploymentError, match="controller_not_bound"):
        _session().view("intruder")


def test_cross_side_position_conflict_is_transactional() -> None:
    session = _session()
    session.submit("red-controller", _plan("red", 101, (2, 2)))
    with pytest.raises(DeploymentError, match="deployment_position_conflict"):
        session.submit("blue-controller", _plan("blue", 201, (2, 2)))

    blue = session.view("blue-controller")
    assert blue.own_plan is None
    assert blue.state == "Deployment"
    session.submit("blue-controller", _plan("blue", 201, (4, 2)))
    assert session.finalized.final_scenario_hash


def test_input_scenario_and_plan_are_detached() -> None:
    source = _scenario()
    before = deepcopy(source.to_dict())
    plan = _plan("red", 101, (2, 2))
    session = DeploymentSession(
        source,
        session_id="deployment-2",
        red_controller_id="red-controller",
        blue_controller_id="blue-controller",
    )
    session.submit("red-controller", plan)

    assert source.to_dict() == before
    assert session.view("red-controller").own_plan == plan


def test_finalized_scenario_enters_normal_game_session_in_combat() -> None:
    deployment = _session()
    deployment.submit("red-controller", _plan("red", 101, (2, 2)))
    deployment.submit("blue-controller", _plan("blue", 201, (4, 2)))
    game = GameSession(NativeEngineBackend(deployment.finalized.scenario))
    world = game.reset(seed=42)

    assert world.stage is Stage.COMBAT
    assert world.operators[101].position == (2.0, 2.0)
    assert world.operators[201].position == (4.0, 2.0)
    assert all(item.deployed for item in world.operators.values())
