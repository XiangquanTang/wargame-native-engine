from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
from typing import Any, Mapping, Sequence

from wargame_native_engine.compat.source_frame.exporter import SourceFrameExporter
from wargame_native_engine.compat.source_frame.contract import (
    CURRENT_SOURCE_CONTRACT, LEGACY_SOURCE_CONTRACT, SOURCE_CONTRACT_KEY,
    require_source_contract,
)
from wargame_native_engine.compat.source_frame.models import SourceFrame
from wargame_native_engine.core.action import EngineAction
from wargame_native_engine.core.event import EventType, is_green_judge_event
from wargame_native_engine.io.serialization import canonical_hash, json_clone
from wargame_native_engine.observations.perspective import (
    ObservationBundle, PerspectiveContext, PerspectiveProjector,
)
from wargame_native_engine.rules.objective_capture import EXPLICIT_CAPTURE_MODE
from wargame_native_engine.core.world import WorldState
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.core.time import (
    require_positive_milliseconds,
    resolve_delta_time_ms,
)
from wargame_native_engine.version import (
    ADJUDICATION_SEMANTICS_VERSION,
    ENGINE_VERSION,
)


class SourceCompatibleEnvironment:
    """Source-frame facade over the one GameSession/NativeEngine step chain."""

    def __init__(self, session: GameSession, *, template: SourceFrame,
                 projector: PerspectiveProjector | None = None,
                 engine_version: str | None = None,
                 source_contract: str = CURRENT_SOURCE_CONTRACT) -> None:
        if engine_version is not None and (
            not isinstance(engine_version, str) or not engine_version.strip()
        ):
            raise ValueError("engine_version must be a non-empty string")
        self.source_contract = require_source_contract(source_contract)
        self.session = session
        self.session.source_frame_compatibility_version = self.source_contract
        self.template = template
        self.exporter = SourceFrameExporter()
        self.projector = projector or PerspectiveProjector()
        # Strict replay must reproduce the archived public identity even when
        # it is executed by a newer compatible engine implementation.
        self.engine_version = engine_version or ENGINE_VERSION
        self._setup = False
        self._last_bundle: ObservationBundle | None = None
        self._last_step_record: Mapping[str, Any] | None = None
        self._last_action_results: tuple[Mapping[str, Any], ...] = ()
        self._last_execution_events = ()
        self._execution_fact_frames: deque[Mapping[str, Any]] = deque(maxlen=512)
        self._execution_fact_frame_index = 0
        self._initial_state_hash: str | None = None
        self._desynchronized = False

    def setup(self, seed: int = 42) -> Mapping[str, Any]:
        return self.setup_views(seed).green

    def setup_views(self, seed: int = 42) -> ObservationBundle:
        world = self.session.reset(seed)
        self._setup = True
        self._desynchronized = False
        self._initial_state_hash = canonical_hash(world.to_dict())
        self._last_step_record = None
        self._last_action_results = ()
        self._last_execution_events = ()
        self._execution_fact_frames.clear()
        self._execution_fact_frame_index = 0
        try:
            snapshot = self.session.snapshot()
            green = self._export_green(
                world, actions=[], judge_info=[], snapshot=snapshot,
            )
            bundle = self._project(green, snapshot=snapshot)
        except BaseException:
            self._desynchronized = True
            raise
        self._last_bundle = bundle
        return bundle

    def reset(self, seed: int = 42) -> Mapping[str, Any]:
        return self.setup(seed)

    def restore_views(self, snapshot: Mapping[str, Any]) -> ObservationBundle:
        contract = require_source_contract(snapshot.get(
            SOURCE_CONTRACT_KEY, LEGACY_SOURCE_CONTRACT))
        world = self.session.restore(snapshot)
        self.source_contract = contract
        self.session.source_frame_compatibility_version = contract
        self._setup = True
        self._desynchronized = False
        self._initial_state_hash = canonical_hash(world.to_dict())
        self._last_step_record = None
        self._last_action_results = ()
        self._last_execution_events = ()
        self._execution_fact_frames.clear()
        self._execution_fact_frame_index = 0
        try:
            current_snapshot = self.session.snapshot()
            green = self._export_green(
                world, actions=[], judge_info=[], snapshot=current_snapshot,
            )
            bundle = self._project(green, snapshot=current_snapshot)
        except BaseException:
            self._desynchronized = True
            raise
        self._last_bundle = bundle
        return bundle

    def step(self, actions: Sequence[EngineAction], *,
             delta_time_seconds: int | float | None = None,
             delta_time_ms: int | None = None,
             ) -> tuple[Mapping[str, Any], bool]:
        milliseconds = resolve_delta_time_ms(
            delta_time_seconds=delta_time_seconds,
            delta_time_ms=delta_time_ms,
        )
        bundle, done = self.step_views(actions, delta_time_ms=milliseconds)
        return bundle.green, done

    def step_ms(self, actions: Sequence[EngineAction], *,
                delta_time_ms: int) -> tuple[Mapping[str, Any], bool]:
        return self.step(
            actions,
            delta_time_ms=require_positive_milliseconds(delta_time_ms),
        )

    def step_views(self, actions: Sequence[EngineAction], *,
                   delta_time_seconds: int | float | None = None,
                   delta_time_ms: int | None = None,
                   ) -> tuple[ObservationBundle, bool]:
        milliseconds = resolve_delta_time_ms(
            delta_time_seconds=delta_time_seconds,
            delta_time_ms=delta_time_ms,
        )
        if not self._setup:
            raise RuntimeError("environment_not_setup")
        self._require_synchronized()
        result = self.session.step(actions, delta_time_ms=milliseconds)
        # The authoritative step is committed.  Do not leave the preceding
        # record looking current if any export/projection work below fails.
        self._last_step_record = None
        self._last_action_results = ()
        self._last_execution_events = ()
        try:
            snapshot = self.session.snapshot()
            world = WorldState.from_dict(snapshot["world"])
            judge = [
                item.to_dict() for item in result.events
                if is_green_judge_event(item.event_type)
            ]
            action_records = []
            action_results = []
            for action, decision in zip(result.actions, result.decisions):
                record = action.to_dict()
                if not decision.accepted:
                    record.update({"accepted": False,
                                   "decision_code": decision.code})
                action_records.append(record)
                action_results.append({
                    "action_id": action.action_id,
                    "side": action.side,
                    "actor_id": action.actor_id,
                    "action_type": action.action_type.value,
                    "weapon_id": action.parameters.get("weapon_id"),
                    "accepted": decision.accepted,
                    "code": decision.code,
                })
            green = self._export_green(
                world, actions=action_records, judge_info=judge,
                snapshot=snapshot,
            )
            bundle = self._project(green, snapshot=snapshot)
            step_record = {
                "schema_version": "native-step/1.0",
                "step_index": int(result.tick_after),
                "tick_before": int(result.tick_before),
                "tick_after": int(result.tick_after),
                "delta_time_ms": int(result.delta_time_ms),
                "sim_time_ms": int(world.elapsed_time_ms),
                "ordered_actions": [item.to_dict() for item in result.actions],
                "action_sequences": list(result.action_sequences),
                "ordered_action_input": bool(result.ordered_action_input),
                "expected_state_hash": canonical_hash(snapshot["world"]),
            }
            detached_results = tuple(json_clone(action_results))
            detached_events = tuple(deepcopy(result.events))
            execution_facts = _build_execution_fact_frame(
                world,
                frame_index=self._execution_fact_frame_index + 1,
                action_results=detached_results,
                events=detached_events,
                side_order=self.session.scenario.side_order,
            )
        except BaseException:
            self._desynchronized = True
            raise
        self._last_action_results = detached_results
        self._last_execution_events = detached_events
        self._execution_fact_frames.append(execution_facts)
        self._execution_fact_frame_index += 1
        self._last_bundle = bundle
        self._last_step_record = step_record
        return bundle, world.done

    def step_views_ms(self, actions: Sequence[EngineAction], *,
                      delta_time_ms: int) -> tuple[ObservationBundle, bool]:
        return self.step_views(
            actions,
            delta_time_ms=require_positive_milliseconds(delta_time_ms),
        )

    @property
    def last_step_record(self) -> Mapping[str, Any] | None:
        return None if self._last_step_record is None else json_clone(
            self._last_step_record)

    @property
    def last_action_results(self) -> tuple[Mapping[str, Any], ...]:
        """Ephemeral adjudication results; intentionally not a replay field."""

        return tuple(json_clone(self._last_action_results))

    @property
    def last_execution_events(self):
        """Detached events from only the most recent committed Engine step.

        This bounded trusted-boundary projection supports runtime correlation;
        it is not added to replay or side observations.
        """

        return tuple(deepcopy(self._last_execution_events))

    def execution_fact_frames(
        self,
        side: str,
        *,
        first_frame_index: int | None = None,
        last_frame_index: int | None = None,
    ) -> tuple[Mapping[str, Any], ...]:
        """Return bounded trusted facts projected while each step was current.

        These detached records contain only the minimum OWN state needed by
        the lifecycle correlator.  They are not replay fields or side
        observations, and reading them does not reconstruct or retain a second
        ``WorldState``.
        """

        normalized = str(side).lower()
        if normalized not in self.session.scenario.side_order:
            raise ValueError("side must be declared by the scenario")
        if first_frame_index is not None and (
            type(first_frame_index) is not int or first_frame_index < 0
        ):
            raise ValueError("first_frame_index must be non-negative or None")
        if last_frame_index is not None and (
            type(last_frame_index) is not int or last_frame_index < 0
        ):
            raise ValueError("last_frame_index must be non-negative or None")
        values = []
        for frame in self._execution_fact_frames:
            frame_index = int(frame["frame_index"])
            if first_frame_index is not None and frame_index < first_frame_index:
                continue
            if last_frame_index is not None and frame_index > last_frame_index:
                continue
            values.append(frame["sides"][normalized])
        return tuple(deepcopy(values))

    @property
    def initial_state_hash(self) -> str:
        if self._initial_state_hash is None:
            raise RuntimeError("environment_not_setup")
        return self._initial_state_hash

    @property
    def last_bundle(self) -> ObservationBundle:
        if self._last_bundle is None:
            raise RuntimeError("environment_not_setup")
        return self._last_bundle

    @property
    def desynchronized(self) -> bool:
        """Whether authoritative state advanced without a complete export."""

        return self._desynchronized

    def _require_synchronized(self) -> None:
        if self._desynchronized:
            raise RuntimeError("source_environment_desynchronized_reset_required")

    def _export_green(self, world: WorldState, *,
                      actions: list[Mapping[str, Any]],
                      judge_info: list[Mapping[str, Any]],
                      snapshot: Mapping[str, Any] | None = None,
                      ) -> Mapping[str, Any]:
        if snapshot is None:
            snapshot = self.session.snapshot()
        by_actor: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for side in self.session.scenario.side_order:
            for value in self.session.valid_actions(side):
                key = "-1" if value.action.actor_id is None else str(
                    value.action.actor_id)
                by_actor[key].append(value.to_dict())
        adjudication = None if not judge_info else {"events": judge_info}
        capture_policy = snapshot.get("objective_capture_policy")
        return self.exporter.export(
            world, template=self.template, actions=actions,
            valid_actions={key: values for key, values in sorted(by_actor.items())},
            adjudication=adjudication,
            source_contract=self.source_contract,
            weapon_inventory=(tuple(
                value for side in self.session.scenario.side_order
                for value in self.session.own_weapon_inventory(side)
            ) if self.source_contract == CURRENT_SOURCE_CONTRACT else ()),
            project_objective_control=(
                isinstance(capture_policy, Mapping)
                and capture_policy.get("mode") == EXPLICIT_CAPTURE_MODE
            ),
        )

    def _project(self, green: Mapping[str, Any], *,
                 snapshot: Mapping[str, Any] | None = None,
                 ) -> ObservationBundle:
        if snapshot is None:
            snapshot = self.session.snapshot()
        return self.projector.project(green, PerspectiveContext(
            red=self.session.observe("red"), blue=self.session.observe("blue"),
            session_id=self.session.session_id,
            public_metadata=self._public_metadata(snapshot),
            own_weapon_inventory=(tuple(
                item for side in self.session.scenario.side_order
                for item in self.session.own_weapon_inventory(side)
            ) if self.source_contract == CURRENT_SOURCE_CONTRACT else ()),
        ))

    def _public_metadata(
        self, snapshot: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        if snapshot is None:
            snapshot = self.session.snapshot()
        value = {
            "engine_version": self.engine_version,
            "adjudication_semantics_version": (
                ADJUDICATION_SEMANTICS_VERSION),
            "ruleset_id": snapshot["ruleset_id"],
            "ruleset_version": snapshot["ruleset_version"],
            "ruleset_hash": snapshot["ruleset_hash"],
            "protocol_version": "source-compatible-environment/1.0",
            **json_clone(snapshot.get("movement_policy", {})),
            **json_clone(snapshot.get("adjudication_table_set", {})),
            "episode_seed": int(snapshot["seed"]),
            "state_hash": canonical_hash(snapshot["world"]),
        }
        if self.source_contract != LEGACY_SOURCE_CONTRACT:
            value[SOURCE_CONTRACT_KEY] = self.source_contract
        if "objective_capture_policy" in snapshot:
            value["objective_capture_policy"] = json_clone(
                snapshot["objective_capture_policy"]
            )
        if "map_runtime_data" in snapshot:
            value["map_runtime_data"] = json_clone(snapshot["map_runtime_data"])
        if "boarding_type_assignment_authority" in snapshot:
            boarding = snapshot["boarding_type_assignment_authority"]
            value["boarding_type_assignment_authority_version"] = boarding[
                "authority_version"
            ]
            value["boarding_type_assignment_authority_hash"] = boarding[
                "authority_hash"
            ]
            value["boarding_type_assignment_policy"] = boarding[
                "assignment_policy"
            ]
        if "operator_lifecycle_authority" in snapshot:
            lifecycle = snapshot["operator_lifecycle_authority"]
            value["operator_lifecycle_authority_version"] = lifecycle[
                "authority_version"
            ]
            value["operator_lifecycle_authority_hash"] = lifecycle[
                "authority_hash"
            ]
        if "loitering_munition_attack_authority" in snapshot:
            attack = snapshot["loitering_munition_attack_authority"]
            value["loitering_munition_attack_authority_version"] = attack[
                "authority_version"
            ]
            value["loitering_munition_attack_authority_hash"] = attack[
                "authority_hash"
            ]
        return value


def _build_execution_fact_frame(
    world: WorldState,
    *,
    frame_index: int,
    action_results: Sequence[Mapping[str, Any]],
    events: Sequence[Any],
    side_order: Sequence[str],
) -> Mapping[str, Any]:
    """Project one committed World while it is already available to export."""

    allowed_event_keys = {
        "action_id", "mission_id", "transition_id", "reason_code",
        "carrier_id", "passenger_id", "planned_hex", "cancellation_reason",
        "terminal_outcome", "event_time_ms",
    }
    side_facts: dict[str, Mapping[str, Any]] = {}
    for side in side_order:
        all_own_operators = tuple(
            value
            for value in sorted(
                world.operators.values(), key=lambda item: item.operator_id
            )
            if value.side == side
        )
        own_ids = {value.operator_id for value in all_own_operators}
        side_events = tuple(
            event for event in events if event.side == side
        )
        event_transition_ids = {
            str(event.data["transition_id"])
            for event in side_events
            if event.data.get("transition_id") is not None
        }
        event_mission_ids = {
            str(event.data["mission_id"])
            for event in side_events
            if event.data.get("mission_id") is not None
        }
        selected_transitions = tuple(
            item
            for item in world.boarding_transitions
            if item.actor_id in own_ids
            and item.carrier_id in own_ids
            and item.passenger_id in own_ids
            and (
                item.status.value == "ACTIVE"
                or item.transition_id in event_transition_ids
            )
        )
        selected_missions = tuple(
            item
            for item in world.indirect_fire_missions
            if item.shooter_id in own_ids
            and (
                item.phase.value in {"PLANNED", "FLYING", "ACTIVE"}
                or item.mission_id in event_mission_ids
            )
        )
        relevant_operator_ids = {
            int(value)
            for event in side_events
            for value in (
                event.actor_id,
                event.data.get("carrier_id"),
                event.data.get("passenger_id"),
            )
            if type(value) is int and value in own_ids
        }
        # Combat destruction facts are attributed to the attacking side, so
        # ``side_events`` alone cannot retain the destroyed OWN target after
        # the authoritative destruction transition clears its movement state.
        # Project only the committed OWN terminal state; do not forward the
        # opponent event, attacker identity, or adjudication evidence.
        relevant_operator_ids.update(
            event.target_id
            for event in events
            if event.event_type in {
                EventType.OPERATOR_DESTROYED,
                EventType.UNIT_DESTROYED,
            }
            and type(event.target_id) is int
            and event.target_id in own_ids
            and world.operators[event.target_id].alive is False
        )
        for transition in selected_transitions:
            relevant_operator_ids.update({
                transition.actor_id,
                transition.carrier_id,
                transition.passenger_id,
            })
        relevant_operator_ids.update(
            mission.shooter_id for mission in selected_missions
        )
        relevant_operator_ids.update(
            value.operator_id
            for value in all_own_operators
            if (
                not value.stopped
                or value.move_path
                or value.move_to_stop_remaining_ms > 0
            )
        )
        operators = tuple({
            "actor_id": value.operator_id,
            "alive": value.alive,
            "deployed": value.deployed,
            "current_hex": value.current_hex,
            "state": value.state,
            "stopped": value.stopped,
            "move_path": list(value.move_path),
            "movement_action_id": value.movement_action_id,
            "stop_transition_remaining_ms": value.move_to_stop_remaining_ms,
            "segment_progress_millionths": value.cur_pos_millionths,
            "mounted_on": value.mounted_on,
            "passenger_id": value.passenger_id,
            "movement_locked": value.movement_locked,
        } for value in all_own_operators
            if value.operator_id in relevant_operator_ids
        )
        transitions = tuple(
            item.to_dict()
            for item in selected_transitions
        )
        missions = tuple({
            "mission_id": item.mission_id,
            "shooter_id": item.shooter_id,
            "weapon_id": item.weapon_id,
            "planned_hex": item.planned_hex,
            "phase": item.phase.value,
            "remaining_flight_seconds": item.remaining_flight_seconds,
            "active_remaining_seconds": item.active_remaining_seconds,
            "cancellation_reason": (
                None if item.cancellation_reason is None
                else item.cancellation_reason.value
            ),
            "terminal_outcome": item.terminal_outcome,
        } for item in selected_missions
        )
        side_facts[str(side)] = {
            "schema_version": "native-operation-execution-facts/2.0",
            "side": str(side),
            "frame_index": frame_index,
            "sim_time_ms": int(world.elapsed_time_ms),
            "operators": operators,
            "boarding_transitions": transitions,
            "indirect_fire_missions": missions,
            "events": tuple({
                "event_type": event.event_type.value,
                "actor_id": event.actor_id,
                **{
                    key: json_clone(value)
                    for key, value in event.data.items()
                    if key in allowed_event_keys
                },
            } for event in side_events),
            "action_results": tuple(
                json_clone(value)
                for value in action_results
                if value.get("side") == side
            ),
        }
    return {
        "schema_version": "native-operation-execution-fact-frame/1.0",
        "frame_index": frame_index,
        "sim_time_ms": int(world.elapsed_time_ms),
        "sides": side_facts,
    }
