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
        self.raw_attributes = json_clone(self.raw_attributes)

    @property
    def position(self) -> tuple[float, float]:
        return (self.x, self.y)

    def to_dict(self) -> dict[str, Any]:
        return {
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
        )
