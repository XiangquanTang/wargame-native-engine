"""Compact eight-factor restoration: contracts, bounded caches, concurrency."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from wargame_native_engine.analysis.analyzer import EightFactorAnalyzer, GreenFrameAnalysisInput
from wargame_native_engine.analysis.native_analyzer import NativeEightFactorAnalyzer
from wargame_native_engine.analysis.serialization import frame_analysis_to_record
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.template import source_frame_template_for_scenario
from wargame_native_engine.gateway.session_manager import _recorder_metadata
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.replay.compact import CompactReplayWriter
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment
from wargame_native_engine.viewer.compact_analysis import ANALYSIS_CACHE_ENTRIES, ANALYSIS_CACHE_BYTES
from wargame_native_engine.viewer.replay_view import load_replay_view
from wargame_native_engine.viewer.server import ReplayViewerHTTPServer

ROOT = Path(__file__).resolve().parents[1]


class CompactAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        scenario = load_scenario(ROOT / 'examples/source_movement_scenario.json')
        self.env = SourceCompatibleEnvironment(GameSession(NativeEngineBackend(scenario)),
            template=source_frame_template_for_scenario(scenario))
        self.addCleanup(self.env.session.close)
        self.frame = self.env.setup_views(42).green
        for unit in self.frame['operators'] + self.frame['passengers']:
            unit['value'] = 10  # Explicit fixture data, not a production default.
        self.metadata = {**_recorder_metadata(self.env, self.frame, 42), 'perspective': 'GREEN'}
        self.sequence = 0

    def view(self, frames=None, metadata=None, names=None):
        self.sequence += 1
        root = self.root / str(self.sequence)
        writer = CompactReplayWriter(root, checkpoint_interval=2)
        writer.start(self.metadata if metadata is None else metadata)
        for index, frame in enumerate(frames or [self.frame]):
            writer.append(frame, name=names[index] if names else None)
        writer.finalize({'truncated': True}); writer.package_zip()
        return load_replay_view(root)

    def test_recorded_native_facts_default_source_switch_and_exact_authority(self):
        view = self.view()
        metadata = view.replay_record()
        self.assertEqual(metadata['analysis_model'], 'native')
        self.assertIsNone(metadata['original_analysis_model'])
        self.assertEqual(metadata['analysis_selection'], 'recorded_native_facts')
        for model, analyzer in [('native', NativeEightFactorAnalyzer()), ('source', EightFactorAnalyzer())]:
            actual = view.analysis_record(0, model)
            expected = frame_analysis_to_record(analyzer.analyze_green_frame(GreenFrameAnalysisInput(
                0, self.frame, scenario_hash=self.metadata['scenario_hash'])))
            self.assertEqual(actual, expected)
            self.assertIsNone(actual['provenance']['frame_hash'])
        self.assertIsNone(view.analysis_record(0, 'source')['advantage']['normalized_advantage'])
        self.assertIsNotNone(view.analysis_record(0, 'native')['advantage']['normalized_advantage'])
        self.assertEqual(len(view._analysis._records), 2)

    def test_explicit_model_header_is_not_overridden(self):
        for model in ['source', 'native']:
            view = self.view(metadata={**self.metadata, 'analysis_model': model})
            self.assertEqual(view.replay_record()['analysis_model'], model)
            self.assertEqual(view.replay_record()['original_analysis_model'], model)
            self.assertEqual(view.analysis_record(0), view.analysis_record(0, model))

    def test_unknown_model_leaves_playback_available(self):
        for declared in ['unsupported', {}, [], None, True]:
            view = self.view(metadata={**self.metadata, 'analysis_model': declared})
            self.assertEqual(view.replay_record()['analysis_status'], 'unavailable')
            self.assertTrue(view.frame_bundle_record(0)['frame']['viewer_units'])
            self.assertIsNone(view.frame_bundle_record(0)['analysis'])
            self.assertEqual(view.timeline_record()['analysis_status'], 'unavailable')

    def test_missing_native_facts_never_use_stale_source_or_command_lost(self):
        for field in ['movement_locked', 'suppression']:
            frame = deepcopy(self.frame)
            unit = frame['operators'][0]
            unit['can_to_move'] = True; unit['lose_control'] = False; unit['tire'] = 0
            del unit['extra']['native']['analysis_facts'][field]
            view = self.view([frame], metadata={**self.metadata, 'analysis_model': 'native'})
            factors = {factor['factor_name']: factor for factor in view.analysis_record(0)['unit_analyses'][0]['factor_values']}
            name = 'mobility' if field == 'movement_locked' else 'status'
            self.assertIsNone(factors[name]['value']); self.assertEqual(factors[name]['status'], 'UNKNOWN')

    def test_lru_byte_limit_detachment_and_no_persistent_analysis_or_hash(self):
        frames = [deepcopy(self.frame) for _ in range(ANALYSIS_CACHE_ENTRIES + 3)]
        view = self.view(frames)
        before = {p.relative_to(view.replay_dir): p.read_bytes() for p in view.replay_dir.rglob('*') if p.is_file()}
        with patch('hashlib.sha256', side_effect=AssertionError('no per-frame hashes')):
            for i in range(len(frames)): view.analysis_record(i)
        self.assertLessEqual(len(view._analysis._records), ANALYSIS_CACHE_ENTRIES)
        self.assertLessEqual(view._analysis._record_bytes, ANALYSIS_CACHE_BYTES)
        actual = view.analysis_record(len(frames) - 1); actual['unit_analyses'].clear()
        self.assertTrue(view.analysis_record(len(frames) - 1)['unit_analyses'])
        self.assertEqual(before, {p.relative_to(view.replay_dir): p.read_bytes() for p in view.replay_dir.rglob('*') if p.is_file()})
        with patch('wargame_native_engine.viewer.compact_analysis.ANALYSIS_CACHE_BYTES', 1):
            view = self.view()
            view.analysis_record(0)
            self.assertEqual(len(view._analysis._records), 0)

    def test_timeline_is_single_linear_pass_cached_and_sparse_indices_preserved(self):
        view = self.view([deepcopy(self.frame) for _ in range(5)], names=['2.json', '8.json', '9.json', '100.json', '111.json'])
        with patch.object(view.reader, 'raw_frame', side_effect=AssertionError('no random seeks')), \
             patch.object(view.reader, '_chunk_records', wraps=view.reader._chunk_records) as chunks:
            timeline = view.timeline_record()
            self.assertEqual(chunks.call_count, 3)
            timeline['red_effective_power'][0] = -999
            second = view.timeline_record()
            self.assertEqual(chunks.call_count, 3)
            self.assertNotEqual(second['red_effective_power'][0], -999)
            self.assertEqual(second['frame_indices'], [2, 8, 9, 100, 111])
        self.assertEqual(len(view._analysis._records), 0)

    def test_http_first_frame_is_not_blocked_by_full_curve(self):
        view = self.view()
        entered, release = Event(), Event()
        original = view.reader.iter_raw_frames
        def blocking_frames():
            entered.set(); self.assertTrue(release.wait(5)); yield from original()
        server = ReplayViewerHTTPServer(('127.0.0.1', 0), view)
        serving = Thread(target=server.serve_forever, daemon=True); serving.start()
        result = []
        base = f'http://127.0.0.1:{server.server_port}'
        def curve():
            with urlopen(base + '/api/timeline') as response: result.append(json.load(response))
        try:
            with patch.object(view.reader, 'iter_raw_frames', blocking_frames):
                worker = Thread(target=curve); worker.start()
                self.assertTrue(entered.wait(3))
                with urlopen(base + '/api/frame-bundle/0', timeout=2) as response:
                    self.assertTrue(json.load(response)['analysis']['unit_analyses'])
                release.set(); worker.join(5)
                self.assertEqual(len(result), 1)
        finally:
            release.set(); server.shutdown(); server.server_close(); serving.join(5)

    def test_late_analysis_contract_mismatch_does_not_block_factual_playback(self):
        later = deepcopy(self.frame)
        later['extra']['native']['source_frame_compatibility_version'] = 'source-frame-compat/1.2'
        view = self.view([self.frame, later])
        bundle = view.frame_bundle_record(1)
        self.assertTrue(bundle['frame']['viewer_units'])
        self.assertIsNone(bundle['analysis'])
        self.assertEqual(bundle['analysis_status'], 'unavailable')
        self.assertIn('contract mismatch', bundle['analysis_note'])
        with self.assertRaisesRegex(ValueError, 'contract mismatch'): view.timeline_record()
        with self.assertRaises(ValueError): view.frame_bundle_record(0, analysis_model='bad')

    def test_new_header_records_known_analyzer_once(self):
        for analyzer, name in [(EightFactorAnalyzer(), 'source'), (NativeEightFactorAnalyzer(), 'native')]:
            self.assertEqual(_recorder_metadata(self.env, self.frame, 42, analyzer=analyzer)['analysis_model'], name)
        self.assertNotIn('analysis_model', self.frame['extra']['native'])


if __name__ == '__main__': unittest.main()
