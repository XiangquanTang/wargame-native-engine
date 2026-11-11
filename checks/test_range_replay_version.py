"""The changed game rule must not silently reinterpret historical replays."""

import json
import tempfile
import unittest
from pathlib import Path

from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.io.replay import (
    ReplayWriter,
    UnsupportedReplaySemanticsError,
    replay_actions,
)
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.version import ADJUDICATION_SEMANTICS_VERSION


class RangeReplayVersionTests(unittest.TestCase):
    def setUp(self):
        document = json.loads(Path('examples/source_movement_scenario.json').read_text())
        for cell in document['map']['hexes']:
            cell['elev'] = 0
            cell['cond'] = 0
        self.scenario = scenario_from_dict(document)
        self.engine = NativeEngine(self.scenario)
        self.engine.reset(seed=42)

    def test_old_snapshot_rejected_before_restore(self):
        original = self.engine.snapshot()
        for version in ('native-joint-prestate/1.2', 'native-joint-prestate/1.3',
                        'native-joint-prestate/1.4', 'native-joint-prestate/1.5',
                        'native-joint-prestate/1.6', 'native-joint-prestate/1.7',
                        'native-joint-prestate/1.8', 'native-joint-prestate/1.9', 'native-joint-prestate/1.10'):
            old = dict(original, adjudication_semantics_version=version)
            with self.subTest(version=version):
                with self.assertRaisesRegex(ValueError, 'adjudication semantics'):
                    self.engine.restore(old)
                self.assertEqual(self.engine.snapshot(), original)

    def test_overshoot_strict_replay_and_old_manifest_rejection(self):
        # Blue infantry weapon 29 has max range 4; red is 5 hexes away.
        with tempfile.TemporaryDirectory() as directory:
            writer = ReplayWriter(
                directory, run_id='range-rule', scenario=self.scenario,
                seed=42, agents={'red': 'test', 'blue': 'test'},
                max_ticks=2, ruleset=self.engine.ruleset, project_commit=None,
            )
            writer.record_initial_state(self.engine.snapshot())
            shot = EngineAction('blue', 2, ActionType.SHOOT, target_id=1,
                                parameters={'weapon_id': 29})
            result = self.engine.step_ms([shot], delta_time_ms=1000)
            self.assertTrue(result.decisions[0].accepted)
            self.assertTrue(any(event.data.get('reason_code') ==
                                'OUT_OF_RANGE_NO_EFFECT' for event in result.events))
            writer.record_step(result, self.engine.snapshot())
            writer.finalize({'result': self.engine.result().to_dict()}, status='completed')
            verification = replay_actions(self.scenario, directory, strict=True)
            self.assertTrue(verification.matches)
            manifest_path = Path(directory) / 'manifest.json'
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest['adjudication_semantics_version'],
                             ADJUDICATION_SEMANTICS_VERSION)
            for version in ('native-joint-prestate/1.2', 'native-joint-prestate/1.6',
                            'native-joint-prestate/1.7', 'native-joint-prestate/1.8',
                            'native-joint-prestate/1.9', 'native-joint-prestate/1.10'):
                with self.subTest(version=version):
                    manifest['adjudication_semantics_version'] = version
                    manifest_path.write_text(json.dumps(manifest))
                    with self.assertRaisesRegex(UnsupportedReplaySemanticsError,
                                                'adjudication semantics'):
                        replay_actions(self.scenario, directory, strict=True)


if __name__ == '__main__':
    unittest.main()
