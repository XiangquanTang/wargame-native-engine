from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence

from wargame_native_engine.compat.source_frame.exporter import SourceFrameExporter
from wargame_native_engine.compat.source_frame.models import SourceFrame
from wargame_native_engine.core.action import EngineAction
from wargame_native_engine.core.world import WorldState
from wargame_native_engine.session.game_session import GameSession


class SourceCompatibleEnvironment:
    """Source-frame facade over the one GameSession/NativeEngine step chain."""

    def __init__(self, session: GameSession, *, template: SourceFrame) -> None:
        self.session = session
        self.template = template
        self.exporter = SourceFrameExporter()
        self._setup = False

    def setup(self, seed: int = 42) -> Mapping[str, Any]:
        world = self.session.reset(seed)
        self._setup = True
        return self._export(world, actions=[], judge_info=[])

    def reset(self, seed: int = 42) -> Mapping[str, Any]:
        return self.setup(seed)

    def step(self, actions: Sequence[EngineAction], *,
             delta_time_seconds: int | float = 1
             ) -> tuple[Mapping[str, Any], bool]:
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
        frame = self._export(
            world, actions=[item.to_dict() for item in result.actions],
            judge_info=judge)
        return frame, world.done

    def _export(self, world: WorldState, *,
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
