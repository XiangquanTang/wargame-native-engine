from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from wargame_native_engine.agents import SeatDecision
from wargame_native_engine.data import ScenarioBundle, ScenarioCatalog
from wargame_native_engine.operations import WargameFacade
from wargame_native_engine.training import (
    NativeMultiSeatEnv,
    decision_transitions_to_jsonl,
    write_decision_transitions_jsonl,
)


SCENARIO = Path("examples/source_movement_scenario.json")


class DeterministicMatchFactory:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.bundle = ScenarioBundle.from_path(SCENARIO)

    def __call__(self, seed: int, episode_sequence: int):
        app = WargameFacade(
            ScenarioCatalog.from_bundles({
                self.bundle.scenario_identity.scenario_id: self.bundle,
            }),
            storage_root=self.root / f"episode-{episode_sequence}",
        )
        return app.create_match(
            scenario_id=self.bundle.scenario_identity.scenario_id,
            red_controller_id="transition-red",
            blue_controller_id="transition-blue",
            seed=seed,
            decision_interval_ms=2_000,
            frame_interval_ms=1_000,
        )


def _run_one(root: Path):
    environment = NativeMultiSeatEnv(
        DeterministicMatchFactory(root), max_decision_windows=1
    )
    initial = environment.reset(seed=42)
    result = environment.step(SeatDecision(), SeatDecision())
    return environment, initial, result


def test_transition_jsonl_is_canonical_red_blue_order_and_deeply_detached(
    tmp_path: Path,
) -> None:
    environment, _, result = _run_one(tmp_path / "run")
    try:
        before = environment.transitions
        path = write_decision_transitions_jsonl(
            tmp_path / "decision_transitions.native.jsonl", before
        )
        payload = path.read_bytes()
        records = [json.loads(line) for line in payload.decode("utf-8").splitlines()]

        assert payload.endswith(b"\n")
        assert not payload.endswith(b"\n\n")
        assert [value["side"] for value in records] == ["red", "blue"]
        assert all(
            value["transition_schema"] == "native-decision-transition/1.0"
            for value in records
        )
        assert all(value["decision_index"] == 0 for value in records)
        assert all(value["fact_frame_start"] == 1 for value in records)
        assert all(value["fact_frame_end"] == 2 for value in records)
        assert all(
            value["fact_frame_range_contract"] == "closed"
            for value in records
        )
        assert "green" not in payload.decode("utf-8").lower()
        assert "frame_analysis" not in payload.decode("utf-8").lower()

        records[0]["observation"]["mission"]["mutated"] = True
        result.transitions[0].observation["mission"]["caller_mutated"] = True
        fresh = environment.transitions
        assert "mutated" not in fresh[0].observation["mission"]
        assert "caller_mutated" not in fresh[0].observation["mission"]
        assert before == environment.transitions
    finally:
        environment.close()


def test_same_seed_decisions_produce_equal_transition_records_and_bytes(
    tmp_path: Path,
) -> None:
    first, first_initial, first_result = _run_one(tmp_path / "first")
    second, second_initial, second_result = _run_one(tmp_path / "second")
    try:
        assert first_initial.to_record() == second_initial.to_record()
        assert first_result.observations == second_result.observations
        assert first_result.capabilities == second_result.capabilities
        assert first_result.rewards == second_result.rewards
        assert first_result.transitions == second_result.transitions
        assert decision_transitions_to_jsonl(first.transitions).encode("utf-8") == (
            decision_transitions_to_jsonl(second.transitions).encode("utf-8")
        )
    finally:
        first.close()
        second.close()


def test_transition_writer_rejects_nonfinite_reward_without_mutating_input(
    tmp_path: Path,
) -> None:
    environment, _, _ = _run_one(tmp_path / "run")
    try:
        source = environment.transitions
        invalid = (replace(source[0], reward=float("nan")), source[1])
        with pytest.raises(ValueError, match="Out of range float values"):
            decision_transitions_to_jsonl(invalid)
        assert source == environment.transitions
    finally:
        environment.close()


def test_reset_starts_new_episode_identity_and_decision_zero(tmp_path: Path) -> None:
    environment = NativeMultiSeatEnv(DeterministicMatchFactory(tmp_path))
    try:
        first = environment.reset(seed=42)
        environment.step(SeatDecision(), SeatDecision())
        second = environment.reset(seed=42)

        assert first.episode_id != second.episode_id
        assert second.decision_index == 0
        assert second.fact_frame_index == 0
        assert second.sim_time_ms == 0
        assert environment.transitions == ()
    finally:
        environment.close()
