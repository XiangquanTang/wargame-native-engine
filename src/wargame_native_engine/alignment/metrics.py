from __future__ import annotations

import math
import statistics
from typing import Any, Iterable, Mapping

from wargame_native_engine.alignment.operator_matching import OperatorMatch
from wargame_native_engine.alignment.time_axis import TimePair
from wargame_native_engine.io.importers.historical_replay import HistoricalReplay


def trajectory_metrics(replay: HistoricalReplay,
                       native_frames: Iterable[Mapping[str, Any]],
                       time_pairs: Iterable[TimePair],
                       matches: Iterable[OperatorMatch]) -> Mapping[str, Any]:
    historical_by_time = {item.historical_time: item for item in replay.frames}
    native_by_tick = {float(item["native_tick"]): item for item in native_frames}
    mapping = {item.historical_operator_id: item.native_operator_id
               for item in matches if item.native_operator_id is not None}
    errors: list[float] = []
    final_errors: list[float] = []
    comparable_points = 0
    possible_points = len(replay.frames) * max(1, len(mapping))
    ordered_pairs = [item for item in time_pairs if item.native_tick is not None]
    for pair in ordered_pairs:
        historical_frame = historical_by_time[pair.historical_time]
        native_frame = native_by_tick.get(float(pair.native_tick))
        if native_frame is None:
            continue
        native_ops = {int(item["operator_id"]): item
                      for item in native_frame["operators"]}
        local: list[float] = []
        for source in historical_frame.operators:
            target_id = mapping.get(source.operator_id)
            target = native_ops.get(target_id) if target_id is not None else None
            if target is None or source.x is None or source.y is None:
                continue
            error = math.hypot(float(target["x"]) - source.x,
                               float(target["y"]) - source.y)
            errors.append(error)
            local.append(error)
            comparable_points += 1
        if pair is ordered_pairs[-1]:
            final_errors.extend(local)
    matched_count = len(mapping)
    total_operators = len(replay.frames[0].operators) if replay.frames else 0
    return {
        "distance_method": "euclidean_2d",
        "mean_position_error": _mean(errors),
        "median_position_error": (None if not errors else statistics.median(errors)),
        "max_position_error": (None if not errors else max(errors)),
        "final_position_error": _mean(final_errors),
        "trajectory_coverage": (0.0 if not possible_points else
                                comparable_points / possible_points),
        "matched_operator_ratio": (0.0 if not total_operators else
                                   matched_count / total_operators),
        "comparable_point_count": comparable_points,
        "status": "comparable" if errors else "not_comparable",
    }


def time_metrics(replay: HistoricalReplay,
                 native_frames: Iterable[Mapping[str, Any]],
                 native_events: Iterable[Mapping[str, Any]]) -> Mapping[str, Any]:
    frames = tuple(native_frames)
    events = tuple(native_events)
    historical_events = replay.events
    historical_destroyed = _event_times_by_target(
        historical_events, {"destroyed", "operator_destroyed"})
    native_destroyed = _event_times_by_target_native(
        events, {"destroyed", "operator_destroyed"})
    destroyed_ids = sorted(set(historical_destroyed) | set(native_destroyed))
    return {
        "first_move_time_error": _time_error(
            _first_movement_time(replay.frames), _first_movement_time_native(frames)),
        "first_contact_time_error": _time_error(
            _first_event(historical_events, {"first_contact", "contact"}),
            _first_event_native(events, {"first_contact", "contact"})),
        "first_attack_time_error": _time_error(
            _first_event(historical_events, {"attack", "attack_declared"}),
            _first_event_native(events, {"attack", "attack_declared"})),
        "first_damage_time_error": _time_error(
            _first_event(historical_events, {"damage", "operator_damaged"}),
            _first_event_native(events, {"damage", "operator_damaged"})),
        "first_destroyed_time_error": _time_error(
            _first_event(historical_events, {"destroyed", "operator_destroyed"}),
            _first_event_native(events, {"destroyed", "operator_destroyed"})),
        "operator_destroyed_time_errors": {
            str(operator_id): _time_error(
                historical_destroyed.get(operator_id),
                native_destroyed.get(operator_id))
            for operator_id in destroyed_ids},
    }


def event_metrics(replay: HistoricalReplay,
                  native_events: Iterable[Mapping[str, Any]]) -> Mapping[str, Any]:
    historical = [(item.event_type, item.target_id) for item in replay.events
                  if item.event_type != "game_terminated"]
    native = [(str(item["event_type"]), item.get("target_id"))
              for item in native_events
              if item["event_type"] not in {"target_hit", "waited",
                                             "game_terminated", "game_truncated"}]
    common = min(len(historical), len(native))
    order = (None if common == 0 else
             sum(historical[index] == native[index] for index in range(common))
             / max(len(historical), len(native)))
    historical_destroyed = [target for event, target in historical
                            if event in {"destroyed", "operator_destroyed"}]
    native_destroyed = [target for event, target in native
                        if event in {"destroyed", "operator_destroyed"}]
    return {
        "event_order_consistency": order,
        "historical_event_count": len(historical),
        "native_event_count": len(native),
        "destroyed_order_consistent": (
            None if not historical_destroyed and not native_destroyed
            else historical_destroyed == native_destroyed),
        "missing_historical_events": max(0, len(native) - len(historical)),
        "missing_native_events": max(0, len(historical) - len(native)),
        "status": "not_comparable" if common == 0 else "comparable",
    }


def terminal_metrics(replay: HistoricalReplay,
                     native_summary: Mapping[str, Any],
                     native_frames: Iterable[Mapping[str, Any]]) -> Mapping[str, Any]:
    historical = replay.summary
    frames = tuple(native_frames)
    terminal_comparable = historical.terminal is not None
    winner_comparable = terminal_comparable and historical.terminal
    native_terminal_time = next((float(item["historical_time"])
                                 for item in frames
                                 if item.get("terminal") or item.get("truncated")), None)
    final_frame = frames[-1] if frames else None
    native_survivors = {"red": 0, "blue": 0}
    if final_frame:
        for operator in final_frame["operators"]:
            if operator["alive"] and operator["side"] in native_survivors:
                native_survivors[operator["side"]] += 1
    survival_error = {
        side: (None if side not in historical.final_survivors else
               native_survivors.get(side, 0) - historical.final_survivors[side])
        for side in sorted(set(native_survivors) | set(historical.final_survivors))}
    return {
        "terminal_consistent": (None if not terminal_comparable else
                                bool(native_summary.get("terminal"))
                                == historical.terminal),
        "truncated_consistent": (None if historical.truncated is None else
                                 bool(native_summary.get("truncated"))
                                 == historical.truncated),
        "winner_consistent": (None if not winner_comparable else
                              native_summary.get("winner") == historical.winner),
        "terminal_reason_consistent": (
            None if historical.terminal_reason is None else
            native_summary.get("reason") == historical.terminal_reason),
        "terminal_time_error": _time_error(historical.terminal_time,
                                           native_terminal_time),
        "final_survivor_count_error": survival_error,
        "status": "comparable" if terminal_comparable else "not_comparable",
    }


def combat_result_metrics(replay: HistoricalReplay,
                          native_frames: Iterable[Mapping[str, Any]]) -> Mapping[str, Any]:
    frames = tuple(native_frames)
    if not frames or not replay.frames:
        return {"status": "not_comparable"}
    initial = {item.operator_id: item for item in replay.frames[0].operators}
    final_native = {int(item["operator_id"]): item
                    for item in frames[-1]["operators"]}
    damage_received = {"red": 0.0, "blue": 0.0}
    destroyed = {"red": 0, "blue": 0}
    for operator_id, target in final_native.items():
        source = initial.get(operator_id)
        if source is None or source.health is None:
            continue
        side = str(target["side"])
        damage_received[side] += max(0.0, source.health - float(target["health"]))
        destroyed[side] += int(not bool(target["alive"]))
    inflicted = {"red": damage_received["blue"],
                 "blue": damage_received["red"]}
    historical_destroyed = {
        side: (len([item for item in replay.frames[0].operators
                    if item.side == side])
               - replay.summary.final_survivors.get(side, 0))
        for side in ("red", "blue")
        if side in replay.summary.final_survivors}
    return {
        "status": "comparable",
        "cumulative_damage_error": {
            side: (None if side not in replay.summary.cumulative_damage else
                   inflicted[side] - replay.summary.cumulative_damage[side])
            for side in ("red", "blue")},
        "destroyed_count": destroyed,
        "destroyed_count_error": {
            side: (None if side not in historical_destroyed else
                   destroyed[side] - historical_destroyed[side])
            for side in ("red", "blue")},
    }


def alignment_score(trajectory: Mapping[str, Any], events: Mapping[str, Any],
                    terminal: Mapping[str, Any], advantage: Mapping[str, Any],
                    weights: Mapping[str, float]) -> Mapping[str, Any]:
    position_error = trajectory.get("mean_position_error")
    position_score = 0.0 if position_error is None else 1.0 / (1.0 + position_error)
    event_score = events.get("event_order_consistency")
    event_score = 0.0 if event_score is None else float(event_score)
    terminal_values = [terminal.get("terminal_consistent"),
                       terminal.get("winner_consistent"),
                       terminal.get("terminal_reason_consistent")]
    comparable_terminal = [float(value) for value in terminal_values
                           if value is not None]
    terminal_score = _mean(comparable_terminal) or 0.0
    advantage_error = advantage.get("mean_absolute_error")
    advantage_score = (0.0 if advantage_error is None else
                       1.0 / (1.0 + float(advantage_error)))
    components = {"position": position_score, "event": event_score,
                  "terminal": terminal_score, "advantage": advantage_score}
    total = sum(float(weights[name]) * components[name] for name in components)
    return {"alignment_score": total, "components": components,
            "weights": dict(weights)}


def _first_movement_time(frames: Iterable[Any]) -> float | None:
    values = tuple(frames)
    if not values:
        return None
    initial = {item.operator_id: (item.x, item.y) for item in values[0].operators}
    for frame in values[1:]:
        if any(initial.get(item.operator_id) != (item.x, item.y)
               for item in frame.operators):
            return frame.historical_time
    return None


def _first_movement_time_native(frames: Iterable[Mapping[str, Any]]) -> float | None:
    values = tuple(frames)
    if not values:
        return None
    initial = {int(item["operator_id"]): (item["x"], item["y"])
               for item in values[0]["operators"]}
    for frame in values[1:]:
        if any(initial.get(int(item["operator_id"])) != (item["x"], item["y"])
               for item in frame["operators"]):
            return float(frame["historical_time"])
    return None


def _first_event(events: Iterable[Any], names: set[str]) -> float | None:
    return next((item.historical_time for item in events
                 if item.event_type in names), None)


def _first_event_native(events: Iterable[Mapping[str, Any]],
                        names: set[str]) -> float | None:
    return next((float(item["historical_time"]) for item in events
                 if item["event_type"] in names), None)


def _event_times_by_target(events: Iterable[Any],
                           names: set[str]) -> dict[int, float]:
    result: dict[int, float] = {}
    for item in events:
        if item.event_type in names and item.target_id is not None:
            result.setdefault(int(item.target_id), float(item.historical_time))
    return result


def _event_times_by_target_native(
        events: Iterable[Mapping[str, Any]], names: set[str]) -> dict[int, float]:
    result: dict[int, float] = {}
    for item in events:
        target_id = item.get("target_id")
        if item["event_type"] in names and target_id is not None:
            result.setdefault(int(target_id), float(item["historical_time"]))
    return result


def _time_error(historical: float | None,
                native: float | None) -> Mapping[str, Any]:
    if historical is None or native is None:
        return {"status": "not_comparable", "historical_time": historical,
                "native_time": native, "error": None}
    return {"status": "comparable", "historical_time": historical,
            "native_time": native, "error": native - historical}


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return None if not items else statistics.fmean(items)
