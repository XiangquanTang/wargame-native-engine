from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.adjudication.dice import DiceRoller
from wargame_native_engine.adjudication.direct_fire import (
    DirectFireAdjudicator, DirectFireResolution,
)
from wargame_native_engine.adjudication.request import DirectFireRequest, WeaponProfile
from wargame_native_engine.adjudication.table_loader import AdjudicationTableSet
from wargame_native_engine.compat.source_frame.models import ArmorClass, UnitDomain
from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.core.rng import NativeRNG
from wargame_native_engine.io.serialization import canonical_hash, file_sha256, json_clone
from wargame_native_engine.version import ENGINE_VERSION
from wargame_native_engine.visibility.result import VisibilityResult


DIRECT_FIRE_REPLAY_SCHEMA = "native-direct-fire-replay/1.0"


@dataclass(frozen=True, slots=True)
class DirectFireReplayVerification:
    matches: bool
    expected_resolution_hash: str
    actual_resolution_hash: str
    expected_final_state_hash: str
    actual_final_state_hash: str


def direct_fire_request_to_dict(request: DirectFireRequest) -> dict[str, Any]:
    attacker = OperatorState.from_dict(request.attacker.to_dict()).to_dict()
    target = OperatorState.from_dict(request.target.to_dict()).to_dict()
    passengers = [OperatorState.from_dict(item.to_dict()).to_dict()
                  for item in request.carried_passengers]
    return {
        "tick": request.tick, "episode_seed": request.episode_seed,
        "action_id": request.action_id,
        "attacker": attacker, "target": target,
        "weapon": {"weapon_id": request.weapon.weapon_id,
                   "weapon_class": request.weapon.weapon_class,
                   "maximum_range": request.weapon.maximum_range,
                   "target_domains": sorted(item.value for item in request.weapon.target_domains),
                   "ammunition_key": request.weapon.ammunition_key,
                   "cooldown_ticks": request.weapon.cooldown_ticks,
                   "requires_unfolded": request.weapon.requires_unfolded,
                   "can_fire_while_moving": request.weapon.can_fire_while_moving},
        "visibility": request.visibility.to_dict(), "distance": request.distance,
        "target_domain": request.target_domain.value,
        "target_armor": request.target_armor.value,
        "ammunition_before": request.ammunition_before,
        "weapon_cooldown_before": request.weapon_cooldown_before,
        "weapon_unfolded": request.weapon_unfolded,
        "elevation_difference": request.elevation_difference,
        "attacker_suppressed": request.attacker_suppressed,
        "attacker_moving": request.attacker_moving,
        "target_terrain": request.target_terrain,
        "target_concealed": request.target_concealed,
        "target_moving": request.target_moving,
        "target_stacked": request.target_stacked,
        "target_marching": request.target_marching,
        "attacker_transitioning": request.attacker_transitioning,
        "attacker_tank_on_water": request.attacker_tank_on_water,
        "attacker_hovering": request.attacker_hovering,
        "target_external_direct_fire_prohibited":
            request.target_external_direct_fire_prohibited,
        "carried_passengers": passengers,
        "metadata": json_clone(request.metadata),
    }


def direct_fire_request_from_dict(value: Mapping[str, Any]) -> DirectFireRequest:
    weapon = value["weapon"]
    visibility = value["visibility"]
    return DirectFireRequest(
        tick=int(value["tick"]), episode_seed=int(value["episode_seed"]),
        action_id=str(value["action_id"]),
        attacker=OperatorState.from_dict(value["attacker"]),
        target=OperatorState.from_dict(value["target"]),
        weapon=WeaponProfile(
            weapon_id=weapon["weapon_id"], weapon_class=str(weapon["weapon_class"]),
            maximum_range=int(weapon["maximum_range"]),
            target_domains=frozenset(UnitDomain(str(item))
                                     for item in weapon["target_domains"]),
            ammunition_key=str(weapon["ammunition_key"]),
            cooldown_ticks=(None if weapon.get("cooldown_ticks") is None
                            else int(weapon["cooldown_ticks"])),
            requires_unfolded=bool(weapon["requires_unfolded"]),
            can_fire_while_moving=bool(weapon["can_fire_while_moving"]),
        ),
        visibility=VisibilityResult(
            bool(visibility["line_of_sight"]), bool(visibility["observable"]),
            int(visibility["distance"]), float(visibility["observation_limit"]),
            tuple(int(item) for item in visibility["blocking_hexes"]),
            tuple(dict(item) for item in visibility["modifiers"]),
            str(visibility["reason"])),
        distance=int(value["distance"]),
        target_domain=UnitDomain(str(value["target_domain"])),
        target_armor=ArmorClass(str(value["target_armor"])),
        ammunition_before=int(value["ammunition_before"]),
        weapon_cooldown_before=int(value["weapon_cooldown_before"]),
        weapon_unfolded=bool(value["weapon_unfolded"]),
        elevation_difference=int(value["elevation_difference"]),
        attacker_suppressed=bool(value["attacker_suppressed"]),
        attacker_moving=bool(value["attacker_moving"]),
        target_terrain=str(value["target_terrain"]),
        target_concealed=bool(value["target_concealed"]),
        target_moving=bool(value["target_moving"]),
        target_stacked=bool(value["target_stacked"]),
        target_marching=bool(value["target_marching"]),
        attacker_transitioning=bool(value["attacker_transitioning"]),
        attacker_tank_on_water=bool(value.get("attacker_tank_on_water", False)),
        attacker_hovering=bool(value.get("attacker_hovering", True)),
        target_external_direct_fire_prohibited=bool(
            value["target_external_direct_fire_prohibited"]),
        carried_passengers=tuple(OperatorState.from_dict(item)
                                 for item in value["carried_passengers"]),
        metadata=json_clone(value.get("metadata", {})),
    )


def write_direct_fire_replay(root: str | Path, *,
                             request_document: Mapping[str, Any],
                             resolution: DirectFireResolution,
                             tables: AdjudicationTableSet,
                             extra_manifest: Mapping[str, Any] | None = None) -> None:
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    request_value = json_clone(request_document)
    resolution_value = resolution.to_dict()
    final_state = _final_state(resolution, request_value, tables=tables)
    _write(path / "request.json", request_value)
    _write(path / "resolution.json", resolution_value)
    _write(path / "final_state.json", final_state)
    (path / "events.jsonl").write_text(
        "".join(json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")) + "\n"
                for item in resolution.events), encoding="utf-8")
    collections = {name: file_sha256(path / name) for name in
                   ("request.json", "resolution.json", "final_state.json", "events.jsonl")}
    manifest = {
        "schema_version": DIRECT_FIRE_REPLAY_SCHEMA,
        "engine_version": ENGINE_VERSION,
        "table_set_id": tables.table_set_id,
        "table_set_version": tables.table_set_version,
        "table_set_hash": tables.content_hash,
        "table_file_hash": tables.file_hash,
        "source_document_hash": tables.source_document_hash,
        "episode_seed": int(request_value["episode_seed"]),
        "action_id": request_value["action_id"],
        "adjudication_id": resolution.adjudication_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "resolution_hash": canonical_hash(resolution_value),
        "final_state_hash": canonical_hash(final_state),
        "collection_hashes": collections,
        **json_clone(extra_manifest or {}),
    }
    _write(path / "manifest.json", manifest)


def replay_direct_fire(root: str | Path, *, tables: AdjudicationTableSet,
                       strict: bool = True) -> DirectFireReplayVerification:
    path = Path(root)
    manifest = _read(path / "manifest.json")
    if manifest.get("schema_version") != DIRECT_FIRE_REPLAY_SCHEMA:
        raise ValueError("unsupported direct-fire replay schema")
    mismatches = []
    for name, expected in manifest["collection_hashes"].items():
        if file_sha256(path / name) != expected:
            mismatches.append(name)
    if (manifest.get("table_set_id") != tables.table_set_id
            or manifest.get("table_set_version") != tables.table_set_version
            or manifest.get("table_set_hash") != tables.content_hash):
        mismatches.append("table_set")
    if mismatches and strict:
        raise ValueError("direct-fire replay compatibility mismatch: "
                         + ", ".join(sorted(mismatches)))
    request_document = _read(path / "request.json")
    request = direct_fire_request_from_dict(request_document)
    resolution = DirectFireAdjudicator(
        tables, DiceRoller(NativeRNG(request.episode_seed))).resolve(request)
    actual_resolution = resolution.to_dict()
    actual_final = {"attacker": request.attacker.to_dict(),
                    "target": request.target.to_dict(),
                    "carried_passengers": [item.to_dict()
                                           for item in request.carried_passengers],
                    "ammunition_after": resolution.ammunition_after,
                    "cooldown_after": resolution.cooldown_after}
    expected_resolution_hash = str(manifest["resolution_hash"])
    expected_final_hash = str(manifest["final_state_hash"])
    actual_resolution_hash = canonical_hash(actual_resolution)
    actual_final_hash = canonical_hash(actual_final)
    return DirectFireReplayVerification(
        expected_resolution_hash == actual_resolution_hash
        and expected_final_hash == actual_final_hash and not mismatches,
        expected_resolution_hash, actual_resolution_hash,
        expected_final_hash, actual_final_hash)


def _final_state(resolution: DirectFireResolution,
                 request_document: Mapping[str, Any], *,
                 tables: AdjudicationTableSet) -> dict[str, Any]:
    # Rebuild the post-adjudication state from the immutable request document and
    # the discrete outcome so strict replay can compare a canonical final hash.
    target = json_clone(request_document["target"])
    losses = 0 if resolution.outcome is None else (
        resolution.outcome.vehicle_losses + resolution.outcome.squad_losses)
    target["blood"] = max(0, int(target["blood"]) - losses)
    target["health"] = target["max_health"] * target["blood"] / target["max_blood"]
    if target["blood"] == 0:
        target["alive"] = False
        target["state"] = "destroyed"
    elif resolution.outcome is not None and resolution.outcome.suppression_applied:
        target["state"] = "SUPPRESSED"
        target.setdefault("raw_attributes", {})["suppression_remaining_seconds"] = int(
            tables.document["runtime_rules"]["suppression"]["effective_duration_seconds"])
    passengers = []
    for passenger in request_document["carried_passengers"]:
        value = json_clone(passenger)
        if resolution.outcome is not None and resolution.outcome.vehicle_losses:
            value["blood"] = max(0, int(value["blood"]) - resolution.outcome.vehicle_losses)
            value["health"] = value["max_health"] * value["blood"] / value["max_blood"]
            if value["blood"] == 0:
                value["alive"] = False
                value["state"] = "destroyed"
        passengers.append(value)
    return {"attacker": json_clone(request_document["attacker"]), "target": target,
            "carried_passengers": passengers,
            "ammunition_after": resolution.ammunition_after,
            "cooldown_after": resolution.cooldown_after}


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(json_clone(value), ensure_ascii=False,
                               sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _read(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"JSON root must be an object: {path}")
    return value
