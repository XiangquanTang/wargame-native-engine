from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
from typing import Any, Iterable, Mapping

from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.cli.run_game import _agent, _git_commit, _validate_output
from wargame_native_engine.io.replay import ReplayReader, replay_actions
from wargame_native_engine.io.scenario import NativeScenario, load_scenario
from wargame_native_engine.io.serialization import canonical_hash, json_clone
from wargame_native_engine.io.symmetry import ScenarioSymmetryTransformer
from wargame_native_engine.rules.config import default_ruleset_path, load_ruleset
from wargame_native_engine.session.episode import EpisodeRunner
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.version import ENGINE_VERSION


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Evaluate procedural symmetry under Native simplified rules")
    value.add_argument("--scenario", required=True, type=Path)
    value.add_argument("--ruleset", type=Path, default=default_ruleset_path())
    value.add_argument("--red-agent", default="random-legal",
                       choices=("first-valid", "random-legal"))
    value.add_argument("--blue-agent", default="random-legal",
                       choices=("first-valid", "random-legal"))
    value.add_argument("--seeds", default="0:99")
    value.add_argument("--swap-sides", action="store_true")
    value.add_argument("--mirror", action="store_true")
    value.add_argument("--max-ticks", type=int)
    value.add_argument("--output", required=True, type=Path)
    return value


def parse_seeds(value: str) -> tuple[int, ...]:
    if ":" not in value:
        result = tuple(sorted({int(item) for item in value.split(",") if item}))
    else:
        start_text, end_text = value.split(":", 1)
        start, end = int(start_text), int(end_text)
        if end < start:
            raise ValueError("seed range end must not precede start")
        result = tuple(range(start, end + 1))
    if not result:
        raise ValueError("at least one seed is required")
    return result


def evaluate(*, scenario: NativeScenario, ruleset: Any,
             red_agent: str, blue_agent: str, seeds: Iterable[int],
             swap_sides: bool, mirror: bool, output: str | Path,
             max_ticks: int | None = None) -> Mapping[str, Any]:
    root = Path(output)
    _validate_output(root)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty evaluation: {root}")
    root.mkdir(parents=True, exist_ok=True)
    replay_root = root / "replays"
    replay_root.mkdir()
    transformer = ScenarioSymmetryTransformer()
    variants: list[tuple[str, NativeScenario, bool]] = [
        ("original", scenario, False)]
    if swap_sides:
        variants.append(("side_swap", transformer.swap_sides(scenario), True))
    if mirror:
        variants.append(("mirror_vertical",
                         transformer.mirror_positions(scenario, axis="vertical"),
                         False))
    seed_values = tuple(sorted(set(int(item) for item in seeds)))
    records: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for seed in seed_values:
        for variant, active_scenario, roles_swapped in variants:
            try:
                record = _run_game(
                    scenario=active_scenario, ruleset=ruleset,
                    red_agent=red_agent, blue_agent=blue_agent,
                    seed=seed, variant=variant, roles_swapped=roles_swapped,
                    output=replay_root / f"{variant}-seed-{seed}",
                    max_ticks=max_ticks)
                records.append(record)
            except Exception as exc:
                failures.append({"seed": seed, "variant": variant,
                                 "error_type": type(exc).__name__,
                                 "message": str(exc)})
    summary = _summarize(records, failures, seed_values, variants)
    manifest = {
        "schema_version": "native-fairness/1.0",
        "engine_version": ENGINE_VERSION,
        "ruleset_id": ruleset.ruleset_id,
        "ruleset_version": ruleset.ruleset_version,
        "ruleset_hash": ruleset.content_hash,
        "scenario_id": scenario.scenario_id,
        "scenario_hash": scenario.scenario_hash,
        "seeds": list(seed_values),
        "variants": [name for name, _, _ in variants],
        "agents": {"role_a": red_agent, "role_b": blue_agent},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project_commit": _git_commit(),
        "summary_hash": canonical_hash(summary),
    }
    _write_json(root / "manifest.json", manifest)
    _write_jsonl(root / "games.jsonl", records)
    _write_json(root / "summary.json", summary)
    _write_jsonl(root / "failures.jsonl", failures)
    return summary


def _run_game(*, scenario: NativeScenario, ruleset: Any,
              red_agent: str, blue_agent: str, seed: int, variant: str,
              roles_swapped: bool, output: Path,
              max_ticks: int | None) -> dict[str, Any]:
    agent_a_seed = seed * 2 + 1001
    agent_b_seed = seed * 2 + 1002
    if roles_swapped:
        names = {"red": blue_agent, "blue": red_agent}
        agent_seeds = {"red": agent_b_seed, "blue": agent_a_seed}
        roles = {"red": "role_b", "blue": "role_a"}
    else:
        names = {"red": red_agent, "blue": blue_agent}
        agent_seeds = {"red": agent_a_seed, "blue": agent_b_seed}
        roles = {"red": "role_a", "blue": "role_b"}
    session = GameSession(NativeEngineBackend(
        scenario, max_ticks=max_ticks, ruleset=ruleset))
    agents = {side: _agent(names[side], agent_seeds[side])
              for side in scenario.side_order}
    outcome = EpisodeRunner(
        session, agents=agents, output_dir=output,
        project_commit=_git_commit()).run(seed)
    verification = replay_actions(scenario, output, ruleset=ruleset,
                                  strict=True, seed=seed)
    bundle = ReplayReader().read(output)
    initial = bundle["initial_state"]["world"]
    final = bundle["summary"]["result"]
    final_snapshot = bundle["initial_state"]["world"]
    snapshot_paths = sorted((output / "snapshots").glob("*.json"))
    if snapshot_paths:
        final_snapshot = json.loads(snapshot_paths[-1].read_text(
            encoding="utf-8"))["world"]
    metrics = _world_metrics(initial, final_snapshot, roles)
    winner_side = final.get("winner")
    winner_role = None if winner_side is None else roles[str(winner_side)]
    action_count = sum(int(item) for item in final["action_counts"].values())
    illegal_count = sum(int(item) for item in final["error_counts"].values())
    return {
        "seed": seed, "variant": variant,
        "scenario_hash": scenario.scenario_hash,
        "roles": roles, "agent_names": names, "agent_seeds": agent_seeds,
        "terminal": bool(final["terminal"]),
        "truncated": bool(final["truncated"]),
        "winner": winner_side, "winner_role": winner_role,
        "reason": final.get("reason"), "ticks": int(final["ticks"]),
        "action_count": action_count, "illegal_action_count": illegal_count,
        "world_hash": final["world_hash"],
        "replay_matches": verification.matches,
        "state_hash_matches": verification.matches,
        **metrics,
    }


def _world_metrics(initial: Mapping[str, Any], final: Mapping[str, Any],
                   roles: Mapping[str, str]) -> dict[str, Any]:
    initial_ops = initial["operators"]
    final_ops = final["operators"]
    survival = {"role_a": 0, "role_b": 0}
    damage_received = {"role_a": 0.0, "role_b": 0.0}
    for operator_id, final_operator in final_ops.items():
        role = roles[str(final_operator["side"])]
        survival[role] += int(bool(final_operator["alive"]))
        damage_received[role] += max(
            0.0, float(initial_ops[operator_id]["health"])
            - float(final_operator["health"]))
    scores = {roles[side]: int(item["objective_score"])
              for side, item in final["sides"].items()}
    return {
        "role_scores": scores,
        "role_survivors": survival,
        "role_damage_inflicted": {
            "role_a": damage_received["role_b"],
            "role_b": damage_received["role_a"],
        },
    }


def _summarize(records: list[Mapping[str, Any]],
               failures: list[Mapping[str, Any]], seeds: tuple[int, ...],
               variants: list[tuple[str, NativeScenario, bool]]) -> dict[str, Any]:
    by_variant = {name: [item for item in records if item["variant"] == name]
                  for name, _, _ in variants}
    original = {int(item["seed"]): item for item in by_variant["original"]}
    swapped = {int(item["seed"]): item
               for item in by_variant.get("side_swap", [])}
    mirrored = {int(item["seed"]): item
                for item in by_variant.get("mirror_vertical", [])}
    paired = sorted(set(original) & set(swapped))
    mirror_pairs = sorted(set(original) & set(mirrored))
    winner_consistency = _proportion(
        original[seed]["winner_role"] == swapped[seed]["winner_role"]
        for seed in paired)
    reversal_candidates = [seed for seed in paired
                           if original[seed]["winner"] is not None
                           and swapped[seed]["winner"] is not None]
    reversal_rate = _proportion(
        original[seed]["winner"] != swapped[seed]["winner"]
        for seed in reversal_candidates)
    mirror_consistency = _proportion(
        original[seed]["winner_role"] == mirrored[seed]["winner_role"]
        for seed in mirror_pairs)
    return {
        "total_games": len(records),
        "requested_games": len(seeds) * len(variants),
        "seed_count": len(seeds),
        "normal_terminal_games": sum(bool(item["terminal"])
                                     and not bool(item["truncated"])
                                     for item in records),
        "truncated_games": sum(bool(item["truncated"]) for item in records),
        "red_wins": sum(item["winner"] == "red" for item in records),
        "blue_wins": sum(item["winner"] == "blue" for item in records),
        "draws": sum(item["winner"] is None and item["terminal"] for item in records),
        "average_ticks": _mean(float(item["ticks"]) for item in records),
        "average_action_count": _mean(float(item["action_count"]) for item in records),
        "illegal_action_count": sum(int(item["illegal_action_count"])
                                    for item in records),
        "replay_mismatch_count": sum(not bool(item["replay_matches"])
                                     for item in records),
        "state_hash_mismatch_count": sum(not bool(item["state_hash_matches"])
                                         for item in records),
        "failure_count": len(failures),
        "variant_results": {name: _variant_summary(values)
                            for name, values in by_variant.items()},
        "paired_metrics": {
            "pair_count": len(paired),
            "paired_winner_consistency": winner_consistency,
            "side_swap_outcome_reversal_rate": reversal_rate,
            "mirrored_outcome_consistency": mirror_consistency,
            "average_score_difference": _mean(
                float(item["role_scores"]["role_a"]
                      - item["role_scores"]["role_b"]) for item in records),
            "average_survival_difference": _mean(
                float(item["role_survivors"]["role_a"]
                      - item["role_survivors"]["role_b"]) for item in records),
            "average_damage_difference": _mean(
                float(item["role_damage_inflicted"]["role_a"]
                      - item["role_damage_inflicted"]["role_b"])
                for item in records),
        },
        "interpretation": (
            "Paired symmetry metrics diagnose implementation-order or side-label "
            "bias; aggregate win rate alone does not establish fairness."),
    }


def _variant_summary(values: list[Mapping[str, Any]]) -> dict[str, Any]:
    total = len(values)
    return {
        "games": total,
        "red_wins": sum(item["winner"] == "red" for item in values),
        "blue_wins": sum(item["winner"] == "blue" for item in values),
        "draws": sum(item["winner"] is None and item["terminal"] for item in values),
        "truncated": sum(bool(item["truncated"]) for item in values),
        "red_win_rate": (None if not total else
                         sum(item["winner"] == "red" for item in values) / total),
        "blue_win_rate": (None if not total else
                          sum(item["winner"] == "blue" for item in values) / total),
        "average_ticks": _mean(float(item["ticks"]) for item in values),
    }


def _proportion(values: Iterable[bool]) -> float | None:
    items = list(values)
    return None if not items else sum(items) / len(items)


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return None if not items else statistics.fmean(items)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(json_clone(value), ensure_ascii=False,
                               sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, values: Iterable[Mapping[str, Any]]) -> None:
    path.write_text("".join(json.dumps(json_clone(value), ensure_ascii=False,
                                       sort_keys=True, separators=(",", ":")) + "\n"
                            for value in values), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.max_ticks is not None and args.max_ticks < 1:
            raise ValueError("max-ticks must be positive")
        ruleset = load_ruleset(args.ruleset)
        scenario = load_scenario(args.scenario)
        summary = evaluate(
            scenario=scenario, ruleset=ruleset,
            red_agent=args.red_agent, blue_agent=args.blue_agent,
            seeds=parse_seeds(args.seeds), swap_sides=args.swap_sides,
            mirror=args.mirror, output=args.output, max_ticks=args.max_ticks)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if (summary["failure_count"] == 0
                     and summary["replay_mismatch_count"] == 0
                     and summary["state_hash_mismatch_count"] == 0) else 2
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "message": str(exc)}, ensure_ascii=False,
                         sort_keys=True, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
