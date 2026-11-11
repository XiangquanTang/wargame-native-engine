"""Compact playback is indexed, analysis-optional, bounded, and side-safe."""
from copy import deepcopy
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.template import source_frame_template_for_scenario
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.gateway.session_manager import _recorder_metadata
from wargame_native_engine.gateway.models import ControllerView
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.replay.compact import CompactReplayReader, CompactReplayWriter, json_equal
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment
from wargame_native_engine.viewer.live_control import native_scenario_map_record, controller_view_record
from wargame_native_engine.viewer.replay_view import load_replay_view
from wargame_native_engine.viewer.recorded_visibility import ReplayPerspectiveUnavailable, _authority
from wargame_native_engine.viewer.server import ReplayViewerHTTPServer

ROOT = Path(__file__).resolve().parents[1]


class CompactReplayViewerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scenario = load_scenario(ROOT / 'examples/source_movement_scenario.json')
        self.env = SourceCompatibleEnvironment(GameSession(NativeEngineBackend(self.scenario)),
            template=source_frame_template_for_scenario(self.scenario))
        self.addCleanup(self.env.session.close)
        self.initial = self.env.setup_views(42)
        self.metadata = {**_recorder_metadata(self.env, self.initial.green, 42), 'perspective': 'GREEN'}

    def publish(self, frames, *, names=None, metadata=None, assets=None, interval=2, suffix='recording'):
        target = self.root / suffix
        writer = CompactReplayWriter(target, checkpoint_interval=interval)
        writer.start(self.metadata if metadata is None else metadata, assets=assets)
        for ordinal, frame in enumerate(frames):
            writer.append(frame, name=None if names is None else names[ordinal])
        writer.finalize({'truncated': True})
        writer.package_zip()
        return target

    def test_actual_live_frames_seek_and_render_without_analysis_or_frame_hashes(self):
        frames = [self.initial]
        bundle, _ = self.env.step_views([EngineAction('red', 1, ActionType.SHOOT,
            parameters={'weapon_id': 36, 'target_hex': 106})], delta_time_ms=1)
        frames.append(bundle)
        for _ in range(3):
            bundle, _ = self.env.step_views([], delta_time_ms=1)
            frames.append(bundle)
        root = self.publish([bundle.green for bundle in frames],
            assets={'map': native_scenario_map_record(self.scenario)})
        self.assertFalse((root / 'frame_analysis.native.jsonl').exists())
        with patch.object(CompactReplayReader, 'iter_raw_frames', side_effect=AssertionError('must fast-start static map')):
            view = load_replay_view(root)
        self.assertEqual(view.replay_record()['frame_validation'], 'on-demand-frame-validation')
        self.assertEqual(view.frame_count, 5)
        self.assertEqual(view.map_record()['mode'], 'configured-hex-map')
        self.assertEqual(view.replay_record()['analysis_status'], 'on_demand')
        self.assertFalse(hasattr(view, 'analysis'))
        for ordinal in (4, 0, 3, 1, 2):
            with patch.object(view.reader, '_chunk_records', wraps=view.reader._chunk_records) as chunks:
                record = view.frame_record(ordinal)
                self.assertEqual(chunks.call_count, 1)
                self.assertEqual(chunks.call_args.args, (ordinal // 2,))
            self.assertEqual(record['time'], frames[ordinal].green['time'])
            self.assertIsNone(record['source_hash'])
            self.assertTrue(json_equal(view.raw_frame_record(ordinal), frames[ordinal].green))
            for side, expected in (('RED', frames[ordinal].red), ('BLUE', frames[ordinal].blue)):
                actual = view.frame_record(ordinal, side)
                self.assertEqual({u['operator_id'] for u in actual['viewer_units']},
                    {u['obj_id'] for u in expected['operators'] + expected['passengers']})
                for enemy in actual['viewer_units']:
                    if enemy['side'] != side.lower():
                        self.assertIsNone(enemy['blood'])
                        self.assertIsNone(enemy['movement_target_hex'])
            self.assertEqual(view.frame_bundle_record(ordinal)['analysis']['provenance']['frame_index'], ordinal)
        self.assertEqual(view.timeline_record()['red_effective_power'], [None] * 5)
        self.run_player(view)

    def test_explicit_source_unknown_offers_native_without_changing_recorded_model(self):
        frame = deepcopy(self.initial.green)
        for unit in frame['operators'] + frame['passengers']:
            unit['value'] = 10
        view = load_replay_view(self.publish([frame], metadata={**self.metadata, 'analysis_model': 'source'}))
        self.assertEqual(view.replay_record()['analysis_model'], 'source')
        self.assertEqual(view.replay_record()['original_analysis_model'], 'source')
        self.assertIsNone(view.analysis_record(0)['advantage']['normalized_advantage'])
        self.assertIsNotNone(view.analysis_record(0, 'native')['advantage']['normalized_advantage'])
        self.run_player(view)

    def test_source13_live_display_march_is_exact_enum_and_null_stays_unknown(self):
        for contract in ('source-frame-compat/1.2', 'source-frame-compat/1.3'):
            for code in (0, 1, 2, 3, None):
                source = deepcopy(self.initial.red)
                source['extra']['native']['source_frame_compatibility_version'] = contract
                source['operators'][0]['move_state'] = code
                view = ControllerView(session_id='viewer-test', perspective='RED', step_index=0,
                    observation={}, source_frame=source, valid_actions=(), terminal=False, winner=None, outcome=None)
                record = controller_view_record(view)
                actual = next(u for u in record['frame']['viewer_units']
                              if u['operator_id'] == source['operators'][0]['obj_id'])['marching']
                expected = (None if code is None else code == 1) if contract.endswith('/1.3') else code not in {None, 0}
                self.assertIs(actual, expected)
                self.assertEqual(source['operators'][0]['move_state'], code)

    def test_source13_unmapped_replay_movement_remains_unknown_in_green_and_own_side(self):
        source = deepcopy(self.initial.green)
        source['extra']['native']['source_frame_compatibility_version'] = 'source-frame-compat/1.3'
        source['operators'][0]['move_state'] = None
        source['operators'][0]['extra']['native']['movement_mode'] = 'UNSUPPORTED_NATIVE_MODE'
        source['operators'][0]['extra']['native']['movement_projection'] = {
            'schema_version': 'native-source-movement-export/1.0', 'status': 'UNMAPPED'}
        metadata = {**self.metadata, 'source_frame_compatibility_version': 'source-frame-compat/1.3'}
        view = load_replay_view(self.publish([source], metadata=metadata))
        for perspective in ('GREEN', 'RED'):
            unit = next(u for u in view.frame_record(0, perspective)['viewer_units'] if u['operator_id'] == 1)
            self.assertIsNone(unit['move_state'])
            self.assertIsNone(unit['marching'])
        self.assertTrue(json_equal(view.raw_frame_record(0), source))

    def test_split_native_assets_are_preserved_and_projected_only_at_display_boundary(self):
        scenario = self.scenario.to_dict()
        native_map = scenario.pop('map')
        assets = {'schema': 'native-scenario-split/1.0', 'map': native_map, 'scenario': scenario}
        expected = deepcopy(assets)
        view = load_replay_view(self.publish([self.initial.green], assets=assets))
        self.assertEqual(view.reader.assets, expected)
        self.assertEqual(view.map_record()['mode'], 'configured-hex-map')
        self.assertEqual(view.map_record()['source']['replay_content_binding']['scenario_hash'], 'matched')
        self.assertEqual(len(view.frame_record(0)['viewer_units']), len(self.initial.green['operators']))
        self.assertEqual(view.reader.assets, expected)
        self.run_player(view)

    def test_legacy_compact_hash_bound_models_are_derived_only_when_visible(self):
        from wargame_native_engine.rules.perspective import load_perspective_policy
        from wargame_native_engine.viewer.compact_view import _embedded_contact_identities
        from types import SimpleNamespace
        old = load_perspective_policy(version='1.1.0').to_provenance()
        scenario = self.scenario.to_dict()
        for operator, source in zip(scenario['operators'], self.initial.green['operators']):
            operator['subtype'] = f"source-{source['type']}-{source['sub_type']}"
        from wargame_native_engine.io.scenario import scenario_from_dict
        scenario_hash = scenario_from_dict(scenario).scenario_hash
        native_map = scenario.pop('map')
        assets = {'schema': 'native-scenario-split/1.0', 'map': native_map, 'scenario': scenario}
        metadata = {**self.metadata, **old, 'scenario_hash': scenario_hash,
                    'native_frame_projection': 'native-source-playback-projection/1.0'}
        frame = deepcopy(self.initial.green)
        frame['extra']['native'].update(old)
        for unit in frame['operators'] + frame['passengers']:
            unit['extra']['native'].pop('contact_identity', None)
            unit['see_enemy_bop_ids'] = [2] if unit['obj_id'] == 1 else []
        hidden = deepcopy(frame)
        hidden['operators'][0]['see_enemy_bop_ids'] = []
        view = load_replay_view(self.publish([frame, hidden], metadata=metadata, assets=assets))
        before = view.raw_frame_record(0)
        enemy = next(u for u in view.frame_record(0, 'RED')['viewer_units'] if u['operator_id'] == 2)
        self.assertEqual(enemy['identity_source'], 'SCENARIO_DERIVED_IDENTITY')
        raw = next(u for u in view.frame_record(0, 'RED')['operators'] if u['obj_id'] == 2)
        self.assertEqual(raw['extra']['observation']['identity_verification'], 'HASH_BOUND_EMBEDDED_SCENARIO')
        self.assertIsNone(enemy['blood'])
        self.assertIsNone(enemy['carrier_id'])
        self.assertNotIn(2, {u['operator_id'] for u in view.frame_record(1, 'RED')['viewer_units']})
        self.assertNotIn('contact_identities', view.map_record())
        self.assertEqual(view.raw_frame_record(0), before)
        contradictory = deepcopy(frame)
        contradictory['operators'][1]['type'] = 999
        changed = view._render_frame(0, 0, 'RED', contradictory)
        enemy = next(u for u in changed['viewer_units'] if u['operator_id'] == 2)
        self.assertEqual(enemy['identity_source'], 'UNKNOWN')
        for bad in ({'scenario_hash': 'mismatch'}, {'scenario_id': 'mismatch'},
                    {'native_frame_projection': 'unknown'}, {'perspective_policy_hash': 'mismatch'}):
            self.assertEqual(_embedded_contact_identities(SimpleNamespace(
                manifest={**view.reader.manifest, **bad}, assets=assets)), {})
        self.assertEqual(_embedded_contact_identities(SimpleNamespace(
            manifest=view.reader.manifest, assets={})), {})

    def test_sparse_source_filenames_are_not_time_steps_or_missing_frames(self):
        frames = [deepcopy(self.initial.green), deepcopy(self.initial.green)]
        for ordinal, frame in enumerate(frames):
            frame.pop('extra', None)  # Historical input has no trusted Native visibility contract.
            frame['time']['cur_step'] = (2228, 2273)[ordinal]
            frame['actions'] = [] if ordinal == 0 else [{'cur_step': 2272,
                'message': {'actor': 'p11', 'obj_id': 62, 'type': 8, 'jm_pos': 4129, 'weapon_id': 72}}]
            frame['unknown_future'] = {'null': None, 'empty': [], 'bool': True, 'numeric': 1.0,
                                       'ordered': [2, 1], '01': {'escaped/key': '保留'}}
        view = load_replay_view(self.publish(frames, names=['2283.json', '2328.json'], metadata={}))
        self.assertEqual(view.replay_record()['frame_indices'], [2283, 2328])
        self.assertEqual(view.frame_count, 2)
        self.assertEqual(view.frame_record(2328)['frame_ordinal'], 1)
        self.assertEqual(view.frame_record(2328)['time']['cur_step'], 2273)
        self.assertEqual(view.frame_record(2328)['actions'][0]['cur_step'], 2272)
        self.assertTrue(json_equal(view.raw_frame_record(2328), frames[1]))
        for missing in (0, 1, 2284, 2273):
            with self.assertRaises(IndexError): view.frame_record(missing)
        for side in ('RED', 'BLUE'):
            self.assertEqual(view.perspectives_record()[side]['status'], 'unavailable')
            with self.assertRaises(ReplayPerspectiveUnavailable): view.frame_record(2283, side)
        self.run_player(view)

    def test_visibility_is_current_frame_and_unknown_enemy_fields_are_not_backfilled(self):
        frames = [deepcopy(self.initial.green) for _ in range(3)]
        for frame, seen in zip(frames, ([2], [], [2])):
            frame['operators'][0]['see_enemy_bop_ids'] = seen
            frame['operators'][0]['launcher'] = 2
        view = load_replay_view(self.publish(frames))
        self.assertEqual([{u['operator_id'] for u in view.frame_record(i, 'RED')['viewer_units']}
                          for i in range(3)], [{1, 2}, {1}, {1, 2}])
        self.assertIsNone(view.frame_record(1, 'RED')['viewer_units'][0]['launcher_id'])
        frames[1]['operators'][0]['see_enemy_bop_ids'] = [999]
        broken = load_replay_view(self.publish(frames, suffix='broken'))
        with self.assertRaises(ReplayPerspectiveUnavailable): broken.frame_record(1, 'RED')
        self.assertEqual(len(broken.frame_record(1)['viewer_units']), 2)

    def test_marked_native_compact_visibility_uses_header_policy_without_reinjection(self):
        source = deepcopy(self.initial.green)
        identity, _, _ = _authority()
        for key in identity:
            source['extra']['native'].pop(key, None)
        metadata = {**self.metadata, 'native_frame_projection': 'native-source-playback-projection/1.0'}
        view = load_replay_view(self.publish([source], metadata=metadata))
        for side in ('RED', 'BLUE'):
            expected = self.initial.red if side == 'RED' else self.initial.blue
            self.assertEqual({u['operator_id'] for u in view.frame_record(0, side)['viewer_units']},
                             {u['obj_id'] for u in expected['operators'] + expected['passengers']})
        self.assertTrue(json_equal(view.raw_frame_record(0), source))
        self.assertTrue(all(key not in view.raw_frame_record(0)['extra']['native'] for key in identity))

    def test_marked_native_compact_missing_conflicting_policy_or_projection_fails_closed(self):
        source = deepcopy(self.initial.green)
        identity, _, _ = _authority()
        for key in identity:
            source['extra']['native'].pop(key, None)
        metadata = {**self.metadata, 'native_frame_projection': 'native-source-playback-projection/1.0'}
        cases = []
        for key in identity:
            missing = dict(metadata); missing.pop(key)
            cases.append((f'missing-{key}', missing, deepcopy(source)))
            cases.append((f'conflict-{key}', {**metadata, key: 'WRONG'}, deepcopy(source)))
        contradictory = deepcopy(source)
        contradictory['extra']['native']['movement_policy_hash'] = 'WRONG'
        cases.extend([
            ('frame-conflict', metadata, contradictory),
            ('unknown-projection', {**metadata, 'native_frame_projection': 'unknown/9'}, source),
            ('contract-conflict', {**metadata, 'frame_contract': 'source-frame-compat/1.0'}, source),
            ('unmarked-raw', self.metadata, source),
        ])
        for suffix, header, frame in cases:
            with self.subTest(case=suffix):
                view = load_replay_view(self.publish([frame], metadata=header, suffix=suffix))
                self.assertEqual(len(view.frame_record(0)['viewer_units']), len(frame['operators']))
                for side in ('RED', 'BLUE'):
                    self.assertEqual(view.perspectives_record()[side]['status'], 'unavailable')
                    with self.assertRaises(ReplayPerspectiveUnavailable):
                        view.frame_record(0, side)

    def test_external_hash_checked_once_not_during_frame_projection(self):
        root = self.publish([self.initial.green])
        with patch('wargame_native_engine.replay.compact._sha256', wraps=__import__(
                'wargame_native_engine.replay.compact', fromlist=['_sha256'])._sha256) as hashing:
            view = load_replay_view(root)
            self.assertTrue(view.reader.hash_verified)
            for _ in range(3): view.frame_record(0)
            self.assertEqual(hashing.call_count, 1)

    def run_player(self, view):
        if not shutil.which('node'):
            self.skipTest('Node.js required for HTTP/controller/render-data integration')
        server = ReplayViewerHTTPServer(('127.0.0.1', 0), view)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = subprocess.run(['node', str(ROOT / 'checks/compact_replay_ui.cjs'),
                f'http://127.0.0.1:{server.server_port}'], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report['passed'])
            self.assertEqual(report['frame_indices'], view.replay_record()['frame_indices'])
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)


if __name__ == '__main__': unittest.main()
