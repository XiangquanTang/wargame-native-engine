"""Opt-in Native analysis preserves Source defaults and replay determinism."""
import json
from pathlib import Path
import tempfile
import unittest

from scripts.run_realtime_dual_control import build_parser
from wargame_native_engine.agents.realtime import ManualRealtimeClock, RealtimeAgentSession
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.contract import (
    ANALYSIS_SOURCE_CONTRACT, CURRENT_SOURCE_CONTRACT, SOURCE_ACTION_CONTRACT, SOURCE_CONTRACT_KEY,
)
from wargame_native_engine.compat.source_frame.template import source_frame_template_for_scenario
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.io.importers.source_replay_archive import SourceReplayArchiveImporter
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.operations import ScenarioMatchFactory
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment
from wargame_native_engine.viewer.replay_view import load_replay_view
from wargame_native_engine.training.publication import _verify_offline_analysis

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / 'examples/source_movement_scenario.json'


class NativeAnalysisLiveTests(unittest.TestCase):
    def setUp(self):
        self.scenario = load_scenario(SCENARIO)

    def environment(self, **kwargs):
        return SourceCompatibleEnvironment(
            GameSession(NativeEngineBackend(self.scenario)),
            template=source_frame_template_for_scenario(self.scenario), **kwargs,
        )

    def test_default_source_contract_and_explicit_source_are_identical(self):
        default = self.environment()
        explicit = self.environment(source_contract=SOURCE_ACTION_CONTRACT)
        first, second = default.setup_views(42), explicit.setup_views(42)
        self.assertEqual(first, second)
        self.assertEqual(default.source_contract, 'source-frame-compat/1.3')
        self.assertTrue(all('analysis_facts' in unit['extra']['native']
                            for unit in first.green['operators']))
        legacy_current = self.environment(source_contract=CURRENT_SOURCE_CONTRACT).setup_views(42)
        self.assertTrue(all('analysis_facts' not in unit['extra']['native']
                            for unit in legacy_current.green['operators']))

    def test_native_facts_are_owned_only_and_detached(self):
        env = self.environment(source_contract=ANALYSIS_SOURCE_CONTRACT)
        views = env.setup_views(42)
        for side, view in enumerate((views.red, views.blue)):
            self.assertEqual(view['extra']['native'][SOURCE_CONTRACT_KEY], ANALYSIS_SOURCE_CONTRACT)
            for unit in view['operators'] + view['passengers']:
                facts = unit.get('extra', {}).get('native', {}).get('analysis_facts')
                if unit['color'] == side:
                    self.assertIsNotNone(facts)
                    self.assertIs(type(facts['movement_locked']), bool)
                    self.assertNotIn('lose_control', facts)
                    self.assertEqual(facts['suppression'], {'active': False, 'remaining_ms': 0})
                else:
                    self.assertIsNone(facts)
        own = views.red['operators'][0]['extra']['native']['analysis_facts']
        own['movement_locked'] = 'changed'
        self.assertIs(type(views.green['operators'][0]['extra']['native']['analysis_facts']['movement_locked']), bool)

    def test_opt_in_factory_publishes_native_analysis_and_strict_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # The small movement fixture intentionally has no base value. Use
            # explicit test data rather than inventing production defaults.
            record = json.loads(SCENARIO.read_text())
            template = source_frame_template_for_scenario(self.scenario)
            sources = {unit.obj_id: unit.raw for unit in template.all_units}
            for unit in record['operators']:
                unit['raw_attributes']['source_frame'] = {
                    **sources[unit['operator_id']], 'value': 2,
                }
            complete = root/'complete-scenario.json'
            complete.write_text(json.dumps(record))
            self.scenario = load_scenario(complete)
            factory = ScenarioMatchFactory.from_path(
                complete, storage_root=root/'storage', red_controller_id='red',
                blue_controller_id='blue', decision_interval_ms=1000,
                frame_interval_ms=1000, analysis_model='native', replay_mode='legacy-debug',
            )
            session = RealtimeAgentSession(factory, run_namespace='native-analysis-test',
                tick_interval_ms=1000, clock=ManualRealtimeClock())
            try:
                session.reset(seed=42)
                session.advance_tick()
                session.advance_tick()
                session.publish(root/'published')
            finally:
                session.close()
            replay_path = root/'published/replay.zip'
            archive = SourceReplayArchiveImporter().import_path(replay_path)
            self.assertEqual(archive.manifest[SOURCE_CONTRACT_KEY], SOURCE_ACTION_CONTRACT)
            self.assertGreaterEqual(len(archive.frames), 3)
            for frame in archive.frames:
                for unit in frame.all_units:
                    self.assertEqual(unit.raw['extra']['native']['analysis_facts']['schema_version'],
                                     'native-analysis-facts/1.0')
            result = replay_source_archive(self.scenario, replay_path)
            self.assertTrue(result.matches, result)
            view = load_replay_view(root/'published')
            self.assertEqual(view.model_id,
                             'native-state-eight-factor')
            for frame in view.analysis.timeline.frame_analyses:
                for unit in frame.unit_analyses:
                    self.assertEqual(unit.observed_factor_count, 8)
                    self.assertTrue(unit.valid)
                    status = next(value for value in unit.factor_values
                                  if value.factor_name == 'status')
                    self.assertEqual(status.value, 1.0)
                self.assertIsNotNone(frame.advantage.normalized_advantage)
            # Selecting Native does not trust a claimed model hash: publication
            # recomputes every record and must reject identity tampering.
            analysis = root/'published/frame_analysis.native.jsonl'
            lines = analysis.read_text().splitlines()
            first = json.loads(lines[0])
            first['provenance']['model_hash'] = '0' * 64
            lines[0] = json.dumps(first, separators=(',', ':'))
            tampered = root/'tampered-analysis.jsonl'
            tampered.write_text('\n'.join(lines) + '\n')
            with self.assertRaisesRegex(ValueError, 'online and offline'):
                _verify_offline_analysis(replay_path, tampered)

    def test_model_selection_is_explicit_and_validated(self):
        required = ['--output-dir', 'output', '--run-namespace', 'test']
        self.assertEqual(build_parser().parse_args(required).analysis_model, 'source')
        self.assertEqual(build_parser().parse_args(required + ['--analysis-model', 'native']).analysis_model, 'native')
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, 'analysis_model'):
                ScenarioMatchFactory(ScenarioBundle.from_scenario(self.scenario), Path(temporary),
                                     'red', 'blue', analysis_model='guess')
