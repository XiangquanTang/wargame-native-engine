#!/usr/bin/env python3
"""List and run deterministic entries from the Native Engine Demo Library."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass, field
import json
from pathlib import Path
import tempfile
from zipfile import ZipFile

from wargame_native_engine.agents import (
    CommandStatus,
    ManualRealtimeClock,
    RealtimeAgentSession,
    RealtimeCancellationReason,
    SeatOperation,
)
from wargame_native_engine.data import ScenarioBundle, ScenarioCatalog
from wargame_native_engine.compat.source_frame.models import UnitDomain
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.demo_registry import (
    CompositeScenarioPhase,
    DemoDescriptor,
    DemoRegistry,
    load_default_demo_registry,
)
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.operations import WargameFacade
from wargame_native_engine.movement import load_source_movement_policy
from wargame_native_engine.visibility import (
    HexCell,
    HexMapProfile,
    evaluate_visibility,
)
from wargame_native_engine.viewer.human_capabilities import (
    build_human_capabilities,
    source_weapon_profiles,
)


class _DemoMatchFactory:
    def __init__(
        self,
        bundle: ScenarioBundle,
        *,
        storage_root: Path,
        tick_interval_ms: int,
    ) -> None:
        self.bundle = bundle
        self.storage_root = storage_root
        self.tick_interval_ms = tick_interval_ms

    def __call__(self, seed: int, episode_sequence: int):
        catalog = ScenarioCatalog.from_bundles({
            self.bundle.scenario_identity.scenario_id: self.bundle,
        })
        facade = WargameFacade(
            catalog,
            storage_root=self.storage_root / f"episode-{episode_sequence}",
        )
        return facade.create_match(
            scenario_id=self.bundle.scenario_identity.scenario_id,
            red_controller_id="demo-red",
            blue_controller_id="demo-blue",
            seed=seed,
            decision_interval_ms=self.tick_interval_ms,
            frame_interval_ms=self.tick_interval_ms,
        )


@dataclass(frozen=True, slots=True)
class ValidationResult:
    name: str
    passed: bool
    evidence: Mapping[str, object]
    failure_reason: str | None = None

    def to_record(self) -> dict[str, object]:
        return {
            "name": self.name,
            "result": self.passed,
            "pass": self.passed,
            "evidence": dict(self.evidence),
            "failure_reason": self.failure_reason,
        }


@dataclass(slots=True)
class DemoExecutionContext:
    demo: DemoDescriptor
    expected: Mapping[str, object]
    bundle: ScenarioBundle
    session: RealtimeAgentSession
    binding: object
    blue_binding: object
    current_view: object
    actors: Mapping[str, object]
    data: dict[str, object] = field(default_factory=dict)
    capability_evidence: dict[str, dict[str, bool]] = field(default_factory=dict)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Run a registered Native demo")
    value.add_argument("demo", help="demo ID, or 'list'")
    value.add_argument("--registry", type=Path)
    value.add_argument("--output-dir", type=Path)
    value.add_argument("--seed", type=int)
    value.add_argument("--tick-interval-ms", type=int, default=1_000)
    value.add_argument("--overwrite", action="store_true")
    return value


def run_registered_demo(
    demo_id: str,
    *,
    registry: DemoRegistry | None = None,
    output_dir: Path | None = None,
    seed: int | None = None,
    tick_interval_ms: int = 1_000,
    overwrite: bool = False,
) -> dict[str, object]:
    catalog = registry or load_default_demo_registry()
    demo = catalog.resolve(demo_id)
    expected = catalog.expected_behavior(demo)
    selected_seed = _exact_int(expected.get("seed"), "seed") if seed is None else seed
    if type(selected_seed) is not int:
        raise ValueError("seed must be an exact int")
    target = output_dir or Path("/tmp") / f"wargame-native-demo-{demo.demo_id}"
    bundle = ScenarioBundle.from_path(catalog.scenario_path(demo))
    print(_banner(demo, expected))
    validator = _validator_for(demo)
    return validator(
        demo,
        expected,
        bundle,
        output_dir=target,
        seed=selected_seed,
        tick_interval_ms=tick_interval_ms,
        overwrite=overwrite,
    )


def _validator_for(demo: DemoDescriptor):
    if demo.composite:
        return _run_composite
    validators = (
        ("BOARDING", _run_boarding),
        ("CONCEALMENT", _run_concealment),
        ("DIRECT_FIRE", _run_direct_fire),
        ("DRONE", _run_drone),
        ("GUIDED_FIRE", _run_guided_fire),
        ("INDIRECT_FIRE", _run_indirect_fire),
        ("LOITERING_MUNITION", _run_loitering_munition),
        ("OBJECTIVE_CONTROL", _run_objective_control),
        ("VISIBILITY", _run_visibility),
    )
    matches = [validator for feature, validator in validators
               if feature in demo.features]
    if len(matches) != 1:
        raise ValueError(
            f"demo {demo.demo_id} must select exactly one executable feature"
        )
    return matches[0]


def _run_drone(
    demo: DemoDescriptor,
    expected: Mapping[str, object],
    bundle: ScenarioBundle,
    *,
    output_dir: Path,
    seed: int,
    tick_interval_ms: int,
    overwrite: bool,
) -> dict[str, object]:
    actors = _object(expected.get("actors"), "actors")
    drone_id = _exact_int(actors.get("drone_id"), "drone_id")
    target_id = _exact_int(
        actors.get("observed_target_id"), "observed_target_id"
    )
    destination_hex = _exact_int(
        actors.get("destination_hex"), "destination_hex"
    )
    outcome = _object(expected.get("outcome"), "outcome")
    movement_duration_ms = _exact_int(
        outcome.get("movement_duration_ms"), "movement_duration_ms"
    )
    formation = _expected_formation(actors.get("formation"))
    formation_matches = _scenario_formation_matches(bundle, formation)
    with tempfile.TemporaryDirectory(prefix="wargame-demo-library.") as storage:
        session = _session(
            bundle, Path(storage), demo, tick_interval_ms=tick_interval_ms
        )
        reset = session.reset(seed=seed)
        try:
            initial_actions = tuple(
                value for value in reset.red.capabilities.frozen_actions
                if isinstance(value, Mapping)
                and isinstance(value.get("action"), Mapping)
                and value["action"].get("actor_id") == drone_id
            )
            action_types = {
                str(value["action"].get("action_type"))
                for value in initial_actions
            }
            move_actions = tuple(
                value for value in initial_actions
                if value["action"].get("action_type") == "MOVE"
                and value["action"].get("parameters", {}).get("path")
                == [destination_hex]
            )
            base = session.operation_capabilities(
                reset.red_binding, [drone_id]
            )
            _issued, controller = session.controller_snapshot(reset.red_binding)
            human = build_human_capabilities(
                base,
                controller,
                [drone_id],
                weapon_profiles=source_weapon_profiles(bundle.scenario.metadata),
            )
            human_types = {
                str(value.get("capability_type"))
                for value in human["capabilities"]
                if isinstance(value, Mapping)
            }
            receipt = session.submit_operation(
                reset.red_binding,
                observation_version=reset.red.observation_version,
                operation=SeatOperation(
                    "MOVE", (drone_id,), destination_hex=destination_hex
                ),
            )
            if not receipt.accepted:
                raise RuntimeError(f"drone MOVE rejected: {receipt.error_code}")
            elapsed_ms = 0
            current = reset.red
            while elapsed_ms < movement_duration_ms:
                current = session.advance_tick().red
                elapsed_ms += tick_interval_ms
            command = _command_event(session, receipt.command_sequence)
            friendlies = {
                value["operator_id"]: value
                for value in current.observation.get("friendly_operators", ())
                if isinstance(value, Mapping)
                and type(value.get("operator_id")) is int
            }
            final_actions = tuple(
                value for value in current.capabilities.frozen_actions
                if isinstance(value, Mapping)
                and isinstance(value.get("action"), Mapping)
                and value["action"].get("actor_id") == drone_id
            )
            final_action_types = {
                str(value["action"].get("action_type"))
                for value in final_actions
            }
            publication, verification = _publish_and_verify(
                session, bundle, output_dir, overwrite=overwrite
            )
            frame = _last_green_frame(publication.replay_path)
            drone_after = _green_operator(frame, drone_id)
            events = _all_green_events(publication.replay_path)
            no_fire_actions = not action_types.intersection({"SHOOT", "FIRE"})
            no_weapon_actions = not action_types.intersection({
                "WEAPON_LOCK", "WEAPON_UNFOLD"
            })
            no_lifecycle = (
                next(
                    value for value in bundle.scenario.operators
                    if value.operator_id == drone_id
                ).lifecycle_profile_id is None
                and not any(
                    value.get("event_type") == "unit_destroyed"
                    and value.get("actor_id") == drone_id
                    and isinstance(value.get("data"), Mapping)
                    and value["data"].get("reason_code") == "LIFECYCLE_EXPIRED"
                    for value in events
                )
            )
            checks = [
                _check("expected_formation", formation_matches),
                _check("engine_move_capability", len(move_actions) == 1),
                _check("agent_move_capability", len(move_actions) == 1),
                _check("human_move_capability", "MOVE" in human_types),
                _check("move_command_applied", command.status is CommandStatus.APPLIED),
                _check("arrived_destination", drone_after.get("cur_hex") == destination_hex),
                _check("visibility_refreshed", _seen_target(friendlies, drone_id, target_id)),
                _check("engine_wait_capability", "WAIT" in final_action_types),
                _check("agent_wait_capability", "WAIT" in final_action_types),
                _check("no_attack_capability", no_fire_actions),
                _check("no_weapon_posture_capability", no_weapon_actions),
                _check("no_limited_lifecycle", no_lifecycle),
                _check("strict_replay", verification.matches),
            ]
            report = {
                "demo_id": demo.demo_id,
                "status": "PASS",
                "scenario_id": bundle.scenario_identity.scenario_id,
                "features": list(demo.features),
                "seed": seed,
                "drone_id": drone_id,
                "destination_hex": destination_hex,
                "elapsed_ms": elapsed_ms,
                "command_status": command.status.value,
                "visibility_refreshed": _seen_target(
                    friendlies, drone_id, target_id
                ),
                "no_attack_capability": no_fire_actions,
                "no_limited_lifecycle": no_lifecycle,
                "strict_replay_matches": verification.matches,
                "formation_roles": sorted(formation),
            }
            return _complete_demo(
                publication.output_dir,
                report,
                {
                    "initial_action_types": sorted(action_types),
                    "final_action_types": sorted(final_action_types),
                    "drone_after": dict(drone_after),
                    "events": list(events),
                },
                checks,
                replay_path=publication.replay_path,
            )
        finally:
            session.close()


def _run_concealment(
    demo: DemoDescriptor,
    expected: Mapping[str, object],
    bundle: ScenarioBundle,
    *,
    output_dir: Path,
    seed: int,
    tick_interval_ms: int,
    overwrite: bool,
) -> dict[str, object]:
    actors = _object(expected.get("actors"), "actors")
    operator_id = _exact_int(actors.get("operator_id"), "operator_id")
    destination_hex = _exact_int(
        actors.get("destination_hex"),
        "destination_hex",
    )
    with tempfile.TemporaryDirectory(prefix="wargame-demo-library.") as storage:
        session = _session(
            bundle,
            Path(storage),
            demo,
            tick_interval_ms=tick_interval_ms,
        )
        reset = session.reset(seed=seed)
        try:
            engine_actions = tuple(
                value for value in reset.red.capabilities.frozen_actions
                if isinstance(value, Mapping)
                and isinstance(value.get("action"), Mapping)
                and value["action"].get("actor_id") == operator_id
                and value["action"].get("action_type")
                == "ENTER_CONCEALMENT"
            )
            base = session.operation_capabilities(
                reset.red_binding,
                [operator_id],
            )
            _issued, controller = session.controller_snapshot(reset.red_binding)
            human = build_human_capabilities(
                base,
                controller,
                [operator_id],
                weapon_profiles=source_weapon_profiles(bundle.scenario.metadata),
            )
            human_options = tuple(
                value for value in human["capabilities"]
                if value.get("capability_type") == "ENTER_CONCEALMENT"
            )
            conceal_receipt = session.submit_operation(
                reset.red_binding,
                observation_version=reset.red.observation_version,
                operation=SeatOperation(
                    "ENTER_CONCEALMENT",
                    (operator_id,),
                ),
            )
            if not conceal_receipt.accepted:
                raise RuntimeError(
                    "ENTER_CONCEALMENT command rejected: "
                    f"{conceal_receipt.error_code}"
                )
            concealed_tick = session.advance_tick()
            conceal_command = _command_event(
                session,
                conceal_receipt.command_sequence,
            )
            concealed_unit = _friendly_operator(
                concealed_tick.red.observation,
                operator_id,
            )

            move_receipt = session.submit_operation(
                reset.red_binding,
                observation_version=concealed_tick.red.observation_version,
                operation=SeatOperation(
                    "MOVE",
                    (operator_id,),
                    destination_hex=destination_hex,
                ),
            )
            if not move_receipt.accepted:
                raise RuntimeError(
                    f"MOVE command rejected: {move_receipt.error_code}"
                )
            moved_tick = session.advance_tick()
            move_command = _command_event(session, move_receipt.command_sequence)
            exposed_unit = _friendly_operator(
                moved_tick.red.observation,
                operator_id,
            )
            publication, verification = _publish_and_verify(
                session,
                bundle,
                output_dir,
                overwrite=overwrite,
            )
            events = _all_green_events(publication.replay_path)
            event_types = tuple(value.get("event_type") for value in events)
            checks = [
                _check("engine_capability", len(engine_actions) == 1),
                _check("agent_capability", len(engine_actions) == 1),
                _check("human_capability", len(human_options) == 1),
                _check(
                    "conceal_command_applied",
                    conceal_command.status is CommandStatus.APPLIED,
                ),
                _check(
                    "concealed_posture",
                    concealed_unit.get("concealment_posture") == "CONCEALED",
                ),
                _check(
                    "move_command_applied",
                    move_command.status is CommandStatus.APPLIED,
                ),
                _check(
                    "move_exposed_posture",
                    exposed_unit.get("concealment_posture") == "EXPOSED",
                ),
                _check("unit_concealed_event", "unit_concealed" in event_types),
                _check("unit_exposed_event", "unit_exposed" in event_types),
                _check("strict_replay", verification.matches),
            ]
            report = {
                "demo_id": demo.demo_id,
                "status": "PASS",
                "scenario_id": bundle.scenario_identity.scenario_id,
                "features": list(demo.features),
                "seed": seed,
                "operator_id": operator_id,
                "destination_hex": destination_hex,
                "conceal_command_status": conceal_command.status.value,
                "move_command_status": move_command.status.value,
                "final_tick": moved_tick.tick_index,
                "strict_replay_matches": verification.matches,
            }
            evidence = {
                "concealed_unit": dict(concealed_unit),
                "exposed_unit": dict(exposed_unit),
                "event_types": list(event_types),
                "events": list(events),
            }
            return _complete_demo(
                publication.output_dir,
                report,
                evidence,
                checks,
                replay_path=publication.replay_path,
            )
        finally:
            session.close()


def _run_composite(
    demo: DemoDescriptor,
    expected: Mapping[str, object],
    bundle: ScenarioBundle,
    *,
    output_dir: Path,
    seed: int,
    tick_interval_ms: int,
    overwrite: bool,
) -> dict[str, object]:
    if not demo.phases:
        raise ValueError(f"composite demo {demo.demo_id} lacks phases")
    pipeline = _composite_validator_pipeline(demo)
    with tempfile.TemporaryDirectory(prefix="wargame-demo-composite.") as storage:
        session = _session(
            bundle, Path(storage), demo, tick_interval_ms=tick_interval_ms
        )
        reset = session.reset(seed=seed)
        context = DemoExecutionContext(
            demo=demo,
            expected=expected,
            bundle=bundle,
            session=session,
            binding=reset.red_binding,
            blue_binding=reset.blue_binding,
            current_view=reset.red,
            actors=_object(expected.get("actors"), "actors"),
        )
        try:
            context.data["initial_target_visible"] = _composite_target_visible(
                context
            )
            phase_results: list[ValidationResult] = []
            for phase, validator in pipeline:
                result = validator(context, phase)
                phase_results.append(result)
                if not result.passed:
                    raise RuntimeError(
                        f"composite phase failed: {phase.name}: "
                        f"{result.failure_reason or 'validation failed'}; "
                        f"evidence={dict(result.evidence)}"
                    )

            publication, verification = _publish_and_verify(
                session, bundle, output_dir, overwrite=overwrite
            )
            events = _all_green_events(publication.replay_path)
            event_types = tuple(value.get("event_type") for value in events)
            expected_events = tuple(
                _text(value, "outcome.events[]")
                for value in _sequence(
                    _object(expected.get("outcome"), "outcome").get("events"),
                    "outcome.events",
                )
            )
            checks = [value.to_record() for value in phase_results]
            capability_phase_names = {
                capability_id: tuple(
                    phase.name for phase in demo.phases
                    if phase.required_capability == capability_id
                )
                for capability_id in demo.capability_ids
            }
            phase_passed = {value.name: value.passed for value in phase_results}
            for capability_id in demo.capability_ids:
                checks.append(_check(
                    f"engine.{capability_id}",
                    all(
                        phase_passed[name]
                        for name in capability_phase_names[capability_id]
                    ),
                ))
                evidence = context.capability_evidence.get(capability_id, {})
                checks.extend((
                    _check(
                        f"human.{capability_id}",
                        evidence.get("human") is True,
                    ),
                    _check(
                        f"agent.{capability_id}",
                        evidence.get("agent") is True,
                    ),
                ))
            checks.extend((
                _check(
                    "composite_event_order",
                    _ordered_event_subset(expected_events, event_types),
                ),
                _check("strict_replay", verification.matches),
            ))
            report = {
                "demo_id": demo.demo_id,
                "status": "PASS",
                "scenario_id": bundle.scenario_identity.scenario_id,
                "features": list(demo.features),
                "capability_ids": list(demo.capability_ids),
                "seed": seed,
                "phase_count": len(phase_results),
                "phase_results": [value.to_record() for value in phase_results],
                "final_tick": context.current_view.tick_index,
                "strict_replay_matches": verification.matches,
            }
            return _complete_demo(
                publication.output_dir,
                report,
                {
                    "phases": [value.to_record() for value in phase_results],
                    "capabilities": context.capability_evidence,
                    "events": list(events),
                    "event_types": list(event_types),
                },
                checks,
                replay_path=publication.replay_path,
            )
        finally:
            session.close()


def _composite_validator_pipeline(
    demo: DemoDescriptor,
) -> tuple[tuple[CompositeScenarioPhase, object], ...]:
    validators = {
        "BOARD": _composite_board,
        "BOARD_COMPLETE": _composite_board_complete,
        "MOVE": _composite_move,
        "UNLOAD": _composite_unload,
        "UNLOAD_COMPLETE": _composite_unload_complete,
        "OBJECTIVE_CONTROL": _composite_objective,
        "VISIBILITY": _composite_visibility,
        "DIRECT_FIRE": _composite_direct_fire,
    }
    result = []
    for phase in demo.phases:
        validator = validators.get(phase.validator)
        if validator is None:
            raise ValueError(
                f"unsupported composite validator: {phase.validator}"
            )
        result.append((phase, validator))
    return tuple(result)


def _composite_board(
    context: DemoExecutionContext, phase: CompositeScenarioPhase,
) -> ValidationResult:
    passenger_id = _actor(context, "passenger_id")
    carrier_id = _actor(context, "carrier_id")
    actions = tuple(
        value for value in context.current_view.capabilities.frozen_actions
        if _boarding_action_matches(
            value, operation_type="BOARD", actor_id=passenger_id,
            target_id=carrier_id,
        )
    )
    human = _composite_human_capability(context, passenger_id, "BOARD")
    operation = boarding_basic_agent_validator(
        context.current_view,
        operation_type="BOARD",
        actor_id=passenger_id,
        target_id=carrier_id,
    )
    context.data["board_started_ms"] = context.current_view.sim_time_ms
    command = _composite_submit(context, operation)
    passenger = _friendly_operator(context.current_view.observation, passenger_id)
    passed = (
        len(actions) == 1
        and human
        and command.status is CommandStatus.APPLIED
        and passenger.get("state") == "ON_BOARD"
    )
    _record_capability(context, "boarding", human=human, agent=len(actions) == 1)
    return _phase_result(phase, passed, {
        "command_status": command.status.value,
        "passenger_state": passenger.get("state"),
        "target_carrier_id": carrier_id,
    })


def _composite_board_complete(
    context: DemoExecutionContext, phase: CompositeScenarioPhase,
) -> ValidationResult:
    carrier_id = _actor(context, "carrier_id")
    passenger_id = _actor(context, "passenger_id")
    elapsed = 0
    for _ in range(80):
        if any(
            _boarding_action_matches(
                value, operation_type="UNLOAD", actor_id=carrier_id,
                target_id=passenger_id,
            )
            for value in context.current_view.capabilities.frozen_actions
        ):
            break
        context.current_view = context.session.advance_tick().red
        elapsed += context.session.tick_interval_ms
    available = any(
        _boarding_action_matches(
            value, operation_type="UNLOAD", actor_id=carrier_id,
            target_id=passenger_id,
        )
        for value in context.current_view.capabilities.frozen_actions
    )
    total_elapsed = (
        context.current_view.sim_time_ms
        - _exact_int(context.data.get("board_started_ms"), "board_started_ms")
    )
    expected_duration = _exact_int(
        _object(context.expected.get("outcome"), "outcome").get(
            "transition_duration_ms"
        ),
        "transition_duration_ms",
    )
    return _phase_result(
        phase, available and total_elapsed == expected_duration, {
        "additional_elapsed_ms": elapsed,
        "total_elapsed_ms": total_elapsed,
        "tick": context.current_view.tick_index,
        "unload_available": available,
    })


def _composite_move(
    context: DemoExecutionContext, phase: CompositeScenarioPhase,
) -> ValidationResult:
    carrier_id = _actor(context, "carrier_id")
    destination = _actor(context, "destination_hex")
    actions = tuple(
        value for value in context.current_view.capabilities.frozen_actions
        if isinstance(value, Mapping)
        and isinstance(value.get("action"), Mapping)
        and value["action"].get("action_type") == "MOVE"
        and value["action"].get("actor_id") == carrier_id
        and value["action"].get("parameters", {}).get("path") == [destination]
    )
    human = _composite_human_capability(context, carrier_id, "MOVE")
    operation = SeatOperation("MOVE", (carrier_id,), destination_hex=destination)
    receipt = context.session.submit_operation(
        context.binding,
        observation_version=context.current_view.observation_version,
        operation=operation,
    )
    if not receipt.accepted:
        raise RuntimeError(f"composite MOVE rejected: {receipt.error_code}")
    tick = _advance_until_positions(
        context.session, {carrier_id: destination}, limit=120
    )
    tick = _advance_until_stopped(context.session, carrier_id, limit=80)
    context.current_view = tick.red
    command = _command_event(context.session, receipt.command_sequence)
    unit = _friendly_operator(context.current_view.observation, carrier_id)
    passed = (
        len(actions) == 1
        and human
        and command.status is CommandStatus.APPLIED
        and unit.get("state") == "STOPPED"
    )
    _record_capability(
        context, "objective_control", human=human, agent=len(actions) == 1
    )
    return _phase_result(phase, passed, {
        "command_status": command.status.value,
        "destination_hex": destination,
        "state": unit.get("state"),
        "tick": context.current_view.tick_index,
    })


def _composite_unload(
    context: DemoExecutionContext, phase: CompositeScenarioPhase,
) -> ValidationResult:
    passenger_id = _actor(context, "passenger_id")
    carrier_id = _actor(context, "carrier_id")
    actions = tuple(
        value for value in context.current_view.capabilities.frozen_actions
        if _boarding_action_matches(
            value, operation_type="UNLOAD", actor_id=carrier_id,
            target_id=passenger_id,
        )
    )
    human = _composite_human_capability(context, carrier_id, "UNLOAD")
    operation = boarding_basic_agent_validator(
        context.current_view,
        operation_type="UNLOAD",
        actor_id=carrier_id,
        target_id=passenger_id,
    )
    context.data["unload_started_ms"] = context.current_view.sim_time_ms
    command = _composite_submit(context, operation)
    passed = (
        len(actions) == 1
        and human
        and command.status is CommandStatus.APPLIED
    )
    _record_capability(context, "boarding", human=human, agent=len(actions) == 1)
    return _phase_result(phase, passed, {
        "command_status": command.status.value,
        "passenger_id": passenger_id,
    })


def _composite_unload_complete(
    context: DemoExecutionContext, phase: CompositeScenarioPhase,
) -> ValidationResult:
    passenger_id = _actor(context, "passenger_id")
    elapsed = 0
    passenger: Mapping[str, object] = {}
    for _ in range(80):
        try:
            passenger = _friendly_operator(
                context.current_view.observation, passenger_id
            )
        except RuntimeError:
            passenger = {}
        if passenger.get("state") == "ACTIVE":
            break
        context.current_view = context.session.advance_tick().red
        elapsed += context.session.tick_interval_ms
    total_elapsed = (
        context.current_view.sim_time_ms
        - _exact_int(context.data.get("unload_started_ms"), "unload_started_ms")
    )
    expected_duration = _exact_int(
        _object(context.expected.get("outcome"), "outcome").get(
            "transition_duration_ms"
        ),
        "transition_duration_ms",
    )
    passed = (
        passenger.get("state") == "ACTIVE"
        and total_elapsed == expected_duration
    )
    return _phase_result(phase, passed, {
        "additional_elapsed_ms": elapsed,
        "total_elapsed_ms": total_elapsed,
        "passenger_state": passenger.get("state"),
        "tick": context.current_view.tick_index,
    })


def _composite_objective(
    context: DemoExecutionContext, phase: CompositeScenarioPhase,
) -> ValidationResult:
    objective_id = _actor(context, "objective_id")
    controlling_side = _objective_control(
        context.current_view.observation, objective_id
    )
    expected_side = _object(
        context.expected.get("outcome"), "outcome"
    ).get("objective_controlling_side")
    passed = controlling_side == expected_side
    existing = context.capability_evidence.get("objective_control", {})
    _record_capability(
        context,
        "objective_control",
        human=existing.get("human") is True,
        agent=existing.get("agent") is True,
    )
    return _phase_result(phase, passed, {
        "objective_id": objective_id,
        "controlling_side": controlling_side,
    })


def _composite_visibility(
    context: DemoExecutionContext, phase: CompositeScenarioPhase,
) -> ValidationResult:
    visible = _composite_target_visible(context)
    initial = context.data.get("initial_target_visible")
    passed = initial is False and visible is True
    _record_capability(context, "visibility", human=visible, agent=visible)
    return _phase_result(phase, passed, {
        "initial_contact": initial,
        "current_contact": visible,
        "target_id": _actor(context, "target_id"),
    })


def _composite_direct_fire(
    context: DemoExecutionContext, phase: CompositeScenarioPhase,
) -> ValidationResult:
    carrier_id = _actor(context, "carrier_id")
    target_id = _actor(context, "target_id")
    weapon_id = _actor(context, "weapon_id")
    actions = tuple(
        value for value in context.current_view.capabilities.frozen_actions
        if _direct_action_matches(
            value,
            attacker_id=carrier_id,
            target_id=target_id,
            weapon_id=weapon_id,
        )
    )
    human = _composite_human_direct_fire(
        context, carrier_id, target_id, weapon_id
    )
    _, blue_before = _composite_views(context)
    target_before = _friendly_operator(blue_before.observation, target_id)
    command = _composite_submit(
        context,
        SeatOperation(
            "SHOOT",
            (carrier_id,),
            target_id=target_id,
            weapon_choices=((carrier_id, weapon_id),),
        ),
    )
    _, blue_after = _composite_views(context)
    target_after = _friendly_operator(blue_after.observation, target_id)
    target_changed = (
        target_before.get("blood") != target_after.get("blood")
        or target_before.get("alive") != target_after.get("alive")
    )
    passed = (
        len(actions) == 1
        and human
        and command.status is CommandStatus.APPLIED
        and target_changed
    )
    _record_capability(
        context, "direct_fire", human=human, agent=len(actions) == 1
    )
    return _phase_result(phase, passed, {
        "command_status": command.status.value,
        "weapon_id": weapon_id,
        "target_id": target_id,
        "target_state_changed": target_changed,
    })


def _composite_submit(
    context: DemoExecutionContext, operation: SeatOperation,
):
    receipt = context.session.submit_operation(
        context.binding,
        observation_version=context.current_view.observation_version,
        operation=operation,
    )
    if not receipt.accepted:
        raise RuntimeError(
            f"composite {operation.operation_type} rejected: "
            f"{receipt.error_code}"
        )
    tick = context.session.advance_tick()
    context.current_view = tick.red
    return _command_event(context.session, receipt.command_sequence)


def _composite_views(
    context: DemoExecutionContext,
) -> tuple[object, object]:
    red, _red_controller = context.session.controller_snapshot(context.binding)
    blue, _blue_controller = context.session.controller_snapshot(
        context.blue_binding
    )
    return red, blue


def _composite_human_capability(
    context: DemoExecutionContext,
    actor_id: int,
    capability_type: str,
) -> bool:
    base = context.session.operation_capabilities(context.binding, [actor_id])
    _seat, controller = context.session.controller_snapshot(context.binding)
    human = build_human_capabilities(
        base,
        controller,
        [actor_id],
        weapon_profiles=source_weapon_profiles(context.bundle.scenario.metadata),
    )
    return any(
        isinstance(value, Mapping)
        and value.get("capability_type") == capability_type
        for value in human.get("capabilities", ())
    )


def _composite_human_direct_fire(
    context: DemoExecutionContext,
    actor_id: int,
    target_id: int,
    weapon_id: int,
) -> bool:
    base = context.session.operation_capabilities(context.binding, [actor_id])
    _seat, controller = context.session.controller_snapshot(context.binding)
    human = build_human_capabilities(
        base,
        controller,
        [actor_id],
        weapon_profiles=source_weapon_profiles(context.bundle.scenario.metadata),
    )
    direct = human.get("direct_fire")
    units = direct.get("units", ()) if isinstance(direct, Mapping) else ()
    return any(
        isinstance(unit, Mapping)
        and unit.get("unit_id") == actor_id
        and any(
            isinstance(weapon, Mapping)
            and weapon.get("weapon_id") == weapon_id
            and target_id in weapon.get("legal_target_ids", ())
            for weapon in unit.get("weapons", ())
        )
        for unit in units
    )


def _composite_target_visible(context: DemoExecutionContext) -> bool:
    carrier_id = _actor(context, "carrier_id")
    target_id = _actor(context, "target_id")
    friendlies = {
        value["operator_id"]: value
        for value in context.current_view.observation.get(
            "friendly_operators", ()
        )
        if isinstance(value, Mapping)
        and type(value.get("operator_id")) is int
    }
    return _seen_target(friendlies, carrier_id, target_id)


def _record_capability(
    context: DemoExecutionContext,
    capability_id: str,
    *,
    human: bool,
    agent: bool,
) -> None:
    previous = context.capability_evidence.get(capability_id)
    context.capability_evidence[capability_id] = {
        "human": human if previous is None else previous["human"] and human,
        "agent": agent if previous is None else previous["agent"] and agent,
    }


def _phase_result(
    phase: CompositeScenarioPhase,
    passed: bool,
    evidence: Mapping[str, object],
) -> ValidationResult:
    return ValidationResult(
        name=phase.name,
        passed=passed,
        evidence=dict(evidence),
        failure_reason=None if passed else f"{phase.validator} contract failed",
    )


def _actor(context: DemoExecutionContext, name: str) -> int:
    return _exact_int(context.actors.get(name), name)


def _ordered_event_subset(
    expected: tuple[str, ...], actual: tuple[object, ...],
) -> bool:
    offset = 0
    for value in actual:
        if offset < len(expected) and value == expected[offset]:
            offset += 1
    return offset == len(expected)


def _run_boarding(
    demo: DemoDescriptor,
    expected: Mapping[str, object],
    bundle: ScenarioBundle,
    *,
    output_dir: Path,
    seed: int,
    tick_interval_ms: int,
    overwrite: bool,
) -> dict[str, object]:
    actors = _object(expected.get("actors"), "actors")
    passenger_id = _exact_int(actors.get("passenger_id"), "passenger_id")
    carrier_id = _exact_int(actors.get("carrier_id"), "carrier_id")
    profile_id = _text(actors.get("profile_id"), "profile_id")
    with tempfile.TemporaryDirectory(prefix="wargame-demo-library.") as storage:
        session = _session(
            bundle, Path(storage), demo, tick_interval_ms=tick_interval_ms
        )
        reset = session.reset(seed=seed)
        try:
            board_actions = tuple(
                value for value in reset.red.capabilities.frozen_actions
                if _boarding_action_matches(
                    value,
                    operation_type="BOARD",
                    actor_id=passenger_id,
                    target_id=carrier_id,
                )
            )
            initial_friendlies = {
                value["operator_id"]: value
                for value in reset.red.observation.get("friendly_operators", ())
                if isinstance(value, Mapping)
                and type(value.get("operator_id")) is int
            }
            initial_same_position = (
                initial_friendlies[passenger_id].get("position")
                == initial_friendlies[carrier_id].get("position")
            )
            base = session.operation_capabilities(
                reset.red_binding, [passenger_id]
            )
            _issued, controller = session.controller_snapshot(reset.red_binding)
            human = build_human_capabilities(
                base,
                controller,
                [passenger_id],
                weapon_profiles=source_weapon_profiles(bundle.scenario.metadata),
            )
            human_board = tuple(
                value for value in human["boarding"]["options"]
                if value["operation_type"] == "BOARD"
                and value["actor_id"] == passenger_id
                and value["target_id"] == carrier_id
                and value["profile_id"] == profile_id
            )
            board_operation = boarding_basic_agent_validator(
                reset.red,
                operation_type="BOARD",
                actor_id=passenger_id,
                target_id=carrier_id,
            )
            board_receipt = session.submit_operation(
                reset.red_binding,
                observation_version=reset.red.observation_version,
                operation=board_operation,
            )
            if not board_receipt.accepted:
                raise RuntimeError(
                    f"BOARD command rejected: {board_receipt.error_code}"
                )
            board_tick = session.advance_tick()
            board_command = _command_event(
                session, board_receipt.command_sequence
            )
            boarded = _friendly_operator(
                board_tick.red.observation, passenger_id
            )

            current_view = board_tick.red
            for _ in range(80):
                unload_actions = tuple(
                    value for value in current_view.capabilities.frozen_actions
                    if _boarding_action_matches(
                        value,
                        operation_type="UNLOAD",
                        actor_id=carrier_id,
                        target_id=passenger_id,
                    )
                )
                if unload_actions:
                    break
                current_view = session.advance_tick().red
            else:
                raise RuntimeError("BOARD transition did not complete in 75 seconds")

            base = session.operation_capabilities(
                reset.red_binding, [carrier_id]
            )
            _issued, controller = session.controller_snapshot(reset.red_binding)
            human = build_human_capabilities(
                base,
                controller,
                [carrier_id],
                weapon_profiles=source_weapon_profiles(bundle.scenario.metadata),
            )
            human_unload = tuple(
                value for value in human["boarding"]["options"]
                if value["operation_type"] == "UNLOAD"
                and value["actor_id"] == carrier_id
                and value["target_id"] == passenger_id
            )
            unload_operation = boarding_basic_agent_validator(
                current_view,
                operation_type="UNLOAD",
                actor_id=carrier_id,
                target_id=passenger_id,
            )
            unload_receipt = session.submit_operation(
                reset.red_binding,
                observation_version=current_view.observation_version,
                operation=unload_operation,
            )
            if not unload_receipt.accepted:
                raise RuntimeError(
                    f"UNLOAD command rejected: {unload_receipt.error_code}"
                )
            unload_tick = session.advance_tick()
            unload_command = _command_event(
                session, unload_receipt.command_sequence
            )
            final_view = unload_tick.red
            for _ in range(80):
                passenger = _friendly_operator(
                    final_view.observation, passenger_id
                )
                passenger_actions = tuple(
                    value for value in final_view.capabilities.frozen_actions
                    if isinstance(value, Mapping)
                    and isinstance(value.get("action"), Mapping)
                    and value["action"].get("actor_id") == passenger_id
                )
                if passenger.get("state") == "ACTIVE" and passenger_actions:
                    break
                final_view = session.advance_tick().red
            else:
                raise RuntimeError("UNLOAD transition did not complete in 75 seconds")

            publication, verification = _publish_and_verify(
                session, bundle, output_dir, overwrite=overwrite
            )
            frames = _green_frames(publication.replay_path)
            events = _all_green_events(publication.replay_path)
            event_types = tuple(value.get("event_type") for value in events)
            mounted_frames = tuple(
                unit
                for frame in frames
                for unit in frame.get("passengers", ())
                if isinstance(unit, Mapping)
                and unit.get("obj_id") == passenger_id
                and unit.get("car") == carrier_id
            )
            locked_frames = tuple(
                unit
                for frame in frames
                for unit in frame.get("operators", ())
                if isinstance(unit, Mapping)
                and unit.get("obj_id") == carrier_id
                and isinstance(unit.get("extra"), Mapping)
                and isinstance(unit["extra"].get("native"), Mapping)
                and unit["extra"]["native"].get("movement_locked") is True
            )
            required_events = (
                "board_started",
                "board_completed",
                "unload_started",
                "unload_completed",
            )
            checks = [
                _check("initial_same_hex", initial_same_position),
                _check("engine_board_capability", len(board_actions) == 1),
                _check("agent_board_capability", len(board_actions) == 1),
                _check("human_board_capability", len(human_board) == 1),
                _check("board_command_applied", board_command.status is CommandStatus.APPLIED),
                _check("passenger_on_board", boarded.get("state") == "ON_BOARD"),
                _check("mounted_relation_recorded", bool(mounted_frames)),
                _check("carrier_locked_during_transition", bool(locked_frames)),
                _check("engine_unload_capability", len(unload_actions) == 1),
                _check("agent_unload_capability", len(unload_actions) == 1),
                _check("human_unload_capability", len(human_unload) == 1),
                _check("unload_command_applied", unload_command.status is CommandStatus.APPLIED),
                _check("passenger_active_after_unload", passenger.get("state") == "ACTIVE"),
                _check("valid_actions_refreshed", bool(passenger_actions)),
                *(
                    _check(f"event.{event_type}", event_type in event_types)
                    for event_type in required_events
                ),
                _check(
                    "boarding_event_order",
                    tuple(
                        value for value in event_types
                        if value in required_events
                    ) == required_events,
                ),
                _check("strict_replay", verification.matches),
            ]
            report = {
                "demo_id": demo.demo_id,
                "status": "PASS",
                "scenario_id": bundle.scenario_identity.scenario_id,
                "features": list(demo.features),
                "seed": seed,
                "passenger_id": passenger_id,
                "carrier_id": carrier_id,
                "profile_id": profile_id,
                "board_command_status": board_command.status.value,
                "unload_command_status": unload_command.status.value,
                "final_tick": final_view.tick_index,
                "strict_replay_matches": verification.matches,
            }
            evidence = {
                "boarded_passenger": dict(boarded),
                "unloaded_passenger": dict(passenger),
                "event_types": list(event_types),
                "events": list(events),
            }
            return _complete_demo(
                publication.output_dir,
                report,
                evidence,
                checks,
                replay_path=publication.replay_path,
            )
        finally:
            session.close()


def _run_direct_fire(
    demo: DemoDescriptor,
    expected: Mapping[str, object],
    bundle: ScenarioBundle,
    *,
    output_dir: Path,
    seed: int,
    tick_interval_ms: int,
    overwrite: bool,
) -> dict[str, object]:
    actors = _object(expected.get("actors"), "actors")
    attacker_id = _exact_int(actors.get("attacker_id"), "attacker_id")
    target_id = _exact_int(actors.get("target_id"), "target_id")
    weapon_id = _exact_int(actors.get("weapon_id"), "weapon_id")
    with tempfile.TemporaryDirectory(prefix="wargame-demo-library.") as storage:
        session = _session(
            bundle, Path(storage), demo, tick_interval_ms=tick_interval_ms
        )
        reset = session.reset(seed=seed)
        try:
            frozen_matches = tuple(
                value for value in reset.red.capabilities.frozen_actions
                if _direct_action_matches(
                    value,
                    attacker_id=attacker_id,
                    target_id=target_id,
                    weapon_id=weapon_id,
                )
            )
            base = session.operation_capabilities(
                reset.red_binding, [attacker_id]
            )
            _issued, controller = session.controller_snapshot(reset.red_binding)
            human = build_human_capabilities(
                base,
                controller,
                [attacker_id],
                weapon_profiles=source_weapon_profiles(bundle.scenario.metadata),
            )
            human_weapons = tuple(
                weapon
                for unit in human["direct_fire"]["units"]
                if unit["unit_id"] == attacker_id
                for weapon in unit["weapons"]
                if weapon["weapon_id"] == weapon_id
                and target_id in weapon["legal_target_ids"]
            )
            before = _friendly_operator(reset.blue.observation, target_id)
            receipt = session.submit_operation(
                reset.red_binding,
                observation_version=reset.red.observation_version,
                operation=SeatOperation(
                    "SHOOT",
                    (attacker_id,),
                    target_id=target_id,
                    weapon_choices=((attacker_id, weapon_id),),
                ),
            )
            if not receipt.accepted:
                raise RuntimeError(
                    f"DIRECT command rejected: {receipt.error_code}"
                )
            tick = session.advance_tick()
            command = _command_event(session, receipt.command_sequence)
            publication, verification = _publish_and_verify(
                session,
                bundle,
                output_dir,
                overwrite=overwrite,
            )
            frame = _last_green_frame(publication.replay_path)
            after = _green_operator(frame, target_id)
            events = _events_from_green_frame(frame)
            event_type = _text(
                _object(expected.get("outcome"), "outcome").get("event_type"),
                "event_type",
            )
            target_changed = (
                after.get("blood") != before.get("blood")
                or after.get("alive") != before.get("alive")
            )
            checks = [
                _check("engine_direct_capability", len(frozen_matches) == 1),
                _check("agent_direct_capability", len(frozen_matches) == 1),
                _check("human_direct_capability", len(human_weapons) == 1),
                _check("command_applied", command.status is CommandStatus.APPLIED),
                _check(
                    "formal_fire_event",
                    any(value.get("event_type") == event_type for value in events),
                ),
                _check("target_state_changed", target_changed),
                _check("strict_replay", verification.matches),
            ]
            report = {
                "demo_id": demo.demo_id,
                "status": "PASS",
                "scenario_id": bundle.scenario_identity.scenario_id,
                "features": list(demo.features),
                "seed": seed,
                "attacker_id": attacker_id,
                "target_id": target_id,
                "weapon_id": weapon_id,
                "command_status": command.status.value,
                "tick_index": tick.tick_index,
                "event_type": event_type,
                "target_state_changed": target_changed,
                "strict_replay_matches": verification.matches,
            }
            evidence = {
                "target_before": dict(before),
                "target_after": dict(after),
                "events": list(events),
            }
            return _complete_demo(
                publication.output_dir, report, evidence, checks,
                replay_path=publication.replay_path,
            )
        finally:
            session.close()


def _run_loitering_munition(
    demo: DemoDescriptor,
    expected: Mapping[str, object],
    bundle: ScenarioBundle,
    *,
    output_dir: Path,
    seed: int,
    tick_interval_ms: int,
    overwrite: bool,
) -> dict[str, object]:
    mode = _text(expected.get("mode"), "mode").upper()
    actors = _object(expected.get("actors"), "actors")
    attacker_id = _exact_int(actors.get("attacker_id"), "attacker_id")
    weapon_id = _exact_int(actors.get("weapon_id"), "weapon_id")
    if mode == "EXPIRY":
        return _run_loitering_munition_expiry(
            demo,
            expected,
            bundle,
            output_dir=output_dir,
            seed=seed,
            attacker_id=attacker_id,
            overwrite=overwrite,
        )
    if mode != "ATTACK":
        raise ValueError(f"unsupported loitering demo mode: {mode}")
    target_id = _exact_int(actors.get("target_id"), "target_id")
    destination_hex = _exact_int(
        actors.get("destination_hex"), "destination_hex"
    )
    with tempfile.TemporaryDirectory(prefix="wargame-demo-library.") as storage:
        session = _session(
            bundle, Path(storage), demo, tick_interval_ms=tick_interval_ms
        )
        reset = session.reset(seed=seed)
        try:
            initial_move_actions = tuple(
                value for value in reset.red.capabilities.frozen_actions
                if isinstance(value, Mapping)
                and isinstance(value.get("action"), Mapping)
                and value["action"].get("actor_id") == attacker_id
                and value["action"].get("action_type") == "MOVE"
            )
            base = session.operation_capabilities(
                reset.red_binding, [attacker_id]
            )
            _issued, controller = session.controller_snapshot(reset.red_binding)
            initial_human = build_human_capabilities(
                base,
                controller,
                [attacker_id],
                weapon_profiles=source_weapon_profiles(bundle.scenario.metadata),
            )
            human_move = any(
                value.get("capability_type") == "MOVE"
                for value in initial_human["capabilities"]
            )
            move_receipt = session.submit_operation(
                reset.red_binding,
                observation_version=reset.red.observation_version,
                operation=SeatOperation(
                    "MOVE", (attacker_id,), destination_hex=destination_hex
                ),
            )
            if not move_receipt.accepted:
                raise RuntimeError(
                    f"loitering MOVE rejected: {move_receipt.error_code}"
                )
            current = session.advance_tick().red
            move_command = _command_event(session, move_receipt.command_sequence)
            attack_actions = tuple(
                value for value in current.capabilities.frozen_actions
                if _direct_action_matches(
                    value,
                    attacker_id=attacker_id,
                    target_id=target_id,
                    weapon_id=weapon_id,
                )
            )
            for _ in range(30):
                if attack_actions:
                    break
                current = session.advance_tick().red
                attack_actions = tuple(
                    value for value in current.capabilities.frozen_actions
                    if _direct_action_matches(
                        value,
                        attacker_id=attacker_id,
                        target_id=target_id,
                        weapon_id=weapon_id,
                    )
                )
            else:
                raise RuntimeError("weapon 76 did not become a legal action")

            friendlies = {
                value["operator_id"]: value
                for value in current.observation.get("friendly_operators", ())
                if isinstance(value, Mapping)
                and type(value.get("operator_id")) is int
            }
            visibility_refreshed = _seen_target(
                friendlies, attacker_id, target_id
            )
            base = session.operation_capabilities(
                reset.red_binding, [attacker_id]
            )
            _issued, controller = session.controller_snapshot(reset.red_binding)
            attack_human = build_human_capabilities(
                base,
                controller,
                [attacker_id],
                weapon_profiles=source_weapon_profiles(bundle.scenario.metadata),
            )
            human_attack = any(
                unit.get("unit_id") == attacker_id
                and any(
                    weapon.get("weapon_id") == weapon_id
                    and target_id in weapon.get("legal_target_ids", ())
                    for weapon in unit.get("weapons", ())
                )
                for unit in attack_human["direct_fire"]["units"]
            )
            attack_receipt = session.submit_operation(
                reset.red_binding,
                observation_version=current.observation_version,
                operation=SeatOperation(
                    "SHOOT",
                    (attacker_id,),
                    target_id=target_id,
                    weapon_choices=((attacker_id, weapon_id),),
                ),
            )
            if not attack_receipt.accepted:
                raise RuntimeError(
                    f"weapon 76 attack rejected: {attack_receipt.error_code}"
                )
            session.advance_tick()
            attack_command = _command_event(
                session, attack_receipt.command_sequence
            )
            publication, verification = _publish_and_verify(
                session, bundle, output_dir, overwrite=overwrite
            )
            frame = _last_green_frame(publication.replay_path)
            attacker_after = _green_operator(frame, attacker_id)
            target_after = _green_operator(frame, target_id)
            events = _all_green_events(publication.replay_path)
            consumed = tuple(
                value for value in events
                if value.get("event_type") == "unit_destroyed"
                and value.get("actor_id") == attacker_id
                and isinstance(value.get("data"), Mapping)
                and value["data"].get("reason_code")
                == "LOITERING_MUNITION_CONSUMED"
            )
            no_ammunition_event = not any(
                value.get("event_type") == "ammunition_consumed"
                and value.get("actor_id") == attacker_id
                for value in events
            )
            no_ammunition_key = (
                attacker_after.get("remain_bullet_nums") == {}
                and no_ammunition_event
            )
            checks = [
                _check("engine_move_capability", bool(initial_move_actions)),
                _check("agent_move_capability", bool(initial_move_actions)),
                _check("human_move_capability", human_move),
                _check("move_command_applied", move_command.status is CommandStatus.APPLIED),
                _check("visibility_refreshed", visibility_refreshed),
                _check("engine_attack_capability", len(attack_actions) == 1),
                _check("agent_attack_capability", len(attack_actions) == 1),
                _check("human_attack_capability", human_attack),
                _check("command_applied", attack_command.status is CommandStatus.APPLIED),
                _check(
                    "formal_weapon76_resolution",
                    any(
                        value.get("event_type") == "combat_result_looked_up"
                        and isinstance(value.get("data"), Mapping)
                        and value["data"].get("weapon_id") == weapon_id
                        for value in events
                    ),
                ),
                _check("attacker_consumed", attacker_after.get("alive") is False and len(consumed) == 1),
                _check("no_ammunition_key", no_ammunition_key),
                _check("strict_replay", verification.matches),
            ]
            report = {
                "demo_id": demo.demo_id,
                "status": "PASS",
                "scenario_id": bundle.scenario_identity.scenario_id,
                "features": list(demo.features),
                "seed": seed,
                "attacker_id": attacker_id,
                "target_id": target_id,
                "weapon_id": weapon_id,
                "command_status": attack_command.status.value,
                "attacker_consumed": attacker_after.get("alive") is False,
                "target_blood_after": target_after.get("blood"),
                "strict_replay_matches": verification.matches,
            }
            return _complete_demo(
                publication.output_dir,
                report,
                {
                    "attacker_after": dict(attacker_after),
                    "target_after": dict(target_after),
                    "events": list(events),
                },
                checks,
                replay_path=publication.replay_path,
            )
        finally:
            session.close()


def _run_loitering_munition_expiry(
    demo: DemoDescriptor,
    expected: Mapping[str, object],
    bundle: ScenarioBundle,
    *,
    output_dir: Path,
    seed: int,
    attacker_id: int,
    overwrite: bool,
) -> dict[str, object]:
    outcome = _object(expected.get("outcome"), "outcome")
    expiry_time_ms = _exact_int(outcome.get("expiry_time_ms"), "expiry_time_ms")
    with tempfile.TemporaryDirectory(prefix="wargame-demo-library.") as storage:
        session = _session(
            bundle, Path(storage), demo, tick_interval_ms=expiry_time_ms
        )
        session.reset(seed=seed)
        try:
            session.advance_tick()
            publication, verification = _publish_and_verify(
                session, bundle, output_dir, overwrite=overwrite
            )
            frame = _last_green_frame(publication.replay_path)
            attacker_after = _green_operator(frame, attacker_id)
            events = _all_green_events(publication.replay_path)
            expiry_events = tuple(
                value for value in events
                if value.get("event_type") == "unit_destroyed"
                and value.get("actor_id") == attacker_id
                and isinstance(value.get("data"), Mapping)
                and value["data"].get("reason_code") == "LIFECYCLE_EXPIRED"
            )
            checks = [
                _check("authoritative_expiry", len(expiry_events) == 1),
                _check("attacker_expired", attacker_after.get("alive") is False),
                _check("strict_replay", verification.matches),
            ]
            report = {
                "demo_id": demo.demo_id,
                "status": "PASS",
                "scenario_id": bundle.scenario_identity.scenario_id,
                "features": list(demo.features),
                "seed": seed,
                "attacker_id": attacker_id,
                "expiry_time_ms": expiry_time_ms,
                "attacker_expired": attacker_after.get("alive") is False,
                "strict_replay_matches": verification.matches,
            }
            return _complete_demo(
                publication.output_dir,
                report,
                {"attacker_after": dict(attacker_after), "events": list(events)},
                checks,
                replay_path=publication.replay_path,
            )
        finally:
            session.close()


def _run_guided_fire(
    demo: DemoDescriptor,
    expected: Mapping[str, object],
    bundle: ScenarioBundle,
    *,
    output_dir: Path,
    seed: int,
    tick_interval_ms: int,
    overwrite: bool,
) -> dict[str, object]:
    actors = _object(expected.get("actors"), "actors")
    side = _text(expected.get("side"), "side").lower()
    guider_id = _exact_int(actors.get("guider_id"), "guider_id")
    shooter_id = _exact_int(actors.get("shooter_id"), "shooter_id")
    weapon_id = _exact_int(actors.get("weapon_id"), "weapon_id")
    target_id = _exact_int(actors.get("target_id"), "target_id")
    profile_id = _text(
        actors.get("guidance_profile_id"), "guidance_profile_id"
    )
    with tempfile.TemporaryDirectory(prefix="wargame-demo-library.") as storage:
        session = RealtimeAgentSession(
            _DemoMatchFactory(
                bundle,
                storage_root=Path(storage),
                tick_interval_ms=tick_interval_ms,
            ),
            run_namespace=f"demo-{demo.demo_id}",
            tick_interval_ms=tick_interval_ms,
            clock=ManualRealtimeClock(),
        )
        reset = session.reset(seed=seed)
        binding = reset.red_binding if side == "red" else reset.blue_binding
        seat_view = reset.red if side == "red" else reset.blue
        try:
            observation = seat_view.observation
            friendlies = {
                value["operator_id"]: value
                for value in observation.get("friendly_operators", ())
                if isinstance(value, Mapping)
                and type(value.get("operator_id")) is int
            }
            guider_seen = _seen_target(friendlies, guider_id, target_id)
            shooter_seen = _seen_target(friendlies, shooter_id, target_id)
            expected_preconditions = _object(
                expected.get("preconditions"), "preconditions"
            )
            _expect(
                guider_seen,
                expected_preconditions.get("guider_observes_target"),
                "guider observation",
            )
            _expect(
                shooter_seen,
                expected_preconditions.get("shooter_observes_target"),
                "shooter observation",
            )

            options = tuple(
                value for value in seat_view.capabilities.frozen_actions
                if _guided_action_matches(
                    value,
                    guider_id=guider_id,
                    shooter_id=shooter_id,
                    weapon_id=weapon_id,
                    target_id=target_id,
                )
            )
            if len(options) != 1:
                raise RuntimeError(
                    "expected exactly one Engine-issued GUIDED capability"
                )
            base = session.operation_capabilities(binding, [guider_id])
            _issued, controller_view = session.controller_snapshot(binding)
            human = build_human_capabilities(
                base,
                controller_view,
                [guider_id],
                weapon_profiles=source_weapon_profiles(bundle.scenario.metadata),
            )
            human_options = tuple(
                value for value in human["guided_fire"]["options"]
                if value["guider_id"] == guider_id
                and value["shooter_id"] == shooter_id
                and value["weapon_id"] == weapon_id
                and value["target_id"] == target_id
            )
            if len(human_options) != 1:
                raise RuntimeError("Human capability projection is unavailable")

            operation = SeatOperation(
                "GUIDED_FIRE",
                (guider_id,),
                target_id=target_id,
                fire_mode="GUIDED",
                guider_id=guider_id,
                shooter_id=shooter_id,
                weapon_id=weapon_id,
            )
            receipt = session.submit_operation(
                binding,
                observation_version=seat_view.observation_version,
                operation=operation,
            )
            if not receipt.accepted:
                raise RuntimeError(f"GUIDED command rejected: {receipt.error_code}")
            tick = session.advance_tick()
            command = next(
                value for value in session.command_events
                if value.command_sequence == receipt.command_sequence
            )
            if command.status is not CommandStatus.APPLIED:
                raise RuntimeError(f"GUIDED command was not applied: {command.status}")
            event_type = _text(
                _object(expected.get("outcome"), "outcome").get("event_type"),
                "event_type",
            )
            publication, verification = _publish_and_verify(
                session,
                bundle,
                output_dir,
                overwrite=overwrite,
            )
            judge_events = _green_events_from_archive(publication.replay_path)
            if not any(event.get("event_type") == event_type for event in judge_events):
                raise RuntimeError(f"expected Engine event is absent: {event_type}")
            report: dict[str, object] = {
                "demo_id": demo.demo_id,
                "status": "PASS",
                "scenario_id": bundle.scenario_identity.scenario_id,
                "features": list(demo.features),
                "seed": seed,
                "guider_id": guider_id,
                "shooter_id": shooter_id,
                "weapon_id": weapon_id,
                "target_id": target_id,
                "guidance_profile_id": profile_id,
                "guider_observes_target": guider_seen,
                "shooter_observes_target": shooter_seen,
                "human_guided_capability": True,
                "command_status": command.status.value,
                "tick_index": tick.tick_index,
                "event_type": event_type,
                "strict_replay_matches": verification.matches,
                "output_dir": str(publication.output_dir),
                "replay": str(publication.replay_path),
            }
            checks = [
                _check("guider_observes_target", guider_seen),
                _check("shooter_does_not_observe_target", not shooter_seen),
                _check("engine_guided_capability", len(options) == 1),
                _check("agent_guided_capability", len(options) == 1),
                _check("human_guided_capability", len(human_options) == 1),
                _check("command_applied", command.status is CommandStatus.APPLIED),
                _check("guided_fire_event", True),
                _check("strict_replay", verification.matches),
            ]
            return _complete_demo(
                publication.output_dir,
                report,
                {"events": list(judge_events)},
                checks,
                replay_path=publication.replay_path,
            )
        finally:
            session.close()


def _run_visibility(
    demo: DemoDescriptor,
    expected: Mapping[str, object],
    bundle: ScenarioBundle,
    *,
    output_dir: Path,
    seed: int,
    tick_interval_ms: int,
    overwrite: bool,
) -> dict[str, object]:
    scenario = bundle.scenario
    engine = NativeEngine(scenario)
    try:
        engine.reset(seed)
        observation = engine.observe(_text(expected.get("side"), "side"))
        visible_by_observer = {
            item.operator_id: set(item.observed_enemy_ids)
            for item in observation.friendly_operators
        }
        operators = {item.operator_id: item for item in scenario.operators}
        cells = {item.hex_id: item for item in scenario.map.hexes}
        policy = load_source_movement_policy()
        profile = HexMapProfile({
            item.hex_id: HexCell(
                item.hex_id,
                float(item.elevation),
                policy.terrain(item.terrain).value,
            )
            for item in scenario.map.hexes
        })
        case_evidence = []
        checks = []
        for case in _objects(expected.get("cases"), "cases"):
            case_id = _text(case.get("case_id"), "case_id")
            observer_id = _exact_int(case.get("observer_id"), "observer_id")
            target_id = _exact_int(case.get("target_id"), "target_id")
            observer = operators[observer_id]
            target = operators[target_id]
            if observer.current_hex is None or target.current_hex is None:
                raise RuntimeError("visibility demo operator lacks current_hex")
            target_cell = cells[target.current_hex]
            result = evaluate_visibility(
                profile,
                observer_hex=observer.current_hex,
                target_hex=target.current_hex,
                observer_domain=UnitDomain(observer.operator_type.upper()),
                target_domain=UnitDomain(target.operator_type.upper()),
                target_concealed=bool(
                    target.raw_attributes.get("concealed", False)
                ),
                target_terrain=policy.terrain(target_cell.terrain).value,
            )
            actual_contact = target_id in visible_by_observer[observer_id]
            terrain_modifier = any(
                value.get("rule") == "target_terrain_halves_observation"
                for value in result.modifiers
            )
            expected_observable = _exact_bool(
                case.get("observable"), "observable"
            )
            expected_blocking = tuple(
                _exact_int(value, "blocking_hexes[]")
                for value in _sequence(case.get("blocking_hexes"), "blocking_hexes")
            )
            evidence = {
                "case_id": case_id,
                "observer_id": observer_id,
                "target_id": target_id,
                "observer_hex": observer.current_hex,
                "target_hex": target.current_hex,
                "observable": result.observable,
                "real_observation_contact": actual_contact,
                "reason": result.reason,
                "line_of_sight": result.line_of_sight,
                "distance": result.distance,
                "observation_limit": result.observation_limit,
                "blocking_hexes": list(result.blocking_hexes),
                "terrain_modifier": terrain_modifier,
                "modifiers": [dict(value) for value in result.modifiers],
            }
            case_evidence.append(evidence)
            checks.extend((
                _check(f"{case_id}.observable", result.observable is expected_observable),
                _check(f"{case_id}.observation_consistent", actual_contact is expected_observable),
                _check(f"{case_id}.reason", result.reason == case.get("reason")),
                _check(f"{case_id}.blocking_hexes", result.blocking_hexes == expected_blocking),
                _check(
                    f"{case_id}.terrain_modifier",
                    terrain_modifier is _exact_bool(
                        case.get("terrain_modifier"), "terrain_modifier"
                    ),
                ),
            ))
        observation_consistent = all(
            value["observable"] == value["real_observation_contact"]
            for value in case_evidence
        )
        checks.extend((
            _check("agent_visibility_observation", observation_consistent),
            _check("human_visibility_observation", observation_consistent),
        ))
    finally:
        engine.close()

    with tempfile.TemporaryDirectory(prefix="wargame-demo-library.") as storage:
        session = _session(
            bundle, Path(storage), demo, tick_interval_ms=tick_interval_ms
        )
        session.reset(seed=seed)
        try:
            session.advance_tick()
            publication, verification = _publish_and_verify(
                session, bundle, output_dir, overwrite=overwrite
            )
            checks.append(_check("strict_replay", verification.matches))
            report = {
                "demo_id": demo.demo_id,
                "status": "PASS",
                "scenario_id": bundle.scenario_identity.scenario_id,
                "features": list(demo.features),
                "seed": seed,
                "case_count": len(case_evidence),
                "strict_replay_matches": verification.matches,
            }
            result = _complete_demo(
                publication.output_dir,
                report,
                {"cases": case_evidence},
                checks,
                replay_path=publication.replay_path,
            )
            _write_json(
                publication.output_dir / "visibility_evidence.json",
                {"demo_id": demo.demo_id, "cases": case_evidence},
            )
            return result
        finally:
            session.close()


def _run_objective_control(
    demo: DemoDescriptor,
    expected: Mapping[str, object],
    bundle: ScenarioBundle,
    *,
    output_dir: Path,
    seed: int,
    tick_interval_ms: int,
    overwrite: bool,
) -> dict[str, object]:
    objective_id = _exact_int(expected.get("objective_id"), "objective_id")
    actors = _object(expected.get("actors"), "actors")
    red_id = _exact_int(actors.get("red_id"), "red_id")
    blue_id = _exact_int(actors.get("blue_id"), "blue_id")
    authority_cases = _objective_authority_probe(
        bundle, seed=seed, objective_id=objective_id,
        red_id=red_id, blue_id=blue_id,
        tick_interval_ms=tick_interval_ms,
    )
    expected_cases = {
        _text(value.get("case_id"), "case_id"): value
        for value in _objects(expected.get("cases"), "cases")
    }
    checks = []
    for case_id, evidence in authority_cases.items():
        contract = expected_cases[case_id]
        checks.extend((
            _check(
                f"{case_id}.controller",
                evidence["controlling_side"] == contract.get("controlling_side"),
            ),
            _check(
                f"{case_id}.score",
                _score_delta_matches(
                    evidence["score_delta"],
                    contract.get("score_increases_for"),
                ),
            ),
        ))

    with tempfile.TemporaryDirectory(prefix="wargame-demo-library.") as storage:
        session = _session(
            bundle, Path(storage), demo, tick_interval_ms=tick_interval_ms
        )
        reset = session.reset(seed=seed)
        try:
            agent_move = any(
                isinstance(value, Mapping)
                and isinstance(value.get("action"), Mapping)
                and value["action"].get("action_type") == "MOVE"
                and value["action"].get("actor_id") == red_id
                for value in reset.red.capabilities.frozen_actions
            )
            base = session.operation_capabilities(reset.red_binding, [red_id])
            _issued, controller = session.controller_snapshot(reset.red_binding)
            human = build_human_capabilities(
                base,
                controller,
                [red_id],
                weapon_profiles=source_weapon_profiles(bundle.scenario.metadata),
            )
            human_move = any(
                value.get("capability_type") == "MOVE"
                for value in human["capabilities"]
            )
            checks.extend((
                _check("agent_objective_move_capability", agent_move),
                _check("human_objective_move_capability", human_move),
            ))
            red_entry = session.submit_operation(
                reset.red_binding,
                observation_version=reset.red.observation_version,
                operation=SeatOperation("MOVE", (red_id,), destination_hex=102),
            )
            if not red_entry.accepted:
                raise RuntimeError("objective entry MOVE was rejected")
            first = _advance_until_positions(
                session, {red_id: 102, blue_id: 103}, limit=30
            )
            realtime_cases = {
                "red_enters": {
                    "tick": first.tick_index,
                    "controlling_side": _objective_control(
                        first.red.observation, objective_id
                    ),
                }
            }
            first_ready = _advance_until_stopped(session, red_id, limit=80)
            red_move = session.submit_operation(
                reset.red_binding,
                observation_version=first_ready.red.observation_version,
                operation=SeatOperation("MOVE", (red_id,), destination_hex=101),
            )
            blue_move = session.submit_operation(
                reset.blue_binding,
                observation_version=first_ready.blue.observation_version,
                operation=SeatOperation("MOVE", (blue_id,), destination_hex=102),
            )
            if not red_move.accepted or not blue_move.accepted:
                raise RuntimeError("objective transfer MOVE was rejected")
            transferred = _advance_until_positions(
                session, {red_id: 101, blue_id: 102}, limit=30
            )
            realtime_cases["blue_enters"] = {
                "tick": transferred.tick_index,
                "controlling_side": _objective_control(
                    transferred.red.observation, objective_id
                ),
            }
            ready = _advance_until_stopped(session, red_id, limit=80)
            red_return = session.submit_operation(
                reset.red_binding,
                observation_version=ready.red.observation_version,
                operation=SeatOperation("MOVE", (red_id,), destination_hex=102),
            )
            if not red_return.accepted:
                raise RuntimeError("objective contest MOVE was rejected")
            contested = _advance_until_positions(
                session, {red_id: 102, blue_id: 102}, limit=30
            )
            realtime_cases["contested"] = {
                "tick": contested.tick_index,
                "controlling_side": _objective_control(
                    contested.red.observation, objective_id
                ),
            }
            for case_id, contract in expected_cases.items():
                checks.append(_check(
                    f"{case_id}.realtime_controller",
                    realtime_cases[case_id]["controlling_side"]
                    == contract.get("controlling_side"),
                ))
            publication, verification = _publish_and_verify(
                session, bundle, output_dir, overwrite=overwrite
            )
            event_type = _text(
                _object(expected.get("outcome"), "outcome").get("event_type"),
                "event_type",
            )
            checks.extend((
                _check(
                    "objective_control_events",
                    sum(
                        value == event_type
                        for case in authority_cases.values()
                        for value in case["event_types"]
                    )
                    >= 3,
                ),
                _check("strict_replay", verification.matches),
            ))
            report = {
                "demo_id": demo.demo_id,
                "status": "PASS",
                "scenario_id": bundle.scenario_identity.scenario_id,
                "features": list(demo.features),
                "seed": seed,
                "objective_id": objective_id,
                "strict_replay_matches": verification.matches,
            }
            return _complete_demo(
                publication.output_dir,
                report,
                {
                    "authority_cases": authority_cases,
                    "realtime_cases": realtime_cases,
                    "objective_event_type": event_type,
                },
                checks,
                replay_path=publication.replay_path,
            )
        finally:
            session.close()


def _run_indirect_fire(
    demo: DemoDescriptor,
    expected: Mapping[str, object],
    bundle: ScenarioBundle,
    *,
    output_dir: Path,
    seed: int,
    tick_interval_ms: int,
    overwrite: bool,
) -> dict[str, object]:
    side = _text(expected.get("side"), "side").lower()
    actors = _object(expected.get("actors"), "actors")
    shooter_id = _exact_int(actors.get("shooter_id"), "shooter_id")
    target_id = _exact_int(actors.get("target_id"), "target_id")
    weapon_id = _exact_int(actors.get("weapon_id"), "weapon_id")
    planned_hex = _exact_int(actors.get("planned_hex"), "planned_hex")
    expected_events = tuple(
        _text(value, "event_types[]")
        for value in _sequence(
            _object(expected.get("outcome"), "outcome").get("event_types"),
            "event_types",
        )
    )
    authority_probe = _indirect_authority_probe(
        bundle,
        seed=seed,
        side=side,
        shooter_id=shooter_id,
        weapon_id=weapon_id,
        target_id=target_id,
        planned_hex=planned_hex,
        tick_interval_ms=tick_interval_ms,
    )
    with tempfile.TemporaryDirectory(prefix="wargame-demo-library.") as storage:
        session = _session(
            bundle, Path(storage), demo, tick_interval_ms=tick_interval_ms
        )
        reset = session.reset(seed=seed)
        binding = reset.red_binding if side == "red" else reset.blue_binding
        seat_view = reset.red if side == "red" else reset.blue
        try:
            indirect = seat_view.capabilities.operation_summary.get(
                "indirect_fire", {}
            )
            authority_options = tuple(
                value for value in indirect.get("capabilities", ())
                if isinstance(value, Mapping)
                and value.get("status") == "AVAILABLE"
                and value.get("shooter_id") == shooter_id
                and value.get("weapon_id") == weapon_id
            ) if isinstance(indirect, Mapping) else ()
            base = session.operation_capabilities(binding, [shooter_id])
            _issued, controller = session.controller_snapshot(binding)
            human = build_human_capabilities(
                base,
                controller,
                [shooter_id],
                weapon_profiles=source_weapon_profiles(bundle.scenario.metadata),
            )
            human_options = tuple(
                value for value in human["indirect_fire"]["options"]
                if value["shooter_id"] == shooter_id
                and value["weapon_id"] == weapon_id
            )
            receipt = session.submit_operation(
                binding,
                observation_version=seat_view.observation_version,
                operation=SeatOperation(
                    "INDIRECT_FIRE",
                    (shooter_id,),
                    target_hex=planned_hex,
                    fire_mode="INDIRECT",
                    shooter_id=shooter_id,
                    weapon_id=weapon_id,
                ),
            )
            if not receipt.accepted:
                raise RuntimeError(
                    f"INDIRECT command rejected: {receipt.error_code}"
                )
            tick = session.advance_tick()
            command = _command_event(session, receipt.command_sequence)
            while tick.red.sim_time_ms < 450_000:
                tick = session.advance_tick()
            publication, verification = _publish_and_verify(
                session, bundle, output_dir, overwrite=overwrite
            )
            events = _all_green_events(publication.replay_path)
            event_types = tuple(authority_probe["event_types"])
            target_changed = bool(authority_probe["target_state_changed"])
            checks = [
                _check("engine_indirect_capability", len(authority_options) == 1),
                _check("agent_indirect_capability", len(authority_options) == 1),
                _check("human_indirect_capability", len(human_options) == 1),
                _check("command_applied", command.status is CommandStatus.APPLIED),
                *(
                    _check(f"event.{event_type}", event_type in event_types)
                    for event_type in expected_events
                ),
                _check("target_state_changed", target_changed),
                _check("strict_replay", verification.matches),
            ]
            report = {
                "demo_id": demo.demo_id,
                "status": "PASS",
                "scenario_id": bundle.scenario_identity.scenario_id,
                "features": list(demo.features),
                "seed": seed,
                "shooter_id": shooter_id,
                "weapon_id": weapon_id,
                "planned_hex": planned_hex,
                "target_id": target_id,
                "human_indirect_capability": True,
                "agent_indirect_capability": True,
                "command_status": command.status.value,
                "target_state_changed": target_changed,
                "strict_replay_matches": verification.matches,
            }
            return _complete_demo(
                publication.output_dir,
                report,
                {
                    "authority_probe": authority_probe,
                    "recorded_green_events": list(events),
                },
                checks,
                replay_path=publication.replay_path,
            )
        finally:
            session.close()


def _session(
    bundle: ScenarioBundle,
    storage_root: Path,
    demo: DemoDescriptor,
    *,
    tick_interval_ms: int,
) -> RealtimeAgentSession:
    return RealtimeAgentSession(
        _DemoMatchFactory(
            bundle,
            storage_root=storage_root,
            tick_interval_ms=tick_interval_ms,
        ),
        run_namespace=f"demo-{demo.demo_id}",
        tick_interval_ms=tick_interval_ms,
        clock=ManualRealtimeClock(),
    )


def _publish_and_verify(
    session: RealtimeAgentSession,
    bundle: ScenarioBundle,
    output_dir: Path,
    *,
    overwrite: bool,
):
    publication = session.publish(
        output_dir,
        truncated=not (session.terminal or session.truncated),
        overwrite=overwrite,
        cancellation_reason=(
            None if session.terminal or session.truncated
            else RealtimeCancellationReason.RUNNER_LIMIT
        ),
    )
    verification = replay_source_archive(
        bundle.scenario,
        publication.replay_path,
        strict=True,
        map_runtime_data=bundle.map_runtime_data,
    )
    if not verification.matches:
        raise RuntimeError("strict Replay did not match")
    return publication, verification


def _complete_demo(
    output_dir: Path,
    report: Mapping[str, object],
    evidence: Mapping[str, object],
    checks: list[dict[str, object]],
    *,
    replay_path: Path,
    validation_status: str = "PASS",
) -> dict[str, object]:
    result = dict(report)
    result.update({
        "output_dir": str(output_dir),
        "replay": str(replay_path),
    })
    validation = {
        "demo": result["demo_id"],
        "status": validation_status,
        "checks": checks,
    }
    _write_json(output_dir / "evidence.json", evidence)
    _write_json(output_dir / "summary.json", result)
    _write_json(output_dir / "validation_report.json", validation)
    failures = [value["name"] for value in checks if value["result"] is not True]
    if failures:
        raise RuntimeError("demo validation failed: " + ", ".join(failures))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return result


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _check(name: str, result: bool) -> dict[str, object]:
    if type(result) is not bool:
        raise TypeError("validation result must be an exact bool")
    return {"name": name, "result": result}


def _expected_formation(value: object) -> dict[str, dict[str, object]]:
    if value is None:
        return {}
    formation = _object(value, "actors.formation")
    result: dict[str, dict[str, object]] = {}
    for role, raw_entry in formation.items():
        if not isinstance(role, str) or not role:
            raise ValueError("formation role must be a non-empty string")
        entry = _object(raw_entry, f"actors.formation.{role}")
        result[role] = {
            "operator_id": _exact_int(
                entry.get("operator_id"),
                f"actors.formation.{role}.operator_id",
            ),
            "operator_type": _text(
                entry.get("operator_type"),
                f"actors.formation.{role}.operator_type",
            ),
            "subtype": _text(
                entry.get("subtype"),
                f"actors.formation.{role}.subtype",
            ),
        }
    return result


def _scenario_formation_matches(
    bundle: ScenarioBundle,
    formation: Mapping[str, Mapping[str, object]],
) -> bool:
    operators = {
        value.operator_id: value for value in bundle.scenario.operators
    }
    return all(
        (operator := operators.get(int(entry["operator_id"]))) is not None
        and operator.side == "red"
        and operator.operator_type == entry["operator_type"]
        and operator.subtype == entry["subtype"]
        for entry in formation.values()
    )


def _command_event(
    session: RealtimeAgentSession,
    sequence: int | None,
):
    if sequence is None:
        raise RuntimeError("accepted command lacks sequence")
    return next(
        value for value in session.command_events
        if value.command_sequence == sequence
    )


def _friendly_operator(
    observation: Mapping[str, object], operator_id: int,
) -> Mapping[str, object]:
    values = observation.get("friendly_operators", ())
    if not isinstance(values, (list, tuple)):
        raise RuntimeError("observation lacks friendly operators")
    for value in values:
        if isinstance(value, Mapping) and value.get("operator_id") == operator_id:
            return value
    raise RuntimeError(f"friendly operator is absent: {operator_id}")


def _green_operator(
    frame: Mapping[str, object], operator_id: int,
) -> Mapping[str, object]:
    values = frame.get("operators", ())
    if not isinstance(values, list):
        raise RuntimeError("GREEN frame lacks operators")
    for value in values:
        if isinstance(value, Mapping) and value.get("obj_id") == operator_id:
            return value
    raise RuntimeError(f"GREEN operator is absent: {operator_id}")


def _direct_action_matches(
    value: object,
    *,
    attacker_id: int,
    target_id: int,
    weapon_id: int,
) -> bool:
    if not isinstance(value, Mapping):
        return False
    action = value.get("action")
    if not isinstance(action, Mapping):
        return False
    parameters = action.get("parameters")
    return (
        action.get("action_type") == "SHOOT"
        and action.get("actor_id") == attacker_id
        and action.get("target_id") == target_id
        and isinstance(parameters, Mapping)
        and parameters.get("weapon_id") == weapon_id
    )


def _boarding_action_matches(
    value: object,
    *,
    operation_type: str,
    actor_id: int,
    target_id: int,
) -> bool:
    if not isinstance(value, Mapping):
        return False
    action = value.get("action")
    return (
        isinstance(action, Mapping)
        and action.get("action_type") == operation_type
        and action.get("actor_id") == actor_id
        and action.get("target_id") == target_id
    )


def boarding_basic_agent_validator(
    seat_view: object,
    *,
    operation_type: str,
    actor_id: int,
    target_id: int,
) -> SeatOperation:
    """Convert exactly one Engine-issued boarding capability to an operation.

    The validator intentionally consumes only the detached realtime seat view;
    it never reads mutable authoritative state and performs no boarding-rule
    inference of its own.
    """

    capabilities = getattr(seat_view, "capabilities", None)
    records = getattr(capabilities, "frozen_actions", ())
    matches = tuple(
        value for value in records
        if _boarding_action_matches(
            value,
            operation_type=operation_type,
            actor_id=actor_id,
            target_id=target_id,
        )
    )
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one Engine-issued {operation_type} capability"
        )
    return SeatOperation(
        operation_type,
        (actor_id,),
        target_id=target_id,
    )


def _objective_authority_probe(
    bundle: ScenarioBundle,
    *,
    seed: int,
    objective_id: int,
    red_id: int,
    blue_id: int,
    tick_interval_ms: int,
) -> dict[str, dict[str, object]]:
    engine = NativeEngine(bundle.scenario)
    try:
        engine.reset(seed)
        red_entry = _find_move_action(engine, "red", red_id, 102)
        cases = {"red_enters": _engine_advance_to(
            engine,
            (red_entry,),
            {red_id: 102, blue_id: 103},
            objective_id=objective_id,
            tick_interval_ms=tick_interval_ms,
        )}
        _wait_for_move_action(
            engine,
            "red",
            red_id,
            101,
            tick_interval_ms=tick_interval_ms,
            limit=80,
        )
        red_out = _find_move_action(engine, "red", red_id, 101)
        blue_in = _find_move_action(engine, "blue", blue_id, 102)
        cases["blue_enters"] = _engine_advance_to(
            engine,
            (red_out, blue_in),
            {red_id: 101, blue_id: 102},
            objective_id=objective_id,
            tick_interval_ms=tick_interval_ms,
        )
        red_in = _wait_for_move_action(
            engine,
            "red",
            red_id,
            102,
            tick_interval_ms=tick_interval_ms,
            limit=80,
        )
        cases["contested"] = _engine_advance_to(
            engine,
            (red_in,),
            {red_id: 102, blue_id: 102},
            objective_id=objective_id,
            tick_interval_ms=tick_interval_ms,
        )
        return cases
    finally:
        engine.close()


def _indirect_authority_probe(
    bundle: ScenarioBundle,
    *,
    seed: int,
    side: str,
    shooter_id: int,
    weapon_id: int,
    target_id: int,
    planned_hex: int,
    tick_interval_ms: int,
) -> dict[str, object]:
    """Execute the official Engine path without substituting recorded state."""

    engine = NativeEngine(bundle.scenario)
    try:
        engine.reset(seed)
        before = engine.snapshot()
        capability = next(
            value for value in engine.get_indirect_fire_capabilities(side)
            if value.shooter_id == shooter_id
            and value.weapon_id == weapon_id
            and value.status.value == "AVAILABLE"
        )
        action = engine.indirect_fire_action(capability, planned_hex)
        events = []
        result = engine.step([action], delta_time_ms=tick_interval_ms)
        events.extend(result.events)
        while engine.snapshot()["world"]["elapsed_time_ms"] < 450_000:
            result = engine.step([], delta_time_ms=tick_interval_ms)
            events.extend(result.events)
        after = engine.snapshot()
        before_target = before["world"]["operators"][str(target_id)]
        after_target = after["world"]["operators"][str(target_id)]
        return {
            "event_types": [event.event_type.value for event in events],
            "events": [event.to_dict() for event in events],
            "target_state_changed": (
                before_target["blood"] != after_target["blood"]
                or before_target["alive"] != after_target["alive"]
            ),
            "world_hash": canonical_hash(after["world"]),
            "rng_hash": canonical_hash(after["rng"]),
        }
    finally:
        engine.close()


def _find_move_action(
    engine: NativeEngine,
    side: str,
    actor_id: int,
    destination_hex: int,
):
    matches = tuple(
        value.action for value in engine.valid_actions(side)
        if value.action.actor_id == actor_id
        and value.action.action_type.value == "MOVE"
        and value.action.parameters.get("path") == [destination_hex]
    )
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one MOVE for {actor_id} to {destination_hex}"
        )
    return matches[0]


def _wait_for_move_action(
    engine: NativeEngine,
    side: str,
    actor_id: int,
    destination_hex: int,
    *,
    tick_interval_ms: int,
    limit: int,
):
    for _ in range(limit + 1):
        matches = tuple(
            value.action for value in engine.valid_actions(side)
            if value.action.actor_id == actor_id
            and value.action.action_type.value == "MOVE"
            and value.action.parameters.get("path") == [destination_hex]
        )
        if len(matches) == 1:
            return matches[0]
        engine.step([], delta_time_ms=tick_interval_ms)
    raise RuntimeError("operator did not become ready for objective return")


def _engine_advance_to(
    engine: NativeEngine,
    initial_actions: tuple[object, ...],
    positions: Mapping[int, int],
    *,
    objective_id: int,
    tick_interval_ms: int,
    limit: int = 30,
) -> dict[str, object]:
    actions = initial_actions
    for _ in range(limit):
        before = engine.snapshot()
        result = engine.step(actions, delta_time_ms=tick_interval_ms)
        actions = ()
        after = engine.snapshot()
        if _snapshot_positions(after, positions):
            return _objective_probe_record(
                before, after, objective_id, result.events
            )
    raise RuntimeError("objective movement did not reach expected positions")


def _snapshot_positions(
    snapshot: Mapping[str, object], positions: Mapping[int, int],
) -> bool:
    world = _object(snapshot.get("world"), "world")
    operators = _object(world.get("operators"), "operators")
    return all(
        _object(operators.get(str(operator_id)), "operator").get("current_hex")
        == target_hex
        for operator_id, target_hex in positions.items()
    )


def _objective_probe_record(
    before: Mapping[str, object],
    after: Mapping[str, object],
    objective_id: int,
    events: object,
) -> dict[str, object]:
    before_world = _object(before.get("world"), "world")
    after_world = _object(after.get("world"), "world")
    before_scores = _snapshot_scores(before_world)
    after_scores = _snapshot_scores(after_world)
    objectives = _object(after_world.get("objectives"), "objectives")
    objective = _object(objectives.get(str(objective_id)), "objective")
    return {
        "tick": after_world.get("tick"),
        "controlling_side": objective.get("controlling_side"),
        "scores": after_scores,
        "score_delta": {
            side: after_scores[side] - before_scores[side]
            for side in sorted(after_scores)
        },
        "event_types": [value.event_type.value for value in events],
    }


def _snapshot_scores(world: Mapping[str, object]) -> dict[str, int]:
    sides = _object(world.get("sides"), "sides")
    return {
        side: _exact_int(
            _object(value, "side").get("objective_score"),
            "objective_score",
        )
        for side, value in sorted(sides.items())
    }


def _score_delta_matches(
    delta: object, expected_side: object,
) -> bool:
    if not isinstance(delta, Mapping):
        return False
    positive = sorted(
        str(side) for side, value in delta.items()
        if type(value) is int and value > 0
    )
    return positive == ([] if expected_side is None else [str(expected_side)])


def _advance_until_positions(
    session: RealtimeAgentSession,
    positions: Mapping[int, int],
    *,
    limit: int,
):
    for _ in range(limit):
        tick = session.advance_tick()
        units = {
            value["operator_id"]: value
            for observation in (tick.red.observation, tick.blue.observation)
            for value in observation.get("friendly_operators", ())
            if isinstance(value, Mapping)
            and type(value.get("operator_id")) is int
        }
        if all(
            units.get(operator_id, {}).get("position")
            == [float(target_hex % 100), float(target_hex // 100)]
            for operator_id, target_hex in positions.items()
        ):
            return tick
    raise RuntimeError("realtime movement did not reach expected positions")


def _advance_until_stopped(
    session: RealtimeAgentSession,
    operator_id: int,
    *,
    limit: int,
):
    for _ in range(limit):
        tick = session.advance_tick()
        unit = _friendly_operator(tick.red.observation, operator_id)
        if unit.get("state") == "STOPPED":
            return tick
    raise RuntimeError("operator did not finish stop transition")


def _objective_control(
    observation: Mapping[str, object], objective_id: int,
) -> object:
    values = observation.get("objectives", ())
    if not isinstance(values, (list, tuple)):
        raise RuntimeError("observation lacks objectives")
    for value in values:
        if isinstance(value, Mapping) and value.get("objective_id") == objective_id:
            return value.get("controlling_side")
    raise RuntimeError(f"objective is absent: {objective_id}")


def _guided_action_matches(
    value: object,
    *,
    guider_id: int,
    shooter_id: int,
    weapon_id: int,
    target_id: int,
) -> bool:
    if not isinstance(value, Mapping):
        return False
    action = value.get("action")
    if not isinstance(action, Mapping):
        return False
    parameters = action.get("parameters")
    return (
        isinstance(parameters, Mapping)
        and action.get("action_type") == "FIRE"
        and action.get("actor_id") == guider_id
        and action.get("target_id") == target_id
        and parameters.get("fire_mode") == "GUIDED"
        and parameters.get("guider_id") == guider_id
        and parameters.get("shooter_id") == shooter_id
        and parameters.get("weapon_id") == weapon_id
    )


def _seen_target(
    friendlies: Mapping[int, Mapping[str, object]],
    observer_id: int,
    target_id: int,
) -> bool:
    observer = friendlies.get(observer_id)
    if observer is None:
        raise RuntimeError(f"friendly operator is absent: {observer_id}")
    seen = observer.get("observed_enemy_ids", ())
    if not isinstance(seen, (list, tuple)):
        return False
    return target_id in seen


def _events(value: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    result = value.get("result")
    events = result.get("events", ()) if isinstance(result, Mapping) else ()
    return tuple(item for item in events if isinstance(item, Mapping))


def _green_events_from_archive(path: Path) -> tuple[Mapping[str, object], ...]:
    return _events_from_green_frame(_last_green_frame(path))


def _last_green_frame(path: Path) -> Mapping[str, object]:
    return _green_frames(path)[-1]


def _green_frames(path: Path) -> tuple[Mapping[str, object], ...]:
    with ZipFile(path, "r") as archive:
        frames = sorted(
            (
                name for name in archive.namelist()
                if name.endswith(".json") and name[:-5].isdigit()
            ),
            key=lambda name: int(name[:-5]),
        )
        if not frames:
            raise RuntimeError("Replay contains no GREEN frames")
        values = tuple(
            json.loads(archive.read(name).decode("utf-8"))
            for name in frames
        )
    if not all(isinstance(value, Mapping) for value in values):
        raise RuntimeError("Replay GREEN frame must be an object")
    return values


def _events_from_green_frame(
    frame: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    judge_info = frame.get("judge_info", ()) if isinstance(frame, Mapping) else ()
    return tuple(
        event
        for item in judge_info
        if isinstance(item, Mapping)
        for event in _events(item)
    )


def _all_green_events(path: Path) -> tuple[Mapping[str, object], ...]:
    return tuple(
        event
        for frame in _green_frames(path)
        for event in _events_from_green_frame(frame)
    )


def _banner(demo: DemoDescriptor, expected: Mapping[str, object]) -> str:
    status = expected.get("status")
    expectation = (
        f"{status}: {expected.get('reason')}"
        if isinstance(status, str)
        else demo.purpose
    )
    return "\n".join((
        "====================",
        "Demo:",
        demo.demo_id,
        "",
        "Features:",
        ", ".join(demo.features),
        "",
        "Expected:",
        expectation,
        "====================",
    ))


def _list(registry: DemoRegistry) -> None:
    for demo in registry.descriptors:
        print(f"{demo.demo_id}\t{demo.name}\t{','.join(demo.features)}")


def _object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _objects(value: object, field: str) -> tuple[Mapping[str, object], ...]:
    sequence = _sequence(value, field)
    if not all(isinstance(item, Mapping) for item in sequence):
        raise ValueError(f"{field} must contain objects")
    return tuple(sequence)


def _sequence(value: object, field: str) -> tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be an array")
    return tuple(value)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _exact_int(value: object, field: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{field} must be an exact int")
    return value


def _exact_bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be an exact bool")
    return value


def _expect(actual: bool, expected: object, field: str) -> None:
    if type(expected) is not bool or actual is not expected:
        raise RuntimeError(f"{field} mismatch: expected={expected}, actual={actual}")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    registry = (
        DemoRegistry.load(args.registry)
        if args.registry is not None else load_default_demo_registry()
    )
    if args.demo == "list":
        _list(registry)
        return 0
    run_registered_demo(
        args.demo,
        registry=registry,
        output_dir=args.output_dir,
        seed=args.seed,
        tick_interval_ms=args.tick_interval_ms,
        overwrite=args.overwrite,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
