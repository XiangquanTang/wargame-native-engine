#!/usr/bin/env python3
"""Compare one RED unit's tactical visibility field with RED contacts.

This is a read-only diagnostic.  It deliberately reuses the Engine's existing
observation, tactical-overlay, LOS and visibility evaluators; it neither
implements visibility rules nor advances the simulation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.core.engine import (
    NativeEngine,
    _unit_domain,
    _uses_drone_observation_profile,
)
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.visibility import evaluate_visibility


DEFAULT_SCENARIO = Path("examples/public_river_valley_scenario.json")
RED = "red"
BLUE = "blue"


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Compare the theoretical Tactical Overlay visibility field with "
            "the enemy contacts present in the authoritative RED observation"
        )
    )
    value.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    value.add_argument("--seed", type=int, default=42)
    value.add_argument(
        "--observer-id",
        type=int,
        help="RED observer ID (defaults to the first alive deployed RED unit)",
    )
    return value


def _terrain_and_elevation(
    engine: NativeEngine,
    hex_id: int,
) -> tuple[str, float]:
    if engine._source_map is None:  # noqa: SLF001 - diagnostic evidence only
        raise ValueError("scenario does not provide a source-compatible hex map")
    cell = engine._source_map.cell(hex_id)  # noqa: SLF001
    return engine._source_map.terrain(hex_id).value, float(cell.elevation)


def _select_observer(engine: NativeEngine, observer_id: int | None) -> Any:
    world = engine._require_world()  # noqa: SLF001 - read-only debug inspection
    candidates = tuple(
        operator
        for operator in sorted(
            world.operators.values(), key=lambda item: item.operator_id
        )
        if operator.side == RED
        and operator.alive
        and operator.deployed
        and operator.current_hex is not None
    )
    if observer_id is None:
        if not candidates:
            raise ValueError("scenario has no alive deployed RED observer with a hex")
        return candidates[0]
    matches = tuple(value for value in candidates if value.operator_id == observer_id)
    if not matches:
        raise ValueError(
            f"RED observer is unavailable, inactive, or has no hex: {observer_id}"
        )
    return matches[0]


def _domain_hexes(overlay: Any) -> Mapping[str, frozenset[int]]:
    return {
        value.target_domain: frozenset(value.hex_ids)
        for value in overlay.visibility_field.by_target_domain
    }


def _los_authority(modifiers: tuple[dict[str, Any], ...]) -> str:
    if any(value.get("rule") == "source_static_visibility_lookup"
           for value in modifiers):
        return "runtime_static_visibility_matrix"
    return "native_dynamic_line_of_sight"


def _classification(
    *,
    in_overlay: bool,
    in_real_observation: bool,
    observer_contact: bool,
    concealed: bool,
    terrain: str,
    result: Any,
    alive: bool,
    deployed: bool,
) -> str:
    if in_overlay and not in_real_observation:
        if not alive or not deployed:
            return "inactive_target_filtered_from_observation"
        if concealed and not result.observable:
            return "concealment_normal_rule"
        if result.reason == "line_of_sight_blocked":
            return "overlay_or_los_authority_mismatch"
        if result.reason == "outside_observation_limit":
            if any(value.get("rule") == "target_terrain_halves_observation"
                   for value in result.modifiers):
                return "terrain_observation_limit"
            return "observation_limit_difference"
        if result.observable and not observer_contact:
            return "selected_observer_contact_refresh_mismatch"
        return "overlay_or_observation_mismatch"
    if in_real_observation and not observer_contact:
        return "contact_detected_by_another_red_observer"
    if in_overlay and in_real_observation:
        return "consistent_visible_contact"
    if terrain in {"FOREST", "SETTLEMENT"} and not result.observable:
        return "terrain_or_los_normal_rule"
    if not result.line_of_sight:
        return "line_of_sight_blocked"
    return "consistent_not_in_selected_overlay"


def run(args: argparse.Namespace) -> dict[str, object]:
    scenario = load_scenario(args.scenario)
    engine = NativeEngine(scenario)
    try:
        engine.reset(seed=args.seed)
        if engine._source_visibility_map is None:  # noqa: SLF001
            raise ValueError("scenario does not provide Native visibility data")
        observer = _select_observer(engine, args.observer_id)
        observer_hex = int(observer.current_hex)
        observer_domain = _unit_domain(observer.operator_type)
        observer_terrain, observer_elevation = _terrain_and_elevation(
            engine, observer_hex
        )

        overlay = engine.query_unit_tactical_overlay(RED, observer.operator_id)
        overlay_by_domain = _domain_hexes(overlay)
        overlay_union = frozenset(overlay.visibility_field.hex_ids)
        red_observation = engine.observe(RED)
        visible_enemy_ids = frozenset(
            value.operator_id for value in red_observation.visible_enemies
        )
        observer_contact_ids = frozenset(
            int(value)
            for value in observer.raw_attributes.get("see_enemy_bop_ids", ())
        )

        print("================================")
        print("Visibility Compare")
        print("================================")
        print("Scenario:", args.scenario)
        print("Seed:", args.seed)
        print()
        print("========================")
        print("Observer")
        print("========================")
        print("observer_id:", observer.operator_id)
        print("side:", observer.side)
        print("position_hex:", observer_hex)
        print("unit_domain:", observer_domain.value)
        print("terrain:", observer_terrain)
        print("elevation:", observer_elevation)
        print()
        print("========================")
        print("Tactical Overlay")
        print("========================")
        print("schema:", overlay.schema)
        print("semantics:", overlay.visibility_field.semantics)
        print("visible_hex_count_union:", len(overlay_union))
        for domain in sorted(overlay_by_domain):
            print(f"target_domain[{domain}]:", len(overlay_by_domain[domain]))
        print()
        print("========================")
        print("RED Observation")
        print("========================")
        print("visible_enemy_ids:", sorted(visible_enemy_ids))
        print(
            "selected_observer_see_enemy_bop_ids:",
            sorted(observer_contact_ids),
        )

        world = engine._require_world()  # noqa: SLF001 - read-only diagnostics
        records: list[dict[str, object]] = []
        mismatches = 0
        for enemy in sorted(
            (value for value in world.operators.values() if value.side == BLUE),
            key=lambda item: item.operator_id,
        ):
            target_hex = enemy.current_hex
            target_domain = _unit_domain(enemy.operator_type)
            if target_hex is None:
                terrain, elevation = None, None
                in_overlay = False
                in_overlay_union = False
                result = None
                base_limit = None
                classification = "target_has_no_hex"
            else:
                target_hex = int(target_hex)
                terrain, elevation = _terrain_and_elevation(engine, target_hex)
                in_overlay = target_hex in overlay_by_domain.get(
                    target_domain.value, frozenset()
                )
                in_overlay_union = target_hex in overlay_union
                line_of_sight = engine._runtime_line_of_sight(  # noqa: SLF001
                    observer, enemy
                )
                result = evaluate_visibility(
                    engine._source_visibility_map,  # noqa: SLF001
                    observer_hex=observer_hex,
                    target_hex=target_hex,
                    observer_domain=observer_domain,
                    target_domain=target_domain,
                    observer_is_drone=_uses_drone_observation_profile(observer),
                    target_is_drone=_uses_drone_observation_profile(enemy),
                    target_concealed=bool(
                        enemy.raw_attributes.get("concealed", False)
                    ),
                    target_terrain=terrain,
                    observation_profiles=(
                        engine._source_observation_profiles  # noqa: SLF001
                    ),
                    line_of_sight_evidence=line_of_sight,
                )
                base_limit = engine._source_observation_profiles.limit(  # noqa: SLF001
                    observer_domain,
                    target_domain,
                    observer_is_drone=_uses_drone_observation_profile(observer),
                    target_is_drone=_uses_drone_observation_profile(enemy),
                )
                classification = _classification(
                    in_overlay=in_overlay,
                    in_real_observation=enemy.operator_id in visible_enemy_ids,
                    observer_contact=enemy.operator_id in observer_contact_ids,
                    concealed=bool(enemy.raw_attributes.get("concealed", False)),
                    terrain=terrain,
                    result=result,
                    alive=bool(enemy.alive),
                    deployed=bool(enemy.deployed),
                )

            record: dict[str, object] = {
                "enemy_id": enemy.operator_id,
                "position_hex": target_hex,
                "target_domain": target_domain.value,
                "in_overlay_visibility": in_overlay,
                "in_overlay_union": in_overlay_union,
                "in_real_observation": enemy.operator_id in visible_enemy_ids,
                "in_selected_observer_contacts": (
                    enemy.operator_id in observer_contact_ids
                ),
                "concealed": bool(enemy.raw_attributes.get("concealed", False)),
                "terrain": terrain,
                "elevation": elevation,
                "alive": bool(enemy.alive),
                "deployed": bool(enemy.deployed),
                "classification": classification,
            }
            if result is not None:
                record["visibility_evidence"] = {
                    "observer_hex": observer_hex,
                    "target_hex": target_hex,
                    "hex_distance": result.distance,
                    "los_visible": result.line_of_sight,
                    "los_authority": _los_authority(result.modifiers),
                    "blocking_hexes": list(result.blocking_hexes),
                    "visibility_reason": result.reason,
                    "base_observation_limit": base_limit,
                    "adjusted_observation_limit": result.observation_limit,
                    "observable": result.observable,
                    "modifiers": [dict(value) for value in result.modifiers],
                }
            records.append(record)
            print()
            print("========================")
            print(f"Enemy {enemy.operator_id}")
            print("========================")
            print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))
            if in_overlay and enemy.operator_id not in visible_enemy_ids:
                mismatches += 1
                print("DIFFERENCE: overlay=True, real_contact=False")
                print("Reason:", classification)

        summary = {
            "observer_id": observer.operator_id,
            "overlay_visible_hex_count": len(overlay_union),
            "visible_enemy_ids": sorted(visible_enemy_ids),
            "difference_count": mismatches,
            "enemies": records,
        }
        print()
        print("================================")
        print("Summary")
        print("================================")
        print("difference_count:", mismatches)
        if mismatches == 0:
            print("classification: no_overlay_true_real_false_difference")
        return summary
    finally:
        engine.close()


def main(argv: list[str] | None = None) -> int:
    run(parser().parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
