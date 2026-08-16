from examples.run_source_release import run_example


def test_minimal_github_source_example_runs_and_strictly_replays(tmp_path) -> None:
    result = run_example(tmp_path / "source-release", seed=42)

    assert result["tick"] == 1
    assert result["red_visible_operators"] >= 1
    assert result["blue_visible_operators"] >= 1
    assert result["strict_replay_matches"] is True
