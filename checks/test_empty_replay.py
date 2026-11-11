"""A published zero-step episode is evidence, not a missing trajectory."""

import json
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from wargame_native_engine.agents import NoOpAgent
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.template import (
    source_frame_template_for_scenario,
)
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.io.replay import (
    ReplayWriter,
    replay_actions,
    replay_source_archive,
)
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.io.serialization import canonical_hash, file_sha256
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment
from wargame_native_engine.training import NativeTrainingEnv

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "replay_manifest.native.jsonl"


class EmptyReplayTests(unittest.TestCase):
    def setUp(self):
        storage = tempfile.TemporaryDirectory()
        self.addCleanup(storage.cleanup)
        self.root = Path(storage.name)
        self.scenario = load_scenario(ROOT / "examples/source_movement_scenario.json")

    def source_archive(self):
        env = NativeTrainingEnv(
            lambda: SourceCompatibleEnvironment(
                GameSession(NativeEngineBackend(self.scenario)),
                template=source_frame_template_for_scenario(self.scenario),
            ),
            controlled_side="red",
            opponent=NoOpAgent(),
            storage_root=self.root / "storage",
            max_steps=2,
            decision_interval_ms=1000,
            frame_interval_ms=1000,
            training_interface_version="decision-v2",
        )
        try:
            env.reset(seed=42)
            return env.publish(self.root / "published").replay_path
        finally:
            env.close()

    def rewrite_source(self, source, *, change_manifest=None, remove=None):
        with ZipFile(source) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        if remove:
            del members[remove]
        if change_manifest:
            manifest = json.loads(members[MANIFEST])
            change_manifest(manifest)
            manifest["deterministic_content_hash"] = canonical_hash({
                key: value for key, value in manifest.items()
                if key not in {"started_at", "deterministic_content_hash"}
            })
            members[MANIFEST] = (json.dumps(manifest) + "\n").encode("utf-8")
        changed = self.root / "changed.zip"
        with ZipFile(changed, "w") as archive:
            for name, payload in members.items():
                archive.writestr(name, payload)
        return changed

    def classic_archive(self):
        engine = NativeEngine(self.scenario, max_ticks=2)
        self.addCleanup(engine.close)
        engine.reset(seed=42)
        directory = self.root / "classic"
        writer = ReplayWriter(
            directory,
            run_id="empty-replay",
            scenario=self.scenario,
            seed=42,
            agents={"red": "noop", "blue": "noop"},
            max_ticks=2,
            ruleset=engine.ruleset,
            project_commit=None,
        )
        writer.record_initial_state(engine.snapshot())
        writer.finalize({"result": engine.result().to_dict()}, status="truncated")
        return directory

    def test_zero_step_source_archive_has_matching_initial_and_final_state(self):
        source = self.source_archive()
        with ZipFile(source) as archive:
            self.assertEqual(archive.read("steps.jsonl"), b"")
        result = replay_source_archive(self.scenario, source, strict=True)
        self.assertTrue(result.matches)
        self.assertEqual(result.ticks, 0)
        self.assertEqual(result.expected_world_hash, result.actual_world_hash)

    def test_zero_step_source_still_requires_steps_member(self):
        source = self.rewrite_source(self.source_archive(), remove="steps.jsonl")
        with self.assertRaisesRegex(ValueError, "steps.jsonl"):
            replay_source_archive(self.scenario, source, strict=True)

    def test_zero_step_source_still_checks_initial_state(self):
        source = self.rewrite_source(
            self.source_archive(),
            change_manifest=lambda value: value.update(initial_state_hash="0" * 64),
        )
        with self.assertRaisesRegex(ValueError, "initial state hash mismatch"):
            replay_source_archive(self.scenario, source, strict=True)

    def test_zero_step_source_still_checks_final_state(self):
        source = self.rewrite_source(
            self.source_archive(),
            change_manifest=lambda value: value.update(final_state_hash="0" * 64),
        )
        result = replay_source_archive(self.scenario, source, strict=True)
        self.assertFalse(result.matches)
        self.assertNotEqual(result.expected_world_hash, result.actual_world_hash)

    def test_zero_step_source_still_checks_semantics(self):
        original = self.source_archive()
        for version in ("native-joint-prestate/1.2", "native-joint-prestate/1.9",
                        "native-joint-prestate/1.10"):
            with self.subTest(version=version):
                source = self.rewrite_source(
                    original,
                    change_manifest=lambda value: value.update(
                        adjudication_semantics_version=version
                    ),
                )
                with self.assertRaisesRegex(ValueError, "adjudication semantics"):
                    replay_source_archive(self.scenario, source, strict=True)

    def test_zero_step_classic_replay_matches(self):
        result = replay_actions(self.scenario, self.classic_archive(), strict=True)
        self.assertTrue(result.matches)
        self.assertEqual(result.ticks, 0)

    def test_zero_step_classic_missing_steps_is_not_empty(self):
        directory = self.classic_archive()
        (directory / "steps.jsonl").unlink()
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        del manifest["collection_hashes"]["steps.jsonl"]
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "requires steps.jsonl"):
            replay_actions(self.scenario, directory, strict=True)

    def test_zero_step_classic_still_checks_snapshot(self):
        directory = self.classic_archive()
        snapshot_path = directory / "snapshots/step-00000000.json"
        snapshot = json.loads(snapshot_path.read_text())
        snapshot["seed"] += 1
        snapshot_path.write_text(json.dumps(snapshot))
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["collection_hashes"]["snapshots/step-00000000.json"] = file_sha256(
            snapshot_path
        )
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "initial snapshot mismatch"):
            replay_actions(self.scenario, directory, strict=True)


if __name__ == "__main__":
    unittest.main()
