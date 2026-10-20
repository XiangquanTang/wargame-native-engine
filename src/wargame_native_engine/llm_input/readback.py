"""TASK-054 output contract, fixed instructions and independent fact checking.

Only detached side JSON enters this module. Reference answers never enter prompts.
"""
from __future__ import annotations

from copy import deepcopy
import json

PROMPT_VERSION = "task054-readback-v2"
UNKNOWN_FIELDS = ["boarding_remaining_ms", "fatigue", "suppression", "transition_remaining_ms"]
# One definition drives instructions, structural validation and documentation.
CONTRACT = {
    "seat": "red|blue", "observation_version": "integer", "frame_index": "integer",
    "own_units": [{"obj_id": "integer", "cur_hex": "integer|null", "alive": "boolean|null",
                   "weapon_ids": "integer[]|null", "ammunition_pools": "object<string,integer>|null"}],
    "visible_enemies": [{"obj_id": "integer", "cur_hex": "integer|null", "alive": "boolean|null",
                         "blood": "integer|null", "ammunition_pools": "object<string,integer>|null"}],
    "unknown_fields": "string[]",
}


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def messages(frame):
    """Full compact input; no scoring values or sample-specific hints."""
    fields = "\n".join(
        key + "[]: " + ", ".join(f"{k} ({v})" for k, v in spec[0].items())
        if isinstance(spec, list) else f"{key} ({spec})"
        for key, spec in CONTRACT.items()
    )
    instruction = (
        "任务：读取Native simplified rules侧方帧，摘录事实。\n"
        "输入与分组：\n"
        "seat、observation_version、frame_index复制输入顶层值。单位只取source_frame.operators和"
        "source_frame.passengers，合并后按obj_id去重；不从valid_actions或其他字段添加单位。\n"
        "color=0是红方，color=1是蓝方。seat=red时color=0放own_units、color=1放visible_enemies；"
        "seat=blue时color=1放own_units、color=0放visible_enemies。\n"
        "输入中每个另一阵营单位都已可见，必须列入visible_enemies，保留obj_id、cur_hex、alive。"
        "blood或弹药未知不表示单位不可见，不能删掉整单位。只有输入确实没有另一阵营单位才输出[]。"
        "每个输入单位只输出一项，不添加obj_id=null的占位项，不猜输入以外的单位。\n"
        "输出字段与类型（这是说明，不是JSON值；每项只用列出的字段）：\n" + fields + "\n"
        "字段取值：\n"
        "obj_id、cur_hex、alive直接读单位字段；cur_hex为row*100+column，不推测地形。"
        "own_units的weapon_ids读carry_weapon_ids；ammunition_pools只读该单位"
        "extra.native.weapon_inventory.ammunition_pools，复制池key和数量，包括空对象{}与已知0。"
        "同池多武器不重复相加；不用remain_bullet_nums、operator_id或武器描述代替弹药池。\n"
        "visible_enemies的blood按输入读取，缺失或null写null；ammunition_pools未知写null，"
        "敌方空武器/弹药占位不表示已知0。缺失或null是未知，不能补0；已知0保留0。\n"
        "unknown_fields：始终包含" + compact(UNKNOWN_FIELDS) + "。"
        "己方cur_hex/alive/weapon_ids/ammunition_pools或敌方cur_hex/alive/blood/ammunition_pools"
        "为null时，逐项添加对应路径own_units.<obj_id>.<字段名>或visible_enemies.<obj_id>.<字段名>，"
        "其中<obj_id>换成该单位的实际ID。仅列这些路径和上述四项，去重排序。"
        "单位按obj_id、武器ID升序。scores和部分time为模板字段，不推断实时分数或墙钟。\n"
        "回答边界：只输出一个完整、合法的JSON对象。数组元素和对象字段之间用逗号。"
        "类型名不能作为值；未知用null，布尔用true/false。不要Markdown、解释、第二个对象或结束标记。"
        "最外层右花括号后立即结束，不再输出引号或其他文字。"
    )
    return [{"role": "system", "content": instruction},
            {"role": "user", "content": compact(frame)}]


def expected_readback(frame):
    """Scoring only: derive every fact from the same supplied side frame."""
    result = {key: frame[key] for key in ("seat", "observation_version", "frame_index")}
    result.update(own_units=[], visible_enemies=[], unknown_fields=list(UNKNOWN_FIELDS))
    color = {"red": 0, "blue": 1}[frame["seat"]]
    units = {}
    for unit in frame["source_frame"]["operators"] + frame["source_frame"].get("passengers", []):
        identity = unit["obj_id"]
        if identity in units and units[identity] != unit:
            raise ValueError("conflicting duplicate unit in side input")
        units[identity] = unit
    for identity, unit in sorted(units.items()):
        own = unit["color"] == color
        row = {key: unit.get(key) for key in ("obj_id", "cur_hex", "alive")}
        inventory = unit.get("extra", {}).get("native", {}).get("weapon_inventory")
        if own:
            row["weapon_ids"] = sorted(unit["carry_weapon_ids"]) if unit.get("carry_weapon_ids") is not None else None
            row["ammunition_pools"] = deepcopy(inventory.get("ammunition_pools")) if inventory else None
        else:
            row.update(blood=unit.get("blood"), ammunition_pools=None)
        group = "own_units" if own else "visible_enemies"
        result[group].append(row)
        result["unknown_fields"].extend(f"{group}.{identity}.{k}" for k, v in row.items() if v is None)
    result["unknown_fields"].sort()
    return result


def structure_errors(value):
    errors = []
    def check(ok, path):
        if not ok:
            errors.append(path)
    if not isinstance(value, dict):
        return ["$: expected object"]
    check(set(value) == set(CONTRACT), "$: keys")
    check(value.get("seat") in ("red", "blue"), "seat: red|blue")
    for key in ("observation_version", "frame_index"):
        check(type(value.get(key)) is int and value[key] >= 0, key + ": nonnegative integer")
    unknown = value.get("unknown_fields")
    check(isinstance(unknown, list) and all(isinstance(x, str) for x in unknown)
          and len(unknown) == len(set(unknown)), "unknown_fields: unique string[]")
    for group in ("own_units", "visible_enemies"):
        rows = value.get(group)
        if not isinstance(rows, list):
            errors.append(group + ": array")
            continue
        ids = []
        for i, row in enumerate(rows):
            path = f"{group}[{i}]"
            if not isinstance(row, dict):
                errors.append(path + ": object")
                continue
            check(set(row) == set(CONTRACT[group][0]), path + ": keys")
            for key in CONTRACT[group][0]:
                item = row.get(key)
                if key == "obj_id":
                    check(type(item) is int, path + "." + key)
                    ids.append(item)
                elif item is None:
                    continue
                elif key in ("cur_hex", "blood"):
                    check(type(item) is int, path + "." + key)
                elif key == "alive":
                    check(type(item) is bool, path + ".alive")
                elif key == "weapon_ids":
                    check(isinstance(item, list) and all(type(x) is int for x in item)
                          and len(item) == len(set(item)), path + ".weapon_ids")
                elif key == "ammunition_pools":
                    check(isinstance(item, dict) and all(isinstance(k, str) and type(v) is int and v >= 0
                          for k, v in item.items()), path + ".ammunition_pools")
        check(len(ids) == len({compact(x) for x in ids}), group + ": duplicate obj_id")
    return errors


def _leaves(value, path=""):
    if isinstance(value, dict) and value:
        return {p: v for k, x in value.items() for p, v in _leaves(x, f"{path}.{k}".lstrip(".")).items()}
    if isinstance(value, list) and value:
        return {p: v for i, x in enumerate(value) for p, v in _leaves(x, f"{path}[{i}]").items()}
    return {path: value}


def check_readback(raw, frame):
    """Strict JSON; never repair, strip fences, retry or substitute an answer."""
    expected = expected_readback(frame)
    reference = _leaves(expected)
    result = {"json_valid": False, "structure_valid": False, "pass": False,
              "parsed": None, "structure_errors": [], "mismatches": [],
              "correct_fields": 0, "total_fields": len(reference)}
    def unique(pairs):
        obj = {}
        for k, v in pairs:
            if k in obj:
                raise ValueError("duplicate JSON key: " + k)
            obj[k] = v
        return obj
    def invalid_constant(text):
        raise ValueError("non-JSON constant: " + text)
    try:
        parsed = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
        result.update(json_valid=True, parsed=parsed)
    except (ValueError, TypeError, RecursionError) as exc:
        result["parse_error"] = str(exc)
        return result
    errors = structure_errors(parsed)
    result.update(structure_errors=errors, structure_valid=not errors)
    if errors:
        return result
    normalized = deepcopy(parsed)
    for group in ("own_units", "visible_enemies"):
        normalized[group].sort(key=lambda row: row["obj_id"])
        for row in normalized[group]:
            if row.get("weapon_ids") is not None:
                row["weapon_ids"].sort()
    normalized["unknown_fields"].sort()
    actual = _leaves(normalized)
    for path in sorted(set(reference) | set(actual)):
        same = path in reference and path in actual and compact(reference[path]) == compact(actual[path])
        if same:
            result["correct_fields"] += 1
        else:
            result["mismatches"].append({"path": path, "expected": reference.get(path),
                "actual": actual.get(path), "expected_present": path in reference, "actual_present": path in actual})
    result["pass"] = not result["mismatches"]
    return result
