"""Current objective flags survive compact/legacy/live projection and unit layers."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.template import source_frame_template_for_scenario
from wargame_native_engine.gateway.models import ControllerView
from wargame_native_engine.gateway.session_manager import _recorder_metadata
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.replay.compact import CompactReplayWriter
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment
from wargame_native_engine.viewer.live_control import controller_view_record, native_scenario_map_record
from wargame_native_engine.viewer.replay_view import _frame_record, _ViewerSourceFrameImporter, load_replay_view

ROOT = Path(__file__).resolve().parents[1]


class ReplayObjectiveTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for Canvas command checks")
    def test_current_flags_after_units_in_compact_legacy_and_live(self):
        scenario = load_scenario(ROOT / "examples/source_movement_scenario.json")
        env = SourceCompatibleEnvironment(GameSession(NativeEngineBackend(scenario)),
            template=source_frame_template_for_scenario(scenario))
        self.addCleanup(env.session.close)
        initial = env.setup_views(42)
        metadata = {**_recorder_metadata(env, initial.green, 42), "perspective": "GREEN"}
        map_data = native_scenario_map_record(scenario)
        frames = []
        # Test evidence explicitly supplies types; score/name never invent type.
        for index, flag in enumerate((-1, 0, 1)):
            frame = deepcopy(initial.green)
            frame["cities"] = [
                {"coord": 101, "flag": flag, "type": 0, "value": 80, "name": "main"},
                {"coord": 106, "flag": flag, "type": 1, "value": 50, "name": "secondary"},
            ]
            frame["time"]["cur_step"] = index
            frames.append(frame)
        expected = deepcopy(frames)
        projected = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = CompactReplayWriter(root / "recording", checkpoint_interval=2)
            writer.start(metadata, assets={"map": map_data})
            for frame in frames:
                writer.append(frame)
            writer.finalize({"truncated": True})
            writer.package_zip()
            view = load_replay_view(root / "recording")
            for index in (0, 1, 2, 2, 0):
                for perspective in ("GREEN", "RED", "BLUE"):
                    record = view.frame_record(index, perspective)
                    self.assertEqual(record["cities"], frames[index]["cities"])
                    projected.append(record)
                legacy = _frame_record(index, str(index), _ViewerSourceFrameImporter().from_dict(
                    frames[index], source_hash=""))
                self.assertEqual(legacy["cities"], frames[index]["cities"])
                projected.append(legacy)
                for perspective, initial_source in (("RED", initial.red), ("BLUE", initial.blue)):
                    source = deepcopy(initial_source)
                    source["cities"] = deepcopy(frames[index]["cities"])
                    controller = ControllerView(session_id="objectives", perspective=perspective,
                        step_index=index, observation={}, source_frame=source, valid_actions=(),
                        terminal=False, winner=None, outcome=None)
                    live = controller_view_record(controller)["frame"]
                    self.assertEqual(live["cities"], frames[index]["cities"])
                    projected.append(live)
                self.assertEqual(view.raw_frame_record(index), frames[index])
            fixture = root / "objectives.json"
            fixture.write_text(json.dumps({"map": map_data, "frames": projected}), encoding="utf-8")
            result = subprocess.run(["node", str(ROOT / "checks/replay_objectives_ui.cjs"), str(fixture)],
                capture_output=True, text=True, check=False, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertGreater(json.loads(result.stdout)["coincident_units"], 0)
        self.assertEqual(frames, expected)


if __name__ == "__main__":
    unittest.main()
