from __future__ import annotations

from wargame_native_engine.agents.first_valid import FirstValidActionAgent
from wargame_native_engine.agents.protocol import AgentResetContext
from wargame_native_engine.agents.random_legal import RandomLegalAgent
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.version import RULESET_VERSION


def _context(engine, side):
    observation = engine.observe(side)
    from wargame_native_engine.agents.protocol import AgentStepContext
    return AgentStepContext("test", side, observation.tick, observation.stage, 42)


def test_first_valid_agent_is_deterministic(scenario) -> None:
    engine = NativeEngine(scenario)
    engine.reset()
    observation = engine.observe("red")
    valid = engine.valid_actions("red")
    agent = FirstValidActionAgent()
    left = agent.act(observation=observation, valid_actions=valid,
                     context=_context(engine, "red"))
    right = agent.act(observation=observation, valid_actions=tuple(reversed(valid)),
                      context=_context(engine, "red"))
    assert left == right


def test_random_agent_is_reproducible(scenario) -> None:
    engine = NativeEngine(scenario)
    engine.reset()
    observation = engine.observe("red")
    valid = engine.valid_actions("red")
    values = []
    for _ in range(2):
        agent = RandomLegalAgent(99)
        agent.reset(AgentResetContext("test", scenario.scenario_id, "red", 99,
                                      RULESET_VERSION))
        values.append(agent.act(observation=observation, valid_actions=valid,
                                context=_context(engine, "red")))
    assert values[0] == values[1]
