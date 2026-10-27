"""Conservative replay views from authoritative recorded Source visibility IDs.

This is not a regenerated observation or a second replay. Source NativeEngine's
_visible_enemies unions the current see_enemy_bop_ids of every friendly unit;
the exporter records those exact arrays after observation refresh. Only the
versioned Native Source hex path establishes that contract. Historical/generic
frames must not interpret missing arrays as an empty view.
"""
from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
import json
from pathlib import Path
from typing import Any

from wargame_native_engine.compat.source_frame.contract import (
    ANALYSIS_SOURCE_CONTRACT, CURRENT_SOURCE_CONTRACT, SOURCE_CONTRACT_KEY,
)
from wargame_native_engine.compat.source_frame.mapping import SourceMappings
from wargame_native_engine.compat.source_frame.models import SourceFrame
from wargame_native_engine.core.observation import (
    ObservedEnemyOperatorObservation, side_safe_enemy_observation,
)
from wargame_native_engine.io.importers.source_replay_archive import SourceReplayArchive
from wargame_native_engine.io.scenario import NativeScenario
from wargame_native_engine.io.serialization import json_clone
from wargame_native_engine.movement.policy import load_source_movement_policy
from wargame_native_engine.observations.perspective import Perspective, PerspectiveProjector
from wargame_native_engine.rules.perspective import load_perspective_policy


class ReplayPerspectiveUnavailable(ValueError):
    """Side evidence is absent or ambiguous; never substitute omniscience."""


# These historical publication contracts bind contact classification to the
# scenario. Playback retains their recorded facts, not current-rule execution.
_PUBLICATION_IDENTITY_SEMANTICS = frozenset({
    "native-joint-prestate/1.6",
    "native-joint-prestate/1.7",
    "native-joint-prestate/1.8",
    "native-joint-prestate/1.9",
    "native-joint-prestate/1.10",
})


@lru_cache(maxsize=1)
def _authority() -> tuple[Mapping[str, str], PerspectiveProjector, SourceMappings]:
    movement = load_source_movement_policy()
    policy = load_perspective_policy()
    return ({
        "movement_policy_id": movement.policy_id,
        "movement_policy_version": movement.policy_version,
        "movement_policy_hash": movement.content_hash,
        **policy.to_provenance(),
    }, PerspectiveProjector(policy), SourceMappings())


def validate_recorded_visibility(
    frame: SourceFrame, manifest: Mapping[str, Any], perspective: str,
) -> frozenset[int]:
    """Return precisely the recorded visible opposite-side IDs, or fail closed."""
    if perspective not in {"RED", "BLUE"}:
        raise ValueError("side perspective must be RED or BLUE")
    extra = frame.raw.get("extra")
    native = extra.get("native") if isinstance(extra, Mapping) else None
    contract = manifest.get(SOURCE_CONTRACT_KEY, manifest.get("frame_contract"))
    if (manifest.get("replay_format") != "native-source-frame-replay/1.0"
            or manifest.get("perspective") != "GREEN"
            or contract not in {CURRENT_SOURCE_CONTRACT, ANALYSIS_SOURCE_CONTRACT}
            or not isinstance(native, Mapping)
            or native.get(SOURCE_CONTRACT_KEY) != contract
            or native.get("perspective") != "GREEN"
            or native.get("protocol_version") != "source-compatible-environment/1.0"):
        raise ReplayPerspectiveUnavailable(
            "阵营视角不可用：录像缺少可验证的 Native Source 1.1/1.2 可见记录。"
        )
    identity, _, _ = _authority()
    if any(manifest.get(key) != value or native.get(key) != value
           for key, value in identity.items()):
        raise ReplayPerspectiveUnavailable(
            "阵营视角不可用：录像的 Source 观测规则或视角策略无法确认。"
        )
    color = 0 if perspective == "RED" else 1
    by_id = {unit.obj_id: unit for unit in frame.all_units}
    visible: set[int] = set()
    for observer in frame.all_units:
        if observer.color != color:
            continue
        ids = observer.raw.get("see_enemy_bop_ids")
        if not isinstance(ids, list) or any(type(value) is not int for value in ids):
            raise ReplayPerspectiveUnavailable(
                f"阵营视角不可用：单位 {observer.obj_id} 缺少或损坏了当前帧可见记录。"
            )
        if observer.raw.get("alive") is not True and ids:
            raise ReplayPerspectiveUnavailable(
                f"阵营视角不可用：单位 {observer.obj_id} 的可见记录与存活记录冲突。"
            )
        for identifier in ids:
            target = by_id.get(identifier)
            if (target is None or target.color != 1 - color
                    or target.raw.get("alive") is not True):
                raise ReplayPerspectiveUnavailable(
                    f"阵营视角不可用：单位 {observer.obj_id} 的可见目标 {identifier} 无法验证。"
                )
            visible.add(identifier)
    return frozenset(visible)


def published_contact_identities(
    root: Path, archive: SourceReplayArchive, scenario: NativeScenario,
) -> dict[int, dict[str, Any]]:
    """Use static identity only with an exact, publication-reported strict match.

    A scenario hash alone is insufficient: an arbitrary restored snapshot may
    replace Native subtype under that same scenario. This optional path trusts
    only the known publication report bound to this exact ZIP, seed and result;
    it does not claim that the viewer reran strict replay verification.
    """
    path = root / "match_summary.native.json"
    try:
        if path.stat().st_size > 512 * 1024:
            return {}
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    if not isinstance(report, Mapping):
        return {}
    manifest = archive.manifest
    if (report.get("summary_schema") != "native-realtime-match-summary/1.1"
            or report.get("strict_replay_match") is not True
            or report.get("replay_sha256") != archive.source_hash
            or manifest.get("adjudication_semantics_version") not in _PUBLICATION_IDENTITY_SEMANTICS
            or report.get("scenario_hash") != scenario.scenario_hash
            or manifest.get("scenario_hash") != scenario.scenario_hash
            or report.get("scenario_id") != scenario.scenario_id
            or manifest.get("scenario_id") != scenario.scenario_id
            or type(report.get("seed")) is not int
            or report.get("seed") < 0
            or type(manifest.get("episode_seed")) is not int
            or manifest.get("episode_seed") < 0
            or report.get("seed") != manifest.get("episode_seed")
            or not isinstance(report.get("final_state_hash"), str)
            or not report.get("final_state_hash")
            or report.get("final_state_hash") != manifest.get("final_state_hash")):
        return {}
    result = {}
    for state in scenario.operators:
        observed = side_safe_enemy_observation(state)
        # Retain public classification only, not scenario positions or roster in
        # a public endpoint. New/mismatching runtime IDs remain generic contacts.
        result[state.operator_id] = {
            key: getattr(observed, key)
            for key in ("side", "operator_type", "subtype", "source_type_code",
                        "source_subtype_code", "canonical_identity", "canonical_status", "domain")
        }
    return result


def recorded_side_frame(
    index: int, name: str, frame: SourceFrame,
    manifest: Mapping[str, Any], perspective: str,
    identities: Mapping[int, Mapping[str, Any]] | None = None,
) -> dict[str, object]:
    """Project only own records plus a public allowlisted contact snapshot.

    Enemy movement/state, health, ammunition and native analysis facts are not
    recoverable side observations. They remain UNKNOWN, even when GREEN has
    complete values. Contacts never inherit GREEN passenger membership.
    """
    from wargame_native_engine.viewer.replay_view import _viewer_unit, _unit_icon_key

    visible = validate_recorded_visibility(frame, manifest, perspective)
    color = 0 if perspective == "RED" else 1
    _, projector, mappings = _authority()
    own = {unit.obj_id for unit in frame.all_units if unit.color == color}
    included = own | visible
    operators: list[dict[str, Any]] = []
    passengers: list[dict[str, Any]] = []
    units: list[dict[str, Any]] = []
    for collection, category in ((frame.operators, "operator"),
                                 (frame.passengers, "passenger")):
        for unit in collection:
            if unit.obj_id not in own:
                continue
            # Reuse exactly the live projection's recursive relation cleaning.
            raw = projector._clean_relations(unit.raw, included)
            (operators if category == "operator" else passengers).append(raw)
            display = _viewer_unit(unit, category, mappings)
            display["carrier_id"] = raw.get("car")
            display["launcher_id"] = raw.get("launcher")
            units.append(display)
    by_id = {unit.obj_id: unit for unit in frame.all_units}
    for identifier in sorted(visible):
        unit = by_id[identifier]
        # GREEN template type/name need not equal the live Native contact's
        # identity. Their presence is not observation evidence: generic contact
        # symbols are deliberate, not a reason to backfill private identity.
        row, column = unit.row_col
        side = "blue" if color == 0 else "red"
        identity = (identities or {}).get(identifier)
        if identity is None or identity.get("side") != side:
            identity = {}
        observed = ObservedEnemyOperatorObservation(
            operator_id=identifier, side=side,
            operator_type=identity.get("operator_type", "UNKNOWN"), subtype=identity.get("subtype"),
            source_type_code=identity.get("source_type_code"),
            source_subtype_code=identity.get("source_subtype_code"),
            canonical_identity=identity.get("canonical_identity"),
            canonical_status=identity.get("canonical_status", "UNKNOWN"),
            domain=identity.get("domain", "UNKNOWN"),
            position=(float(column), float(row)), altitude=unit.altitude,
            alive=True, destroyed=False, motion_state="UNKNOWN",
        )
        raw = projector._visible_enemy(observed, Perspective[perspective])
        raw["extra"]["observation"]["mapping_status"] = "DERIVED_FROM_RECORDED_VISIBILITY"
        identity_source = "SCENARIO_DERIVED_IDENTITY" if identity else "UNKNOWN"
        raw["extra"]["observation"]["identity_source"] = identity_source
        if identity:
            raw["extra"]["observation"]["identity_verification"] = "PUBLICATION_REPORTED_STRICT_REPLAY_MATCH"
        operators.append(raw)
        # Start with unknowns rather than trimming a GREEN display record.
        units.append({
            "operator_id": identifier, "category": "operator",
            "side": observed.side, "name": raw["name"],
            "type_code": raw["type"], "sub_type_code": raw["sub_type"],
            "icon_key": _unit_icon_key(raw["type"], raw["sub_type"]),
            "identity_source": identity_source,
            "current_hex": unit.current_hex, "row": row, "column": column,
            "movement_target_hex": None, "movement_progress": None,
            "blood": None, "max_blood": None, "health_ratio": None,
            "alive": True, "destroyed": False, "state": "UNKNOWN",
            "stopped": None, "marching": None, "suppressed": None,
            "move_state": None, "direction": None, "lose_control": None,
            "can_to_move": None, "on_board": None,
            "carrier_id": None, "launcher_id": None,
        })
    return {
        "frame_index": index, "frame_name": name, "source_hash": frame.source_hash,
        "perspective": perspective,
        "visibility_status": "derived_from_recorded_visibility",
        "visibility_note": "来源：当前帧可见记录",
        "time": json_clone(frame.time.raw), "operators": operators,
        "passengers": passengers, "viewer_units": units,
        # This derived view does not claim complete side-filtered event evidence.
        "actions": [], "judge_info": [], "done": None, "winner": None,
        "outcome": None, "scenario_id": frame.scenario_id,
        "terrain_id": json_clone(frame.terrain_id),
        "cities": [json_clone(city.raw) for city in frame.cities],
    }
