from __future__ import annotations

import json
from pathlib import Path

from wargame_native_engine.agents import (
    CommandStatus,
    ManualRealtimeClock,
    RealtimeAgentSession,
    SeatDecision,
    SeatOperation,
)
from wargame_native_engine.data import (
    DataProvenance,
    ScenarioBundle,
    ScenarioCatalog,
)
from wargame_native_engine.operations import ProductOperationService, WargameFacade


SCENARIO = Path("examples/source_movement_scenario.json")


def _paused_bundle() -> ScenarioBundle:
    record = json.loads(SCENARIO.read_text(encoding="utf-8"))
    record["scenario_id"] = "suppression-movement-interface-test"
    operator = record["operators"][0]
    operator.update({
        "operator_type": "PERSONNEL",
        "subtype": "fixture-paused-personnel",
        "max_speed": 20,
        "basic_speed": 20,
        "state": "SUPPRESSED",
        "speed": 0,
        "stopped": True,
        "move_path": [102],
        "movement_action_id": "preexisting-move",
        "movement_action_sequence": 0,
        "movement_task_pause_reason": "SUPPRESSION",
        "suppression_remaining_ms": 1,
    })
    return ScenarioBundle.from_record(
        record,
        provenance=DataProvenance(
            source_sha256="fixture-suppression-movement-interface",
            source_kind="test_fixture",
        ),
    )


def _match(tmp_path: Path):
    bundle = _paused_bundle()
    app = WargameFacade(
        ScenarioCatalog.from_bundles({
            bundle.scenario_identity.scenario_id: bundle,
        }),
        storage_root=tmp_path / "managed",
    )
    return app.create_match(
        scenario_id=bundle.scenario_identity.scenario_id,
        red_controller_id="suppression-red",
        blue_controller_id="suppression-blue",
        seed=3,
        decision_interval_ms=112_000,
        frame_interval_ms=112_000,
    )


def _whole_side_wait(
    service: ProductOperationService,
    *,
    request_id: str,
) -> None:
    preview = service.preview_operation(
        [],
        "WAIT",
        request_id=request_id + "-preview",
        whole_side_wait=True,
    )
    service.execute_preview(
        preview.preview_token,
        request_id=request_id + "-execute",
    )


def test_human_product_path_gets_new_move_after_automatic_resume(
    tmp_path: Path,
) -> None:
    match = _match(tmp_path)
    try:
        red = ProductOperationService(match.bind_controller(
            "red",
            controller_id="suppression-red",
        ))
        blue = ProductOperationService(match.bind_controller(
            "blue",
            controller_id="suppression-blue",
        ))
        assert red.capabilities([1])["operations"]["MOVE"][
            "applicable_count"
        ] == 0

        _whole_side_wait(red, request_id="red-expiry")
        _whole_side_wait(blue, request_id="blue-expiry")

        assert red.capabilities([1])["operations"]["MOVE"][
            "applicable_count"
        ] == 1
        preview = red.preview_operation(
            [1],
            "MOVE",
            request_id="new-human-move-preview",
            destination_hex=103,
        )
        executed = red.execute_preview(
            preview.preview_token,
            request_id="new-human-move-execute",
        )
        assert executed.executed_count == 1
        _whole_side_wait(blue, request_id="blue-new-move")
        step = match.gateway.manager._managed(  # noqa: SLF001
            match.session_id
        ).environment.last_step_record
        assert step["ordered_actions"][0]["action_type"] == "MOVE"
        assert step["ordered_actions"][0]["actor_id"] == 1
    finally:
        match.close()


def test_agent_uses_detached_frozen_move_after_suppression_task_finishes(
    tmp_path: Path,
) -> None:
    bundle = _paused_bundle()
    matches = []

    def factory(seed: int, episode_sequence: int):
        app = WargameFacade(
            ScenarioCatalog.from_bundles({
                bundle.scenario_identity.scenario_id: bundle,
            }),
            storage_root=tmp_path / f"agent-managed-{episode_sequence}",
        )
        match = app.create_match(
            scenario_id=bundle.scenario_identity.scenario_id,
            red_controller_id="agent-red",
            blue_controller_id="agent-blue",
            seed=seed,
            decision_interval_ms=112_000,
            frame_interval_ms=112_000,
        )
        matches.append(match)
        return match

    environment = RealtimeAgentSession(
        factory,
        run_namespace="suppression-agent-interface",
        tick_interval_ms=112_000,
        clock=ManualRealtimeClock(),
    )
    try:
        reset = environment.reset(seed=3)
        assert "MOVE" not in reset.red.capabilities.operation_types
        environment.submit(
            reset.red_binding,
            observation_version=reset.red.observation_version,
            decision=SeatDecision(),
        )
        environment.submit(
            reset.blue_binding,
            observation_version=reset.blue.observation_version,
            decision=SeatDecision(),
        )
        advanced = environment.advance_tick()
        assert "MOVE" in advanced.red.capabilities.operation_types

        receipt = environment.submit(
            reset.red_binding,
            observation_version=advanced.red.observation_version,
            decision=SeatDecision.single(SeatOperation(
                "MOVE",
                (1,),
                destination_hex=103,
            )),
        )
        environment.submit(
            reset.blue_binding,
            observation_version=advanced.blue.observation_version,
            decision=SeatDecision(),
        )
        environment.advance_tick()

        assert receipt.command_sequence is not None
        assert environment.command_events[
            receipt.command_sequence
        ].status is CommandStatus.APPLIED
    finally:
        environment.close()
