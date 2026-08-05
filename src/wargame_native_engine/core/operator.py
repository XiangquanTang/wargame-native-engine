from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from wargame_native_engine.io.serialization import json_clone


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
                     "weapon_cooldown_remaining_ms", "suppression_remaining_ms"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        self.move_path = [int(item) for item in self.move_path]
        self.raw_attributes = json_clone(self.raw_attributes)

    @property
    def position(self) -> tuple[float, float]:
        return (self.x, self.y)

    @property
    def cur_pos(self) -> float:
        return self.cur_pos_millionths / 1_000_000.0

    @property
    def moving(self) -> bool:
        return self.segment_to_hex is not None

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
        if self.current_hex is not None:
            value.update({
                "basic_speed": self.basic_speed,
                "current_hex": self.current_hex,
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
        )
