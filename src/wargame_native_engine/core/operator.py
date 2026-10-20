from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from wargame_native_engine.io.serialization import json_clone
from wargame_native_engine.core.concealment import ConcealmentPosture
from wargame_native_engine.rules.guidance import GuidanceAffiliationEvidence


class MovementTaskPauseReason(str, Enum):
    """Typed reason that owns a resumable authoritative movement task."""

    SUPPRESSION = "SUPPRESSION"


@dataclass(slots=True)
class OperatorState:
    operator_id: int
    side: str
    operator_type: str
    subtype: str | None
    x: float
    y: float
    altitude: float
    heading: float
    health: float
    max_health: float
    speed: float
    max_speed: float
    move_range: float
    sensor_range: float
    state: str
    alive: bool
    weapon_ids: list[int]
    raw_attributes: dict[str, Any] = field(default_factory=dict)
    attack_range: float = 0.0
    attack_damage: float = 0.0
    deployed: bool = False
    blood: int | None = None
    max_blood: int | None = None
    basic_speed: float | None = None
    current_hex: int | None = None
    cur_pos_millionths: int = 0
    move_path: list[int] = field(default_factory=list)
    movement_mode: str = "NORMAL"
    stopped: bool = True
    move_to_stop_remaining_ms: int = 0
    fatigue_level: int = 0
    fatigue_recovery_elapsed_ms: int = 0
    movement_action_id: str | None = None
    movement_action_sequence: int | None = None
    movement_stop_requested: bool = False
    movement_blocked_reason: str | None = None
    movement_task_pause_reason: MovementTaskPauseReason | None = None
    segment_from_hex: int | None = None
    segment_to_hex: int | None = None
    segment_elapsed_ms: int = 0
    segment_traversal_ms: int = 0
    segment_event_id: str | None = None
    transition_kind: str | None = None
    transition_remaining_ms: int = 0
    weapon_locked: bool = False
    weapon_unfolded: bool = True
    weapon_cooldown_remaining_ms: int = 0
    suppression_remaining_ms: int = 0
    guidance_profile_id: str | None = None
    guidance_affiliation_id: int | None = None
    guided_fire_recovery_remaining_ms: int = 0
    indirect_fire_planned_cooldown_remaining_ms: int = 0
    guidance_affiliation_evidence: GuidanceAffiliationEvidence | None = None
    boarding_profile_id: str | None = None
    mounted_on: int | None = None
    passenger_id: int | None = None
    movement_locked: bool = False
    concealment_posture: ConcealmentPosture | None = None
    lifecycle_profile_id: str | None = None
    lifecycle_remaining_ms: int | None = None

    def __post_init__(self) -> None:
        if self.operator_id < 0:
            raise ValueError("operator_id must be non-negative")
        if self.max_health <= 0 or not 0 <= self.health <= self.max_health:
            raise ValueError("health must be within [0, max_health]")
        for name in ("max_speed", "move_range", "sensor_range",
                     "attack_range", "attack_damage"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.health == 0:
            self.alive = False
            self.state = "destroyed"
        if self.blood is None:
            self.blood = int(round(self.health))
        if self.max_blood is None:
            self.max_blood = int(round(self.max_health))
        if self.max_blood <= 0 or not 0 <= self.blood <= self.max_blood:
            raise ValueError("blood must be within [0, max_blood]")
        if self.blood == 0:
            self.alive = False
            self.state = "destroyed"
        self.weapon_ids = [int(item) for item in self.weapon_ids]
        if self.basic_speed is None:
            self.basic_speed = self.max_speed
        if self.basic_speed < 0:
            raise ValueError("basic_speed must be non-negative")
        if not 0 <= self.cur_pos_millionths <= 1_000_000:
            raise ValueError("cur_pos_millionths must be within [0, 1000000]")
        for name in ("move_to_stop_remaining_ms", "fatigue_level",
                     "fatigue_recovery_elapsed_ms", "segment_elapsed_ms",
                     "segment_traversal_ms", "transition_remaining_ms",
                     "weapon_cooldown_remaining_ms", "suppression_remaining_ms",
                     "guided_fire_recovery_remaining_ms",
                     "indirect_fire_planned_cooldown_remaining_ms"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.guidance_profile_id is not None and (
            not isinstance(self.guidance_profile_id, str)
            or not self.guidance_profile_id.strip()
        ):
            raise ValueError("guidance_profile_id must be a non-empty string or None")
        if self.guidance_affiliation_id is not None:
            if (
                type(self.guidance_affiliation_id) is not int
                or self.guidance_affiliation_id < 0
            ):
                raise ValueError(
                    "guidance_affiliation_id must be a non-negative exact int or None"
                )
            if self.guidance_affiliation_id == self.operator_id:
                raise ValueError("guidance affiliation cannot reference the same operator")
            if self.guidance_affiliation_evidence is None:
                raise ValueError("guidance affiliation requires explicit evidence")
        elif self.guidance_affiliation_evidence is not None:
            raise ValueError("guidance affiliation evidence requires an affiliation id")
        if (
            self.guidance_affiliation_evidence is not None
            and not isinstance(
                self.guidance_affiliation_evidence,
                GuidanceAffiliationEvidence,
            )
        ):
            raise TypeError(
                "guidance_affiliation_evidence must be GuidanceAffiliationEvidence"
            )
        if self.boarding_profile_id is not None and (
            not isinstance(self.boarding_profile_id, str)
            or not self.boarding_profile_id.strip()
        ):
            raise ValueError("boarding_profile_id must be non-empty or None")
        for name in ("mounted_on", "passenger_id"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a non-negative exact int or None")
            if value == self.operator_id:
                raise ValueError(f"{name} cannot reference the same operator")
        if self.mounted_on is not None and self.passenger_id is not None:
            raise ValueError("an operator cannot be passenger and carrier")
        if type(self.movement_locked) is not bool:
            raise TypeError("movement_locked must be boolean")
        if (
            self.movement_task_pause_reason is not None
            and not isinstance(
                self.movement_task_pause_reason,
                MovementTaskPauseReason,
            )
        ):
            self.movement_task_pause_reason = MovementTaskPauseReason(
                str(self.movement_task_pause_reason).upper()
            )
        if self.lifecycle_profile_id is not None and (
            not isinstance(self.lifecycle_profile_id, str)
            or not self.lifecycle_profile_id.strip()
        ):
            raise ValueError("lifecycle_profile_id must be non-empty or None")
        if self.lifecycle_remaining_ms is not None and (
            type(self.lifecycle_remaining_ms) is not int
            or self.lifecycle_remaining_ms < 0
        ):
            raise ValueError(
                "lifecycle_remaining_ms must be a non-negative exact int or None"
            )
        if (self.lifecycle_profile_id is None) != (
            self.lifecycle_remaining_ms is None
        ):
            raise ValueError(
                "lifecycle profile and remaining time must be specified together"
            )
        self.raw_attributes = json_clone(self.raw_attributes)
        if self.concealment_posture is None:
            # Explicit compatibility migration at the construction boundary.
            # Rule code never reads the legacy raw flag after this point.
            self.concealment_posture = (
                ConcealmentPosture.CONCEALED
                if self.raw_attributes.get("concealed") is True
                else ConcealmentPosture.EXPOSED
            )
        elif not isinstance(self.concealment_posture, ConcealmentPosture):
            self.concealment_posture = ConcealmentPosture(
                str(self.concealment_posture).upper()
            )
        self.set_concealment_posture(
            self.concealment_posture,
            preserve_absent_exposed_key=True,
        )
        self.move_path = [int(item) for item in self.move_path]

    @property
    def position(self) -> tuple[float, float]:
        return (self.x, self.y)

    @property
    def cur_pos(self) -> float:
        return self.cur_pos_millionths / 1_000_000.0

    @property
    def moving(self) -> bool:
        return self.alive and self.segment_to_hex is not None

    @property
    def concealed(self) -> bool:
        return self.concealment_posture is ConcealmentPosture.CONCEALED

    def set_concealment_posture(
        self,
        posture: ConcealmentPosture,
        *,
        preserve_absent_exposed_key: bool = False,
    ) -> None:
        """Set typed authority and its legacy compatibility projection."""

        if not isinstance(posture, ConcealmentPosture):
            raise TypeError("posture must be ConcealmentPosture")
        self.concealment_posture = posture
        if (
            posture is ConcealmentPosture.EXPOSED
            and preserve_absent_exposed_key
            and "concealed" not in self.raw_attributes
        ):
            return
        self.raw_attributes["concealed"] = (
            posture is ConcealmentPosture.CONCEALED
        )

    def to_dict(self) -> dict[str, Any]:
        value = {
            "operator_id": self.operator_id,
            "side": self.side,
            "operator_type": self.operator_type,
            "subtype": self.subtype,
            "x": self.x,
            "y": self.y,
            "altitude": self.altitude,
            "heading": self.heading,
            "health": self.health,
            "max_health": self.max_health,
            "speed": self.speed,
            "max_speed": self.max_speed,
            "move_range": self.move_range,
            "sensor_range": self.sensor_range,
            "state": self.state,
            "alive": self.alive,
            "weapon_ids": list(self.weapon_ids),
            "raw_attributes": json_clone(self.raw_attributes),
            "attack_range": self.attack_range,
            "attack_damage": self.attack_damage,
            "deployed": self.deployed,
            "blood": self.blood,
            "max_blood": self.max_blood,
        }
        runtime_state = {
            "basic_speed": self.basic_speed,
            "cur_pos_millionths": self.cur_pos_millionths,
            "move_path": list(self.move_path),
            "movement_mode": self.movement_mode,
            "stopped": self.stopped,
            "move_to_stop_remaining_ms": self.move_to_stop_remaining_ms,
            "fatigue_level": self.fatigue_level,
            "fatigue_recovery_elapsed_ms": self.fatigue_recovery_elapsed_ms,
            "movement_action_id": self.movement_action_id,
            "movement_action_sequence": self.movement_action_sequence,
            "movement_stop_requested": self.movement_stop_requested,
            "movement_blocked_reason": self.movement_blocked_reason,
            "segment_from_hex": self.segment_from_hex,
            "segment_to_hex": self.segment_to_hex,
            "segment_elapsed_ms": self.segment_elapsed_ms,
            "segment_traversal_ms": self.segment_traversal_ms,
            "segment_event_id": self.segment_event_id,
            "transition_kind": self.transition_kind,
            "transition_remaining_ms": self.transition_remaining_ms,
            "weapon_locked": self.weapon_locked,
            "weapon_unfolded": self.weapon_unfolded,
            "weapon_cooldown_remaining_ms": self.weapon_cooldown_remaining_ms,
            "suppression_remaining_ms": self.suppression_remaining_ms,
        }
        if self.current_hex is not None:
            # Preserve the historical on-map wire shape exactly.  Spatial
            # presence controls only the position field, not the eligibility
            # of the remaining typed operator state for serialization.
            value["basic_speed"] = runtime_state.pop("basic_speed")
            value["current_hex"] = self.current_hex
            value.update(runtime_state)
        else:
            # Off-map and mounted operators remain complete authoritative
            # OperatorState objects.  Emit only non-default runtime values so
            # legacy default-valued snapshots keep their existing wire/hash
            # representation while meaningful state survives clone/restore.
            runtime_defaults = {
                "basic_speed": self.max_speed,
                "cur_pos_millionths": 0,
                "move_path": [],
                "movement_mode": "NORMAL",
                "stopped": True,
                "move_to_stop_remaining_ms": 0,
                "fatigue_level": 0,
                "fatigue_recovery_elapsed_ms": 0,
                "movement_action_id": None,
                "movement_action_sequence": None,
                "movement_stop_requested": False,
                "movement_blocked_reason": None,
                "segment_from_hex": None,
                "segment_to_hex": None,
                "segment_elapsed_ms": 0,
                "segment_traversal_ms": 0,
                "segment_event_id": None,
                "transition_kind": None,
                "transition_remaining_ms": 0,
                "weapon_locked": False,
                "weapon_unfolded": True,
                "weapon_cooldown_remaining_ms": 0,
                "suppression_remaining_ms": 0,
            }
            value.update({
                name: item
                for name, item in runtime_state.items()
                if item != runtime_defaults[name]
            })
        if self.movement_task_pause_reason is not None:
            value["movement_task_pause_reason"] = (
                self.movement_task_pause_reason.value
            )
        if (
            self.guidance_profile_id is not None
            or self.guidance_affiliation_id is not None
            or self.guided_fire_recovery_remaining_ms != 0
            or self.guidance_affiliation_evidence is not None
        ):
            value.update({
                "guidance_profile_id": self.guidance_profile_id,
                "guidance_affiliation_id": self.guidance_affiliation_id,
                "guided_fire_recovery_remaining_ms": (
                    self.guided_fire_recovery_remaining_ms
                ),
                "guidance_affiliation_evidence": (
                    None
                    if self.guidance_affiliation_evidence is None
                    else self.guidance_affiliation_evidence.to_dict()
                ),
            })
        if self.indirect_fire_planned_cooldown_remaining_ms != 0:
            value["indirect_fire_planned_cooldown_remaining_ms"] = (
                self.indirect_fire_planned_cooldown_remaining_ms
            )
        if (
            self.boarding_profile_id is not None
            or self.mounted_on is not None
            or self.passenger_id is not None
            or self.movement_locked
        ):
            value.update({
                "boarding_profile_id": self.boarding_profile_id,
                "mounted_on": self.mounted_on,
                "passenger_id": self.passenger_id,
                "movement_locked": self.movement_locked,
            })
        if self.lifecycle_profile_id is not None:
            value.update({
                "lifecycle_profile_id": self.lifecycle_profile_id,
                "lifecycle_remaining_ms": self.lifecycle_remaining_ms,
            })
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OperatorState":
        return cls(
            operator_id=int(value["operator_id"]),
            side=str(value["side"]),
            operator_type=str(value["operator_type"]),
            subtype=None if value.get("subtype") is None else str(value["subtype"]),
            x=float(value.get("x", 0.0)), y=float(value.get("y", 0.0)),
            altitude=float(value.get("altitude", 0.0)),
            heading=float(value.get("heading", 0.0)),
            health=float(value.get("health", value.get("max_health", 100.0))),
            max_health=float(value.get("max_health", 100.0)),
            speed=float(value.get("speed", 0.0)),
            max_speed=float(value.get("max_speed", value.get("move_range", 0.0))),
            move_range=float(value.get("move_range", 0.0)),
            sensor_range=float(value.get("sensor_range", 0.0)),
            state=str(value.get("state", "ready")),
            alive=bool(value.get("alive", True)),
            weapon_ids=list(value.get("weapon_ids", [])),
            raw_attributes=dict(value.get("raw_attributes", {})),
            attack_range=float(value.get("attack_range", 0.0)),
            attack_damage=float(value.get("attack_damage", 0.0)),
            deployed=bool(value.get("deployed", False)),
            blood=(None if value.get("blood") is None else int(value["blood"])),
            max_blood=(None if value.get("max_blood") is None
                       else int(value["max_blood"])),
            basic_speed=(None if value.get("basic_speed") is None
                         else float(value["basic_speed"])),
            current_hex=(None if value.get("current_hex") is None
                         else int(value["current_hex"])),
            cur_pos_millionths=int(value.get("cur_pos_millionths", 0)),
            move_path=list(value.get("move_path", [])),
            movement_mode=str(value.get("movement_mode", "NORMAL")),
            stopped=bool(value.get("stopped", True)),
            move_to_stop_remaining_ms=int(value.get("move_to_stop_remaining_ms", 0)),
            fatigue_level=int(value.get("fatigue_level", 0)),
            fatigue_recovery_elapsed_ms=int(value.get("fatigue_recovery_elapsed_ms", 0)),
            movement_action_id=value.get("movement_action_id"),
            movement_action_sequence=(None if value.get("movement_action_sequence") is None
                                      else int(value["movement_action_sequence"])),
            movement_stop_requested=bool(value.get("movement_stop_requested", False)),
            movement_blocked_reason=value.get("movement_blocked_reason"),
            movement_task_pause_reason=(
                None
                if value.get("movement_task_pause_reason") is None
                else MovementTaskPauseReason(
                    str(value["movement_task_pause_reason"]).upper()
                )
            ),
            segment_from_hex=(None if value.get("segment_from_hex") is None
                              else int(value["segment_from_hex"])),
            segment_to_hex=(None if value.get("segment_to_hex") is None
                            else int(value["segment_to_hex"])),
            segment_elapsed_ms=int(value.get("segment_elapsed_ms", 0)),
            segment_traversal_ms=int(value.get("segment_traversal_ms", 0)),
            segment_event_id=value.get("segment_event_id"),
            transition_kind=value.get("transition_kind"),
            transition_remaining_ms=int(value.get("transition_remaining_ms", 0)),
            weapon_locked=bool(value.get("weapon_locked", False)),
            weapon_unfolded=bool(value.get("weapon_unfolded", True)),
            weapon_cooldown_remaining_ms=int(
                value.get("weapon_cooldown_remaining_ms", 0)),
            suppression_remaining_ms=int(value.get("suppression_remaining_ms", 0)),
            guidance_profile_id=(
                None
                if value.get("guidance_profile_id") is None
                else str(value["guidance_profile_id"])
            ),
            guidance_affiliation_id=(
                None
                if value.get("guidance_affiliation_id") is None
                else int(value["guidance_affiliation_id"])
            ),
            guided_fire_recovery_remaining_ms=int(
                value.get("guided_fire_recovery_remaining_ms", 0)
            ),
            indirect_fire_planned_cooldown_remaining_ms=int(
                value.get(
                    "indirect_fire_planned_cooldown_remaining_ms",
                    0,
                )
            ),
            guidance_affiliation_evidence=(
                None
                if value.get("guidance_affiliation_evidence") is None
                else GuidanceAffiliationEvidence.from_dict(
                    value["guidance_affiliation_evidence"]
                )
            ),
            boarding_profile_id=(
                None
                if value.get("boarding_profile_id") is None
                else str(value["boarding_profile_id"])
            ),
            mounted_on=(
                None if value.get("mounted_on") is None
                else int(value["mounted_on"])
            ),
            passenger_id=(
                None if value.get("passenger_id") is None
                else int(value["passenger_id"])
            ),
            movement_locked=bool(value.get("movement_locked", False)),
            concealment_posture=(
                None
                if value.get("concealment_posture") is None
                else ConcealmentPosture(
                    str(value["concealment_posture"]).upper()
                )
            ),
            lifecycle_profile_id=(
                None
                if value.get("lifecycle_profile_id") is None
                else str(value["lifecycle_profile_id"])
            ),
            lifecycle_remaining_ms=(
                None
                if value.get("lifecycle_remaining_ms") is None
                else int(value["lifecycle_remaining_ms"])
            ),
        )
