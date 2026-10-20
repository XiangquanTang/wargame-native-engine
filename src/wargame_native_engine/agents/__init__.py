from wargame_native_engine.agents.baselines import NoOpAgent, ScriptedAgent
from wargame_native_engine.agents.first_valid import FirstValidActionAgent
from wargame_native_engine.agents.protocol import (
    Agent,
    AgentAction,
    DecisionAgent,
    AgentResetContext,
    AgentStepContext,
)
from wargame_native_engine.agents.random_legal import RandomLegalAgent
from wargame_native_engine.agents.random_seat import RandomProductSeatAgent
from wargame_native_engine.agents.seat import (
    AgentDecisionContext,
    LocalSeatAgent,
    ScriptedSeatAgent,
    SeatActionCapabilities,
    SeatDecision,
    SeatOperation,
    WaitSeatAgent,
)


_RUNNER_EXPORTS = frozenset({
    "AgentExecutionError",
    "LocalAgentMatchError",
    "LocalAgentMatchResult",
    "LocalAgentMatchRunner",
})
_OPERATION_RUNNER_EXPORTS = frozenset({
    "LocalSeatMatchError",
    "LocalSeatMatchResult",
    "LocalSeatMatchRunner",
    "OperationAgentMatchResult",
    "OperationAgentMatchRunner",
    "SeatAgentExecutionError",
    "SeatOperationError",
    "run_local_agent_match",
})
_REALTIME_EXPORTS = frozenset({
    "CommandStatus",
    "ManualRealtimeClock",
    "RealtimeAgentSession",
    "RealtimeAgentSessionError",
    "RealtimeCancellationReason",
    "RealtimeClock",
    "RealtimeExecutionMode",
    "RealtimeLifecycleState",
    "RealtimeResetResult",
    "RealtimeSeatBinding",
    "RealtimeSeatView",
    "RealtimeShutdownTimeoutError",
    "RealtimeTickResult",
    "RealtimeTickRecord",
    "SeatCommandEvent",
    "SeatCommandReceipt",
    "SystemMonotonicClock",
    "realtime_episode_id",
})
_REALTIME_PUBLICATION_EXPORTS = frozenset({
    "REALTIME_SUMMARY_NAME",
    "REALTIME_TICKS_NAME",
    "SEAT_COMMANDS_NAME",
    "RealtimeEpisodePublication",
    "RealtimeEpisodePublicationError",
    "publish_realtime_episode",
    "realtime_ticks_to_jsonl",
    "seat_commands_to_jsonl",
})
_REALTIME_RUNNER_EXPORTS = frozenset({
    "RealtimeAgentFailure",
    "RealtimeLocalAgentResult",
    "RealtimeLocalAgentRunner",
})


def __getattr__(name: str):
    if name in _RUNNER_EXPORTS:
        from wargame_native_engine.agents import runner

        return getattr(runner, name)
    if name in _OPERATION_RUNNER_EXPORTS:
        from wargame_native_engine.agents import operation_runner

        return getattr(operation_runner, name)
    if name in _REALTIME_EXPORTS:
        from wargame_native_engine.agents import realtime

        return getattr(realtime, name)
    if name in _REALTIME_PUBLICATION_EXPORTS:
        from wargame_native_engine.agents import realtime_publication

        return getattr(realtime_publication, name)
    if name in _REALTIME_RUNNER_EXPORTS:
        from wargame_native_engine.agents import realtime_runner

        return getattr(realtime_runner, name)
    raise AttributeError(name)

__all__ = [
    "Agent",
    "AgentAction",
    "DecisionAgent",
    "AgentExecutionError",
    "AgentResetContext",
    "AgentStepContext",
    "AgentDecisionContext",
    "FirstValidActionAgent",
    "LocalAgentMatchError",
    "LocalAgentMatchResult",
    "LocalAgentMatchRunner",
    "LocalSeatAgent",
    "LocalSeatMatchError",
    "LocalSeatMatchResult",
    "LocalSeatMatchRunner",
    "NoOpAgent",
    "OperationAgentMatchResult",
    "OperationAgentMatchRunner",
    "CommandStatus",
    "ManualRealtimeClock",
    "RandomLegalAgent",
    "RandomProductSeatAgent",
    "RealtimeAgentSession",
    "RealtimeAgentSessionError",
    "RealtimeCancellationReason",
    "RealtimeAgentFailure",
    "RealtimeClock",
    "RealtimeExecutionMode",
    "RealtimeEpisodePublication",
    "RealtimeEpisodePublicationError",
    "RealtimeResetResult",
    "RealtimeLocalAgentResult",
    "RealtimeLocalAgentRunner",
    "RealtimeLifecycleState",
    "RealtimeSeatBinding",
    "RealtimeSeatView",
    "RealtimeShutdownTimeoutError",
    "RealtimeTickResult",
    "RealtimeTickRecord",
    "ScriptedAgent",
    "ScriptedSeatAgent",
    "SeatActionCapabilities",
    "SeatCommandEvent",
    "SeatCommandReceipt",
    "SeatDecision",
    "SeatAgentExecutionError",
    "SeatOperation",
    "SeatOperationError",
    "WaitSeatAgent",
    "SystemMonotonicClock",
    "REALTIME_SUMMARY_NAME",
    "REALTIME_TICKS_NAME",
    "SEAT_COMMANDS_NAME",
    "publish_realtime_episode",
    "realtime_episode_id",
    "realtime_ticks_to_jsonl",
    "run_local_agent_match",
    "seat_commands_to_jsonl",
]
