"""Recorded per-frame visibility, fail-closed side APIs and unchanged GREEN."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import json
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen
from zipfile import ZipFile

from wargame_native_engine.analysis.analyzer import EightFactorAnalyzer
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.template import source_frame_template_for_scenario
from wargame_native_engine.compat.source_frame.importer import SourceFrameImporter
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.gateway.session_manager import _recorder_metadata
from wargame_native_engine.io.scenario import load_scenario, scenario_from_dict
from wargame_native_engine.replay.source_frame_recorder import SourceFrameRecorder
from wargame_native_engine.rules.perspective import load_perspective_policy
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment
from wargame_native_engine.viewer.replay_view import load_replay_view, _frame_record
from wargame_native_engine.viewer.recorded_visibility import (
    ReplayPerspectiveUnavailable, published_contact_identities, recorded_side_frame,
)
from wargame_native_engine.viewer.server import ReplayViewerHTTPServer, _json_bytes

ROOT = Path(__file__).resolve().parents[1]


class ReplayVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        scenario_record = json.loads((ROOT / 'examples/source_movement_scenario.json').read_text())
        scenario_record['operators'][0]['subtype'] = 'source-2-0'
        scenario_record['operators'][1]['subtype'] = 'source-1-2'
        scenario = self.scenario = scenario_from_dict(scenario_record)
        self.scenario_path = self.root / 'scenario.json'
        self.scenario_path.write_text(json.dumps(scenario_record))
        self.env = SourceCompatibleEnvironment(
            GameSession(NativeEngineBackend(scenario)),
            template=source_frame_template_for_scenario(scenario),
        )
        self.bundle = self.env.setup_views(42)

    def tearDown(self):
        self.temporary.cleanup()

    def publish(self, frames, name='replay'):
        root = self.root / name
        recorder = SourceFrameRecorder(root)
        recorder.start({**frames[0]['extra']['native'],
                        'scenario_id': frames[0]['scenario_id'],
                        'scenario_hash': self.scenario.scenario_hash})
        recorder.record_initial(frames[0], initial_state_hash='initial')
        for frame in frames[1:]:
            recorder.record_step(frame, {'delta_time_ms': 1000,
                                         'ordered_actions': [], 'expected_state_hash': 'state'})
        recorder.finalize({'terminal': False, 'truncated': True}, final_state_hash='final')
        recorder.package_zip(analysis_analyzer=EightFactorAnalyzer())
        with ZipFile(root / 'replay.zip') as archive:
            (root / 'frame_analysis.native.jsonl').write_bytes(archive.read('frame_analysis.native.jsonl'))
        return load_replay_view(root)

    def frames(self):
        """Synthetic recorded facts, not a claim of an actual combat outcome."""
        frames = []
        for index, (red_sees, blue_sees) in enumerate((([2], []), ([], [1]), ([2], [1]))):
            frame = deepcopy(self.bundle.green)
            frame['time']['cur_step'] = index
            frame['extra']['native']['current_step'] = index
            by_id = {unit['obj_id']: unit for unit in frame['operators']}
            by_id[1]['see_enemy_bop_ids'] = red_sees
            by_id[2]['see_enemy_bop_ids'] = blue_sees
            by_id[1]['launcher'] = 2  # Relation must disappear when enemy is hidden.
            passenger = deepcopy(by_id[1])
            passenger.update(obj_id=3, name='own-passenger', on_board=True, car=1,
                             launcher=None, see_enemy_bop_ids=[])
            frame['passengers'].append(passenger)
            frames.append(frame)
        return frames

    def test_recorded_weapon_binding_is_own_only_in_side_view(self):
        frame = deepcopy(self.bundle.green)
        for unit in frame['operators']:
            unit.setdefault('extra', {}).setdefault('native', {}).update(
                weapon_locked=True, locked_weapon_id=36)
        frame['operators'][0]['see_enemy_bop_ids'] = [2]
        view = self.publish([frame], 'weapon-binding')
        all_units = {u['operator_id']: u for u in view.frame_record(0, 'GREEN')['viewer_units']}
        own_units = {u['operator_id']: u for u in view.frame_record(0, 'RED')['viewer_units']}
        self.assertEqual(all_units[1]['locked_weapon_id'], 36)
        self.assertEqual(own_units[1]['locked_weapon_id'], 36)
        self.assertNotIn('locked_weapon_id', own_units[2])

    def test_current_recorded_ids_match_live_projection(self):
        view = self.publish([self.bundle.green])
        for side, actual in (('RED', self.bundle.red), ('BLUE', self.bundle.blue)):
            record = view.frame_record(0, side)
            self.assertEqual({unit['operator_id'] for unit in record['viewer_units']},
                             {unit['obj_id'] for unit in actual['operators'] + actual['passengers']})
            self.assertEqual(record['visibility_status'], 'derived_from_recorded_visibility')

    def legacy_frames(self):
        """A historical1.1 recording lacks explicit current-contact identities."""
        frames = self.frames()
        for frame in frames:
            frame['extra']['native'].update(load_perspective_policy(version='1.1.0').to_provenance())
            for unit in frame['operators'] + frame['passengers']:
                unit.get('extra', {}).get('native', {}).pop('contact_identity', None)
        return frames

    def assert_live_membership(self, env, bundle):
        frame = SourceFrameImporter().from_dict(bundle.green, source_hash='live-fixture')
        manifest = {**_recorder_metadata(env, bundle.green, 42),
                    'replay_format': 'native-source-frame-replay/1.0', 'perspective': 'GREEN'}
        for side, actual in (('RED', bundle.red), ('BLUE', bundle.blue)):
            derived = recorded_side_frame(0, '0.json', frame, manifest, side)
            self.assertEqual({unit['operator_id'] for unit in derived['viewer_units']},
                             {unit['obj_id'] for unit in actual['operators'] + actual['passengers']})

    def test_real_empty_fire_visibility_appears_then_expires_without_memory(self):
        env = self.env
        self.assert_live_membership(env, self.bundle)
        bundle, _ = env.step_views([EngineAction('red', 1, ActionType.SHOOT,
                                   parameters={'weapon_id': 36, 'target_hex': 106})], delta_time_ms=1)
        self.assert_live_membership(env, bundle)
        self.assertIn(1, {unit['obj_id'] for unit in bundle.blue['operators']})
        for _ in range(10):
            bundle, _ = env.step_views([], delta_time_ms=1)
            self.assert_live_membership(env, bundle)
        self.assertNotIn(1, {unit['obj_id'] for unit in bundle.blue['operators']})

    def test_real_boarding_visibility_matches_live_passenger_phases(self):
        scenario = load_scenario(ROOT / 'examples/maps/scenarios/boarding.json')
        env = SourceCompatibleEnvironment(GameSession(NativeEngineBackend(scenario)),
                                         template=source_frame_template_for_scenario(scenario))
        self.addCleanup(env.session.close)
        bundle = env.setup_views(42)
        self.assert_live_membership(env, bundle)
        for kind in (ActionType.BOARD, ActionType.UNLOAD):
            action = next(value.action for value in env.session.valid_actions('red')
                          if value.action.action_type == kind)
            bundle, _ = env.step_views([action], delta_time_ms=1)
            self.assert_live_membership(env, bundle)
            bundle, _ = env.step_views([], delta_time_ms=74999)
            self.assert_live_membership(env, bundle)

    def test_visibility_changes_by_frame_and_side_with_passengers(self):
        view = self.publish(self.frames())
        expected = {'RED': ({1, 2, 3}, {1, 3}, {1, 2, 3}),
                    'BLUE': ({2}, {1, 2}, {1, 2})}
        for side, frames in expected.items():
            for index, ids in enumerate(frames):
                record = view.frame_record(index, side)
                self.assertEqual({unit['operator_id'] for unit in record['viewer_units']}, ids)
                if side == 'RED':
                    self.assertEqual([unit['obj_id'] for unit in record['passengers']], [3])
                    own = next(unit for unit in record['viewer_units'] if unit['operator_id'] == 1)
                    self.assertEqual(own['launcher_id'], 2 if index != 1 else None)
        self.assertEqual(view.perspectives_record()['RED']['status'], 'available')

    def test_enemy_fields_and_analysis_are_never_green_backfilled(self):
        frames = self.frames()
        view = self.publish(frames)
        record = view.frame_record(0, 'RED')
        enemy = next(unit for unit in record['viewer_units'] if unit['operator_id'] == 2)
        for key in ('blood', 'max_blood', 'health_ratio', 'movement_target_hex',
                    'movement_progress', 'stopped', 'suppressed', 'on_board', 'carrier_id'):
            self.assertIsNone(enemy[key], key)
        self.assertEqual(enemy['name'], 'source-1-2')
        self.assertEqual(enemy['type_code'], 1)
        self.assertEqual(enemy['identity_source'], 'RECORDED_CONTACT_IDENTITY')
        raw = next(unit for unit in record['operators'] if unit['obj_id'] == 2)
        self.assertEqual(raw['extra']['observation']['mapping_status'], 'DERIVED_FROM_RECORDED_VISIBILITY')
        self.assertNotIn('native', raw['extra'])
        self.assertEqual(record['actions'], [])
        self.assertEqual(record['judge_info'], [])
        # Private or template fields may change without altering the side contact.
        changed = deepcopy(frames)
        target = next(unit for unit in changed[0]['operators'] if unit['obj_id'] == 2)
        target.update(name='PRIVATE-NAME', type=3, sub_type=7, blood=1,
                      move_path=[102, 103], speed=99, cur_pos=0.9,
                      passenger_ids=[912], launcher=812)
        target['extra']['native']['private'] = 'never-copy'
        other = self.publish(changed, 'changed').frame_record(0, 'RED')
        self.assertEqual(raw, next(unit for unit in other['operators'] if unit['obj_id'] == 2))
        self.assertEqual(enemy, next(unit for unit in other['viewer_units'] if unit['operator_id'] == 2))

    def test_new_identity_missing_corrupt_or_foreign_side_stays_unknown(self):
        for change in ('missing', 'wrong-side', 'private-field', 'bad-name', 'bad-status'):
            frames = self.frames()
            native = frames[0]['operators'][1]['extra']['native']
            if change == 'missing':
                native.pop('contact_identity')
            elif change == 'wrong-side':
                native['contact_identity']['side'] = 'red'
            elif change == 'private-field':
                native['contact_identity']['ammunition'] = {'secret': 99}
            elif change == 'bad-status':
                native['contact_identity']['canonical_status'] = []
            else:
                native['contact_identity']['display_name'] = 'bad\nname'
            view = self.publish(frames, change)
            enemy = next(u for u in view.frame_record(0, 'RED')['viewer_units'] if u['operator_id'] == 2)
            self.assertEqual(enemy['name'], 'UNKNOWN')
            self.assertEqual(enemy['type_code'], -1)
            self.assertEqual(enemy['identity_source'], 'UNKNOWN')
            self.assertIsNone(enemy['blood'])

    def test_malformed_policy_version_fails_closed_without_typeerror(self):
        frames = self.frames()
        for frame in frames:
            frame['extra']['native']['perspective_policy_version'] = []
        view = self.publish(frames, 'bad-policy-type')
        self.assertEqual(view.perspectives_record()['RED']['status'], 'unavailable')
        with self.assertRaises(ReplayPerspectiveUnavailable):
            view.frame_record(0, 'RED')

    def test_old_identity_without_bound_evidence_stays_unknown(self):
        view = self.publish(self.legacy_frames())
        enemy = next(u for u in view.frame_record(0, 'RED')['viewer_units'] if u['operator_id'] == 2)
        self.assertEqual(enemy['identity_source'], 'UNKNOWN')
        self.assertEqual(enemy['type_code'], -1)
        self.assertEqual(enemy['name'], 'UNKNOWN')

    def test_missing_or_malformed_evidence_is_unavailable_not_empty(self):
        for bad in (None, '2', [True], [999], [1]):
            with self.subTest(value=bad):
                frames = self.frames()
                frames[1]['operators'][0]['see_enemy_bop_ids'] = bad
                view = self.publish(frames, f'bad-{repr(bad)}')
                self.assertEqual(view.perspectives_record()['RED']['status'], 'unavailable')
                with self.assertRaises(ReplayPerspectiveUnavailable):
                    view.frame_record(1, 'RED')
                self.assertEqual(len(view.frame_record(1)['viewer_units']), 3)
                self.assertEqual(view.perspectives_record()['BLUE']['status'], 'available')

    def test_legacy_contract_and_unrecognized_source_policy_fail_closed(self):
        for missing in ('source-contract', 'policy'):
            frames = self.frames()
            for frame in frames:
                native = frame['extra']['native']
                if missing == 'source-contract':
                    native['source_frame_compatibility_version'] = 'source-frame-compat/1.0'
                else:
                    native.pop('movement_policy_hash')
            view = self.publish(frames, missing)
            with self.assertRaises(ReplayPerspectiveUnavailable):
                view.frame_record(0, 'RED')
            self.assertEqual(len(view.frame_record(0)['viewer_units']), 3)

    def publication_report(self, view):
        return {'summary_schema': 'native-realtime-match-summary/1.1',
                'strict_replay_match': True, 'replay_sha256': view.archive.source_hash,
                'scenario_id': self.scenario.scenario_id,
                'scenario_hash': self.scenario.scenario_hash,
                'seed': view.archive.manifest['episode_seed'],
                'final_state_hash': view.archive.manifest['final_state_hash']}

    def test_scenario_identity_requires_exact_publication_bindings(self):
        view = self.publish(self.legacy_frames())
        path = view.replay_dir / 'match_summary.native.json'
        self.assertEqual(published_contact_identities(view.replay_dir, view.archive, self.scenario), {})
        report = self.publication_report(view)
        path.write_text(json.dumps(report))
        loaded = load_replay_view(view.replay_dir, native_scenario_map=self.scenario_path)
        enemy = next(unit for unit in loaded.frame_record(0, 'RED')['viewer_units'] if unit['operator_id'] == 2)
        self.assertEqual((enemy['type_code'], enemy['sub_type_code']), (1, 2))
        self.assertEqual(enemy['identity_source'], 'SCENARIO_DERIVED_IDENTITY')
        self.assertIsNone(enemy['health_ratio'])
        self.assertNotIn('contact_identities', loaded.map_record())
        for key, value in (('strict_replay_match', False), ('strict_replay_match', 1),
                           ('summary_schema', 'unrecognized'), ('replay_sha256', 'different'),
                           ('scenario_hash', 'different'), ('seed', 999),
                           ('final_state_hash', 'different')):
            with self.subTest(key=key, value=value):
                path.write_text(json.dumps({**report, key: value}))
                self.assertEqual(published_contact_identities(view.replay_dir, view.archive, self.scenario), {})
        path.write_text('{bad-json')
        self.assertEqual(published_contact_identities(view.replay_dir, view.archive, self.scenario), {})
        path.write_text(json.dumps({**report, 'seed': 1}))
        malformed = replace(view.archive, manifest={**view.archive.manifest, 'episode_seed': True})
        self.assertEqual(published_contact_identities(view.replay_dir, malformed, self.scenario), {})

    def test_bound_class_uses_scenario_not_green_template_or_new_ids(self):
        frames = self.legacy_frames()
        view = self.publish(frames)
        (view.replay_dir / 'match_summary.native.json').write_text(json.dumps(self.publication_report(view)))
        identities = published_contact_identities(view.replay_dir, view.archive, self.scenario)
        def contact(source, mapping):
            record = recorded_side_frame(0, '0.json', source, view.archive.manifest, 'RED', mapping)
            return next(unit for unit in record['viewer_units'] if unit['operator_id'] == 2)
        original = contact(view.archive.frames[0], identities)
        frames[0]['operators'][1].update(type=3, sub_type=7, name='PRIVATE-FALSE-IDENTITY')
        changed = self.publish(frames, 'template-mutated')
        self.assertEqual(contact(changed.archive.frames[0], identities), original)
        self.assertEqual(contact(view.archive.frames[0], {})['identity_source'], 'UNKNOWN')
        wrong_side = {2: {**identities[2], 'side': 'red'}}
        self.assertEqual(contact(view.archive.frames[0], wrong_side)['type_code'], -1)

    def test_recorded_visibility_and_bound_identity_survive_rule_version_change(self):
        # Playback reads recorded facts. It never re-runs today's observation
        # rule over a historical frame or calls that playback strict verification.
        for version in ('native-joint-prestate/1.6', 'native-joint-prestate/1.7',
                        'native-joint-prestate/1.8', 'native-joint-prestate/1.9',
                        'native-joint-prestate/1.10', 'native-joint-prestate/1.11'):
            with self.subTest(version=version):
                frames = self.frames()
                for frame in frames:
                    frame['extra']['native']['adjudication_semantics_version'] = version
                # Generate a synthetic archive carrying each historical identity;
                # the production recorder correctly refuses mixed-rule writes.
                with patch('wargame_native_engine.replay.source_frame_recorder.'
                           'ADJUDICATION_SEMANTICS_VERSION', version):
                    view = self.publish(frames, version.rsplit('/', 1)[1])
                report = self.publication_report(view)
                (view.replay_dir / 'match_summary.native.json').write_text(json.dumps(report))
                identities = published_contact_identities(
                    view.replay_dir, view.archive, self.scenario,
                )
                self.assertEqual(set(identities), {1, 2})
                self.assertEqual(
                    [{unit['operator_id'] for unit in view.frame_record(index, 'RED')['viewer_units']}
                     for index in range(3)],
                    [{1, 2, 3}, {1, 3}, {1, 2, 3}],
                )
                unknown = replace(view.archive, manifest={
                    **view.archive.manifest,
                    'adjudication_semantics_version': 'native-joint-prestate/999',
                })
                self.assertEqual(published_contact_identities(
                    view.replay_dir, unknown, self.scenario,
                ), {})

    def test_own_factor_scope_requires_actual_owner_and_analysis_side(self):
        view = self.publish(self.frames())
        for side, own_ids in (('RED', {1, 3}), ('BLUE', {2})):
            bundle = view.frame_bundle_record(0, side)
            self.assertIsNone(bundle['analysis'])
            self.assertEqual(bundle['own_analysis_source'], 'GLOBAL_MODEL_DERIVED_OWN_FACTORS')
            self.assertTrue(all(unit['operator_id'] in own_ids and unit['side'] == side.lower()
                                for unit in bundle['own_unit_analyses']))
            self.assertTrue(bundle['own_unit_analyses'])

    def test_http_side_cache_separation_and_legacy_default_byte_parity(self):
        view = self.publish(self.frames())
        before = (view.replay_dir / 'replay.zip').read_bytes()
        server = ReplayViewerHTTPServer(('127.0.0.1', 0), view)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            def get(path):
                with urlopen(base + path) as response:
                    return response.read()
            expected = _json_bytes({'frame': _frame_record(0, '0.json', view.archive.frames[0]),
                                    'analysis': view.analysis_record(0)})
            route = '/api/frame-bundle/0'
            self.assertEqual(get(route), expected)
            self.assertEqual(get(route + '?perspective=GREEN'), expected)
            for side, ids in (('RED', {1, 2, 3}), ('BLUE', {2})):
                body = json.loads(get(route + '?perspective=' + side))
                self.assertIsNone(body['analysis'])
                self.assertEqual({unit['operator_id'] for unit in body['frame']['viewer_units']}, ids)
                self.assertIn(route + '?perspective=' + side, server._response_cache)
            self.assertEqual(get(route), expected)
            self.assertEqual(json.loads(get('/api/perspectives'))['GREEN']['status'], 'available')
            for path, status in (('/api/analysis/0?perspective=RED', 409),
                                 (route + '?perspective=red', 400),
                                 (route + '?perspective=RED&perspective=BLUE', 400),
                                 ('/api/frame/999?perspective=RED', 400)):
                with self.assertRaises(HTTPError) as caught:
                    get(path)
                self.assertEqual(caught.exception.code, status)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
        self.assertEqual((view.replay_dir / 'replay.zip').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
