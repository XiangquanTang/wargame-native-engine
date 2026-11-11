"""Small synchronous host for public, side-bound product-seat policies.

Only this host advances time. Both policies see the same completed tick and each
seat submits at most one complete decision before the next adjudication. A paced
run can expose the usual HTTP viewer; a fast run changes wall time, not game time.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from itertools import count
import signal
from threading import current_thread, main_thread
from time import monotonic, sleep

from wargame_native_engine.agents.realtime import (
    CommandStatus, ManualRealtimeClock, RealtimeCancellationReason, RealtimeExecutionMode,
)
from wargame_native_engine.agents.seat import AgentDecisionContext, SeatDecision


def create_simple_policies(session, bindings, names):
    """Construct independent policies with only a bound advisory preview hook."""
    from wargame_native_engine.agents.simple_capture import SimpleCaptureSeatAgent

    def bound_preview(binding):
        sequence = count()

        def preview(context, operation):
            view = session.latest_view(binding)
            if (context.side, context.decision_index, context.frame_index) != (
                view.seat, view.tick_index, view.frame_index,
            ):
                raise ValueError("rule_policy_preview_snapshot_mismatch")
            return session.preview_operation(
                binding, operation, observation_version=view.observation_version,
                request_id=f"simple-policy-{binding.seat}-{view.tick_index}-{next(sequence)}",
            ).to_record()

        return preview

    return {
        side: SimpleCaptureSeatAgent(preview=bound_preview(bindings[side]))
        for side, name in names.items() if name == "simple-capture"
    }


def run_policy_loop(session, bindings, policies, *, max_ticks=None, paced=True,
                    check_server=None):
    """Run already-reset policies, returning the stop reason and compact counts.

    Policies receive detached own-seat observations/capabilities, never the match
    or world. Empty decisions do not enqueue WAIT and cannot replace commands.
    HTTP writes for policy-owned seats must be disabled by the caller.
    """
    with _deferred_sigint() as stop:
        return _run_policy_loop(
            session, bindings, policies, max_ticks=max_ticks, paced=paced,
            check_server=check_server, stop=stop,
        )


@contextmanager
def _deferred_sigint():
    """Do not raise KeyboardInterrupt through an authoritative tick.

    Python only permits signal handlers on the main thread. Embedded worker
    callers retain their owner's signal setup; the ordinary CLI uses this
    temporary handler and always restores it, including on exceptions.
    """
    stop = _StopRequest()
    previous = None
    installed = False
    if current_thread() is main_thread():
        previous = signal.getsignal(signal.SIGINT)
        try:
            def request_stop(_signum, _frame):
                # A Python handler can interrupt code holding a non-reentrant
                # lock. Never call Event.set() or acquire any lock here.
                stop.requested = True

            signal.signal(signal.SIGINT, request_stop)
            installed = True
        except (ValueError, OSError):
            # Some embedding runtimes do not permit signal registration.
            pass
    try:
        yield stop
    finally:
        if installed:
            signal.signal(signal.SIGINT, previous)


class _StopRequest:
    """Main-thread signal flag with bounded, lock-free paced waiting."""

    def __init__(self):
        self.requested = False

    def is_set(self):
        return self.requested

    def wait(self, timeout):
        deadline = monotonic() + timeout
        while not self.requested:
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            sleep(min(0.05, remaining))
        return self.requested


def _run_policy_loop(session, bindings, policies, *, max_ticks, paced,
                     check_server, stop):
    if session.execution_mode is not RealtimeExecutionMode.MANUAL:
        raise ValueError("rule_policy_host_requires_manual_session")
    if not isinstance(session.clock, ManualRealtimeClock):
        raise ValueError("rule_policy_host_requires_manual_clock")
    if max_ticks is not None and (type(max_ticks) is not int or max_ticks <= 0):
        raise ValueError("max_ticks must be a positive exact int")
    if set(policies) - {"red", "blue"}:
        raise ValueError("unsupported policy seat")
    counts = {side: Counter() for side in policies}
    decisions = {side: 0 for side in policies}
    applied = {side: Counter() for side in policies}
    outcomes = {side: Counter() for side in policies}
    rejected = []
    rejected_count = 0
    catalogs = {
        side: session.weapon_catalog_snapshot(bindings[side])[1]
        for side in policies
    }
    for side, policy in policies.items():
        policy.reset(session.agent_reset_context(
            bindings[side], agent_id=policy.agent_id,
        ))
    reason = RealtimeCancellationReason.USER_STOP
    try:
        while not (session.terminal or session.truncated):
            if stop.is_set():
                break
            if max_ticks is not None and session.tick_count >= max_ticks:
                reason = RealtimeCancellationReason.RUNNER_LIMIT
                break
            if check_server is not None:
                check_server()
            deadline = monotonic() + session.tick_interval_ms / 1000
            snapshots = {
                side: session.controller_snapshot(bindings[side]) for side in policies
            }
            selected = {}
            for side, policy in policies.items():
                if stop.is_set():
                    break
                view, controller = snapshots[side]
                visible_ids = {
                    enemy["operator_id"]
                    for enemy in view.observation.get("visible_enemies", ())
                }
                visible_hexes = {
                    operator["obj_id"]: operator["cur_hex"]
                    for operator in controller.source_frame.get("operators", ())
                    if operator.get("obj_id") in visible_ids
                    and type(operator.get("cur_hex")) is int
                }
                capabilities = replace(view.capabilities, operation_summary={
                    **view.capabilities.operation_summary,
                    "policy_own_weapon_inventory": controller.own_weapon_inventory,
                    "policy_weapon_catalog": catalogs[side],
                    "policy_visible_enemy_hexes": visible_hexes,
                })
                decision = policy.act(context=AgentDecisionContext(
                    side, view.tick_index, view.frame_index, view.sim_time_ms,
                    view.observation, capabilities,
                    view.decision_semantic, view.action_catalog,
                ))
                if not isinstance(decision, SeatDecision):
                    raise TypeError("LocalSeatAgent.act must return SeatDecision")
                selected[side] = decision
            if stop.is_set():
                break
            submitted = {}
            for side, decision in selected.items():
                if stop.is_set():
                    break
                if not decision.operations:
                    continue
                receipt = session.submit(
                    bindings[side], observation_version=snapshots[side][0].observation_version,
                    decision=decision,
                )
                if not receipt.accepted:
                    raise RuntimeError(
                        f"rule_policy_submit_rejected:{side}:{receipt.error_code}"
                    )
                decisions[side] += 1
                counts[side].update(op.operation_type for op in decision.operations)
                submitted[side] = receipt.command_sequence
            if paced:
                stop.wait(max(0.0, deadline - monotonic()))
            if stop.is_set():
                break
            session.advance_tick()
            session.clock.advance_ms(session.tick_interval_ms)
            for side, sequence in submitted.items():
                event = session.command_event(bindings[side], sequence)
                if event is None:
                    raise RuntimeError(f"rule_policy_command_missing:{side}:{sequence}")
                outcomes[side][event.status.value] += 1
                if event.status is CommandStatus.APPLIED:
                    applied[side].update(op.operation_type for op in event.decision.operations)
                else:
                    rejected_count += 1
                    if len(rejected) < 100:
                        rejected.append({
                            "side": side, "command_sequence": sequence,
                            "tick_index": event.target_tick, "status": event.status.value,
                            "error_code": event.error_code,
                            "decision": event.decision.to_record(),
                        })
            if rejected_count:
                # A queue receipt is not an execution success. Stop at the
                # first failed batch rather than retrying the same bad intent.
                reason = RealtimeCancellationReason.FAILURE
                break
        if reason is RealtimeCancellationReason.FAILURE:
            pass
        elif session.terminal:
            reason = RealtimeCancellationReason.TERMINAL
        elif session.truncated:
            reason = RealtimeCancellationReason.TRUNCATION
    except KeyboardInterrupt:
        reason = RealtimeCancellationReason.USER_STOP
    return reason, {
        "schema": "native-simple-policy-run/1.0",
        "clock": "paced-manual" if paced else "fast-manual",
        "stop_reason": reason.value,
        "tick_count": session.tick_count,
        "max_ticks": max_ticks,
        "non_applied_command_count": rejected_count,
        "non_applied_commands_first_100": rejected,
        "policies": {
            side: {
                "agent_id": policy.agent_id,
                "submitted_decisions": decisions[side],
                "submitted_operations": dict(sorted(counts[side].items())),
                "operations_in_applied_commands": dict(sorted(applied[side].items())),
                "command_outcomes": dict(sorted(outcomes[side].items())),
                "last_decision_reasons": getattr(policy, "status", {}),
            }
            for side, policy in policies.items()
        },
        "counter_semantics": (
            "Operation counts describe selected intents inside command bundles, not combat effects. "
            "A rejected bundle can include successfully applied sibling operations; inspect replay "
            "adjudication for actual outcomes."
        ),
    }
