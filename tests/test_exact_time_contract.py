from __future__ import annotations

from pathlib import Path

import pytest

from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.session.game_session import GameSession


SCENARIO_PATH = Path("examples/source_movement_scenario.json")


def test_exact_milliseconds_and_seconds_compatibility_are_identical() -> None:
    scenario = load_scenario(SCENARIO_PATH)
    exact = GameSession(NativeEngineBackend(scenario))
    compatible = GameSession(NativeEngineBackend(scenario))
    exact.reset(42)
    compatible.reset(42)

    exact_result = exact.step_ms((), delta_time_ms=1_250)
    compatible_result = compatible.step((), delta_time_seconds=1.25)

    assert exact_result == compatible_result
    assert exact_result.delta_time_ms == 1_250
    assert canonical_hash(exact.snapshot()) == canonical_hash(compatible.snapshot())
    assert exact.snapshot()["world"]["elapsed_time_ms"] == 1_250


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.0, "1000", None])
def test_exact_milliseconds_reject_non_positive_or_non_exact_int(value) -> None:
    engine = NativeEngine(load_scenario(SCENARIO_PATH))
    engine.reset(42)
    with pytest.raises(ValueError, match="delta_time_ms must be a positive exact int"):
        engine.step_ms((), delta_time_ms=value)  # type: ignore[arg-type]


def test_step_rejects_ambiguous_seconds_and_milliseconds() -> None:
    engine = NativeEngine(load_scenario(SCENARIO_PATH))
    engine.reset(42)
    before = canonical_hash(engine.snapshot())
    with pytest.raises(ValueError, match="not both"):
        engine.step((), delta_time_seconds=1, delta_time_ms=1_000)
    assert canonical_hash(engine.snapshot()) == before


def test_seconds_compatibility_rejects_sub_millisecond_time() -> None:
    engine = NativeEngine(load_scenario(SCENARIO_PATH))
    engine.reset(42)
    with pytest.raises(ValueError, match="positive integer milliseconds"):
        engine.step((), delta_time_seconds=0.0005)
