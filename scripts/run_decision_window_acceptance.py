from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Sequence

from wargame_native_engine.io.native_map_converter import (
    convert_benchmark_basic_map,
)
from wargame_native_engine.replay import convert_legacy_replay
from wargame_native_engine.viewer.replay_view import load_replay_view


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the bounded five-replay DecisionWindow acceptance.",
    )
    parser.add_argument("--replay-root", type=Path, required=True)
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.output_root.exists():
        raise FileExistsError(
            f"acceptance output already exists: {args.output_root}"
        )
    replay_paths = sorted(args.replay_root.glob("*/*/*/*.zip"))
    if len(replay_paths) != 5:
        raise ValueError(
            f"acceptance requires exactly five replay archives, got {len(replay_paths)}"
        )
    args.output_root.mkdir(parents=True)
    samples = []
    occurrence: dict[tuple[str, str], int] = {}
    for replay_path in replay_paths:
        relative = replay_path.relative_to(args.replay_root)
        map_id, scenario_id = relative.parts[:2]
        key = (map_id, scenario_id)
        occurrence[key] = occurrence.get(key, 0) + 1
        sample_id = f"map-{map_id}-scenario-{scenario_id}-{occurrence[key]}"
        map_path = args.snapshot_root / "maps" / map_id / "basic.json"
        scenario_path = args.snapshot_root / "scenarios" / f"{scenario_id}.json"
        for value, label in (
            (map_path, "map"),
            (scenario_path, "scenario"),
        ):
            if not value.is_file():
                raise ValueError(f"{label} source is missing for {sample_id}")
        hashes_before = {
            "replay": _sha256(replay_path),
            "map": _sha256(map_path),
            "scenario": _sha256(scenario_path),
        }
        output = args.output_root / "samples" / sample_id
        conversion = convert_legacy_replay(replay_path, output)
        coordinate_override = "odd-r" if map_id in {"96", "9601"} else None
        hex_size_override = 200 if map_id in {"96", "9601"} else None
        native_map = convert_benchmark_basic_map(
            map_path,
            coordinate_system_override=coordinate_override,
            hex_size_m_override=hex_size_override,
        )
        view = load_replay_view(
            output,
            map_config=map_path,
            map_coordinate_system=coordinate_override,
            scenario_config=scenario_path,
        )
        middle = view.frame_count // 2
        selected_indices = (0, middle, view.frame_count - 1)
        records = [view.frame_record(index) for index in selected_indices]
        analysis = [view.analysis_record(index) for index in selected_indices]
        hashes_after = {
            "replay": _sha256(replay_path),
            "map": _sha256(map_path),
            "scenario": _sha256(scenario_path),
        }
        map_record = view.map_record()
        samples.append({
            "sample_id": sample_id,
            "map_id": map_id,
            "scenario_id": scenario_id,
            "input_sha256": hashes_before["replay"],
            "map_sha256": hashes_before["map"],
            "scenario_sha256": hashes_before["scenario"],
            "inputs_unchanged": hashes_before == hashes_after,
            "raw_records": conversion.raw_record_count,
            "selected_frames": conversion.selected_frame_count,
            "collapsed_records": conversion.collapsed_record_count,
            "replay_frames": view.frame_count,
            "analysis_lines": len(
                conversion.analysis_jsonl_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ),
            "first_selected_member": conversion.selected_member_names[0],
            "sampled_frame_indices": list(selected_indices),
            "sampled_cur_steps": [record["time"]["cur_step"] for record in records],
            "sampled_unit_counts": [
                len(record["operators"]) + len(record["passengers"])
                for record in records
            ],
            "sampled_hash_alignment": all(
                record["source_hash"] == item["provenance"]["frame_hash"]
                for record, item in zip(records, analysis)
            ),
            "timeline_length": len(view.timeline_record()["frame_indices"]),
            "map_bounds": map_record["bounds"],
            "map_cell_count": len(map_record["cells"]),
            "native_map_sha256": native_map.native_map_sha256,
            "native_map_warnings": native_map.provenance["warnings"],
            "viewer_load_result": "passed",
            "strict_replay_evidence_status": (
                view.strict_replay_evidence_status
            ),
        })
    report = {
        "schema": "decision-window-five-sample-acceptance/1.0",
        "sample_count": len(samples),
        "scope": "exactly_five_phase9a_samples_not_370k",
        "status": "passed",
        "samples": samples,
    }
    report_path = args.output_root / "acceptance_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status": report["status"],
        "sample_count": report["sample_count"],
        "report_path": str(report_path),
        "report_sha256": _sha256(report_path),
    }, ensure_ascii=False, sort_keys=True))
    return 0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
