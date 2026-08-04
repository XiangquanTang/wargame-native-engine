from __future__ import annotations

from wargame_native_engine.agents import FirstValidActionAgent
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.io.replay import ReplayReader, replay_actions
from wargame_native_engine.session.episode import EpisodeRunner
from wargame_native_engine.session.game_session import GameSession


def _run(scenario, output):
    session = GameSession(NativeEngineBackend(scenario, max_ticks=50))
    agents = {side: FirstValidActionAgent() for side in scenario.side_order}
    return EpisodeRunner(session, agents=agents, output_dir=output).run(42)


def test_action_event_are_separate(scenario, tmp_path) -> None:
    output = tmp_path / "replay"
    _run(scenario, output)
    bundle = ReplayReader().read(output)
    assert bundle["actions"] and bundle["events"]
    assert {item["schema_version"] for item in bundle["actions"]} == {
        "native-action/1.0"}
    assert {item["schema_version"] for item in bundle["events"]} == {
        "native-event/1.0"}
    assert "event_type" not in bundle["actions"][0]
    assert "action" not in bundle["events"][0]


def test_replay_round_trip(scenario, tmp_path) -> None:
    output = tmp_path / "replay"
    outcome = _run(scenario, output)
    bundle = ReplayReader().read(output)
    verification = replay_actions(scenario, output)
    assert bundle["summary"]["result"] == outcome.result
    assert verification.matches
    assert verification.ticks == outcome.result["ticks"]
    assert (output / "checksums.sha256").is_file()
