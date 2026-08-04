from __future__ import annotations

from wargame_native_engine.agents import FirstValidActionAgent
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.io.replay import ReplayReader
from wargame_native_engine.session.episode import EpisodeRunner
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.local_tools import LocalAgentTools


def test_full_minimal_game_reaches_terminal(scenario, tmp_path) -> None:
    session = GameSession(NativeEngineBackend(scenario, max_ticks=50))
    agents = {side: FirstValidActionAgent() for side in scenario.side_order}
    output = tmp_path / "full-game"
    outcome = EpisodeRunner(session, agents=agents, output_dir=output).run(42)
    assert outcome.closed
    assert outcome.result["terminal"] is True
    assert outcome.result["truncated"] is False
    assert outcome.result["winner"] == "red"
    assert outcome.result["reason"] == "side_eliminated"
    assert outcome.result["ticks"] == 6
    assert ReplayReader().read(output)["manifest"]["status"] == "complete"


def test_local_agent_tools_advance_synchronous_batch(scenario) -> None:
    session = GameSession(NativeEngineBackend(scenario))
    session.reset(42)
    tools = LocalAgentTools(session)
    actions = {side: session.valid_actions(side)[0].action
               for side in scenario.side_order}
    pending = tools.submit_action("red", actions["red"])
    assert not pending["advanced"] and pending["pending_sides"] == ["blue"]
    advanced = tools.submit_action("blue", actions["blue"])
    assert advanced["advanced"]
    assert tools.get_episode_status()["tick"] == 1
    session.close()
