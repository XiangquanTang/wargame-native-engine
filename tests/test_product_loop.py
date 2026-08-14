from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

from wargame_native_engine.analysis import analyze_replay_to_jsonl
from wargame_native_engine.data import (
    DataProvenance,
    ScenarioBundle,
    ScenarioCatalog,
)
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.operations import WargameFacade


def _short_product_bundle() -> ScenarioBundle:
    record = load_scenario("examples/source_movement_scenario.json").to_dict()
    record["scenario_id"] = "product-loop-move-shoot"
    record["terminal_rules"]["max_ticks"] = 200
    return ScenarioBundle.from_record(
        record,
        provenance=DataProvenance(
            source_sha256=canonical_hash(record),
            source_kind="deterministic_test_derivative",
            warnings=("short terminal horizon for product integration test",),
        ),
    )


def test_multicell_move_shoot_replay_and_analysis_product_loop(
    tmp_path: Path,
) -> None:
    bundle = _short_product_bundle()
    app = WargameFacade(
        ScenarioCatalog.from_bundles({
            bundle.scenario_identity.scenario_id: bundle,
        }),
        storage_root=tmp_path / "managed",
    )
    match = app.create_match(
        scenario_id=bundle.scenario_identity.scenario_id,
        red_controller_id="product-red",
        blue_controller_id="product-blue",
        seed=42,
        decision_interval_ms=10_000,
        frame_interval_ms=1_000,
    )
    red = match.bind_controller("RED", controller_id="product-red")
    blue = match.bind_controller("BLUE", controller_id="product-blue")

    proposal = red.plan_move(1, 105, request_id="move-to-105")
    assert proposal.reachable is True
    assert proposal.ordered_path == (102, 103, 104, 105)
    assert proposal.estimated_duration_ms == sum(
        int(item["traversal_seconds"] * 1000)
        for item in proposal.segment_details
    )
    red.stage_action(proposal.action_id)
    red.submit_batch(request_id="red-window-0")
    blue.submit_batch(request_id="blue-window-0")

    saw_shoot = False
    submitted_shoot_action_id = None
    while not red.observe().terminal:
        view = red.observe()
        shoots = tuple(
            item
            for item in view.valid_actions
            if item["action"]["action_type"] == "SHOOT"
        )
        if shoots and not saw_shoot:
            saw_shoot = True
            submitted_shoot_action_id = shoots[0]["action_id"]
            red.stage_action(submitted_shoot_action_id)
        window = view.step_index
        red.submit_batch(request_id=f"red-window-{window}")
        blue.submit_batch(request_id=f"blue-window-{window}")
    assert saw_shoot is True

    output = tmp_path / "published"
    publication = match.gateway.publish_session(
        match.session_id,
        "product-red",
        str(output),
        truncated=False,
    )
    archive = SourceReplayArchiveImporter().import_path(publication.replay_path)
    verification = replay_source_archive(
        bundle.scenario,
        publication.replay_path,
        strict=True,
    )
    offline = tmp_path / "offline.jsonl"
    analyze_replay_to_jsonl(publication.replay_path, offline)

    assert verification.matches is True
    assert publication.frame_count == len(archive.frames)
    assert publication.step_count == len(archive.steps)
    assert publication.frame_count == publication.step_count + 1
    assert publication.analysis_path.read_bytes() == offline.read_bytes()
    assert len(publication.analysis_path.read_text(encoding="utf-8").splitlines()) == (
        publication.frame_count
    )
    assert archive.steps[0]["ordered_actions"][0]["parameters"]["path"] == (
        list(proposal.ordered_path)
    )
    assert any(
        any(
            action["action_type"] == "SHOOT"
            and action["target_id"] == 2
            for action in step["ordered_actions"]
        )
        for step in archive.steps
    )
    assert any(
        any(
            action.get("action_type") == "SHOOT"
            for action in frame.actions
        )
        for frame in archive.frames
    )
    assert submitted_shoot_action_id is not None
    positions = [
        next(unit.current_hex for unit in frame.operators if unit.obj_id == 1)
        for frame in archive.frames
    ]
    assert positions[0] == 101
    assert 105 in positions
    assert len(set(positions)) >= 5
    assert archive.manifest["scenario_hash"] == bundle.scenario.scenario_hash
    assert match.map_record["map_identity"]["sha256"] == bundle.map_identity.sha256
    with ZipFile(publication.replay_path) as stream:
        assert "frame_analysis.native.jsonl" not in stream.namelist()
        assert "control_audit.native.jsonl" not in stream.namelist()
