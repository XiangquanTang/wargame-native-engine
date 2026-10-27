"""Source-batch evaluation, deliberately separate from catalog-policy scoring.

Only trusted Python agents belong here. Decision budgets are post-return checks,
not process isolation or hard deadlines. The runner never imports uploaded code.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from time import perf_counter_ns
from typing import Callable, Any

from wargame_native_engine.agents import FirstValidActionAgent
from wargame_native_engine.agents.authorization import freeze_valid_actions, resolve_agent_selection
from wargame_native_engine.agents.protocol import Agent, AgentResetContext
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.template import source_frame_template_for_scenario
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.evaluation.runner import _engine_identity, _write_json
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.serialization import canonical_hash, canonical_json, file_sha256, json_clone
from wargame_native_engine.replay import SourceFrameRecorder
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment


def run_miaosuan_match(
    bundle: ScenarioBundle, output_dir: str | Path,
    agent_factory: Callable[[str, int], Any], *,
    policy_id: str, policy_provenance: dict | None = None,
    controlled_side: str = "red", seed: int = 42,
    opponent_factory: Callable[[str, int], Agent] | None = None,
    opponent_id: str | None = None, opponent_provenance: dict | None = None,
    self_play: bool = False, max_decisions: int = 3,
    decision_interval_ms: int = 1000, decision_budget_ms: int | None = None,
    max_trace_bytes: int = 16 * 1024 * 1024,
) -> dict:
    """Run one fresh match, retaining translated/rejected/unsupported proposals.

    Factories receive (side, independent policy seed). The scenario engine seed
    is unchanged. In self-play, two separate external instances are constructed.
    The normal native opponent sees only its own native observation/catalog.
    """
    from wargame_native_engine.agents.miaosuan import (
        MiaosuanBaselineAdapter, build_miaosuan_setup,
    )
    if controlled_side not in ("red", "blue") or type(self_play) is not bool:
        raise ValueError("invalid side or self_play")
    if type(seed) is not int or not 0 <= seed < 2**63:
        raise ValueError("seed must be an integer in [0, 2**63)")
    for name, value in (("max_decisions", max_decisions),
                        ("decision_interval_ms", decision_interval_ms),
                        ("max_trace_bytes", max_trace_bytes)):
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive exact integer")
    if decision_budget_ms is not None and (type(decision_budget_ms) is not int or decision_budget_ms <= 0):
        raise ValueError("decision_budget_ms must be positive or None")
    if not isinstance(policy_id, str) or not policy_id.strip():
        raise ValueError("policy_id must be nonempty")
    if opponent_factory is not None and (not isinstance(opponent_id, str) or not opponent_id.strip()):
        raise ValueError("custom opponent_factory requires explicit opponent_id")
    opponent_id = opponent_id or "first-valid"

    if self_play and opponent_factory is not None:
        raise ValueError("self_play and native opponent_factory are mutually exclusive")
    provenance = json_clone(policy_provenance or {})
    other_provenance = json_clone(opponent_provenance or {})
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=False)
    scenario = bundle.scenario
    environment = SourceCompatibleEnvironment(
        GameSession(NativeEngineBackend(scenario, map_runtime_data=bundle.map_runtime_data)),
        template=source_frame_template_for_scenario(scenario),
    )
    other = "blue" if controlled_side == "red" else "red"
    seeds = {side: int(canonical_hash({"seed": seed, "side": side})[:8], 16)
             for side in ("red", "blue")}
    report = {
        "schema": "native-miaosuan-source-batch-result/1.0",
        "track": "source-batch", "control_mode": "sole-commander-full-side",
        "status": "running", "seed": seed, "policy_seeds": seeds,
        "controlled_side": controlled_side, "self_play": self_play,
        "comparison_scope": "same-source-batch" if self_play else "unequal-action-budget-integration-only",
        "side_control_modes": {side: "source-full-side-batch" if self_play or side == controlled_side
                               else "native-single-action" for side in ("red", "blue")},
        "policy": {"id": policy_id, "provenance": provenance},
        "opponent": {"id": policy_id if self_play else opponent_id,
                     "provenance": provenance if self_play else other_provenance},
        "scenario": dict(bundle.public_record()), "engine": _engine_identity(),
        "limits": {"max_decisions": max_decisions, "decision_interval_ms": decision_interval_ms,
                   "decision_budget_ms": decision_budget_ms, "budget_mode": "post-return-per-side",
                   "max_trace_bytes": max_trace_bytes},
        "strict_replay_matches": False, "replay_verification_status": "not_run",
        "errors": [], "applied_decisions": 0, "trace_bytes": 0,
    }
    _write_json(output / "result.json", report)
    adapters = {}
    native_opponent = None
    recorder = None
    semantic = []
    phase = "setup"
    try:
        views = environment.setup_views(seed)
        native = json_clone(views.green["extra"]["native"])
        native.update({"scenario_id": scenario.scenario_id, "scenario_hash": scenario.scenario_hash,
                       "terrain_id": views.green["terrain_id"], "episode_seed": seed,
                       "max_ticks": environment.session.max_ticks,
                       "episode_id": environment.session.session_id})
        recorder = SourceFrameRecorder(output / "recording")
        recorder.start(native, started_at="1970-01-01T00:00:00+00:00")
        recorder.record_initial(views.green, initial_state_hash=environment.initial_state_hash)
        for side in (("red", "blue") if self_play else (controlled_side,)):
            external = agent_factory(side, seeds[side])
            adapter = MiaosuanBaselineAdapter(external, side=side, seed=seeds[side])
            adapters[side] = adapter
            adapter.setup(build_miaosuan_setup(bundle, side))
            report.setdefault("adapters", {})[side] = json_clone(adapter.provenance)
        if not self_play:
            native_opponent = (opponent_factory(other, seeds[other]) if opponent_factory
                               else FirstValidActionAgent())
            native_opponent.reset(AgentResetContext(
                environment.session.session_id, scenario.scenario_id, other, seeds[other],
                environment.session.backend.ruleset.ruleset_version, native_opponent.agent_id))
        report["stop_reason"] = "decision_limit"
        with (output / "decisions.jsonl").open("w", encoding="utf-8") as stream:
            for index in range(max_decisions):
                if environment.session.is_terminal():
                    report["stop_reason"] = "engine_terminal"
                    break
                if report["trace_bytes"] + 8192 > max_trace_bytes:
                    report["stop_reason"] = "trace_budget_exceeded"
                    break
                joint = []
                row = {"index": index, "sides": {}, "applied": False}
                exceeded = False
                for side in ("red", "blue"):
                    phase = f"decision_{side}"
                    legal = tuple(environment.session.valid_actions(side))
                    started = perf_counter_ns()
                    if side in adapters:
                        frame = json_clone(getattr(views, side))
                        batch = adapters[side].step(
                            frame, deepcopy(legal),
                            indirect_capabilities=environment.session.get_indirect_fire_capabilities(side),
                            indirect_action_builder=environment.session.indirect_fire_action,
                        )
                        actions = batch.actions
                        record = {"input_hash": canonical_hash(frame), "trace": list(batch.trace)}
                    else:
                        frozen = freeze_valid_actions(side, legal)
                        observation = deepcopy(environment.session.observe(side))
                        selected = native_opponent.act(
                            observation=observation, valid_actions=deepcopy(legal),
                            context=environment.session.agent_context(side))
                        action = resolve_agent_selection(side, selected, frozen)
                        actions = () if action is None else (action,)
                        record = {"control_mode": "native-single-action", "trace": []}
                    record["latency_ms"] = (perf_counter_ns() - started) / 1_000_000
                    record["actions"] = [action.to_dict() for action in actions]
                    row["sides"][side] = record
                    joint.extend(actions)
                    if decision_budget_ms is not None and record["latency_ms"] > decision_budget_ms:
                        exceeded = True
                # Budget includes proposed actions and adapter rejection evidence.
                encoded = canonical_json(row)
                if report["trace_bytes"] + len(encoded.encode("utf-8")) + 8192 > max_trace_bytes:
                    report["stop_reason"] = "trace_budget_exceeded"
                    report["unapplied_overbudget_proposal"] = {
                        "hash": canonical_hash(row), "required_bytes": len(encoded.encode("utf-8")),
                        "action_count": len(joint),
                    }
                    break
                if exceeded:
                    report["stop_reason"] = "decision_budget_exceeded"
                else:
                    phase = "engine_step"
                    views, done = environment.step_views(joint, delta_time_ms=decision_interval_ms)
                    recorder.record_step(views.green, environment.last_step_record)
                    row["applied"] = True
                    # Adjudication evidence lives in the privileged replay, never policy inputs.
                    row["state_hash"] = environment.session.result().world_hash
                    report["applied_decisions"] += 1
                    if done:
                        report["stop_reason"] = "engine_terminal"
                encoded = canonical_json(row) + "\n"
                stream.write(encoded)
                stream.flush()
                report["trace_bytes"] += len(encoded.encode("utf-8"))
                semantic.append({**row, "sides": {side: {key: value for key, value in record.items()
                                 if key != "latency_ms"} for side, record in row["sides"].items()}})
                if exceeded or report["stop_reason"] == "engine_terminal":
                    break
        phase = "publish"
        outcome = environment.session.result().to_dict()
        recorder.finalize(outcome, final_state_hash=outcome["world_hash"])
        replay_path = recorder.package_zip(output / "replay.zip")
        phase = "strict_replay"
        verification = replay_source_archive(scenario, replay_path, strict=True,
                                             map_runtime_data=bundle.map_runtime_data)
        report.update({"strict_replay_matches": verification.matches,
                       "replay_verification_status": "matched" if verification.matches else "mismatched",
                       "final_state_hash": verification.actual_world_hash,
                       "replay_sha256": file_sha256(replay_path), "outcome": outcome,
                       "horizon_truncated": report["stop_reason"] == "decision_limit"})
        report["status"] = ("completed" if verification.matches and report["stop_reason"] in
                            ("decision_limit", "engine_terminal") else "failed")
    except Exception as error:
        # Preserve a synchronized committed prefix even when a policy fails.
        failed_phase = phase
        if recorder is not None and phase not in ("publish", "strict_replay"):
            try:
                outcome = environment.session.result().to_dict()
                recorder.finalize(outcome, final_state_hash=outcome["world_hash"])
                replay_path = recorder.package_zip(output / "replay.zip")
                verification = replay_source_archive(scenario, replay_path, strict=True,
                                                     map_runtime_data=bundle.map_runtime_data)
                report.update({"strict_replay_matches": verification.matches,
                               "replay_verification_status": "matched" if verification.matches else "mismatched",
                               "final_state_hash": verification.actual_world_hash,
                               "replay_sha256": file_sha256(replay_path), "outcome": outcome})
            except Exception as audit_error:
                report["errors"].append({"phase": "failed_prefix_audit", "type": type(audit_error).__name__})
        phase = failed_phase
        report["status"] = "failed"
        report["errors"].append({"phase": phase, "type": type(error).__name__})
        if phase == "strict_replay":
            report["replay_verification_status"] = "error"
    finally:
        resources = [getattr(adapter, "agent", None) for adapter in adapters.values()]
        resources += [native_opponent, environment.session]
        for resource in resources:
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as error:
                    report["status"] = "failed"
                    report["errors"].append({"phase": "close", "type": type(error).__name__})
        report["trajectory_hash"] = canonical_hash(semantic)
        report["build_unchanged"] = report["engine"] == _engine_identity()
        if not report["build_unchanged"]:
            report["status"] = "invalidated_provenance_drift"
        _write_json(output / "result.json", report)
    return report
