"""Explicit, restricted-side bridge for an externally supplied Miaosuan Agent.

No uploaded code is imported here. This is a heuristic compatibility adapter,
not a reproduction of the original environment or a learned policy. The native
legal catalogue and NativeEngine remain the action and adjudication authorities.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
import random
import json
from threading import RLock
from typing import Any, Mapping, Sequence

from wargame_native_engine.core.action import ActionType, EngineAction, ValidAction
from wargame_native_engine.data.map_runtime import VISIBILITY_MODE_RECORDS
from wargame_native_engine.core.fire_capability import (
    IndirectFireCapability, IndirectFireCapabilityStatus,
)

ADAPTER_VERSION = "miaosuan-standalone/1.0"
POLICY = {
    "identity": "miaosuan_flexible_native_v1",
    "algorithm": "external_fixed_priority_rule_baseline_not_drl",
    "movement_policy": "original_city_route_next_hop_exact_native_candidate",
    "role_policy": "standalone_commander_role_1_all_own_units",
    "communication_policy": "unsupported_recorded_not_applied",
    "attack_level_policy": "equal_legal_candidates_stable_native_order",
    "unknown_enemy_observation_range": "None_not_inferred",
    "action_policy": "exact_native_legal_candidate_or_validated_indirect_builder",
    "duplicate_action_policy": "reject_repeated_native_action_id",
}
_RNG_LOCK = RLock()
_NUMERIC = {
    ActionType.MOVE: 1, ActionType.SHOOT: 2, ActionType.ATTACK: 2,
    ActionType.BOARD: 3, ActionType.UNLOAD: 4, ActionType.CAPTURE: 5,
    ActionType.CHANGE_STATE: 6, ActionType.STOP_MOVE: 10,
    ActionType.WEAPON_LOCK: 11, ActionType.WEAPON_UNFOLD: 12,
    ActionType.END_DEPLOYMENT: 333,
}


@dataclass(frozen=True)
class _VisibilityMode:
    """Scalar source indexing over the existing read-only mmap, never a copy."""
    lookup: Any
    mode: int

    def __getitem__(self, coordinates):
        r1, c1, r2, c2 = coordinates
        _, h1, h2, _ = VISIBILITY_MODE_RECORDS[self.mode]
        return self.lookup.visible(r1 * 100 + c1, r2 * 100 + c2,
                                   observer_height_m=h1, target_height_m=h2)

    def __deepcopy__(self, memo):
        return self


def build_miaosuan_setup(bundle, side: str, seat: int = 1) -> dict[str, Any]:
    """Project only static map resources; never disclose scenario enemy state."""
    if side not in {"red", "blue"}:
        raise ValueError("miaosuan_side_must_be_red_or_blue")
    runtime = bundle.map_runtime_data
    if runtime is None:
        raise ValueError("miaosuan_requires_validated_map_runtime_data")
    native_map = bundle.scenario.map
    cells = {cell.hex_id: cell for cell in native_map.hexes}
    rows, cols = runtime.movement.rows, runtime.movement.columns
    basic = [[cells[r * 100 + c].to_dict() for c in range(cols)]
             for r in range(rows)]
    costs = []
    for mode in range(4):
        grid = []
        for row in range(rows):
            values = []
            for col in range(cols):
                source = row * 100 + col
                edges = {}
                for target in cells[source].neighbors:
                    if target < 0:
                        continue
                    factor = runtime.movement.factor(mode, source, target)
                    if factor is not None:
                        edges[target] = float(factor)
                values.append(edges)
            grid.append(values)
        costs.append(grid)
    return {
        "scenario": bundle.scenario_identity.scenario_id,
        "basic_data": {"map_data": basic}, "cost_data": costs,
        "see_data": tuple(_VisibilityMode(runtime.visibility, mode) for mode in range(10)),
        "seat": seat, "faction": 0 if side == "red" else 1, "role": 1,
        "user_name": "native-standalone-baseline", "user_id": seat,
        "adapter": {"version": ADAPTER_VERSION, **POLICY,
                    "map_runtime_data": runtime.identity_record()},
    }


def _numeric_type(action: EngineAction) -> int | None:
    # The original guided_obj_id is the firing unit; obj_id is the guider.
    if action.action_type == ActionType.FIRE and action.parameters.get("fire_mode") == "GUIDED":
        return 9
    return _NUMERIC.get(action.action_type)


def _candidate(action: EngineAction) -> Any:
    kind = _numeric_type(action)
    if kind == 1:
        path = action.parameters.get("path", ())
        return path[-1] if path else None
    value = {}
    if kind in {2, 9}:
        value = {"target_obj_id": action.target_id,
                 "weapon_id": action.parameters.get("weapon_id"), "attack_level": 0}
        if kind == 9:
            value["guided_obj_id"] = action.parameters.get("shooter_id")
    elif kind == 11:
        value = {"weapon_id": action.parameters.get("weapon_id")}
    elif kind in {3, 4}:
        value = {"target_obj_id": action.target_id}
    elif kind == 6:
        value = {"target_state": action.parameters.get("state")}
    return value


@dataclass(frozen=True)
class MiaosuanActionBatch:
    actions: tuple[EngineAction, ...]
    trace: tuple[Mapping[str, Any], ...]


class MiaosuanBaselineAdapter:
    """Own one Agent lifecycle and its independent Python random stream.

    Call in a single-threaded worker: the lock isolates other adapter calls,
    but cannot guard unrelated threads using the process-global random module.
    Input snapshots are detached; exceptions propagate with RNG restored.
    """
    def __init__(self, agent, *, side: str, seat: int = 1, seed: int = 0,
                 source_sha256: str | None = None):
        if side not in {"red", "blue"}:
            raise ValueError("miaosuan_side_must_be_red_or_blue")
        if type(seat) is not int:
            raise ValueError("miaosuan_seat_must_be_int")
        self.agent, self.side, self.seat, self.seed = agent, side, seat, seed
        self.source_sha256 = source_sha256
        self._rng = random.Random(seed).getstate()
        self._setup = False

    @property
    def provenance(self):
        return {"adapter_version": ADAPTER_VERSION, **POLICY,
                "source_sha256": self.source_sha256, "seed": self.seed,
                "side": self.side, "seat": self.seat}

    def _call(self, method, *args):
        with _RNG_LOCK:
            previous = random.getstate()
            random.setstate(self._rng)
            try:
                return method(*args)
            finally:
                self._rng = random.getstate()
                random.setstate(previous)

    def setup(self, setup_info: Mapping[str, Any]):
        if self._setup:
            raise RuntimeError("miaosuan_reset_required_before_setup")
        if (setup_info.get("seat") != self.seat
                or setup_info.get("faction") != (0 if self.side == "red" else 1)
                or setup_info.get("role") != 1):
            raise ValueError("miaosuan_setup_identity_mismatch")
        self._call(self.agent.setup, deepcopy(dict(setup_info)))
        self._setup = True

    def reset(self):
        try:
            self._call(self.agent.reset)
        finally:
            self._setup = False
            self._rng = random.Random(self.seed).getstate()

    def step(self, side_frame: Mapping[str, Any],
             legal_actions: Sequence[ValidAction], *,
             indirect_capabilities: Sequence[IndirectFireCapability] = (),
             indirect_action_builder=None) -> MiaosuanActionBatch:
        if not self._setup:
            raise RuntimeError("miaosuan_not_setup")
        native = side_frame.get("extra", {}).get("native", {})
        if native.get("perspective") != self.side.upper():
            raise ValueError("miaosuan_requires_matching_restricted_side_frame")
        color = 0 if self.side == "red" else 1
        observation = deepcopy(dict(side_frame))
        own = {item["obj_id"] for key in ("operators", "passengers")
               for item in observation.get(key, ()) if item.get("color") == color}
        # Unknown enemy ranges stay unknown. [] otherwise causes the external
        # legacy helper to index a missing private range and raise IndexError.
        for item in observation.get("operators", ()):
            if item.get("color") != color and item.get("observe_distance") == []:
                item["observe_distance"] = None
        visible = {item["obj_id"] for item in observation.get("operators", ())
                   if item.get("color") != color}
        candidates = tuple(value.action for value in legal_actions
                           if value.action.side == self.side
                           and (value.action.actor_id in own or value.action.actor_id is None)
                           and (_numeric_type(value.action) not in {2, 9}
                                or value.action.target_id in visible)
                           and (_numeric_type(value.action) not in {3, 4}
                                or value.action.target_id in own))
        catalogue = defaultdict(lambda: defaultdict(list))
        for action in candidates:
            kind = _numeric_type(action)
            if kind == 1 and action.actor_id is not None:
                catalogue[action.actor_id][kind] = []
                continue
            if kind is not None and action.actor_id is not None:
                item = _candidate(action)
                if item is not None:
                    catalogue[action.actor_id][kind].append(item)
        indirect = tuple(value for value in indirect_capabilities
                         if type(value) is IndirectFireCapability
                         and value.shooter_id in own
                         and value.status is IndirectFireCapabilityStatus.AVAILABLE)
        if indirect_action_builder is not None:
            for capability in indirect:
                catalogue[capability.shooter_id][8].append({"weapon_id": capability.weapon_id})
        observation["valid_actions"] = {actor: dict(types) for actor, types in catalogue.items()}
        observation["role_and_grouping_info"] = {
            self.seat: {"role": 1, "faction": color, "operators": sorted(own)}}
        observation["communication"] = []
        raw = self._call(self.agent.step, observation)
        if not isinstance(raw, (list, tuple)):
            raise TypeError("miaosuan_step_must_return_action_list")
        translated, trace = [], []
        seen = set()
        for index, record in enumerate(raw):
            entry = {"index": index}
            try:
                entry["source_action"] = json.loads(json.dumps(record, allow_nan=False))
            except (TypeError, ValueError, OverflowError):
                entry.update(status="rejected", reason="action_not_json_serializable",
                             source_action_type=type(record).__name__)
                trace.append(entry)
                continue
            if not isinstance(record, Mapping):
                entry.update(status="rejected", reason="action_not_mapping")
            elif type(record.get("actor")) is not int or record.get("actor") != self.seat:
                entry.update(status="rejected", reason="seat_mismatch")
            elif type(record.get("type")) is not int:
                entry.update(status="rejected", reason="numeric_action_type_required")
            elif any(key in record and type(record[key]) is not int
                     for key in ("obj_id", "target_obj_id", "guided_obj_id")):
                entry.update(status="rejected", reason="integer_operator_id_required")
            elif record["type"] in {100, 200, 201, 202, 203, 204, 205, 206}:
                entry.update(status="unsupported", reason="standalone_communication_or_grouping")
            elif record["type"] not in set(_NUMERIC.values()) | {8, 9}:
                entry.update(status="unsupported", reason="unsupported_source_action_type")
            elif record.get("obj_id") not in own and record["type"] != 333:
                entry.update(status="rejected", reason="actor_not_owned")
            elif set(record) - _allowed_fields(record["type"]):
                entry.update(status="rejected", reason="unexpected_source_action_fields")
            else:
                matching = [action for action in candidates if _matches(record, action)]
                if record["type"] == 8:
                    capability = next((value for value in indirect
                                       if value.shooter_id == record.get("obj_id")
                                       and type(record.get("weapon_id")) is int
                                       and value.weapon_id == record["weapon_id"]), None)
                    if capability is None or indirect_action_builder is None:
                        entry.update(status="rejected", reason="indirect_capability_unavailable")
                        trace.append(entry)
                        continue
                    target_hex = record.get("jm_pos")
                    if type(target_hex) is not int or target_hex < 0:
                        entry.update(status="rejected", reason="indirect_target_hex_invalid")
                        trace.append(entry)
                        continue
                    try:
                        built = indirect_action_builder(capability, target_hex)
                    except (ValueError, TypeError):
                        entry.update(status="rejected", reason="native_indirect_builder_rejected")
                        trace.append(entry)
                        continue
                    if (type(built) is not EngineAction or built.side != self.side
                            or built.actor_id != capability.shooter_id
                            or built.action_type is not ActionType.FIRE
                            or built.parameters.get("fire_mode") != "INDIRECT"
                            or built.parameters.get("target_hex") != target_hex
                            or built.parameters.get("weapon_id") != capability.weapon_id
                            or built.target_id is not None):
                        entry.update(status="rejected", reason="native_indirect_builder_mismatch")
                        trace.append(entry)
                        continue
                    matching = [built]
                if matching:
                    selected = matching[0]
                    if selected.action_id in seen:
                        entry.update(status="rejected", reason="duplicate_native_action",
                                     native_action_id=selected.action_id)
                        trace.append(entry)
                        continue
                    seen.add(selected.action_id)
                    translated.append(deepcopy(selected))
                    entry.update(status="translated", native_action_id=selected.action_id,
                                 native_action=selected.to_dict(),
                                 matching_candidates=len(matching))
                else:
                    entry.update(status="rejected", reason="no_matching_native_legal_candidate")
            trace.append(entry)
        return MiaosuanActionBatch(tuple(translated), tuple(trace))


def _allowed_fields(kind: int) -> set[str]:
    fields = {"actor", "obj_id", "type"}
    fields.update({1: {"move_path"}, 2: {"target_obj_id", "weapon_id"},
                   3: {"target_obj_id"}, 4: {"target_obj_id"},
                   6: {"target_state"}, 8: {"jm_pos", "weapon_id"},
                   9: {"target_obj_id", "weapon_id", "guided_obj_id"}, 11: {"weapon_id"}}.get(kind, set()))
    return fields


def _matches(record: Mapping[str, Any], action: EngineAction) -> bool:
    kind = _numeric_type(action)
    if record["type"] != kind:
        return False
    if kind == 333:
        return record.get("obj_id") is None
    if record.get("obj_id") != action.actor_id:
        return False
    if kind == 1:
        path = record.get("move_path")
        return (isinstance(path, list) and all(type(value) is int for value in path)
                and path == list(action.parameters.get("path", ())))
    candidate = _candidate(action)
    if kind in {2, 3, 4, 6, 9, 11}:
        return all(type(record.get(key)) is type(value) and record.get(key) == value
                   for key, value in candidate.items()
                   if key != "attack_level")
    return True
