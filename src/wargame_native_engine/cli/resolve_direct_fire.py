from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.adjudication import (
    AdjudicationTableSet, DiceRoller, DirectFireAdjudicator,
    DirectFireRequest, WeaponProfile,
)
from wargame_native_engine.adjudication.replay import (
    direct_fire_request_to_dict, write_direct_fire_replay,
)
from wargame_native_engine.cli.run_game import _validate_output
from wargame_native_engine.compat.source_frame.models import ArmorClass, UnitDomain
from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.core.rng import NativeRNG
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.visibility import (
    HexCell, HexMapProfile, evaluate_visibility,
)


DEFAULT_TABLE_SET = (Path(__file__).resolve().parents[3]
                     / "config/adjudication/source_tables_v1/native_adjudication_tables_final.json")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Resolve one transparent table-driven direct-fire request")
    value.add_argument("--scenario", required=True, type=Path)
    value.add_argument("--attacker", required=True, type=int)
    value.add_argument("--target", required=True, type=int)
    value.add_argument("--weapon", required=True, type=int)
    value.add_argument("--seed", type=int, default=42)
    value.add_argument("--table-set", type=Path, default=DEFAULT_TABLE_SET)
    value.add_argument("--output", required=True, type=Path)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        _validate_output(args.output)
        if args.output.exists() and any(args.output.iterdir()):
            raise FileExistsError(f"refusing to overwrite non-empty output: {args.output}")
        args.output.mkdir(parents=True, exist_ok=True)
        scenario = _load(args.scenario)
        units = {int(item["operator_id"]): OperatorState.from_dict(item)
                 for item in scenario["operators"]}
        attacker, target = units[args.attacker], units[args.target]
        weapon_value = next(item for item in scenario["weapons"]
                            if int(item["weapon_id"]) == args.weapon)
        weapon = WeaponProfile(
            weapon_id=args.weapon, weapon_class=str(weapon_value["weapon_class"]),
            maximum_range=int(weapon_value["maximum_range"]),
            target_domains=frozenset(UnitDomain(str(item))
                                     for item in weapon_value["target_domains"]),
            ammunition_key=str(weapon_value.get("ammunition_key", "0")),
            cooldown_ticks=(None if weapon_value.get("cooldown_ticks") is None
                            else int(weapon_value["cooldown_ticks"])),
            requires_unfolded=bool(weapon_value.get("requires_unfolded", True)),
            can_fire_while_moving=bool(weapon_value.get("can_fire_while_moving", False)),
        )
        cells = {int(item["hex_id"]): HexCell(int(item["hex_id"]),
                                              float(item["elevation"]),
                                              str(item.get("terrain", "OPEN")))
                 for item in scenario["map"]["cells"]}
        profile = HexMapProfile(cells)
        attacker_hex = int(attacker.raw_attributes["hex_id"])
        target_hex = int(target.raw_attributes["hex_id"])
        visibility = evaluate_visibility(
            profile, observer_hex=attacker_hex, target_hex=target_hex,
            observer_domain=UnitDomain(attacker.operator_type),
            target_domain=UnitDomain(target.operator_type),
            target_concealed=bool(target.raw_attributes.get("concealed", False)),
            target_terrain=profile.cell(target_hex).terrain,
        )
        ammo = {str(key): int(value) for key, value in
                attacker.raw_attributes.get("ammunition", {}).items()}
        request = DirectFireRequest(
            tick=0, episode_seed=args.seed,
            action_id=f"cli-direct-fire-{args.attacker}-{args.target}-{args.weapon}",
            attacker=attacker, target=target, weapon=weapon,
            visibility=visibility, distance=visibility.distance,
            target_domain=UnitDomain(target.operator_type),
            target_armor=ArmorClass(str(target.raw_attributes.get("armor_class", "NONE"))),
            ammunition_before=ammo.get(weapon.ammunition_key, 0),
            weapon_cooldown_before=int(attacker.raw_attributes.get("weapon_cooldown", 0)),
            weapon_unfolded=bool(attacker.raw_attributes.get("weapon_unfolded", True)),
            elevation_difference=int(profile.cell(target_hex).elevation
                                     - profile.cell(attacker_hex).elevation),
            attacker_suppressed=attacker.state == "SUPPRESSED",
            attacker_moving=attacker.state == "MOVING",
            target_terrain=profile.cell(target_hex).terrain,
            target_concealed=bool(target.raw_attributes.get("concealed", False)),
            target_moving=target.state == "MOVING",
            target_stacked=bool(target.raw_attributes.get("stacked", False)),
            target_marching=target.state == "MARCHING",
            attacker_transitioning=attacker.state == "TRANSITIONING",
            target_external_direct_fire_prohibited=target.state == "CLOSE_COMBAT",
            carried_passengers=tuple(units[int(item)] for item in
                                     target.raw_attributes.get("passenger_ids", [])
                                     if int(item) in units),
        )
        tables = AdjudicationTableSet(args.table_set)
        request_document = direct_fire_request_to_dict(request)
        result = DirectFireAdjudicator(tables, DiceRoller(NativeRNG(args.seed))).resolve(request)
        report = result.to_dict()
        report["scenario_hash"] = canonical_hash(scenario)
        report["visibility"] = visibility.to_dict()
        write_direct_fire_replay(
            args.output, request_document=request_document, resolution=result,
            tables=tables,
            extra_manifest={"scenario_hash": canonical_hash(scenario),
                            "scenario_id": scenario["scenario_id"],
                            "resolved": result.outcome is not None,
                            "error": result.error})
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if result.outcome is not None else 4
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "message": str(exc)}, ensure_ascii=False,
                         sort_keys=True, indent=2))
        return 2


def _load(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("direct-fire scenario root must be an object")
    if value.get("schema_version") != "native-direct-fire-scenario/1.0":
        raise ValueError("unsupported direct-fire scenario schema")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
