from pathlib import Path

from examples.run_joint_fire import run_example as run_joint_fire
from examples.run_source_release import run_example
from examples.verify_replay import verify_replay
from wargame_native_engine.viewer.map_config import load_viewer_map_config


PUBLIC_MAP = Path("examples/public_source_movement_map.json")


def test_minimal_github_source_example_runs_and_strictly_replays(tmp_path) -> None:
    result = run_example(tmp_path / "source-release", seed=42)

    assert result["tick"] == 1
    assert result["red_visible_operators"] >= 1
    assert result["blue_visible_operators"] >= 1
    assert result["strict_replay_matches"] is True

    verified = verify_replay(result["replay"])
    assert verified["matches"] is True
    assert verified["actual_world_hash"] == result["final_state_hash"]


def test_public_joint_fire_example_uses_same_pre_state_and_strict_replay(
    tmp_path,
) -> None:
    result = run_joint_fire(tmp_path / "joint-fire", seed=1)

    assert result["red_submit_state"] == "WAITING_OPPONENT"
    assert result["blue_advanced"] is True
    assert result["outcome"] == "draw"
    assert result["final_units"] == {
        "red": {
            "operator_id": 1,
            "blood": 0,
            "alive": False,
            "current_hex": 101,
        },
        "blue": {
            "operator_id": 2,
            "blood": 0,
            "alive": False,
            "current_hex": 102,
        },
    }
    assert result["strict_replay_matches"] is True


def test_public_viewer_map_matches_the_synthetic_scenario() -> None:
    value = load_viewer_map_config(PUBLIC_MAP)

    assert value.map_id == "source-movement-fixture-map"
    assert [(cell.row, cell.column) for cell in value.cells] == [
        (1, column) for column in range(1, 7)
    ]
