from __future__ import annotations

import json
from pathlib import Path

from wargame_native_engine.core.action import ActionType


AUDIT = Path("docs/design/boarding_contract_audit_v1.md")
CONTRACT = Path("docs/design/boarding_capability_contract_v1.md")


def test_boarding_audit_and_contract_documents_exist() -> None:
    assert AUDIT.is_file()
    assert CONTRACT.is_file()


def test_audit_records_rule_evidence_and_implementation_blockers() -> None:
    text = AUDIT.read_text(encoding="utf-8")

    for evidence in (
        "推演规则_完整纯文本_Final.md",
        "Native兵棋裁决知识库_Final.md",
        "native_adjudication_tables_final.json",
        "Source relationship fields",
        "loading_capacity",
        "passenger_ids",
        "on_board",
    ):
        assert evidence in text
    for topic in (
        "Unit relationship",
        "State changes",
        "Capacity",
        "Time and interruption",
        "Movement and objective participation",
        "Combat and observation",
        "Disembark",
        "Implementation blockers",
    ):
        assert topic in text


def test_contract_keeps_advanced_unknown_authority_explicit_and_blocking() -> None:
    text = CONTRACT.read_text(encoding="utf-8")

    assert "MINIMAL_V1_IMPLEMENTED" in text
    assert "BOARDING_EXECUTION_ENABLED: `true`" in text
    for field in (
        "BOARDING_ELIGIBILITY_UNKNOWN",
        "BOARDING_CAPACITY_METRIC_UNKNOWN",
        "BOARDING_CAPACITY_VALUE_UNKNOWN",
        "BOARDING_RELATION_MAPPING_UNKNOWN",
        "BOARDING_INTERRUPTION_POLICY_UNKNOWN",
        "BOARDING_OBSERVATION_POLICY_UNKNOWN",
        "BOARDING_DESTRUCTION_POLICY_UNKNOWN",
    ):
        assert field in text
    assert "UNKNOWN values block execution" in text


def test_contract_records_minimal_boarding_implementation() -> None:
    audit = AUDIT.read_text(encoding="utf-8")
    contract = CONTRACT.read_text(encoding="utf-8")

    for text in (audit, contract):
        assert "BOARDING_ENGINE_STATUS: `MINIMAL_V1_IMPLEMENTED`" in text
        assert "BOARDING_EXECUTION_ENABLED: `true`" in text
    assert "BOARD" in ActionType.__members__
    assert "UNLOAD" in ActionType.__members__


def test_capability_registry_publishes_completed_boarding_phase_c() -> None:
    payload = json.loads(
        Path("config/capabilities/native_capability_registry_v1.json").read_text(
            encoding="utf-8"
        )
    )
    boarding = next(
        item for item in payload["capabilities"]
        if item["capability_id"] == "boarding"
    )

    assert boarding["engine_status"] == "IMPLEMENTED"
    assert boarding["demo_status"] == "PASS"
    assert boarding["demo_id"] == "boarding_basic"
    assert boarding["human_support"] is True
    assert boarding["agent_support"] is True
    assert boarding["replay_support"] is True


def test_contract_preserves_source_evidence_boundary_and_replay_compatibility() -> None:
    text = CONTRACT.read_text(encoding="utf-8")

    assert "A Source field is evidence, not Native rule authority" in text
    assert "display names and symbols are forbidden inputs" in text
    assert "must never have boarding actions inferred" in text
    assert "Old replays without BOARD/UNLOAD remain unchanged" in text
