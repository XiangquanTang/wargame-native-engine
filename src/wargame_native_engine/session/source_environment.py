from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence

from wargame_native_engine.compat.source_frame.exporter import SourceFrameExporter
from wargame_native_engine.compat.source_frame.models import SourceFrame
from wargame_native_engine.core.action import EngineAction
from wargame_native_engine.io.serialization import canonical_hash, json_clone
from wargame_native_engine.observations.perspective import (
    ObservationBundle, PerspectiveContext, PerspectiveProjector,
)
from wargame_native_engine.core.world import WorldState
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.version import ENGINE_VERSION


class SourceCompatibleEnvironment:
    """Source-frame facade over the one GameSession/NativeEngine step chain."""

    def __init__(self, session: GameSession, *, template: SourceFrame,
                 projector: PerspectiveProjector | None = None) -> None:
        self.session = session
        self.template = template
        self.exporter = SourceFrameExporter()
        self.projector = projector or PerspectiveProjector()
        self._setup = False
        self._last_bundle: ObservationBundle | None = None
        self._last_step_record: Mapping[str, Any] | None = None
        self._initial_state_hash: str | None = None

    def setup(self, seed: int = 42) -> Mapping[str, Any]:
        return self.setup_views(seed).green

    def setup_views(self, seed: int = 42) -> ObservationBundle:
        world = self.session.reset(seed)
        self._setup = True
        self._initial_state_hash = canonical_hash(world.to_dict())
        self._last_step_record = None
        green = self._export_green(world, actions=[], judge_info=[])
        bundle = self._project(green)
        self._last_bundle = bundle
        return bundle

    def reset(self, seed: int = 42) -> Mapping[str, Any]:
        return self.setup(seed)

    def restore_views(self, snapshot: Mapping[str, Any]) -> ObservationBundle:
        world = self.session.restore(snapshot)
        self._setup = True
        self._initial_state_hash = canonical_hash(world.to_dict())
        self._last_step_record = None
        bundle = self._project(self._export_green(
            world, actions=[], judge_info=[]))
        self._last_bundle = bundle
        return bundle

    def step(self, actions: Sequence[EngineAction], *,
             delta_time_seconds: int | float = 1
             ) -> tuple[Mapping[str, Any], bool]:
        bundle, done = self.step_views(actions,
                                       delta_time_seconds=delta_time_seconds)
        return bundle.green, done

    def step_views(self, actions: Sequence[EngineAction], *,
                   delta_time_seconds: int | float = 1
                   ) -> tuple[ObservationBundle, bool]:
        if not self._setup:
            raise RuntimeError("environment_not_setup")
        result = self.session.step(actions,
                                   delta_time_seconds=delta_time_seconds)
        world = WorldState.from_dict(self.session.snapshot()["world"])
        judge = [item.to_dict() for item in result.events
                 if item.event_type.value.startswith("direct_fire_")
                 or item.event_type.value in {
                     "attack_level_selected", "attack_level_modified",
                     "dice_rolled", "roll_modifiers_applied",
                     "combat_result_looked_up", "losses_applied",
                     "suppression_applied", "unit_destroyed",
                     "weapon_cooldown_started", "ammunition_consumed"}]
        action_records = []
        for action, decision in zip(result.actions, result.decisions):
            record = action.to_dict()
            if not decision.accepted:
                record.update({"accepted": False,
                               "decision_code": decision.code})
            action_records.append(record)
        green = self._export_green(
            world, actions=action_records,
            judge_info=judge)
        bundle = self._project(green)
        self._last_bundle = bundle
        self._last_step_record = {
            "schema_version": "native-step/1.0",
            "step_index": int(result.tick_after),
            "tick_before": int(result.tick_before),
            "tick_after": int(result.tick_after),
            "delta_time_ms": int(result.delta_time_ms),
            "sim_time_ms": int(world.elapsed_time_ms),
            "ordered_actions": [item.to_dict() for item in result.actions],
            "action_sequences": list(result.action_sequences),
            "ordered_action_input": bool(result.ordered_action_input),
            "expected_state_hash": canonical_hash(world.to_dict()),
        }
        return bundle, world.done

    @property
    def last_step_record(self) -> Mapping[str, Any] | None:
        return None if self._last_step_record is None else json_clone(
            self._last_step_record)

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

    def _export_green(self, world: WorldState, *,
                      actions: list[Mapping[str, Any]],
                      judge_info: list[Mapping[str, Any]]) -> Mapping[str, Any]:
        by_actor: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for side in self.session.scenario.side_order:
            for value in self.session.valid_actions(side):
                key = "-1" if value.action.actor_id is None else str(
                    value.action.actor_id)
                by_actor[key].append(value.to_dict())
        adjudication = None if not judge_info else {"events": judge_info}
        return self.exporter.export(
            world, template=self.template, actions=actions,
            valid_actions={key: values for key, values in sorted(by_actor.items())},
            adjudication=adjudication)

    def _project(self, green: Mapping[str, Any]) -> ObservationBundle:
        return self.projector.project(green, PerspectiveContext(
            red=self.session.observe("red"), blue=self.session.observe("blue"),
            session_id=self.session.session_id,
            public_metadata=self._public_metadata(),
        ))

    def _public_metadata(self) -> Mapping[str, Any]:
        snapshot = self.session.snapshot()
        return {
            "engine_version": ENGINE_VERSION,
            "ruleset_id": snapshot["ruleset_id"],
            "ruleset_version": snapshot["ruleset_version"],
            "ruleset_hash": snapshot["ruleset_hash"],
            "protocol_version": "source-compatible-environment/1.0",
            **json_clone(snapshot.get("movement_policy", {})),
            **json_clone(snapshot.get("adjudication_table_set", {})),
            "episode_seed": int(snapshot["seed"]),
            "state_hash": canonical_hash(snapshot["world"]),
        }
