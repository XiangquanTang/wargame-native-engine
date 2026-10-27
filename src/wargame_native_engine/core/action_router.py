from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from wargame_native_engine.adjudication.guided_fire import (
    GuidedFireRequest,
    GuidedFireStageResult,
)
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.fire_capability import GuidedFireCapability
from wargame_native_engine.core.fire_intent import (
    FireIntent,
    FireMode,
    HexTarget,
    UnitTarget,
)
from wargame_native_engine.core.world import WorldState


@dataclass(frozen=True, slots=True)
class RoutedAction:
    action_sequence: int
    action: EngineAction


class ActionRouter:
    """Normalize and common-validate one source-compatible action list."""

    supported_types = frozenset({
        ActionType.MOVE, ActionType.SHOOT, ActionType.FIRE,
        ActionType.STOP_MOVE, ActionType.BOARD, ActionType.UNLOAD,
        ActionType.CAPTURE,
        ActionType.ENTER_CONCEALMENT,
        ActionType.CHANGE_STATE, ActionType.WEAPON_LOCK, ActionType.WEAPON_UNLOCK,
        ActionType.WEAPON_UNFOLD, ActionType.WAIT,
    })

    def normalize(self, actions: Mapping[str, EngineAction | None]
                  | Sequence[EngineAction], *, side_order: Sequence[str],
                  start_sequence: int) -> tuple[RoutedAction, ...]:
        values: list[EngineAction] = []
        if isinstance(actions, Mapping):
            for side in side_order:
                item = actions.get(side)
                values.append(item if item is not None else
                              EngineAction(side, None, ActionType.WAIT))
        elif isinstance(actions, Sequence) and not isinstance(actions, (str, bytes)):
            for item in actions:
                if not isinstance(item, EngineAction):
                    raise TypeError("source-compatible actions must be EngineAction values")
                values.append(item)
        else:
            raise TypeError("actions must be a side mapping or ordered action sequence")
        return tuple(RoutedAction(start_sequence + index, item)
                     for index, item in enumerate(values))

    def common_error(self, routed: RoutedAction, world: WorldState,
                     side_order: Sequence[str]) -> str | None:
        action = routed.action
        if action.side not in side_order:
            return "UNKNOWN_SIDE"
        if action.action_type not in self.supported_types:
            return "UNSUPPORTED_ACTION_TYPE"
        if action.action_type is ActionType.WAIT and action.actor_id is None:
            return None
        if action.actor_id is None:
            return "ACTOR_REQUIRED"
        operator = world.operators.get(action.actor_id)
        if operator is None:
            return "ACTOR_NOT_FOUND"
        if operator.side != action.side:
            return "ACTOR_NOT_CONTROLLED"
        if not operator.alive:
            return "ACTOR_DESTROYED"
        if operator.mounted_on is not None:
            return "ACTOR_ON_BOARD"
        if not operator.deployed:
            return "ACTOR_NOT_DEPLOYED"
        if operator.movement_locked and action.action_type is not ActionType.WAIT:
            return "ACTOR_BOARDING_LOCKED"
        return None

    def fire_intent(self, action: EngineAction) -> FireIntent:
        """Convert one legacy SHOOT action at the execution boundary.

        The source action remains the replay and action-hash authority.  This
        conversion only gives adjudication a typed internal contract.
        """

        if action.action_type not in {ActionType.SHOOT, ActionType.FIRE}:
            raise ValueError("FIRE_INTENT_REQUIRES_SHOOT")
        if action.action_type is ActionType.FIRE:
            return self._explicit_fire_intent(action)
        if action.actor_id is None:
            raise ValueError("ACTOR_REQUIRED")
        if action.target_id is None:
            raise ValueError("TARGET_NOT_FOUND")
        raw_weapon_id = action.parameters.get("weapon_id")
        if raw_weapon_id is None:
            raise ValueError("WEAPON_REQUIRED")
        try:
            shooter_id = int(action.actor_id)
            target_id = int(action.target_id)
            weapon_id = int(raw_weapon_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("INVALID_DIRECT_FIRE_IDENTIFIERS") from exc
        return FireIntent(
            mode=FireMode.DIRECT,
            shooter_id=shooter_id,
            weapon_id=weapon_id,
            target=UnitTarget(target_id),
        )

    @staticmethod
    def _explicit_fire_intent(action: EngineAction) -> FireIntent:
        raw_mode = action.parameters.get("fire_mode")
        try:
            mode = FireMode(str(raw_mode).upper())
        except ValueError as exc:
            raise ValueError("INVALID_FIRE_MODE") from exc
        if mode is FireMode.GUIDED:
            return ActionRouter._guided_fire_intent(action)
        if mode is FireMode.INDIRECT:
            return ActionRouter._indirect_fire_intent(action)
        raise ValueError("FIRE_ACTION_REQUIRES_EXPLICIT_NON_DIRECT_MODE")

    @staticmethod
    def _guided_fire_intent(action: EngineAction) -> FireIntent:
        if action.actor_id is None:
            raise ValueError("GUIDER_REQUIRED")
        if action.target_id is None:
            raise ValueError("TARGET_NOT_FOUND")
        if str(action.parameters.get("fire_mode")).upper() != FireMode.GUIDED.value:
            raise ValueError("FIRE_ACTION_REQUIRES_GUIDED_MODE")
        guider_id = _exact_identifier(
            action.parameters.get("guider_id"),
            "guider_id",
        )
        if guider_id != action.actor_id:
            raise ValueError("GUIDED_FIRE_ACTOR_MUST_BE_GUIDER")
        return FireIntent(
            mode=FireMode.GUIDED,
            shooter_id=_exact_identifier(
                action.parameters.get("shooter_id"),
                "shooter_id",
            ),
            weapon_id=_exact_identifier(
                action.parameters.get("weapon_id"),
                "weapon_id",
            ),
            target=UnitTarget(_exact_identifier(
                action.target_id,
                "target_id",
            )),
            guider_id=guider_id,
        )

    @staticmethod
    def _indirect_fire_intent(action: EngineAction) -> FireIntent:
        if action.actor_id is None:
            raise ValueError("SHOOTER_REQUIRED")
        if action.target_position is not None:
            raise ValueError("INDIRECT_FIRE_REQUIRES_HEX_TARGET_ONLY")
        if action.target_id is not None:
            _exact_identifier(action.target_id, "target_id")
        return FireIntent(
            mode=FireMode.INDIRECT,
            shooter_id=_exact_identifier(action.actor_id, "shooter_id"),
            weapon_id=_exact_identifier(
                action.parameters.get("weapon_id"),
                "weapon_id",
            ),
            target=HexTarget(_exact_identifier(
                action.parameters.get("target_hex"),
                "target_hex",
            )),
        )

    @staticmethod
    def guided_fire_action(
        side: str,
        capability: GuidedFireCapability,
    ) -> EngineAction:
        """Encode one server-issued capability as a replayable FIRE action."""

        if not isinstance(side, str) or not side:
            raise ValueError("side must be a non-empty string")
        if not isinstance(capability, GuidedFireCapability):
            raise TypeError("capability must be GuidedFireCapability")
        return EngineAction(
            side=side,
            actor_id=capability.guider_id,
            action_type=ActionType.FIRE,
            target_id=capability.target_id,
            parameters={
                "fire_mode": FireMode.GUIDED.value,
                "guider_id": capability.guider_id,
                "shooter_id": capability.shooter_id,
                "weapon_id": capability.weapon_id,
            },
        )

    @staticmethod
    def route_fire_intent(
        intent: FireIntent,
        *,
        guided_stage: Callable[[FireIntent], GuidedFireStageResult] | None = None,
    ) -> FireIntent | GuidedFireStageResult:
        """Route a typed fire contract without owning simulation state.

        DIRECT retains the original pass-through path.  GUIDED is routed only
        when NativeEngine supplies a staging callback bound to one detached
        pre-state and detached RNG.  The router therefore cannot bypass Native
        authority or commit effects by itself.
        """

        if not isinstance(intent, FireIntent):
            raise TypeError("intent must be FireIntent")
        if intent.mode is FireMode.DIRECT:
            if not isinstance(intent.target, UnitTarget):
                raise ValueError("DIRECT_FIRE_REQUIRES_UNIT_TARGET")
            return intent
        if intent.mode is FireMode.GUIDED:
            if not isinstance(intent.target, UnitTarget):
                raise ValueError("GUIDED_FIRE_REQUIRES_UNIT_TARGET")
            if intent.guider_id is None:
                raise ValueError("GUIDER_REQUIRED")
            if guided_stage is None:
                raise ValueError("GUIDED_FIRE_STAGER_REQUIRED")
            staged = guided_stage(intent)
            if not isinstance(staged, GuidedFireStageResult):
                raise TypeError(
                    "guided_stage must return GuidedFireStageResult"
                )
            return staged
        if intent.mode is FireMode.INDIRECT:
            if not isinstance(intent.target, HexTarget):
                raise ValueError("INDIRECT_FIRE_REQUIRES_HEX_TARGET")
            return intent
        raise ValueError(f"UNSUPPORTED_FIRE_MODE:{intent.mode.value}")

    def route_fire(
        self,
        action: EngineAction,
        *,
        guided_stage: Callable[[FireIntent], GuidedFireStageResult] | None = None,
    ) -> FireIntent | GuidedFireStageResult:
        routed = self.route_fire_intent(
            self.fire_intent(action),
            guided_stage=guided_stage,
        )
        if action.action_type is ActionType.SHOOT and not isinstance(
            routed,
            FireIntent,
        ):
            raise RuntimeError("legacy SHOOT unexpectedly left DIRECT route")
        return routed

    @staticmethod
    def route_guided_fire_request(
        intent: FireIntent,
        request: GuidedFireRequest,
    ) -> GuidedFireRequest:
        """Validate the GUIDED preview contract without executing it."""

        if not isinstance(intent, FireIntent):
            raise TypeError("intent must be FireIntent")
        if not isinstance(request, GuidedFireRequest):
            raise TypeError("request must be GuidedFireRequest")
        if intent.mode is not FireMode.GUIDED:
            raise ValueError("GUIDED_FIRE_REQUEST_REQUIRES_GUIDED_INTENT")
        if not isinstance(intent.target, UnitTarget):
            raise ValueError("GUIDED_FIRE_REQUIRES_UNIT_TARGET")
        if intent.guider_id is None:
            raise ValueError("GUIDER_REQUIRED")
        if (
            intent.guider_id != request.guider_id
            or intent.shooter_id != request.shooter_id
            or intent.weapon_id != request.weapon_id
            or intent.target.unit_id != request.target_id
        ):
            raise ValueError("GUIDED_FIRE_INTENT_REQUEST_MISMATCH")
        return request


def _exact_identifier(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative exact int")
    return value
