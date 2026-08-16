from __future__ import annotations

import json
from pathlib import Path

from wargame_native_engine.agents import (
    CommandStatus,
    ManualRealtimeClock,
    RealtimeAgentSession,
    SeatDecision,
    realtime_ticks_to_jsonl,
    seat_commands_to_jsonl,
)
from wargame_native_engine.data import ScenarioBundle, ScenarioCatalog
from wargame_native_engine.operations import WargameFacade


SCENARIO = Path("examples/source_movement_scenario.json")


class MatchFactory:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.bundle = ScenarioBundle.from_path(SCENARIO)

    def __call__(self, seed: int, episode_sequence: int):
        app = WargameFacade(
            ScenarioCatalog.from_bundles({
                self.bundle.scenario_identity.scenario_id: self.bundle,
            }),
            storage_root=self.root / f"managed-{episode_sequence}",
        )
        return app.create_match(
            scenario_id=self.bundle.scenario_identity.scenario_id,
            red_controller_id="event-red",
            blue_controller_id="event-blue",
            seed=seed,
            decision_interval_ms=50,
            frame_interval_ms=50,
        )


def _session(root: Path):
    return RealtimeAgentSession(
        MatchFactory(root),
        run_namespace="event-worker-000",
        tick_interval_ms=50,
        clock=ManualRealtimeClock(),
    )


def test_command_and_tick_jsonl_are_canonical_and_detached(tmp_path: Path) -> None:
    environment = _session(tmp_path)
    try:
        reset = environment.reset(seed=42)
        environment.submit(
            reset.red_binding,
            observation_version=reset.red.observation_version,
            decision=SeatDecision(),
        )
        environment.submit(
            reset.blue_binding,
            observation_version=reset.blue.observation_version,
            decision=SeatDecision(),
        )
        environment.advance_tick()
        commands = seat_commands_to_jsonl(environment.command_events)
        ticks = realtime_ticks_to_jsonl(environment.tick_results)
        assert commands.endswith("\n") and not commands.endswith("\n\n")
        assert ticks.endswith("\n") and not ticks.endswith("\n\n")
        command_records = [json.loads(line) for line in commands.splitlines()]
        tick_records = [json.loads(line) for line in ticks.splitlines()]
        assert [value["command_sequence"] for value in command_records] == [0, 1]
        assert [value["seat"] for value in command_records] == ["red", "blue"]
        assert all(value["status"] == "APPLIED" for value in command_records)
        assert tick_records[0]["red_command_sequences"] == [0]
        assert tick_records[0]["blue_command_sequences"] == [1]
        assert "green" not in commands.lower()
        assert "frame_analysis" not in commands
    finally:
        environment.close()


def test_recorded_target_tick_replay_does_not_use_new_arrival_time(
    tmp_path: Path,
) -> None:
    original = _session(tmp_path / "original")
    replayed = _session(tmp_path / "replayed")
    try:
        initial = original.reset(seed=42)
        original.clock.advance_ms(49)  # type: ignore[attr-defined]
        original.submit(
            initial.blue_binding,
            observation_version=initial.blue.observation_version,
            decision=SeatDecision(),
        )
        original.clock.advance_ms(2)  # type: ignore[attr-defined]
        original.submit(
            initial.red_binding,
            observation_version=initial.red.observation_version,
            decision=SeatDecision(),
        )
        original.advance_tick()
        original.advance_tick()

        replay_reset = replayed.reset(seed=42)
        bindings = {
            "red": replay_reset.red_binding,
            "blue": replay_reset.blue_binding,
        }
        versions = {
            "red": replay_reset.red.observation_version,
            "blue": replay_reset.blue.observation_version,
        }
        for event in original.command_events:
            assert event.target_tick is not None
            replayed.submit_recorded(
                bindings[event.seat],
                command_sequence=event.command_sequence,
                observation_version=versions[event.seat],
                target_tick=event.target_tick,
                decision=event.decision,
                received_monotonic_ns=event.received_monotonic_ns,
            )
        replayed.advance_tick()
        replayed.advance_tick()

        assert [
            value.authoritative_record() for value in replayed.tick_results
        ] == [
            value.authoritative_record() for value in original.tick_results
        ]
        assert [value.status for value in replayed.command_events] == [
            CommandStatus.APPLIED, CommandStatus.APPLIED,
        ]
        original_match = original._require_match()  # noqa: SLF001
        replayed_match = replayed._require_match()  # noqa: SLF001
        original_facts = original_match.gateway.manager._managed(  # noqa: SLF001
            original_match.session_id
        ).recorder
        replayed_facts = replayed_match.gateway.manager._managed(  # noqa: SLF001
            replayed_match.session_id
        ).recorder
        assert original_facts is not None and replayed_facts is not None
        assert original_match.status.to_record() == replayed_match.status.to_record()
        assert original_facts._frame_hashes == replayed_facts._frame_hashes  # noqa: SLF001

        first_package = original.publish(
            tmp_path / "original-package", truncated=True
        )
        second_package = replayed.publish(
            tmp_path / "replayed-package", truncated=True
        )
        assert first_package.analysis_path.read_bytes() == second_package.analysis_path.read_bytes()
        assert first_package.command_path.read_bytes() == second_package.command_path.read_bytes()
        first_ticks = [
            json.loads(line)
            for line in first_package.tick_path.read_text(encoding="utf-8").splitlines()
        ]
        second_ticks = [
            json.loads(line)
            for line in second_package.tick_path.read_text(encoding="utf-8").splitlines()
        ]
        for values in (first_ticks, second_ticks):
            for value in values:
                value.pop("processing_duration_ns")
                value.pop("overrun_ns")
        assert first_ticks == second_ticks
        from wargame_native_engine.io.importers.source_replay_archive import (
            SourceReplayArchiveImporter,
        )

        first_archive = SourceReplayArchiveImporter().import_path(
            first_package.replay_path
        )
        second_archive = SourceReplayArchiveImporter().import_path(
            second_package.replay_path
        )
        assert [value.source_hash for value in first_archive.frames] == [
            value.source_hash for value in second_archive.frames
        ]
        assert first_archive.manifest["final_state_hash"] == second_archive.manifest[
            "final_state_hash"
        ]
    finally:
        original.close()
        replayed.close()
