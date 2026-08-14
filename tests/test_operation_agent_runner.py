from __future__ import annotations

from pathlib import Path

from wargame_native_engine.agents import (
    NoOpAgent,
    OperationAgentMatchRunner,
)
from wargame_native_engine.data import ScenarioBundle, ScenarioCatalog
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)
from wargame_native_engine.operations import WargameFacade


def test_operation_agent_runner_uses_facade_gateway_and_publishes_evidence(
    tmp_path: Path,
) -> None:
    bundle = ScenarioBundle.from_path("examples/source_movement_scenario.json")
    app = WargameFacade(
        ScenarioCatalog.from_bundles({
            bundle.scenario_identity.scenario_id: bundle,
        }),
        storage_root=tmp_path / "managed",
    )
    match = app.create_match(
        scenario_id=bundle.scenario_identity.scenario_id,
        red_controller_id="agent-red",
        blue_controller_id="agent-blue",
        decision_interval_ms=10_000,
        frame_interval_ms=1_000,
    )
    result = OperationAgentMatchRunner(
        match,
        red_agent=NoOpAgent("agent-red-policy"),
        blue_agent=NoOpAgent("agent-blue-policy"),
    ).run(tmp_path / "published", max_decision_windows=2)

    archive = SourceReplayArchiveImporter().import_path(
        result.output_dir / "replay.zip"
    )
    assert result.truncated is True
    assert result.decision_window_count == 2
    assert result.frame_count == 21
    assert len(archive.frames) == 21
    assert len(archive.steps) == 20
    assert result.strict_replay_match is None
    assert result.online_offline_analysis_match is True
    assert (result.output_dir / "control_audit.native.jsonl").is_file()
