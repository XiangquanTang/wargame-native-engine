#!/usr/bin/env python3
"""Create an explicit Native test derivative with indirect-fire ammunition.

The source scenario is read-only.  Eligible shooters are identified only by
the executable Native simplified indirect-fire profile and ammunition mapping;
display names and Source numeric type labels are not used as rules.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
from typing import Any

from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.io.serialization import canonical_json
from wargame_native_engine.rules.indirect_fire import (
    load_indirect_fire_ammunition_authority,
    load_indirect_fire_authority,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a separate Native simplified indirect-fire test scenario"
        ),
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenario-id", required=True)
    parser.add_argument("--ammunition", type=int, default=2)
    parser.add_argument("--expected-shooters", type=int, default=12)
    return parser


def derive_indirect_fire_test_record(
    source: Mapping[str, Any],
    *,
    scenario_id: str,
    ammunition: int = 2,
    expected_shooters: int = 12,
    source_file_sha256: str,
) -> tuple[dict[str, Any], tuple[dict[str, object], ...]]:
    if not scenario_id.strip():
        raise ValueError("scenario_id must be non-empty")
    if type(ammunition) is not int or ammunition <= 0:
        raise ValueError("ammunition must be a positive exact int")
    if type(expected_shooters) is not int or expected_shooters <= 0:
        raise ValueError("expected_shooters must be a positive exact int")
    if len(source_file_sha256) != 64:
        raise ValueError("source_file_sha256 must be SHA-256")

    original = scenario_from_dict(source)
    if scenario_id == original.scenario_id:
        raise ValueError("derived scenario_id must differ from the source")
    record = json.loads(canonical_json(original.to_dict()))
    indirect = load_indirect_fire_authority()
    ammunition_authority = load_indirect_fire_ammunition_authority()
    eligible_weapons = {
        profile.weapon_id: ammunition_authority.mapping_for_weapon(
            profile.weapon_id
        )
        for profile in indirect.profiles
        if not indirect.execution_blockers(profile.weapon_id)
    }
    eligible_weapons = {
        weapon_id: mapping
        for weapon_id, mapping in eligible_weapons.items()
        if mapping is not None
    }

    changes: list[dict[str, object]] = []
    for operator in sorted(
        record["operators"], key=lambda value: int(value["operator_id"])
    ):
        weapon_ids = operator.get("weapon_ids")
        if not isinstance(weapon_ids, list):
            raise ValueError("operator weapon_ids must be an array")
        matches = sorted({
            int(weapon_id)
            for weapon_id in weapon_ids
            if type(weapon_id) is int and weapon_id in eligible_weapons
        })
        if not matches:
            continue
        raw = operator.get("raw_attributes")
        if not isinstance(raw, dict):
            raise ValueError("eligible shooter lacks raw_attributes")
        pools = raw.get("ammunition")
        if not isinstance(pools, dict):
            raise ValueError("eligible shooter lacks an ammunition pool")
        for weapon_id in matches:
            mapping = eligible_weapons[weapon_id]
            key = mapping.ammunition_key
            if key not in pools:
                raise ValueError(
                    f"operator {operator['operator_id']} lacks ammunition key {key}"
                )
            before = pools[key]
            if type(before) is not int or before < 0:
                raise ValueError("existing ammunition must be a non-negative int")
            pools[key] = ammunition
            changes.append({
                "side": operator["side"],
                "shooter_id": operator["operator_id"],
                "weapon_id": weapon_id,
                "ammunition_key": key,
                "before": before,
                "after": ammunition,
            })

    shooter_ids = {int(value["shooter_id"]) for value in changes}
    if len(shooter_ids) != expected_shooters:
        raise ValueError(
            "indirect-fire shooter count mismatch: "
            f"expected {expected_shooters}, found {len(shooter_ids)}"
        )
    side_counts = Counter(str(value["side"]) for value in changes)
    if set(side_counts) != set(original.side_order):
        raise ValueError("derived shooters do not cover every scenario side")

    record["scenario_id"] = scenario_id
    metadata = record.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("scenario metadata must be an object")
    metadata["native_test_derivative"] = {
        "schema": "native-indirect-fire-test-derivative/1.0",
        "purpose": "twelve-shooter-ammunition-display-and-side-isolation-test",
        "source_scenario_id": original.scenario_id,
        "source_scenario_hash": original.scenario_hash,
        "source_file_sha256": source_file_sha256,
        "identification_policy": (
            "EXECUTABLE_INDIRECT_PROFILE_AND_EXISTING_AMMUNITION_MAPPING"
        ),
        "indirect_fire_authority_hash": indirect.content_hash,
        "ammunition_mapping_hash": ammunition_authority.content_hash,
        "changed_fields": "operators[].raw_attributes.ammunition[existing_key]",
        "ammunition_per_pool": ammunition,
        "shooter_count": len(shooter_ids),
        "side_counts": dict(sorted(side_counts.items())),
        "changes": changes,
        "rules_unchanged": True,
    }
    # Reparse to validate every normal scenario contract before writing.
    scenario_from_dict(record)
    return record, tuple(changes)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.input.is_file():
        raise FileNotFoundError(args.input)
    if args.output.exists():
        raise FileExistsError(f"refusing to overwrite: {args.output}")
    payload = args.input.read_bytes()
    source = json.loads(payload.decode("utf-8"))
    if not isinstance(source, Mapping):
        raise ValueError("scenario root must be an object")
    record, changes = derive_indirect_fire_test_record(
        source,
        scenario_id=args.scenario_id,
        ammunition=args.ammunition,
        expected_shooters=args.expected_shooters,
        source_file_sha256=hashlib.sha256(payload).hexdigest(),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded = (canonical_json(record) + "\n").encode("utf-8")
    with args.output.open("xb") as stream:
        stream.write(encoded)
    derived = scenario_from_dict(record)
    print(json.dumps({
        "output": str(args.output),
        "scenario_id": derived.scenario_id,
        "scenario_hash": derived.scenario_hash,
        "file_sha256": hashlib.sha256(encoded).hexdigest(),
        "shooter_count": len({value["shooter_id"] for value in changes}),
        "side_counts": dict(sorted(Counter(
            str(value["side"]) for value in changes
        ).items())),
        "ammunition": args.ammunition,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
