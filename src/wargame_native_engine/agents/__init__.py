from wargame_native_engine.agents.baselines import NoOpAgent, ScriptedAgent
from wargame_native_engine.agents.first_valid import FirstValidActionAgent
from wargame_native_engine.agents.protocol import (
    Agent,
    AgentAction,
    AgentResetContext,
    AgentStepContext,
)
from wargame_native_engine.agents.random_legal import RandomLegalAgent
from wargame_native_engine.agents.seat import (
    AgentDecisionContext,
    LocalSeatAgent,
    ScriptedSeatAgent,
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


def __getattr__(name: str):
    if name in _RUNNER_EXPORTS:
        from wargame_native_engine.agents import runner

        return getattr(runner, name)
    if name in _OPERATION_RUNNER_EXPORTS:
        from wargame_native_engine.agents import operation_runner

        return getattr(operation_runner, name)
    raise AttributeError(name)

__all__ = [
    "Agent",
    "AgentAction",
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
    "RandomLegalAgent",
    "ScriptedAgent",
    "ScriptedSeatAgent",
    "SeatAgentExecutionError",
    "SeatOperation",
    "SeatOperationError",
    "WaitSeatAgent",
    "run_local_agent_match",
]
