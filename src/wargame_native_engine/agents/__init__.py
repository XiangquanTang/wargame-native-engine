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


def __getattr__(name: str):
    if name in _RUNNER_EXPORTS:
        from wargame_native_engine.agents import runner

        return getattr(runner, name)
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
    "RandomLegalAgent",
    "ScriptedAgent",
]
