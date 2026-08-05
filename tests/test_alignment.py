from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from wargame_native_engine.alignment.aligner import ReplayAligner
from wargame_native_engine.alignment.lanchester_adapter import compare_advantage_curves
from wargame_native_engine.alignment.metrics import event_metrics, terminal_metrics
from wargame_native_engine.alignment.operator_matching import OperatorMatcher
from wargame_native_engine.alignment.time_axis import TimeAlignmentConfig, align_times
from wargame_native_engine.io.importers.historical_replay import HistoricalReplayImporter
from wargame_native_engine.rules.config import load_ruleset


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples" / "historical_replay_reduced_fixture"
RULESET = ROOT / "config" / "rulesets" / "native_default_v1.json"


def test_exact_time_alignment() -> None:
    pairs = align_times([0, 1], [0, 1, 2], TimeAlignmentConfig("exact", 1, 0))
    assert [item.status for item in pairs] == ["EXACT", "EXACT"]


def test_nearest_time_alignment() -> None:
    pair = align_times([1.4], [0, 2], TimeAlignmentConfig("nearest", 1, 0))[0]
    assert pair.native_tick == 2
    assert abs(pair.error - 0.6) < 1e-9


def test_fixed_ratio_alignment() -> None:
    pair = align_times([4], [2, 3], TimeAlignmentConfig("fixed_ratio", 0.25, 2))[0]
    assert pair.target_native_tick == 3
    assert pair.status == "EXACT"


def test_unmatched_operator_is_reported() -> None:
    replay = HistoricalReplayImporter().import_path(FIXTURES / "smoke-state-01").replay
    native = [{"operator_id": 999, "side": "red", "operator_type": "ground_unit",
               "x": 1.0, "y": 2.0}]
    matches = OperatorMatcher().match(replay.frames[0].operators[:1], native)
    assert matches[0].status == "UNMATCHED"


def test_position_error_metrics() -> None:
    replay = HistoricalReplayImporter().import_path(FIXTURES / "calibration-action-01").replay
    result = ReplayAligner().align(replay, load_ruleset(RULESET))
    assert result.trajectory_metrics["mean_position_error"] is not None
    assert result.trajectory_metrics["matched_operator_ratio"] == 1.0


def test_missing_event_is_not_zero() -> None:
    replay = HistoricalReplayImporter().import_path(FIXTURES / "calibration-action-01").replay
    metrics = event_metrics(replay, ())
    assert metrics["event_order_consistency"] is None
    assert metrics["status"] == "not_comparable"


def test_terminal_alignment() -> None:
    replay = HistoricalReplayImporter().import_path(FIXTURES / "calibration-action-01").replay
    metrics = terminal_metrics(replay, {"terminal": True, "truncated": False,
                                        "winner": "red", "reason": "side_eliminated"}, ())
    assert metrics["terminal_consistent"] is True
    assert metrics["terminal_time_error"]["status"] == "not_comparable"


def test_winner_consistency() -> None:
    replay = HistoricalReplayImporter().import_path(FIXTURES / "calibration-action-01").replay
    metrics = terminal_metrics(replay, {"terminal": True, "truncated": False,
                                        "winner": "blue", "reason": "side_eliminated"}, ())
    assert metrics["winner_consistent"] is False


def test_event_order_metrics() -> None:
    replay = HistoricalReplayImporter().import_path(FIXTURES / "calibration-action-01").replay
    native = [{"event_type": item.event_type, "target_id": item.target_id,
               "historical_time": item.historical_time} for item in replay.events
              if item.event_type != "game_terminated"]
    metrics = event_metrics(replay, native)
    assert metrics["event_order_consistency"] == 1.0


def test_advantage_curve_metrics() -> None:
    metrics = compare_advantage_curves([(0, 0.0), (1, 0.5)],
                                       [(0, 0.0), (1, 0.25)])
    assert metrics["supported"] is True
    assert metrics["curve_direction_consistency"] == 1.0
    assert metrics["mean_absolute_error"] == 0.125
