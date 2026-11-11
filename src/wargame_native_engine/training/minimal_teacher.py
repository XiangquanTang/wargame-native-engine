"""Single-command BC adapter for the existing SimpleCaptureSeatAgent.

The rule implementation is reused, not copied.  The minimal learner chooses one
catalog action per decision, while the realtime rule host submits a seat-wide
bundle.  This adapter therefore schedules one ready actor at a time, rotating
its first actor.  It never invents a log-probability for a rule action.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, TYPE_CHECKING

from wargame_native_engine.agents.seat import AgentDecisionContext, SeatActionCapabilities, SeatOperation
from wargame_native_engine.agents.simple_capture import SimpleCaptureSeatAgent

if TYPE_CHECKING:
    from wargame_native_engine.training.env import NativeTrainingEnv, TrainingDecisionBinding

TEACHER_ID = "simple-capture-single-command/1.0"


@dataclass(frozen=True)
class TeacherSelection:
    binding: "TrainingDecisionBinding"
    action_index: int | None
    operation_type: str
    actor_id: int | None
    teacher_status: tuple[tuple[int, str], ...]


class SimpleCaptureTeacher:
    """Invoke the real rule policy and resolve its intent to an exact catalog ID.

    Idle windows deliberately have no supervised label.  The caller still
    advances the environment, so lock/move/stop transitions complete normally.
    ``select`` may register a server-planned MOVE candidate before freezing its
    returned binding.  This changes no world state and no legality rule.
    """

    teacher_id = TEACHER_ID

    def __init__(self) -> None:
        self._env: NativeTrainingEnv | None = None
        self._scope: str | None = None
        self._cursor = 0
        self._preview_counter = 0
        self._move_ids: dict[tuple[int, int, str], str] = {}
        self._agent = SimpleCaptureSeatAgent(preview=self._preview)

    def reset(self, env: "NativeTrainingEnv") -> None:
        self._env = env
        binding = env.current_decision_binding
        self._scope = binding.binding_scope_id
        self._cursor = 0
        self._preview_counter = 0
        self._move_ids.clear()
        self._agent.reset(env._manager.agent_reset_context(
            env._require_session_id(), env.controlled_side, self._agent.agent_id,
        ))

    def select(self, env: "NativeTrainingEnv") -> TeacherSelection:
        if self._env is not env or self._scope != env.current_decision_binding.binding_scope_id:
            self.reset(env)
        view = env._controlled_view()
        if view.terminal:
            raise ValueError("teacher cannot select from a terminal view")
        actors = sorted(
            int(actor["operator_id"])
            for actor in view.observation.get("friendly_operators", ())
            if actor.get("side") == env.controlled_side
        )
        self._move_ids.clear()
        reasons: dict[int, str] = {}
        if actors:
            start = self._cursor % len(actors)
            ordered = actors[start:] + actors[:start]
            for actor_id in ordered:
                # Refresh after earlier read-only planner calls.  Context's
                # semantic projection and catalog always describe one snapshot.
                context = self._context(env, actor_id)
                decision = self._agent.act(context=context)
                reasons[actor_id] = self._agent.status.get(actor_id, "NO_INTENT")
                if not decision.operations:
                    continue
                if len(decision.operations) != 1:
                    raise ValueError("single-actor rule returned multiple operations")
                operation = decision.operations[0]
                binding = env.current_decision_binding
                index = self._resolve(binding, operation)
                self._cursor = (actors.index(actor_id) + 1) % len(actors)
                return TeacherSelection(
                    binding, index, operation.operation_type, actor_id,
                    tuple(sorted(reasons.items())),
                )
            self._cursor = (start + 1) % len(actors)
        return TeacherSelection(
            env.current_decision_binding, None, "IDLE", None,
            tuple(sorted(reasons.items())),
        )

    def _context(self, env: "NativeTrainingEnv", actor_id: int) -> AgentDecisionContext:
        view = env._controlled_view()
        side = env.controlled_side
        visible_ids = {
            enemy["operator_id"] for enemy in view.observation.get("visible_enemies", ())
        }
        visible_hexes = {
            operator["obj_id"]: operator["cur_hex"]
            for operator in view.source_frame.get("operators", ())
            if operator.get("obj_id") in visible_ids
            and type(operator.get("cur_hex")) is int
        }
        summary = {
            "policy_own_weapon_inventory": view.own_weapon_inventory,
            "policy_weapon_catalog": env._manager.get_weapon_catalog(env._require_session_id()),
            "policy_visible_enemy_hexes": visible_hexes,
        }
        capability = SeatActionCapabilities(
            side, view.step_index, (actor_id,), (), view.valid_actions, summary,
        )
        return AgentDecisionContext(
            side, view.step_index, view.frame_index, view.sim_time_ms,
            view.observation, capability, view.decision_semantic, view.action_catalog,
        )

    def _preview(self, context: AgentDecisionContext, operation: SeatOperation) -> Mapping[str, Any]:
        env = self._env
        if env is None:
            raise RuntimeError("teacher not reset")
        view = env._controlled_view()
        if (context.side, context.decision_index, context.frame_index) != (
            env.controlled_side, view.step_index, view.frame_index,
        ):
            raise ValueError("teacher preview snapshot mismatch")
        if operation.operation_type != "MOVE" or len(operation.actor_ids) != 1:
            raise ValueError("simple-capture teacher preview only supports MOVE")
        actor = operation.actor_ids[0]
        destination = operation.destination_hex
        if destination is None:
            raise ValueError("MOVE teacher destination missing")
        self._preview_counter += 1
        proposal = env.plan_move(
            actor, destination, movement_mode=operation.movement_mode,
            request_id=f"bc-teacher-{view.step_index}-{self._preview_counter}",
        )
        can_execute = bool(proposal.reachable and proposal.action_id is not None)
        if can_execute:
            self._move_ids[(actor, destination, operation.movement_mode)] = proposal.action_id
        return {"can_execute": can_execute, "blocked_reason": proposal.blocked_reason}

    def _resolve(self, binding: "TrainingDecisionBinding", operation: SeatOperation) -> int:
        if len(operation.actor_ids) != 1:
            raise ValueError("teacher operation must contain one actor")
        actor = operation.actor_ids[0]
        move_id = None
        if operation.operation_type == "MOVE":
            move_id = self._move_ids.get((actor, operation.destination_hex, operation.movement_mode))
            if move_id is None:
                raise ValueError("teacher MOVE lacks a server-planned action ID")
        expected_weapon = dict(operation.weapon_choices).get(actor)
        matches = []
        for index, record in enumerate(binding.action_space.actions):
            if not binding.action_space.mask[index]:
                continue
            action = record.get("action", {})
            if action.get("side") != self._env.controlled_side:
                continue
            if action.get("actor_id") != actor or action.get("action_type") != operation.operation_type:
                continue
            if move_id is not None:
                if record.get("action_id") == move_id:
                    matches.append(index)
                continue
            if action.get("target_id") != operation.target_id:
                continue
            parameters = action.get("parameters", {})
            if expected_weapon is not None and parameters.get("weapon_id") != expected_weapon:
                continue
            if operation.operation_type == "SHOOT" and parameters.get("fire_mode", "DIRECT") != "DIRECT":
                continue
            matches.append(index)
        if len(matches) != 1:
            raise ValueError(
                f"teacher intent must resolve exactly once: {operation.operation_type} actor={actor} matches={len(matches)}"
            )
        return matches[0]

    def close(self) -> None:
        self._agent.close()
        self._env = None
        self._scope = None
