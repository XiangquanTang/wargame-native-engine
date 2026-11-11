"""Normal compact publication, actual operations, and atomic retry guarantees."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from checks import test_all_game_tool_operations as fixtures
from wargame_native_engine.operations import ScenarioMatchFactory
from wargame_native_engine.replay.compact import CompactReplayReader, json_equal
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.io.importers.source_replay_archive import SourceReplayArchiveReader
from wargame_native_engine.viewer.replay_view import load_replay_view

ROOT = Path(__file__).resolve().parents[1]


class CompactOperationPublicationTests(fixtures.AllGameToolOperationsTests):
    """Same real tool actions as the strict legacy suite, new publication path."""
    replay_mode = "compact"

    def verify_replay(self, scenario, session, directory):
        match = session._match
        managed = match.gateway.manager._managed(match.session_id)
        expected_count = managed.step_index + 1
        result = session.publish(directory / "published")
        self.assertIsNone(result.analysis_path)
        self.assertIsNone(result.command_path)
        self.assertIsNone(result.tick_path)
        reader = CompactReplayReader(result.replay_path)
        self.assertTrue(reader.hash_verified)
        self.assertEqual(reader.manifest["native_frame_projection"], "native-source-playback-projection/1.0")
        self.assertNotIn("world_hash", reader.manifest["outcome"])
        self.assertEqual(reader.frame_count, expected_count)
        self.assertFalse((result.output_dir / "frame_analysis.native.jsonl").exists())
        self.assertEqual(reader.assets["schema"], "native-scenario-split/1.0")
        restored = scenario_from_dict({**reader.assets["scenario"], "map": reader.assets["map"]})
        self.assertEqual(restored.scenario_hash, scenario.scenario_hash)
        for index, (_, frame, evidence) in enumerate(reader.iter_records()):
            self.assertTrue(json_equal(frame, managed.recorder.raw_frame(index)))
            self.assertIn("valid_actions", frame)
            self.assertIn("judge_info", frame)
            self.assertIsNone(evidence)
            # These exact capability-binding parameters are business inputs,
            # not state audit checksums. Keep their original keys/values usable.
            binding_keys = {"authority_hash", "ammo_mapping_hash", "damage_policy_hash",
                            "execution_policy_hash", "indirect_fire_authority_hash", "observation_policy_hash"}
            def audit_hashes(value, path=()):
                hits = []
                if isinstance(value, dict):
                    for key, child in value.items():
                        current = path + (key,)
                        if "hash" in key or "sha256" in key:
                            candidate = (len(path) == 5 and path[0] == "valid_actions" and path[2:] == ("[]", "action", "parameters"))
                            action = path in {("actions", "[]", "extra", "native", "parameters"),
                                              ("actions", "[]", "extra", "native", "action", "parameters")}
                            event = path == ("judge_info", "[]", "result", "events", "[]", "data")
                            if key not in binding_keys or not (candidate or action or event):
                                hits.append(".".join(current))
                        hits.extend(audit_hashes(child, current))
                elif isinstance(value, list):
                    for child in value:
                        hits.extend(audit_hashes(child, path + ("[]",)))
                return hits
            self.assertEqual(audit_hashes(frame), [])
            self.assertNotIn("state_hash", frame["extra"]["native"])
            self.assertNotIn("movement_policy_hash", frame["extra"]["native"])
            for action in frame["actions"]:
                detail = action.get("extra", {}).get("native", {})
                if detail.get("source_projection", {}).get("status") == "MAPPED":
                    self.assertNotIn("action", detail)
                    if detail.get("action_type") == "WEAPON_LOCK":
                        self.assertIn("weapon_id", detail.get("parameters", {}))
        self.assertIsNone(managed.live._analysis_history)
        self.assertEqual(managed.live._analysis_offsets, [])
        self.assertEqual(len(managed.live._frame_analyses), 1)
        view = load_replay_view(result.output_dir)
        final = view.frame_bundle_record(expected_count - 1)
        self.assertEqual(final["analysis"]["provenance"]["frame_index"], expected_count - 1)
        self.assertIsNone(final["analysis"]["provenance"]["frame_hash"])
        self.assertEqual(reader.manifest["analysis_model"], "source")
        self.assertFalse((result.output_dir / "frame_analysis.native.jsonl").exists())
        for side in ("RED", "BLUE"):
            side_frame = view.frame_record(expected_count - 1, side)
            self.assertEqual(side_frame["perspective"], side)
        # A compact package never masquerades as old strict Native evidence.
        with self.assertRaises(ValueError):
            SourceReplayArchiveReader(result.replay_path)


class CompactPublicationRetryTests(unittest.TestCase):
    def test_interrupted_publication_preserves_spool_and_can_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            user_cache = root / "existing-user-cache"
            user_cache.write_text("do not delete")
            factory = ScenarioMatchFactory.from_path(
                ROOT / "examples/source_movement_scenario.json", storage_root=root / "store",
                red_controller_id="red", blue_controller_id="blue")
            match = factory(42, 1)
            self.addCleanup(match.close)
            target = root / "published"
            with patch("wargame_native_engine.gateway.session_manager._publish_directory",
                       side_effect=OSError("injected publication interruption")):
                with self.assertRaisesRegex(OSError, "injected"):
                    match.publish(target, truncated=True)
            self.assertFalse(target.exists())
            self.assertEqual(user_cache.read_text(), "do not delete")
            result = match.publish(target, truncated=True)
            reader = CompactReplayReader(result.replay_path)
            self.assertEqual(reader.frame_count, 1)
            self.assertTrue(reader.hash_verified)
            self.assertEqual(user_cache.read_text(), "do not delete")

class NativeCompactBoundaryTests(unittest.TestCase):
    def test_imported_raw_audit_named_fields_remain_lossless(self):
        from wargame_native_engine.replay.compact import CompactReplayWriter
        raw = {"extra": {"native": {"state_hash": "user-original", "movement_policy_hash": "original-policy"}},
               "actions": [{"cur_step": 6, "message": {"type": 1}}], "valid_actions": {"01": None}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            writer = CompactReplayWriter(root / "facts")
            writer.start()
            writer.append(raw, name="71.json")
            writer.finalize()
            path = writer.package_zip(root / "replay.zip")
            self.assertTrue(json_equal(CompactReplayReader(path).raw_frame(0), raw))

    def test_static_adjudication_sources_have_collision_free_versioned_references(self):
        from wargame_native_engine.replay.native_projection import native_playback_frame
        def frame(version):
            return {"extra": {"native": {"state_hash": "debug"}}, "judge_info": [{"result": {"events": [{
                "data": {"source_rule_set": {"table_set_id": "same-id", "table_set_version": version,
                  "table_set_hash": "hash-" + version, "source_document_hash": "doc"}, "actual_damage": 1.25}}]}}]}
        sources = []
        first, _ = native_playback_frame(frame("1"), source_registry=sources)
        repeated, _ = native_playback_frame(frame("1"), source_registry=sources)
        changed, _ = native_playback_frame(frame("2"), source_registry=sources)
        self.assertEqual(len(sources), 2)
        def data(value):
            return value["judge_info"][0]["result"]["events"][0]["data"]
        self.assertEqual(data(first), data(repeated))
        self.assertNotEqual(data(first)["source_rule_set"], data(changed)["source_rule_set"])
        self.assertEqual(data(changed)["actual_damage"], 1.25)
        self.assertEqual(sources[1]["record"]["table_set_version"], "2")

    def test_normal_run_cannot_pretend_retrospective_strict_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            match = ScenarioMatchFactory.from_path(
                ROOT / "examples/source_movement_scenario.json", storage_root=root / "store",
                red_controller_id="red", blue_controller_id="blue")(42, 1)
            self.addCleanup(match.close)
            with self.assertRaisesRegex(ValueError, "before the first recorded step"):
                match.publish(root / "fake-debug", truncated=True, replay_mode="legacy-debug")
            self.assertFalse((root / "fake-debug").exists())
            self.assertTrue(match.publish(root / "real-normal", truncated=True).replay_path.is_file())

    def test_rejected_before_engine_is_preserved_without_command_audit_sidecar(self):
        from wargame_native_engine.agents.realtime import RealtimeAgentSession, ManualRealtimeClock
        from wargame_native_engine.agents.seat import SeatOperation
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            factory = ScenarioMatchFactory.from_path(
                ROOT / "examples/source_movement_scenario.json", storage_root=root / "store",
                red_controller_id="red", blue_controller_id="blue", decision_interval_ms=1000, frame_interval_ms=1000)
            session = RealtimeAgentSession(factory, run_namespace="reject-facts", tick_interval_ms=1000,
                                           clock=ManualRealtimeClock())
            self.addCleanup(session.close)
            binding = session.reset(seed=42).red_binding
            receipt = session.submit_operation(binding, observation_version=99999,
                                               operation=SeatOperation("MOVE", actor_ids=(1,), destination_hex=105))
            self.assertFalse(receipt.accepted)
            result = session.publish(root / "published")
            self.assertIsNone(result.command_path)
            self.assertIsNone(result.tick_path)
            reader = CompactReplayReader(result.replay_path)
            outcomes = reader.manifest["command_outcomes"]
            self.assertEqual(outcomes[0]["status"], "REJECTED_AT_SUBMIT")
            self.assertTrue(outcomes[0]["error_code"])
            self.assertEqual(outcomes[0]["decision"]["operations"][0]["operation_type"], "MOVE")
            self.assertNotIn("observation_version", outcomes[0])


if __name__ == "__main__":
    unittest.main()
