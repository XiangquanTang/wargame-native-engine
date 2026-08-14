from wargame_native_engine.agents.baselines import NoOpAgent, ScriptedAgent
from wargame_native_engine.agents.first_valid import FirstValidActionAgent
from wargame_native_engine.agents.protocol import (
    Agent,
    AgentAction,
    AgentResetContext,
    AgentStepContext,
)
from wargame_native_engine.agents.random_legal import RandomLegalAgent


_RUNNER_EXPORTS = frozenset({
    "AgentExecutionError",
    "LocalAgentMatchError",
    "LocalAgentMatchResult",
    "LocalAgentMatchRunner",
})
_OPERATION_RUNNER_EXPORTS = frozenset({
    "OperationAgentMatchResult",
    "OperationAgentMatchRunner",
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
    "FirstValidActionAgent",
    "LocalAgentMatchError",
    "LocalAgentMatchResult",
    "LocalAgentMatchRunner",
    "NoOpAgent",
    "OperationAgentMatchResult",
    "OperationAgentMatchRunner",
    "RandomLegalAgent",
    "ScriptedAgent",
]
