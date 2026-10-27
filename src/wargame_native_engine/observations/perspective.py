from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Mapping, Sequence

from wargame_native_engine.compat.source_frame.contract import (
    ANALYSIS_SOURCE_CONTRACT, SOURCE_CONTRACT_KEY, is_dynamic_source_contract,
)
from wargame_native_engine.core.observation import (
    SideObservation,
    side_safe_enemy_observation,
)
from wargame_native_engine.io.serialization import canonical_hash, json_clone
from wargame_native_engine.rules.perspective import (
    PerspectivePolicy,
    load_perspective_policy,
)


class Perspective(IntEnum):
    RED = 0
    BLUE = 1
    GREEN = -1
    GOD = -1

    @property
    def side(self) -> str | None:
        return {Perspective.RED: "red", Perspective.BLUE: "blue"}.get(self)


@dataclass(frozen=True, slots=True)
class PerspectiveContext:
    red: SideObservation
    blue: SideObservation
    session_id: str
    public_metadata: Mapping[str, Any]
    own_weapon_inventory: tuple[Mapping[str, Any], ...] = ()

    def observation(self, perspective: Perspective) -> SideObservation:
        if perspective is Perspective.RED:
            return self.red
        if perspective is Perspective.BLUE:
            return self.blue
        raise ValueError("GREEN has no side observation")


@dataclass(frozen=True, slots=True)
class ObservationBundle:
    red: Mapping[str, Any]
    blue: Mapping[str, Any]
    green: Mapping[str, Any]

    def for_perspective(self, perspective: Perspective) -> Mapping[str, Any]:
        if perspective is Perspective.RED:
            return json_clone(self.red)
        if perspective is Perspective.BLUE:
            return json_clone(self.blue)
        if perspective in {Perspective.GREEN, Perspective.GOD}:
            return json_clone(self.green)
        raise ValueError(f"unsupported perspective: {perspective}")


class PerspectiveProjector:
    """Pure Source-frame projection; visibility is supplied, never computed here."""

    _TOP_LEVEL = (
        "scenario_id", "terrain_id", "time", "operators", "passengers",
        "cities", "landmarks", "jm_points", "scores", "valid_actions",
        "role_and_grouping_info", "communication", "actions", "judge_info",
        "extra",
    )
    _NATIVE_EVIDENCE_KEYS = frozenset({
        "native_evidence", "observation_evidence", "source_rule_set",
        "table_cell", "individual_dice", "raw_roll", "roll_index",
        "episode_seed", "rng", "rng_transition", "state_hash", "world_hash",
        "resolution_evidence", "base_dice", "correction_die", "modified_roll",
        "result_correction", "table_content_hash", "table_id", "modifiers",
    })
    _GREEN_ONLY_INDIRECT_EVENTS = frozenset({
        "indirect_fire_mission_created",
        "indirect_fire_mission_cancelled",
        "indirect_fire_active_started",
        "indirect_fire_area_triggered",
    })
    _ID_KEYS = frozenset({
        "obj_id", "actor_id", "operator_id", "attacker_id", "att_obj_id",
        "target_id", "target_obj_id", "partner_id", "car", "launcher",
    })

    def __init__(self, policy: PerspectivePolicy | None = None) -> None:
        self.policy = policy or load_perspective_policy()

    def project(self, green_frame: Mapping[str, Any],
                context: PerspectiveContext) -> ObservationBundle:
        original_hash = canonical_hash(green_frame)
        green = self._green(green_frame, context)
        red = self._side(green_frame, Perspective.RED, context)
        blue = self._side(green_frame, Perspective.BLUE, context)
        if canonical_hash(green_frame) != original_hash:
            raise RuntimeError("perspective projection mutated GREEN input")
        return ObservationBundle(red, blue, green)

    def _green(self, frame: Mapping[str, Any],
               context: PerspectiveContext) -> dict[str, Any]:
        result = json_clone(frame)
        stable = self._stable_root(frame)
        for key, value in stable.items():
            result.setdefault(key, value)
        source_extra = json_clone(frame.get("extra", {}))
        native = source_extra.get("native")
        if not isinstance(native, Mapping):
            native = {}
        source_extra["native"] = {
            **json_clone(native), **json_clone(context.public_metadata),
            **self.policy.to_provenance(), "perspective": "GREEN",
            "session_id": context.session_id,
            "current_step": int(result["time"].get("cur_step", 0)),
        }
        result["extra"] = source_extra
        return result

    def _side(self, frame: Mapping[str, Any], perspective: Perspective,
              context: PerspectiveContext) -> dict[str, Any]:
        side = perspective.side
        if side is None:
            raise ValueError("side projection requires RED or BLUE")
        observation = context.observation(perspective)
        friendly_ids = {value.operator_id for value in observation.friendly_operators}
        visible_by_id = {
            value.operator_id: side_safe_enemy_observation(
                value,
                policy=self.policy,
            )
            for value in observation.visible_enemies
        }
        included_ids = friendly_ids | set(visible_by_id)
        source_units = {
            int(item["obj_id"]): item
            for name in ("operators", "passengers")
            for item in frame.get(name, [])
            if isinstance(item, Mapping) and item.get("obj_id") is not None
        }
        passenger_ids = {
            int(item["obj_id"]) for item in frame.get("passengers", [])
            if isinstance(item, Mapping) and item.get("obj_id") is not None
        }
        operators: list[Mapping[str, Any]] = []
        passengers: list[Mapping[str, Any]] = []
        for operator_id in sorted(included_ids):
            if operator_id in friendly_ids:
                source = source_units.get(operator_id)
                if source is None:
                    continue
                projected = self._clean_relations(source, included_ids)
                if is_dynamic_source_contract(context.public_metadata.get(SOURCE_CONTRACT_KEY)):
                    # OWN inventory was detached by the session read model;
                    # never backfill inventory from GREEN or enemy attributes.
                    inventory = next((item for item in context.own_weapon_inventory
                                      if item["operator_id"] == operator_id), None)
                    status = source.get("extra", {}).get("native", {}).get(
                        "ammunition_projection", {})
                    projected.setdefault("extra", {})["native"] = {
                        "weapon_inventory": json_clone(inventory),
                        "ammunition_projection": {
                            key: json_clone(status[key])
                            for key in ("status", "unmapped_source_slots") if key in status
                        },
                    }
                    if isinstance(inventory, Mapping) and type(inventory.get("command_lost")) is bool:
                        projected["extra"]["native"]["command_lost"] = inventory["command_lost"]
                    if context.public_metadata.get(SOURCE_CONTRACT_KEY) == ANALYSIS_SOURCE_CONTRACT:
                        facts = source.get("extra", {}).get("native", {}).get("analysis_facts")
                        if isinstance(facts, Mapping):
                            own_facts = json_clone(facts)
                            boarding = own_facts.get("boarding")
                            if isinstance(boarding, dict):
                                for key in ("mounted_on", "passenger_id"):
                                    if boarding.get(key) is not None and boarding[key] not in included_ids:
                                        boarding.pop(key, None)
                            projected["extra"]["native"]["analysis_facts"] = own_facts
            else:
                projected = self._visible_enemy(visible_by_id[operator_id], perspective)
            if operator_id in friendly_ids and operator_id in passenger_ids:
                passengers.append(projected)
            else:
                # Enemy collection membership must not be backfilled from the
                # GREEN passenger ledger.  A current observed contact is one
                # side-safe operator record; hidden boarding relations remain
                # invisible.
                operators.append(projected)
        result = self._stable_root(frame)
        result.update({
            "operators": operators,
            "passengers": passengers,
            "valid_actions": self._valid_actions(frame.get("valid_actions", {}),
                                                   side, included_ids),
            "actions": [json_clone(value) for value in frame.get("actions", [])
                        if isinstance(value, Mapping)
                        and str(value.get("side", "")).lower() == side
                        and value.get("accepted", True) is not False],
            "judge_info": self._judge_info(frame.get("judge_info", []),
                                             friendly_ids, set(visible_by_id),
                                             side),
            "role_and_grouping_info": self._owned(
                frame.get("role_and_grouping_info", {}), side,
                public_unknown=False),
            "communication": self._owned(frame.get("communication", []), side,
                                           public_unknown=False),
            "jm_points": self._owned(frame.get("jm_points", []), side,
                                       public_unknown=False),
            "landmarks": self._owned(frame.get("landmarks", {}), side,
                                      public_unknown=True),
            "extra": self._side_extra(perspective, context,
                                       int(result["time"].get("cur_step", 0))),
        })
        return result

    def _stable_root(self, frame: Mapping[str, Any]) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "scenario_id": "", "terrain_id": None, "time": {},
            "operators": [], "passengers": [], "cities": [], "landmarks": {},
            "jm_points": [], "scores": {}, "valid_actions": {},
            "role_and_grouping_info": {}, "communication": [], "actions": [],
            "judge_info": [], "extra": {},
        }
        return {key: json_clone(frame.get(key, defaults[key]))
                for key in self._TOP_LEVEL}

    @staticmethod
    def _visible_enemy(value: Any, perspective: Perspective) -> dict[str, Any]:
        x, y = value.position
        current_hex = int(round(y)) * 100 + int(round(x))
        stopped = (
            True if value.motion_state == "STATIONARY" else
            False if value.motion_state == "MOVING" else None
        )
        return {
            "obj_id": value.operator_id,
            "color": 1 if perspective is Perspective.RED else 0,
            "type": (-1 if value.source_type_code is None
                     else value.source_type_code),
            "sub_type": (-1 if value.source_subtype_code is None
                         else value.source_subtype_code),
            "name": (value.canonical_identity or value.subtype or "UNKNOWN"),
            "owner": None,
            "armor": -1, "A1": None, "value": None,
            "cur_hex": current_hex, "cur_pos": 0.0,
            "basic_speed": None, "speed": None, "move_state": None,
            "stop": stopped, "move_path": [],
            "move_to_stop_remain_time": None,
            "blood": None, "max_blood": None, "stack": None, "keep": None,
            "keep_remain_time": None, "close_combat": None,
            "alive_remain_time": None, "alive": bool(value.alive),
            "altitude": value.altitude, "state": value.motion_state,
            "carry_weapon_ids": [], "remain_bullet_nums": {},
            "remain_bullet_nums_bk": {}, "observe_distance": [],
            "weapon_cool_time": None, "weapon_unfold_time": None,
            "weapon_unfold_state": None, "guide_ability": None,
            "passenger_ids": [], "car": None, "launcher": None,
            "launch_ids": [], "on_board": None, "loading_capacity": None,
            "valid_passenger_types": [], "max_passenger_nums": None,
            "see_enemy_bop_ids": [],
            "extra": {"observation": {
                "operator_type": value.operator_type,
                "subtype": value.subtype,
                "source_type_code": value.source_type_code,
                "source_subtype_code": value.source_subtype_code,
                "canonical_identity": value.canonical_identity,
                "canonical_status": value.canonical_status,
                "domain": value.domain,
                "motion_state": value.motion_state,
                "mapping_status": "SIDE_OBSERVATION",
            }},
        }

    def _clean_relations(self, value: Mapping[str, Any],
                         allowed_ids: set[int]) -> Mapping[str, Any]:
        def clean(item: Any, key: str | None = None) -> Any:
            if key in self.policy.relation_id_fields:
                if key == "owner" and _int_or_none(item) is None:
                    return json_clone(item)
                if isinstance(item, list):
                    return [identifier for identifier in item
                            if _int_or_none(identifier) in allowed_ids]
                identifier = _int_or_none(item)
                return item if identifier in allowed_ids else None
            if isinstance(item, Mapping):
                return {str(name): clean(child, str(name))
                        for name, child in item.items()}
            if isinstance(item, list):
                return [clean(child) for child in item]
            return json_clone(item)
        result = clean(value)
        extra = result.get("extra")
        if isinstance(extra, Mapping) and "native" in extra:
            extra = dict(extra)
            extra.pop("native", None)
            result["extra"] = extra
        result["see_enemy_bop_ids"] = [identifier for identifier in
                                        result.get("see_enemy_bop_ids", [])
                                        if identifier in allowed_ids]
        return result

    def _valid_actions(self, value: Any, side: str,
                       allowed_ids: set[int]) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            return {}
        result: dict[str, list[Any]] = {}
        for key, records in value.items():
            candidates = records if isinstance(records, list) else []
            kept = []
            for record in candidates:
                if not isinstance(record, Mapping):
                    continue
                action = record.get("action", record)
                if not isinstance(action, Mapping):
                    continue
                if str(action.get("side", "")).lower() != side:
                    continue
                actor = _int_or_none(action.get("actor_id"))
                target = _int_or_none(action.get("target_id"))
                if actor is not None and actor not in allowed_ids:
                    continue
                if target is not None and target not in allowed_ids:
                    continue
                if self._referenced_ids(action) - allowed_ids:
                    continue
                kept.append(json_clone(record))
            if kept:
                result[str(key)] = kept
        return result

    def _judge_info(self, value: Any, friendly_ids: set[int],
                    visible_enemy_ids: set[int], side: str) -> list[Any]:
        if not isinstance(value, list):
            return []
        allowed = friendly_ids | visible_enemy_ids
        result = []
        for record in value:
            if not isinstance(record, Mapping):
                continue
            if record.get("event_type") in self._GREEN_ONLY_INDIRECT_EVENTS:
                continue
            if (
                record.get("event_type") == "indirect_fire_resolved"
                and str(record.get("side", "")).lower() != side
            ):
                continue
            record = json_clone(record)
            nested_result = record.get("result")
            events = (
                nested_result.get("events")
                if isinstance(nested_result, Mapping)
                else None
            )
            if isinstance(events, list):
                owned_events = []
                for event in events:
                    if not isinstance(event, Mapping):
                        owned_events.append(event)
                        continue
                    event_type = event.get("event_type")
                    if event_type in self._GREEN_ONLY_INDIRECT_EVENTS:
                        continue
                    if (
                        event_type == "indirect_fire_resolved"
                        and str(event.get("side", "")).lower() != side
                    ):
                        continue
                    owned_events.append(event)
                if events and not owned_events:
                    continue
                nested_result["events"] = owned_events
            referenced = self._referenced_ids(record)
            if referenced - allowed:
                continue
            result.append(self._strip_native_evidence(record))
        return result

    def _referenced_ids(self, value: Any, key: str | None = None) -> set[int]:
        found: set[int] = set()
        if key in self._ID_KEYS:
            identifier = _int_or_none(value)
            if identifier is not None:
                found.add(identifier)
        if isinstance(value, Mapping):
            for name, item in value.items():
                found.update(self._referenced_ids(item, str(name)))
        elif isinstance(value, list):
            for item in value:
                found.update(self._referenced_ids(item))
        return found

    def _strip_native_evidence(self, value: Any, key: str | None = None) -> Any:
        if isinstance(value, Mapping):
            return {str(name): self._strip_native_evidence(item, str(name))
                    for name, item in value.items()
                    if str(name) not in self._NATIVE_EVIDENCE_KEYS}
        if isinstance(value, list):
            return [self._strip_native_evidence(item) for item in value]
        return json_clone(value)

    def _owned(self, value: Any, side: str, *, public_unknown: bool) -> Any:
        if isinstance(value, list):
            return [json_clone(item) for item in value
                    if self._ownership(item) in {side, "public"}
                    or (public_unknown and self._ownership(item) is None)]
        if isinstance(value, Mapping):
            result = {}
            for key, item in value.items():
                key_side = _normalize_side(key)
                owner = key_side or self._ownership(item)
                if owner in {side, "public"} or (public_unknown and owner is None):
                    result[str(key)] = json_clone(item)
            return result
        return json_clone(value) if public_unknown else []

    @staticmethod
    def _ownership(value: Any) -> str | None:
        if not isinstance(value, Mapping):
            return None
        for key in ("side", "faction", "owner", "color"):
            if key in value:
                return _normalize_side(value[key])
        if bool(value.get("public", False)):
            return "public"
        return None

    def _side_extra(self, perspective: Perspective, context: PerspectiveContext,
                    current_step: int) -> Mapping[str, Any]:
        candidates = {
            **json_clone(context.public_metadata),
            "perspective": perspective.name,
            "current_step": current_step,
            "session_id": context.session_id,
            **self.policy.to_provenance(),
        }
        if is_dynamic_source_contract(candidates.get(SOURCE_CONTRACT_KEY)):
            allowed = self.policy.side_extra_allowlist | {SOURCE_CONTRACT_KEY}
        else:
            allowed = self.policy.side_extra_allowlist
        return {"native": {key: candidates[key]
                           for key in sorted(allowed)
                           if key in candidates}}


def _normalize_side(value: Any) -> str | None:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"red", "0"}:
            return "red"
        if normalized in {"blue", "1"}:
            return "blue"
        if normalized in {"public", "green", "-1"}:
            return "public"
    if value == 0:
        return "red"
    if value == 1:
        return "blue"
    if value == -1:
        return "public"
    return None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
