"""User-confirmed weapon labels, independent of combat support and profiles.

Only presentation uses these names.  A known name does not grant an unknown
weapon an ammunition pool, range, firing capability, or execution policy.
"""
from types import MappingProxyType


WEAPON_DISPLAY_NAMES = MappingProxyType({
    71: "便携导弹", 5: "便携导弹（对地）", 29: "步兵轻武器",
    69: "车载导弹", 43: "车载轻武器", 36: "大号直瞄炮",
    35: "火箭筒", 56: "速射炮", 4: "速射炮（对地）",
    84: "炮射导弹", 89: "轻型炮", 54: "小号直瞄炮",
    75: "小型导弹", 76: "巡飞导弹", 37: "中号直瞄炮",
    83: "中型导弹（标准）", 74: "中型导弹（便携）", 88: "中型炮",
    73: "重型导弹", 72: "重型炮", 1: "防空高炮",
    2: "便携防空导弹", 3: "车载防空导弹",
})


def weapon_display_name(weapon_id: int, fallback: str | None = None) -> str:
    """Resolve an exact numeric ID without inferring any weapon mechanics."""
    if type(weapon_id) is not int:
        raise TypeError("weapon_id must be an exact integer")
    known = WEAPON_DISPLAY_NAMES.get(weapon_id)
    if known is not None:
        return known
    if isinstance(fallback, str) and fallback.strip() and fallback.strip() != "UNKNOWN":
        return fallback
    return f"武器 {weapon_id}"
