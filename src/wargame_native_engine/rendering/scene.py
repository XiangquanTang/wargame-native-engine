"""One scene contract for replay, side-control and referee presentation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.gateway.models import ControllerView
from wargame_native_engine.io.serialization import json_clone
from wargame_native_engine.operations.proposals import ActionProposal
from wargame_native_engine.viewer.live_control import controller_view_record


@dataclass(frozen=True, slots=True)
class RenderScene:
    mode: str
    perspective: str
    map: Mapping[str, Any]
    frame: Mapping[str, Any]
    overlays: Mapping[str, Any]
    timeline: Mapping[str, Any] | None = None

    def to_record(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "perspective": self.perspective,
            "map": json_clone(self.map),
            "frame": json_clone(self.frame),
            "overlays": json_clone(self.overlays),
            "timeline": None if self.timeline is None else json_clone(self.timeline),
        }


def live_render_scene(
    bundle: ScenarioBundle,
    view: ControllerView,
    *,
    selected_unit_id: int | None = None,
    proposal: ActionProposal | None = None,
    shoot_target_ids: Sequence[int] = (),
) -> RenderScene:
    """Build a side-only Live scene from the canonical Bundle and view."""

    if view.perspective not in {"RED", "BLUE"}:
        raise ValueError("Live side scene requires RED or BLUE perspective")
    record = controller_view_record(view)
    map_record = bundle.render_map_record()
    terrain_id = record["source_frame"].get("terrain_id")
    if str(terrain_id) != bundle.map_identity.map_id:
        raise ValueError("controller frame map identity does not match ScenarioBundle")
    if proposal is not None:
        if proposal.session_id != view.session_id:
            raise ValueError("proposal session does not match rendered view")
        if proposal.side.upper() != view.perspective:
            raise ValueError("proposal side does not match rendered perspective")
        if proposal.decision_window_index != view.step_index:
            raise ValueError("proposal window does not match rendered view")
    overlays = {
        "selected_unit_id": selected_unit_id,
        "planned_path": [] if proposal is None else list(proposal.ordered_path),
        "planned_destination": (
            None if proposal is None else proposal.destination_hex
        ),
        "proposal_id": None if proposal is None else proposal.proposal_id,
        "proposal_action_id": None if proposal is None else proposal.action_id,
        "shoot_target_ids": [int(value) for value in shoot_target_ids],
        "adjudication_events": [],
    }
    return RenderScene(
        mode=f"live-{view.perspective.lower()}",
        perspective=view.perspective,
        map=json_clone(map_record),
        frame=json_clone(record["frame"]),
        overlays=overlays,
    )


def replay_render_scene(
    map_record: Mapping[str, Any],
    frame_record: Mapping[str, Any],
    *,
    timeline: Mapping[str, Any] | None = None,
    selected_unit_id: int | None = None,
) -> RenderScene:
    """Build the same scene shape from an already validated GREEN replay."""

    if not isinstance(frame_record.get("viewer_units"), list):
        raise ValueError("replay frame lacks viewer_units")
    return RenderScene(
        mode="replay",
        perspective="GREEN",
        map=json_clone(map_record),
        frame=json_clone(frame_record),
        overlays={
            "selected_unit_id": selected_unit_id,
            "planned_path": [],
            "planned_destination": None,
            "proposal_id": None,
            "proposal_action_id": None,
            "shoot_target_ids": [],
            "adjudication_events": json_clone(
                frame_record.get("judge_info", [])
            ),
        },
        timeline=None if timeline is None else json_clone(timeline),
    )
