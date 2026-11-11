"""Compare same-workload tick runs, refusing mismatched semantics or facts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

IDENTICAL_INPUTS = (
    "engine_version", "adjudication_semantics_version", "benchmark_script_sha256",
    "config_tree_sha256", "rules_docs_tree_sha256", "assets_tree_sha256",
    "python", "platform", "scenario_sha256", "map_runtime_manifest_sha256",
    "seed", "tick_interval_ms", "warmup_ticks", "workload",
)
FACT_HASHES = ("tick_facts_hash", "red_view_hash", "blue_view_hash")


def compare(control: dict, candidate: dict) -> dict:
    for field in (*IDENTICAL_INPUTS, *FACT_HASHES):
        if field not in control or field not in candidate or control[field] != candidate[field]:
            raise ValueError(f"incomparable runs: {field}")
    if control["tick"]["count"] != candidate["tick"]["count"]:
        raise ValueError("incomparable runs: tick count")
    return {
        "control_source_sha256": control["source_tree_sha256"],
        "candidate_source_sha256": candidate["source_tree_sha256"],
        "count": control["tick"]["count"],
        "identical_inputs": list(IDENTICAL_INPUTS),
        "identical_facts": {key: control[key] for key in FACT_HASHES},
        "tick": {
            metric: {
                "control_ms": control["tick"][metric],
                "candidate_ms": candidate["tick"][metric],
                "reduction_percent": 100 * (
                    1 - candidate["tick"][metric] / control["tick"][metric]
                ),
            }
            for metric in ("p50_ms", "p95_ms")
        },
        "limits": "Sequential shared-host no-op ticks; not combat or HTTP throughput.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("control", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = compare(json.loads(args.control.read_text()), json.loads(args.candidate.read_text()))
    args.output.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
