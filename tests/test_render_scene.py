from __future__ import annotations

from pathlib import Path

import pytest

from wargame_native_engine.data import ScenarioBundle, ScenarioCatalog
from wargame_native_engine.operations import WargameFacade
from wargame_native_engine.rendering import live_render_scene, replay_render_scene


SCENARIO_PATH = Path("examples/source_movement_scenario.json")


@pytest.fixture
def match(tmp_path: Path):
    bundle = ScenarioBundle.from_path(SCENARIO_PATH)
    facade = WargameFacade(
        ScenarioCatalog.from_bundles({
            bundle.scenario_identity.scenario_id: bundle,
        }),
        storage_root=tmp_path / "render",
    )
    return facade.create_match(
        scenario_id=bundle.scenario_identity.scenario_id,
        red_controller_id="red-render",
        blue_controller_id="blue-render",
    )


def test_live_scenes_share_map_identity_but_not_hidden_units(match) -> None:
    red = match.bind_controller("RED", controller_id="red-render")
    blue = match.bind_controller("BLUE", controller_id="blue-render")
    red_scene = live_render_scene(match.bundle, red.observe())
    blue_scene = live_render_scene(match.bundle, blue.observe())

    assert red_scene.map["map_identity"] == blue_scene.map["map_identity"]
    assert red_scene.map["map_identity"]["sha256"] == (
        match.bundle.map_identity.sha256
    )
    red_ids = {item["operator_id"] for item in red_scene.frame["viewer_units"]}
    blue_ids = {item["operator_id"] for item in blue_scene.frame["viewer_units"]}
    assert 1 in red_ids
    assert 2 in blue_ids
    assert red_scene.perspective == "RED"
    assert blue_scene.perspective == "BLUE"
    assert "source_frame" not in red_scene.to_record()


def test_live_scene_path_overlay_is_exact_server_proposal_and_detached(match) -> None:
    red = match.bind_controller("RED", controller_id="red-render")
    proposal = red.plan_move(1, 106, request_id="render-route")
    scene = live_render_scene(
        match.bundle,
        red.observe(),
        selected_unit_id=1,
        proposal=proposal,
    )
    record = scene.to_record()

    assert record["overlays"]["planned_path"] == list(proposal.ordered_path)
    record["overlays"]["planned_path"].clear()
    assert scene.to_record()["overlays"]["planned_path"] == list(
        proposal.ordered_path
    )


def test_replay_scene_uses_the_identical_map_contract(match) -> None:
    red = match.bind_controller("RED", controller_id="red-render")
    live = live_render_scene(match.bundle, red.observe())
    replay_frame = live.to_record()["frame"]
    replay = replay_render_scene(
        match.bundle.render_map_record(),
        replay_frame,
    )

    assert replay.perspective == "GREEN"
    assert replay.map == live.map
    assert replay.frame["viewer_units"] == live.frame["viewer_units"]


def test_scene_rejects_cross_side_or_stale_proposal(match) -> None:
    red = match.bind_controller("RED", controller_id="red-render")
    blue = match.bind_controller("BLUE", controller_id="blue-render")
    proposal = red.plan_move(1, 105, request_id="red-only")

    with pytest.raises(ValueError, match="proposal side"):
        live_render_scene(match.bundle, blue.observe(), proposal=proposal)


def test_controller_frame_uses_fact_frame_not_decision_window_index(match) -> None:
    red = match.bind_controller("RED", controller_id="red-render")
    blue = match.bind_controller("BLUE", controller_id="blue-render")
    red.submit_batch(request_id="red-empty")
    blue.submit_batch(request_id="blue-empty")
    view = red.observe()
    scene = live_render_scene(match.bundle, view)

    assert view.step_index == 1
    assert scene.frame["frame_index"] == view.frame_index == 1
