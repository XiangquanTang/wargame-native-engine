"""Small, dependency-free evaluator contract tests, not performance measurements."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from wargame_native_engine.evaluation import (
    FirstLegalPolicy,
    NoOpPolicy,
    load_manifest,
    run_benchmark,
)
from wargame_native_engine.evaluation.cli import main
from wargame_native_engine.evaluation.runner import _distribution, _engine_identity
from wargame_native_engine.io.serialization import file_sha256

ROOT = Path(__file__).resolve().parents[1]


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        source = ROOT / "examples/source_movement_scenario.json"
        self.scenario = self.root / "scenario.json"
        self.scenario.write_bytes(source.read_bytes())
        self.config = {
            "schema": "native-agent-benchmark/1.0",
            "benchmark_id": "test",
            "scenarios": [
                {
                    "id": "tiny",
                    "path": "scenario.json",
                    "sha256": file_sha256(self.scenario),
                }
            ],
            "seeds": [42],
            "sides": ["red"],
            "max_decisions": 2,
            "decision_interval_ms": 1000,
            "training_interface": "decision-v2",
        }
        self.manifest_path = self.root / "manifest.json"
        self.write_manifest()

    def write_manifest(self):
        self.manifest_path.write_text(json.dumps(self.config), encoding="utf-8")

    def run_case(self, factory=lambda _: NoOpPolicy(), **kwargs):
        return run_benchmark(
            load_manifest(self.manifest_path),
            self.root / "result",
            factory,
            policy_id="test-policy/1",
            **kwargs,
        )

    def test_deterministic_semantics_and_strict_replay(self):
        manifest = load_manifest(self.manifest_path)
        # Other development workers may edit sources during this test. Fix only
        # build provenance, not observations/actions/world/replay evidence.
        identity = _engine_identity()
        with patch(
            "wargame_native_engine.evaluation.runner._engine_identity",
            return_value=identity,
        ):
            first = self.run_case()
            second = run_benchmark(
                manifest,
                self.root / "second",
                lambda _: NoOpPolicy(),
                policy_id="test-policy/1",
            )
        self.assertEqual(first["semantic_fingerprint"], second["semantic_fingerprint"])
        episode = first["episodes"][0]
        self.assertTrue(episode["strict_replay_matches"])
        self.assertEqual(episode["metrics"]["decision_calls"], 2)
        self.assertEqual(episode["metrics"]["applied_decisions"], 2)
        self.assertFalse(episode["metrics"]["engine_terminated"])
        self.assertTrue(episode["metrics"]["environment_truncated"])
        self.assertEqual(episode["ticks"], 2)
        self.assertEqual(first["summary"]["completed_protocol"], 1)

    def test_first_legal_exercises_bound_actions(self):
        report = self.run_case(lambda _: FirstLegalPolicy())
        episode = report["episodes"][0]
        self.assertEqual(report["status"], "completed")
        self.assertGreater(episode["metrics"]["selected_actions"], 0)
        self.assertTrue(episode["strict_replay_matches"])

    def test_invalid_bool_integer_and_stale_ids_never_advance(self):
        for index, bad in enumerate(
            (True, 0, "not-a-current-action", {"action": "WAIT"})
        ):

            class Invalid(NoOpPolicy):
                def __init__(self, value):
                    self.value = value

                def decide(self, observation):
                    return self.value

            report = run_benchmark(
                load_manifest(self.manifest_path),
                self.root / f"invalid-{index}",
                lambda _, value=bad: Invalid(value),
                policy_id="bad",
            )
            episode = report["episodes"][0]
            self.assertEqual(episode["stop_reason"], "invalid_action")
            self.assertEqual(episode["metrics"]["applied_decisions"], 0)
            self.assertEqual(episode["metrics"]["valid_selections"], 0)
            self.assertTrue(episode["strict_replay_matches"])
            self.assertEqual(episode["ticks"], 0)

    def test_policy_error_recorded_and_closed(self):
        policy = NoOpPolicy()
        policy.decide = lambda _: (_ for _ in ()).throw(RuntimeError("private detail"))
        policy.close = unittest.mock.Mock()
        report = self.run_case(lambda _: policy)
        self.assertEqual(report["episodes"][0]["stop_reason"], "policy_error")
        policy.close.assert_called_once()
        text = (self.root / "result/tiny--42--red/decisions.jsonl").read_text()
        self.assertIn("RuntimeError", text)
        self.assertNotIn("private detail", text)

    def test_slow_valid_decision_not_applied(self):
        self.config["decision_budget_ms"] = 10
        self.write_manifest()
        with patch(
            "wargame_native_engine.evaluation.runner._decide",
            return_value=(None, 11.0, None),
        ):
            report = self.run_case()
        episode = report["episodes"][0]
        self.assertEqual(episode["stop_reason"], "decision_budget_exceeded")
        self.assertEqual(episode["metrics"]["valid_selections"], 1)
        self.assertEqual(episode["metrics"]["applied_decisions"], 0)

    def test_budget_boundary_inclusive(self):
        self.config["decision_budget_ms"] = 10
        self.write_manifest()
        with patch(
            "wargame_native_engine.evaluation.runner._decide",
            return_value=(None, 10.0, None),
        ):
            report = self.run_case()
        self.assertEqual(report["status"], "completed")

    def test_trace_budget_stops_before_policy_call(self):
        self.config["max_trace_bytes"] = 1
        self.write_manifest()
        policy = NoOpPolicy()
        policy.decide = unittest.mock.Mock(return_value=None)
        report = self.run_case(lambda _: policy)
        policy.decide.assert_not_called()
        episode = report["episodes"][0]
        self.assertEqual(episode["stop_reason"], "trace_budget_exceeded")
        self.assertEqual(episode["metrics"]["trace_bytes"], 0)
        self.assertTrue(episode["strict_replay_matches"])

    def test_observation_mutation_cannot_modify_environment_or_log(self):
        class Mutating(NoOpPolicy):
            def decide(self, observation):
                observation.clear()

        report = self.run_case(lambda _: Mutating())
        self.assertEqual(report["status"], "completed")
        row = json.loads(
            (self.root / "result/tiny--42--red/decisions.jsonl")
            .read_text()
            .splitlines()[0]
        )
        self.assertIn("action_space", row["input"])
        self.assertEqual(row["input"]["observation"]["side"], "red")
        self.assertNotIn("session_id", row["input"])
        self.assertNotIn("world", row["input"])

    def test_fresh_factory_per_episode(self):
        self.config["sides"] = ["red", "blue"]
        self.write_manifest()
        contexts = []

        def factory(context):
            contexts.append(context)
            return NoOpPolicy()

        report = self.run_case(factory)
        self.assertEqual([context.side for context in contexts], ["red", "blue"])
        self.assertEqual(report["summary"]["episodes"], 2)

    def test_step_failure_retains_attempt_and_nonzero_result(self):
        with patch(
            "wargame_native_engine.evaluation.runner.NativeTrainingEnv.step",
            side_effect=RuntimeError("engine-fault"),
        ):
            report = self.run_case()
        episode = report["episodes"][0]
        self.assertEqual(episode["stop_reason"], "infrastructure_error")
        self.assertEqual(episode["metrics"]["decision_calls"], 1)
        self.assertEqual(episode["metrics"]["valid_selections"], 1)
        self.assertEqual(episode["metrics"]["applied_decisions"], 0)
        row = json.loads(
            (self.root / "result/tiny--42--red/decisions.jsonl").read_text()
        )
        self.assertEqual(row["error_phase"], "environment_step")
        self.assertFalse(row["applied_decision"])
        self.assertFalse(episode["strict_replay_matches"])

    def test_factory_failure_does_not_drop_episode(self):
        def factory(_):
            raise RuntimeError("factory failure")

        report = self.run_case(factory)
        episode = report["episodes"][0]
        self.assertEqual(
            episode["errors"], [{"phase": "policy_factory", "type": "RuntimeError"}]
        )
        self.assertEqual(report["summary"]["failed_protocol"], 1)

    def test_close_failure_marks_failure(self):
        policy = NoOpPolicy()
        policy.close = lambda: (_ for _ in ()).throw(RuntimeError("close-fault"))
        report = self.run_case(lambda _: policy)
        self.assertEqual(report["status"], "completed_with_failures")
        self.assertEqual(report["episodes"][0]["errors"][-1]["phase"], "policy_close")

    def test_changed_input_after_load_is_rejected(self):
        manifest = load_manifest(self.manifest_path)
        self.scenario.write_text("{}")
        report = run_benchmark(
            manifest, self.root / "result", lambda _: NoOpPolicy(), policy_id="test"
        )
        self.assertEqual(report["episodes"][0]["errors"][0]["phase"], "scenario")

    def test_output_is_never_overwritten(self):
        target = self.root / "result"
        target.mkdir()
        marker = target / "keep.txt"
        marker.write_text("keep")
        with self.assertRaises(FileExistsError):
            self.run_case()
        self.assertEqual(marker.read_text(), "keep")

    def test_manifest_rejects_bad_values_and_unknown_fields(self):
        original = self.config.copy()
        for key, value in (
            ("seeds", [True]),
            ("seeds", [42, 42]),
            ("max_decisions", False),
            ("max_decisions", 0),
            ("sides", ["red", "red"]),
            ("training_interface", "future"),
            ("typo", 1),
        ):
            self.config = {**original, key: value}
            self.write_manifest()
            with self.assertRaises((ValueError, TypeError), msg=key):
                load_manifest(self.manifest_path)

    def test_manifest_rejects_duplicate_json_keys(self):
        self.manifest_path.write_text('{"schema":"a","schema":"b"}')
        with self.assertRaisesRegex(ValueError, "duplicate"):
            load_manifest(self.manifest_path)

    def test_manifest_hash_does_not_depend_on_source_location(self):
        manifest = load_manifest(self.manifest_path)
        relocated = replace(
            manifest,
            cases=(replace(manifest.cases[0], path=Path("/different/location")),),
        )
        self.assertEqual(manifest.semantic_hash, relocated.semantic_hash)

    def test_cli_reports_failures_with_nonzero_exit(self):
        self.config["max_trace_bytes"] = 1
        self.write_manifest()
        with redirect_stdout(io.StringIO()):
            code = main(
                [
                    "--manifest",
                    str(self.manifest_path),
                    "--output",
                    str(self.root / "cli"),
                ]
            )
        self.assertEqual(code, 1)

    def test_percentile_convention(self):
        self.assertEqual(
            _distribution([]), {"count": 0, "p50": None, "p95": None, "max": None}
        )
        self.assertEqual(_distribution([2, 1, 3, 4])["p50"], 2)
        self.assertEqual(_distribution([2, 1, 3, 4])["p95"], 4)

    def test_terminal_stage_label_is_not_task_success(self):
        scenario = json.loads(self.scenario.read_text())
        scenario["initial_stage"] = "terminal"
        self.scenario.write_text(json.dumps(scenario))
        self.config["scenarios"][0]["sha256"] = file_sha256(self.scenario)
        self.write_manifest()
        with patch(
            "wargame_native_engine.evaluation.runner.NativeTrainingEnv.step",
            side_effect=RuntimeError("test-step"),
        ):
            report = self.run_case()
        episode = report["episodes"][0]
        self.assertEqual(episode["stop_reason"], "infrastructure_error")
        self.assertFalse(episode["metrics"]["engine_terminated"])
        self.assertEqual(episode["errors"][0]["phase"], "environment_step")

    def test_provenance_requires_object(self):
        for bad in ([], 0, False, ""):
            with self.assertRaises(TypeError):
                self.run_case(policy_provenance=bad)
        self.assertFalse((self.root / "result").exists())

    def test_build_drift_invalidates_report(self):
        identity = _engine_identity()
        changed = {**identity, "python_source_sha256": "0" * 64}
        with patch(
            "wargame_native_engine.evaluation.runner._engine_identity",
            side_effect=[identity, changed],
        ):
            report = self.run_case()
        self.assertEqual(report["status"], "invalidated_provenance_drift")
        self.assertFalse(report["build_unchanged"])


if __name__ == "__main__":
    unittest.main()
