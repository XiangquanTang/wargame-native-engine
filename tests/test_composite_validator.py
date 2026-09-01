from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from scripts.run_demo import (
    ValidationResult,
    _composite_validator_pipeline,
    _run_composite,
    _validator_for,
)
from wargame_native_engine.demo_registry import (
    CompositeScenarioPhase,
    load_default_demo_registry,
)


def test_composite_validator_pipeline_preserves_declared_phase_order() -> None:
    descriptor = load_default_demo_registry().resolve(
        "mechanized_assault_basic"
    )
    pipeline = _composite_validator_pipeline(descriptor)

    assert tuple(phase.name for phase, _validator in pipeline) == tuple(
        phase.name for phase in descriptor.phases
    )
    assert _validator_for(descriptor) is _run_composite


def test_validation_result_is_immutable_and_serializable() -> None:
    result = ValidationResult(
        name="phase",
        passed=True,
        evidence={"tick": 1},
    )

    assert result.to_record() == {
        "name": "phase",
        "result": True,
        "pass": True,
        "evidence": {"tick": 1},
        "failure_reason": None,
    }
    with pytest.raises(FrozenInstanceError):
        result.passed = False  # type: ignore[misc]


def test_unknown_composite_validator_fails_before_execution() -> None:
    descriptor = load_default_demo_registry().resolve(
        "mechanized_assault_basic"
    )
    invalid = replace(
        descriptor,
        phases=(
            CompositeScenarioPhase(
                "unknown", "boarding", "NOT_REGISTERED"
            ),
        ),
    )

    with pytest.raises(ValueError, match="unsupported composite validator"):
        _composite_validator_pipeline(invalid)
