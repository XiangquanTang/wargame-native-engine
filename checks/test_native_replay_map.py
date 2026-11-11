"""Native replay terrain uses hex coordinates, never physical metre dimensions."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.run_replay_viewer import build_parser
from wargame_native_engine.agents import NoOpAgent
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.template import (
    source_frame_template_for_scenario,
)
from wargame_native_engine.io.scenario import load_scenario, native_map_identity_hash
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment
from wargame_native_engine.training import NativeTrainingEnv
from wargame_native_engine.viewer.map_config import (
    load_native_scenario_map_record,
    load_viewer_map_config,
)
from wargame_native_engine.viewer.replay_view import (
    ReplayViewerLoadError,
    load_replay_view,
)

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "examples/source_movement_scenario.json"


class NativeReplayMapTests(unittest.TestCase):
    def test_standard_map123_is_10000_cells_not_metres(self):
        record = load_native_scenario_map_record(
            ROOT / "examples/map123/scenario.json",
            expected_scenario_id="source-2201010105-native-v1",
            expected_map_id="123",
        )
        self.assertEqual((record["rows"], record["columns"]), (100, 100))
        self.assertEqual(len(record["cells"]), 10000)
        self.assertEqual(record["bounds"]["max_row"], 99)
        self.assertTrue(any(cell["terrain_code"] for cell in record["cells"]))
        self.assertEqual(record["scenario"]["dynamic_units_source"], "green_replay_frames")
        self.assertNotIn("operators", record)

    def test_missing_hashes_are_explicitly_unavailable(self):
        record = load_native_scenario_map_record(
            NATIVE,
            expected_scenario_id="source-movement-small",
            expected_map_id="source-movement-fixture-map",
        )
        self.assertEqual(record["source"]["replay_content_binding"], {
            "scenario_hash": "unavailable", "map_hash": "unavailable",
        })

    def test_present_hashes_match_or_reject_same_id_changed_content(self):
        scenario = load_scenario(NATIVE)
        identity = {
            "expected_scenario_id": scenario.scenario_id,
            "expected_map_id": scenario.map.map_id,
        }
        record = load_native_scenario_map_record(
            NATIVE, **identity,
            expected_scenario_hash=scenario.scenario_hash,
            expected_map_hash=native_map_identity_hash(scenario.map),
        )
        self.assertEqual(record["source"]["replay_content_binding"], {
            "scenario_hash": "matched", "map_hash": "matched",
        })
        with tempfile.TemporaryDirectory() as directory:
            changed = json.loads(NATIVE.read_text())
            changed["map"]["hexes"][0]["cond"] = 4
            path = Path(directory) / "changed.json"
            path.write_text(json.dumps(changed))
            for field, expected in (
                ("scenario_hash", scenario.scenario_hash),
                ("map_hash", native_map_identity_hash(scenario.map)),
            ):
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                    load_native_scenario_map_record(
                        path, **identity, **{f"expected_{field}": expected},
                    )

    def test_standard_map123_rejects_ammunition_variant(self):
        original = load_scenario(ROOT / "examples/map123/scenario.json")
        with self.assertRaisesRegex(ValueError, "scenario_(id|hash)"):
            load_native_scenario_map_record(
                ROOT / "examples/map123/scenario-artillery-ammo2.json",
                expected_scenario_id=original.scenario_id,
                expected_map_id=str(original.map.map_id),
                expected_scenario_hash=original.scenario_hash,
            )

    def test_native_identity_mismatches_are_rejected(self):
        for scenario_id, map_id, message in (
            ("wrong", "source-movement-fixture-map", "scenario_id"),
            ("source-movement-small", "wrong", "map_id"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                load_native_scenario_map_record(
                    NATIVE,
                    expected_scenario_id=scenario_id,
                    expected_map_id=map_id,
                )

    def test_cli_native_and_basic_map_are_mutually_exclusive(self):
        args = build_parser().parse_args([
            "--replay-dir", "run", "--native-scenario-map", str(NATIVE),
        ])
        self.assertEqual(args.native_scenario_map, str(NATIVE))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args([
                "--replay-dir", "run", "--native-scenario-map", str(NATIVE),
                "--map-config", "basic.json",
            ])

    def test_basic_map_api_remains_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "basic.json"
            path.write_text(json.dumps({
                "coordinate_system": "odd-r", "map_data": [[{"cond": 1}]],
            }))
            record = load_viewer_map_config(path).to_record()
            self.assertEqual((record["rows"], record["columns"]), (1, 1))

    def test_native_api_rejects_mixed_sources_before_loading(self):
        for keyword in ("map_config", "scenario_config", "map_coordinate_system"):
            with self.subTest(keyword=keyword), self.assertRaisesRegex(
                ReplayViewerLoadError, "cannot be combined"
            ):
                load_replay_view("missing", native_scenario_map=NATIVE, **{keyword: "x"})

    def test_native_map_loads_with_published_green_replay(self):
        scenario = load_scenario(NATIVE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = NativeTrainingEnv(
                lambda: SourceCompatibleEnvironment(
                    GameSession(NativeEngineBackend(scenario)),
                    template=source_frame_template_for_scenario(scenario),
                ),
                controlled_side="red",
                opponent=NoOpAgent(),
                storage_root=root / "storage",
                max_steps=2,
                decision_interval_ms=1000,
                frame_interval_ms=1000,
                training_interface_version="decision-v2",
            )
            try:
                env.reset(seed=42)
                env.publish(root / "published")
            finally:
                env.close()
            view = load_replay_view(root / "published", native_scenario_map=NATIVE)
            self.assertEqual(len(view.map_data["cells"]), 6)
            self.assertEqual(
                view.map_data["source"]["replay_content_binding"]["scenario_hash"],
                "matched",
            )
            self.assertEqual(view.map_data["bounds"]["min_row"], 1)
            self.assertEqual(view.map_data["scenario"]["scenario_id"], scenario.scenario_id)
            changed = json.loads(NATIVE.read_text())
            changed["map"]["map_id"] = "wrong"
            wrong = root / "wrong.json"
            wrong.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ReplayViewerLoadError, "map_id"):
                load_replay_view(root / "published", native_scenario_map=wrong)
            changed = json.loads(NATIVE.read_text())
            changed["scenario_id"] = "wrong"
            wrong.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ReplayViewerLoadError, "scenario_id"):
                load_replay_view(root / "published", native_scenario_map=wrong)
            changed = json.loads(NATIVE.read_text())
            changed["map"]["hexes"] = changed["map"]["hexes"][:1]
            wrong.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ReplayViewerLoadError, "scenario_hash"):
                load_replay_view(root / "published", native_scenario_map=wrong)


if __name__ == "__main__":
    unittest.main()
