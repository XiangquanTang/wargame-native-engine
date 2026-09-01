from __future__ import annotations

from pathlib import Path

from scripts.run_demo import run_registered_demo
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.event import EventType
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.demo_registry import load_default_demo_registry


def _scenario() -> ScenarioBundle:
    registry = load_default_demo_registry()
    return ScenarioBundle.from_path(
        registry.scenario_path("guided_fire_basic")
    )


def test_guided_fire_demo_has_source_compatible_roles_and_relationship() -> None:
    bundle = _scenario()
    operators = {
        value.operator_id: value for value in bundle.scenario.operators
    }

    assert operators[20].subtype == "source-2-4"
    assert operators[20].guidance_profile_id == "GROUND_AFFILIATED_GUIDER"
    assert operators[20].guidance_affiliation_id == 10
    assert operators[10].subtype == "source-2-1"
    assert operators[10].weapon_ids == [73]
    assert operators[30].side == "blue"


def test_guided_fire_demo_generates_only_the_expected_capability() -> None:
    engine = NativeEngine(_scenario().scenario)
    engine.reset(42)

    capabilities = engine.get_guided_fire_capabilities("red")

    assert len(capabilities) == 1
    assert capabilities[0].guider_id == 20
    assert capabilities[0].shooter_id == 10
    assert capabilities[0].weapon_id == 73
    assert capabilities[0].target_id == 30
    observation = engine.observe("red")
    friendlies = {
        value.operator_id: value for value in observation.friendly_operators
    }
    assert 30 in friendlies[20].observed_enemy_ids
    assert 30 not in friendlies[10].observed_enemy_ids
    engine.close()


def test_guided_fire_demo_executes_and_emits_formal_event() -> None:
    engine = NativeEngine(_scenario().scenario)
    engine.reset(42)
    action = engine.guided_fire_action(
        engine.get_guided_fire_capabilities("red")[0]
    )

    result = engine.step([action], delta_time_seconds=1)

    assert result.decisions[0].accepted is True
    assert any(
        value.event_type is EventType.GUIDED_FIRE_RESOLVED
        for value in result.events
    )
    engine.close()


def test_registered_guided_fire_runner_publishes_strict_replay(
    tmp_path: Path,
) -> None:
    output = tmp_path / "guided-fire-demo"

    report = run_registered_demo(
        "guided_fire_basic",
        output_dir=output,
        seed=42,
    )

    assert report["guider_observes_target"] is True
    assert report["shooter_observes_target"] is False
    assert report["human_guided_capability"] is True
    assert report["command_status"] == "APPLIED"
    assert report["event_type"] == "guided_fire_resolved"
    assert report["strict_replay_matches"] is True
    assert (output / "replay.zip").is_file()
