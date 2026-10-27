"""Bounded sequential evaluation with versioned evidence, not a training loop."""

from __future__ import annotations

import json
import math
import platform
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from wargame_native_engine.agents import NoOpAgent
from wargame_native_engine.agents.protocol import Agent
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.template import (
    source_frame_template_for_scenario,
)
from wargame_native_engine.evaluation.manifest import BenchmarkManifest, ScenarioCase
from wargame_native_engine.evaluation.policy import EpisodeContext, EvaluationPolicy
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.io.serialization import (
    canonical_hash,
    canonical_json,
    file_sha256,
    json_clone,
)
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment
from wargame_native_engine.training.env import NativeTrainingEnv
from wargame_native_engine.version import (
    ADJUDICATION_SEMANTICS_VERSION,
    ENGINE_VERSION,
    RULESET_VERSION,
)

RESULT_SCHEMA = "native-agent-benchmark-result/1.0"
POLICY_INPUT_SCHEMA = "native-agent-benchmark-policy-input/1.0"
PolicyFactory = Callable[[EpisodeContext], EvaluationPolicy]


def run_benchmark(
    manifest: BenchmarkManifest,
    output_dir: str | Path,
    policy_factory: PolicyFactory,
    *,
    policy_id: str,
    policy_provenance: dict[str, Any] | None = None,
    opponent_factory: Callable[[EpisodeContext], Agent] | None = None,
    opponent_id: str | None = None,
    opponent_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run every case × seed × side, retaining failures instead of dropping them.

    Output must not exist. Policy code executes in this process with the caller's
    permissions; only trusted adapters belong here. Supply provider timeouts in
    an adapter or an external process supervisor for hard wall-clock deadlines.
    Policy provenance must identify model/configuration revisions, not secrets.
    """
    if not isinstance(manifest, BenchmarkManifest):
        raise TypeError("manifest must be a loaded BenchmarkManifest")
    if not isinstance(policy_id, str) or not policy_id.strip():
        raise ValueError("policy_id must be nonempty text")
    if policy_provenance is not None and not isinstance(policy_provenance, dict):
        raise TypeError("policy_provenance must be a JSON object or None")
    provenance = json_clone({} if policy_provenance is None else policy_provenance)
    if opponent_factory is not None and (
        not callable(opponent_factory) or not isinstance(opponent_id, str)
        or not opponent_id.strip()
    ):
        raise ValueError("custom opponent requires a factory and explicit opponent_id")
    if opponent_factory is None and (opponent_id is not None or opponent_provenance is not None):
        raise ValueError("opponent metadata requires opponent_factory")
    if opponent_provenance is not None and not isinstance(opponent_provenance, dict):
        raise TypeError("opponent_provenance must be a JSON object")
    manifest_record = manifest.to_record()
    if opponent_factory is not None:
        manifest_record["opponent"] = {
            "id": opponent_id,
            "provenance": json_clone(opponent_provenance or {}),
            "control_mode": "native-agent-batch",
        }
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "manifest": manifest_record,
        "manifest_hash": canonical_hash(manifest_record),
        "policy": {"id": policy_id, "provenance": provenance},
        "engine": _engine_identity(),
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "execution": "sequential-in-process",
        },
        "status": "running",
        "episodes": [],
    }
    _write_json(output / "result.json", report)
    for case in manifest.cases:
        for seed in manifest.seeds:
            for side in manifest.sides:
                episode_id = f"{case.case_id}--{seed}--{side}"
                target = output / episode_id
                target.mkdir()
                result = _run_episode(
                    manifest, case, seed, side, target, policy_factory, opponent_factory
                )
                result["episode_id"] = episode_id
                report["episodes"].append(result)
                _write_json(target / "result.json", result)
                _write_json(output / "result.json", report)
    report["status"] = (
        "completed"
        if all(row["status"] == "completed" for row in report["episodes"])
        else "completed_with_failures"
    )
    report["summary"] = _summarize(report["episodes"])
    report["build_unchanged"] = report["engine"] == _engine_identity()
    if not report["build_unchanged"]:
        report["status"] = "invalidated_provenance_drift"
    # Timings and artifact ZIP bytes can vary. Only semantic evidence is hashed.
    report["semantic_fingerprint"] = canonical_hash(
        {
            "manifest_hash": report["manifest_hash"],
            "policy": report["policy"],
            "engine": report["engine"],
            "build_unchanged": report["build_unchanged"],
            "episodes": [row["semantic"] for row in report["episodes"]],
        }
    )
    _write_json(output / "result.json", report)
    return report


def _run_episode(
    manifest: BenchmarkManifest,
    case: ScenarioCase,
    seed: int,
    side: str,
    target: Path,
    policy_factory: PolicyFactory,
    opponent_factory: Callable[[EpisodeContext], Agent] | None = None,
) -> dict[str, Any]:
    policy = None
    opponent = None
    env = None
    phase = "scenario"
    rows: list[dict[str, Any]] = []
    semantic_rows: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "case_id": case.case_id,
        "seed": seed,
        "side": side,
        "status": "failed",
        "stop_reason": "infrastructure_error",
        "strict_replay_matches": False,
        "replay_verification_status": "not_run",
        "errors": [],
    }
    terminated = truncated = False
    total_reward = 0.0
    trace_bytes = 0
    try:
        # Validate again so a file changed after loading cannot silently enter a run.
        payload = case.path.read_bytes()
        if sha256(payload).hexdigest() != case.sha256:
            raise ValueError("scenario changed after manifest validation")
        scenario_path = target / "scenario.json"
        scenario_path.write_bytes(payload)
        scenario = load_scenario(scenario_path)
        result["scenario_hash"] = scenario.scenario_hash
        context = EpisodeContext(case.case_id, seed, side, manifest.training_interface)
        phase = "policy_factory"
        policy = policy_factory(context)
        phase = "opponent_factory"
        opponent = (NoOpAgent("benchmark-noop/1.0") if opponent_factory is None
                    else opponent_factory(EpisodeContext(
                        case.case_id, seed, "blue" if side == "red" else "red",
                        manifest.training_interface)))
        phase = "environment_reset"
        env = NativeTrainingEnv(
            lambda: SourceCompatibleEnvironment(
                GameSession(NativeEngineBackend(scenario)),
                template=source_frame_template_for_scenario(scenario),
            ),
            controlled_side=side,
            opponent=opponent,
            storage_root=target / "storage",
            max_steps=manifest.max_decisions,
            decision_interval_ms=manifest.decision_interval_ms,
            frame_interval_ms=manifest.decision_interval_ms,
            training_interface_version=manifest.training_interface,
        )
        reset = env.reset(seed=seed)
        terminated, truncated = reset.info["terminated"], reset.info["truncated"]
        result.update(
            {key: reset.info[key] for key in ("winner", "outcome", "truncation_source")}
        )
        result["interface_authority"] = {
            key: value
            for key, value in reset.info.items()
            if key.endswith(("_schema", "_version", "_hash")) and key != "catalog_hash"
        }
        result["stop_reason"] = (
            "engine_terminated"
            if terminated
            else "environment_truncated"
            if truncated
            else "decision_limit"
        )
        with (target / "decisions.jsonl").open("w", encoding="utf-8") as stream:
            for index in range(
                0 if terminated or truncated else manifest.max_decisions
            ):
                phase = "policy_input"
                binding = env.current_decision_binding
                observation = {
                    "schema": POLICY_INPUT_SCHEMA,
                    "side": side,
                    "decision_index": index,
                    "observation": env.current_side_observation,
                    "encoded_observation": binding.observation.policy_record(),
                    "action_space": binding.action_space.to_record(),
                    "weapon_inventory": list(binding.weapon_inventory),
                }
                # Reserve bounded room for result metadata before invoking a
                # policy. No opaque/invalid model response is persisted.
                input_size = len(canonical_json(observation).encode("utf-8"))
                if trace_bytes + input_size * 2 + 8192 > manifest.max_trace_bytes:
                    result["stop_reason"] = "trace_budget_exceeded"
                    break
                row: dict[str, Any] = {
                    "decision_index": index,
                    "input": observation,
                    "input_hash": canonical_hash(observation),
                    "selected_action_id": None,
                    "selection_valid": False,
                    "applied_decision": False,
                }
                phase = "policy_decision"
                selection, elapsed, failure = _decide(policy, json_clone(observation))
                row["policy_latency_ms"] = elapsed
                # Never serialize arbitrary objects returned by a broken adapter.
                valid_selection = failure is None and (
                    selection is None
                    or (
                        type(selection) is str
                        and selection in binding.action_space.action_ids
                    )
                )
                row["selection_valid"] = valid_selection
                if valid_selection:
                    row["selected_action_id"] = selection
                if failure:
                    row["error_type"] = failure
                    result["stop_reason"] = "policy_error"
                elif (
                    manifest.decision_budget_ms is not None
                    and elapsed > manifest.decision_budget_ms
                ):
                    result["stop_reason"] = "decision_budget_exceeded"
                elif not valid_selection:
                    result["stop_reason"] = "invalid_action"
                else:
                    phase = "environment_step"
                    started = perf_counter_ns()
                    try:
                        step = (
                            env.step(None)
                            if selection is None
                            else env.step_bound_action(
                                binding.action_space.action_ids.index(selection),
                                binding,
                            )
                        )
                    except Exception as error:
                        row["engine_step_latency_ms"] = (
                            perf_counter_ns() - started
                        ) / 1_000_000
                        row["error_type"] = type(error).__name__
                        row["error_phase"] = phase
                        trace_bytes += _record_decision(
                            stream, row, rows, semantic_rows
                        )
                        raise
                    row["engine_step_latency_ms"] = (
                        perf_counter_ns() - started
                    ) / 1_000_000
                    row["applied_decision"] = True
                    total_reward += step.reward
                    terminated, truncated = step.terminated, step.truncated
                    result.update(
                        {
                            key: step.info[key]
                            for key in ("winner", "outcome", "truncation_source")
                        }
                    )
                    after = env.current_decision_binding
                    row["transition"] = {
                        "reward": step.reward,
                        "terminated": terminated,
                        "truncated": truncated,
                        "frame_after": after.observation.frame_index,
                        "sim_time_after_ms": after.observation.sim_time_ms,
                        "decision_after_hash": after.observation.decision_semantic_hash,
                        "catalog_after_hash": after.action_space.catalog_hash,
                        "winner": step.info["winner"],
                        "outcome": step.info["outcome"],
                        "truncation_source": step.info["truncation_source"],
                    }
                    if terminated:
                        result["stop_reason"] = "engine_terminated"
                    elif truncated:
                        result["stop_reason"] = "environment_truncated"
                trace_bytes += _record_decision(stream, row, rows, semantic_rows)
                if not row["applied_decision"] or terminated or truncated:
                    break
        phase = "publish"
        publication = env.publish(target / "episode")
        result.update(
            {
                "replay": "episode/replay.zip",
                "replay_sha256": file_sha256(publication.replay_path),
                "replay_truncated": publication.truncated,
            }
        )
        phase = "strict_replay"
        replay = replay_source_archive(scenario, publication.replay_path, strict=True)
        result.update(
            {
                "strict_replay_matches": replay.matches,
                "replay_verification_status": "matched"
                if replay.matches
                else "mismatched",
                "final_state_hash": replay.actual_world_hash,
                "expected_final_state_hash": replay.expected_world_hash,
                "ticks": replay.ticks,
            }
        )
        if not replay.matches:
            raise ValueError("strict replay did not match")
        if result["stop_reason"] in {
            "engine_terminated",
            "environment_truncated",
            "decision_limit",
        }:
            result["status"] = "completed"
    except Exception as error:  # noqa: BLE001 - episode failures are explicit report data.
        result["errors"].append({"phase": phase, "type": type(error).__name__})
        if phase == "strict_replay":
            result["replay_verification_status"] = "error"
        if result["stop_reason"] in {
            "engine_terminated",
            "environment_truncated",
            "decision_limit",
        }:
            result["stop_reason"] = "infrastructure_error"
    finally:
        resources = [("policy_close", policy), ("environment_close", env)]
        if env is None and opponent is not None:
            resources.append(("opponent_close", opponent))
        for name, resource in resources:
            if resource is not None:
                try:
                    resource.close()
                except Exception as error:  # noqa: BLE001 - one close must not skip the other.
                    result["errors"].append(
                        {"phase": name, "type": type(error).__name__}
                    )
                    result["status"] = "failed"
        result["metrics"] = {
            "trace_bytes": trace_bytes,
            "decision_calls": len(rows),
            "valid_selections": sum(row["selection_valid"] for row in rows),
            "applied_decisions": sum(row["applied_decision"] for row in rows),
            "selected_actions": sum(
                row["applied_decision"] and row["selected_action_id"] is not None
                for row in rows
            ),
            "return": total_reward,
            "engine_terminated": terminated,
            "environment_truncated": truncated,
            "policy_latency_ms": _distribution(
                [row["policy_latency_ms"] for row in rows]
            ),
            "engine_step_latency_ms": _distribution(
                [
                    row["engine_step_latency_ms"]
                    for row in rows
                    if "engine_step_latency_ms" in row
                ]
            ),
        }
        result["semantic"] = {
            "case_id": case.case_id,
            "seed": seed,
            "side": side,
            "status": result["status"],
            "stop_reason": result["stop_reason"],
            "trajectory_hash": canonical_hash(semantic_rows),
            "final_state_hash": result.get("final_state_hash"),
            "strict_replay_matches": result["strict_replay_matches"],
            "errors": result["errors"],
        }
    return result


def _decide(
    policy: EvaluationPolicy, observation: dict
) -> tuple[Any, float, str | None]:
    started = perf_counter_ns()
    try:
        selection = policy.decide(observation)
    except Exception as error:  # noqa: BLE001 - arbitrary policy failures are evaluation outcomes.
        return None, (perf_counter_ns() - started) / 1_000_000, type(error).__name__
    return selection, (perf_counter_ns() - started) / 1_000_000, None


def _distribution(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "p50": None, "p95": None, "max": None}
    # Nearest-rank percentiles, explicit even for tiny smoke samples.
    return {
        "count": len(ordered),
        "p50": ordered[math.ceil(0.50 * len(ordered)) - 1],
        "p95": ordered[math.ceil(0.95 * len(ordered)) - 1],
        "max": ordered[-1],
    }


def _summarize(episodes: list[dict[str, Any]]) -> dict[str, Any]:
    calls = sum(row["metrics"]["decision_calls"] for row in episodes)
    valid = sum(row["metrics"]["valid_selections"] for row in episodes)
    return {
        "episodes": len(episodes),
        "completed_protocol": sum(row["status"] == "completed" for row in episodes),
        "failed_protocol": sum(row["status"] != "completed" for row in episodes),
        "strict_replay_matches": sum(row["strict_replay_matches"] for row in episodes),
        "decision_calls": calls,
        "valid_selection_rate": valid / calls if calls else None,
        "engine_terminations": sum(
            row["metrics"]["engine_terminated"] for row in episodes
        ),
    }


def _engine_identity() -> dict[str, str]:
    root = Path(__file__).resolve().parents[1]
    source_hash = canonical_hash(
        {
            path.relative_to(root).as_posix(): file_sha256(path)
            for path in sorted(root.rglob("*.py"))
        }
    )
    return {
        "engine_version": ENGINE_VERSION,
        "ruleset_version": RULESET_VERSION,
        "adjudication_semantics": ADJUDICATION_SEMANTICS_VERSION,
        "python_source_sha256": source_hash,
        "config_sha256": _tree_hash(root.parents[1] / "config"),
        "rules_docs_sha256": _tree_hash(root.parents[1] / "docs" / "rules"),
    }


def _tree_hash(root: Path) -> str:
    return canonical_hash(
        {
            path.relative_to(root).as_posix(): file_sha256(path)
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }
    )


def _record_decision(stream, row: dict, rows: list, semantic_rows: list) -> int:
    payload = canonical_json(row) + "\n"
    stream.write(payload)
    stream.flush()
    # Retain compact metrics/evidence, not every full policy observation in RAM.
    row.pop("input")
    rows.append(row)
    semantic_rows.append(
        {
            key: value
            for key, value in row.items()
            if key not in {"policy_latency_ms", "engine_step_latency_ms"}
        }
    )
    return len(payload.encode("utf-8"))


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
