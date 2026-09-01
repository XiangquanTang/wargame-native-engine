from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.run_benchmark import run_capability_benchmark
from scripts.run_demo import run_registered_demo
from wargame_native_engine.demo_registry import load_default_demo_registry
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)


@pytest.fixture(scope="module")
def demo_outputs(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, tuple[Path, dict[str, object]]]:
    root = tmp_path_factory.mktemp("loitering-demo")
    return {
        demo_id: (
            root / demo_id,
            run_registered_demo(
                demo_id,
                output_dir=root / demo_id,
                seed=None,
            ),
        )
        for demo_id in (
            "loitering_munition_basic",
            "loitering_munition_expiry",
        )
    }


def test_demo_registry_exposes_attack_and_expiry_scenarios() -> None:
    registry = load_default_demo_registry()

    assert registry.resolve("loitering_munition_basic").capability_ids == (
        "loitering_munition",
    )
    assert registry.resolve("loitering_munition_expiry").capability_ids == (
        "loitering_munition",
    )


def test_attack_demo_uses_human_agent_capabilities_and_strict_replay(
    demo_outputs: dict[str, tuple[Path, dict[str, object]]],
) -> None:
    output, report = demo_outputs["loitering_munition_basic"]
    validation = json.loads(
        (output / "validation_report.json").read_text(encoding="utf-8")
    )
    checks = {
        value["name"]: value["result"] for value in validation["checks"]
    }

    assert report["status"] == "PASS"
    assert report["attacker_consumed"] is True
    assert report["strict_replay_matches"] is True
    assert checks["human_move_capability"] is True
    assert checks["human_attack_capability"] is True
    assert checks["agent_move_capability"] is True
    assert checks["agent_attack_capability"] is True
    assert checks["no_ammunition_key"] is True

    archive = SourceReplayArchiveImporter().import_path(output / "replay.zip")
    assert archive.manifest["operator_lifecycle_authority_version"]
    assert archive.manifest["operator_lifecycle_authority_hash"]
    assert archive.manifest["loitering_munition_attack_authority_version"]
    assert archive.manifest["loitering_munition_attack_authority_hash"]


def test_expiry_demo_uses_authoritative_time_and_distinct_reason(
    demo_outputs: dict[str, tuple[Path, dict[str, object]]],
) -> None:
    output, report = demo_outputs["loitering_munition_expiry"]
    evidence = json.loads(
        (output / "evidence.json").read_text(encoding="utf-8")
    )

    assert report["status"] == "PASS"
    assert report["expiry_time_ms"] == 1_200_000
    assert report["attacker_expired"] is True
    assert report["strict_replay_matches"] is True
    reasons = {
        value.get("data", {}).get("reason_code")
        for value in evidence["events"]
        if value.get("event_type") == "unit_destroyed"
    }
    assert reasons == {"LIFECYCLE_EXPIRED"}


def test_loitering_capability_benchmark_passes(tmp_path: Path) -> None:
    report = run_capability_benchmark(
        "loitering_munition_basic",
        output_dir=tmp_path / "benchmark",
    )

    assert report["passed"] is True
    assert report["engine_result"]["passed"] is True
    assert report["human_result"]["result"] == "PASS"
    assert report["agent_result"]["result"] == "PASS"
    assert report["replay_result"]["matches"] is True
    assert report["expected_events_match"] is True
