from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from test_indirect_fire_effect_resolution import _active_impact
from wargame_native_engine.core.action import ActionType
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.event import EventType
from wargame_native_engine.core.indirect_fire import FireMissionPhase
from wargame_native_engine.io.replay import ReplayWriter, replay_actions
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.io.serialization import canonical_hash


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DIRECT_SCENARIO = (
    REPOSITORY_ROOT / "examples/demos/direct_fire_basic/scenario.json"
)


def _resolved_bundle(*, seed: int = 5):
    engine, impact = _active_impact(seed=seed)
    result = engine.resolve_indirect_fire_effects(impact)
    assert result.effect_bundle is not None
    return engine, result.effect_bundle


def test_effect_bundle_starts_active_area_once_atomically() -> None:
    engine, bundle = _resolved_bundle(seed=5)

    event = engine.commit_indirect_fire_effect_bundle(bundle)

    assert event.event_type is EventType.INDIRECT_FIRE_ACTIVE_STARTED
    assert engine._last_events[0] == event  # noqa: SLF001
    assert engine._last_events[1].event_type is (  # noqa: SLF001
        EventType.INDIRECT_FIRE_AREA_TRIGGERED
    )


def test_commit_applies_loss_to_blood_and_health() -> None:
    engine, bundle = _resolved_bundle(seed=5)
    before = engine._world.operators[2]  # type: ignore[union-attr]  # noqa: SLF001
    assert before.blood == 3

    engine.commit_indirect_fire_effect_bundle(bundle)

    target = engine._world.operators[2]  # type: ignore[union-attr]  # noqa: SLF001
    assert target.blood == 2
    assert target.health == pytest.approx(target.max_health * 2 / 3)
    assert target.alive is True


def test_commit_applies_suppression_without_loss() -> None:
    engine, bundle = _resolved_bundle(seed=10)

    engine.commit_indirect_fire_effect_bundle(bundle)

    target = engine._world.operators[2]  # type: ignore[union-attr]  # noqa: SLF001
    assert target.blood == 3
    assert target.state == "SUPPRESSED"
    assert target.suppression_remaining_ms == 150_000


def test_commit_persists_active_area_mission() -> None:
    engine, bundle = _resolved_bundle(seed=5)

    engine.commit_indirect_fire_effect_bundle(bundle)

    mission = engine._world.indirect_fire_missions[0]  # type: ignore[union-attr]  # noqa: SLF001
    assert mission.phase is FireMissionPhase.ACTIVE
    assert mission.active_remaining_seconds == 300
    assert mission.impact_hex == bundle.impact_hex
    assert mission.affected_operator_ids == (2,)
    assert mission.cancelable is False


def test_commit_advances_authoritative_rng_to_staged_after_state() -> None:
    engine, bundle = _resolved_bundle(seed=5)
    before = engine.snapshot()["rng"]

    engine.commit_indirect_fire_effect_bundle(bundle)

    assert before == bundle.rng_transition.before.to_record()
    assert engine.snapshot()["rng"] == bundle.rng_transition.after.to_record()
    assert bundle.rng_transition.draw_count == 6


def test_same_seed_commit_has_same_world_hash() -> None:
    left, left_bundle = _resolved_bundle(seed=5)
    right, right_bundle = _resolved_bundle(seed=5)

    left.commit_indirect_fire_effect_bundle(left_bundle)
    right.commit_indirect_fire_effect_bundle(right_bundle)

    assert canonical_hash(left.snapshot()["world"]) == canonical_hash(
        right.snapshot()["world"]
    )


def test_commit_does_not_consume_mission_ammunition_twice() -> None:
    engine, bundle = _resolved_bundle(seed=5)
    shooter = engine._world.operators[1]  # type: ignore[union-attr]  # noqa: SLF001
    before = deepcopy(shooter.raw_attributes["ammunition"])

    engine.commit_indirect_fire_effect_bundle(bundle)

    shooter = engine._world.operators[1]  # type: ignore[union-attr]  # noqa: SLF001
    assert shooter.raw_attributes["ammunition"] == before


def test_active_events_contain_impact_and_damage_evidence() -> None:
    engine, bundle = _resolved_bundle(seed=5)

    event = engine.commit_indirect_fire_effect_bundle(bundle)
    record = event.to_dict()

    assert record["event_type"] == "indirect_fire_active_started"
    data = record["data"]
    assert data["mission_id"] == bundle.mission_id
    assert data["planned_hex"] == bundle.planned_hex
    assert data["impact_hex"] == bundle.impact_hex
    assert data["scatter_evidence"]["table_result"] == "HIT"
    triggered = engine._last_events[1].to_dict()  # noqa: SLF001
    assert triggered["event_type"] == "indirect_fire_area_triggered"
    assert triggered["target_id"] == 2
    assert triggered["data"]["damage_summary"]["damage_result"] == "LOSS_1"
    assert engine._world.last_judge_info == [  # type: ignore[union-attr]  # noqa: SLF001
        item.to_dict() for item in engine._last_events  # noqa: SLF001
    ]


def test_apply_failure_rolls_back_world_rng_events_and_mission(monkeypatch) -> None:
    engine, bundle = _resolved_bundle(seed=5)
    before = deepcopy(engine.snapshot())
    events_before = engine._last_events  # noqa: SLF001

    def fail(working, submitted_bundle):
        del submitted_bundle
        working.operators[2].blood = 0
        raise RuntimeError("indirect-commit-injected-failure")

    monkeypatch.setattr(engine, "_apply_indirect_fire_damage_effects", fail)
    with pytest.raises(RuntimeError, match="indirect-commit-injected-failure"):
        engine.commit_indirect_fire_effect_bundle(bundle)

    assert engine.snapshot() == before
    assert engine._last_events == events_before  # noqa: SLF001
    mission = engine._world.indirect_fire_missions[0]  # type: ignore[union-attr]  # noqa: SLF001
    assert mission.phase is FireMissionPhase.ACTIVE


def test_duplicate_commit_is_rejected_without_further_change() -> None:
    engine, bundle = _resolved_bundle(seed=5)
    engine.commit_indirect_fire_effect_bundle(bundle)
    committed = deepcopy(engine.snapshot())
    events = engine._last_events  # noqa: SLF001

    with pytest.raises(ValueError, match="PRESTATE_MISMATCH"):
        engine.commit_indirect_fire_effect_bundle(bundle)

    assert engine.snapshot() == committed
    assert engine._last_events == events  # noqa: SLF001


def test_unresolved_effect_bundle_cannot_commit() -> None:
    engine, impact = _active_impact(seed=1, target_armor="NONE")
    result = engine.resolve_indirect_fire_effects(impact)
    assert result.effect_bundle is not None
    before = deepcopy(engine.snapshot())

    with pytest.raises(ValueError, match="EFFECT_UNRESOLVED"):
        engine.commit_indirect_fire_effect_bundle(result.effect_bundle)

    assert engine.snapshot() == before


def test_existing_direct_replay_stays_strictly_replayable(tmp_path: Path) -> None:
    scenario = scenario_from_dict(json.loads(
        DIRECT_SCENARIO.read_text(encoding="utf-8")
    ))
    engine = NativeEngine(scenario)
    engine.reset(42)
    writer = ReplayWriter(
        tmp_path / "direct-replay",
        run_id="direct-after-indirect-commit",
        scenario=scenario,
        seed=42,
        agents={"red": "test", "blue": "test"},
        max_ticks=engine.terminal_rules.max_ticks,
        ruleset=engine.ruleset,
        project_commit=None,
    )
    writer.record_initial_state(engine.snapshot())
    action = next(
        value.action for value in engine.valid_actions("red")
        if value.action.action_type is ActionType.SHOOT
    )
    result = engine.step([action], delta_time_ms=1_000)
    writer.record_step(result, engine.snapshot())
    writer.finalize({"result": engine.result().to_dict()}, status="complete")

    assert replay_actions(scenario, writer.root, strict=True).matches is True
