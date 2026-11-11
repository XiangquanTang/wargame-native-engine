"""Original action envelopes are export-only, versioned, and lossless."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.action_export import (
    export_source_action, owned_source_action,
)
from wargame_native_engine.compat.source_frame.contract import (
    LEGACY_SOURCE_CONTRACT, CURRENT_SOURCE_CONTRACT, ANALYSIS_SOURCE_CONTRACT,
    SOURCE_ACTION_CONTRACT,
)
from wargame_native_engine.compat.source_frame.importer import SourceFrameImporter
from wargame_native_engine.compat.source_frame.movement_projection import source_move_state
from wargame_native_engine.compat.source_frame.template import source_frame_template_for_scenario
from wargame_native_engine.core.action import ActionType as A, EngineAction, ActionDecision
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment

ROOT = Path(__file__).resolve().parents[1]


class SourceActionExportTests(unittest.TestCase):
    def setUp(self):
        self.scenario = load_scenario(ROOT / 'examples/source_movement_scenario.json')
        self.template = source_frame_template_for_scenario(self.scenario)
        # Real owner format from the user's Source files, not an integer seat.
        for unit in self.template.all_units:
            if unit.color == 0:
                unit.raw['owner'] = 'p11'
                object.__setattr__(unit, 'owner', 'p11')

    def action(self, kind, **kwargs):
        return EngineAction('red', 1, kind, **kwargs)

    def export(self, action, *, accepted=True):
        return export_source_action(action, cur_step=2272, template=self.template,
            decision=ActionDecision(action.action_id, accepted, 'OK' if accepted else 'DENIED', ''))

    def test_every_verified_original_action_shape(self):
        cases = [
            (self.action(A.MOVE, parameters={'path': [102, 103], 'movement_mode': 'NORMAL'}),
             {'type': 1, 'move_path': [102, 103]}),
            (self.action(A.SHOOT, target_id=2, parameters={'weapon_id': 36}),
             {'type': 2, 'target_obj_id': 2, 'weapon_id': 36}),
            (self.action(A.ATTACK, target_id=2, parameters={'weapon_id': 36}),
             {'type': 2, 'target_obj_id': 2, 'weapon_id': 36}),
            (self.action(A.FIRE, target_id=2, parameters={'fire_mode': 'DIRECT', 'weapon_id': 36}),
             {'type': 2, 'target_obj_id': 2, 'weapon_id': 36}),
            (self.action(A.BOARD, target_id=2), {'type': 3, 'target_obj_id': 2}),
            (self.action(A.UNLOAD, target_id=2), {'type': 4, 'target_obj_id': 2}),
            (self.action(A.CAPTURE, parameters={'objective_id': 9}), {'type': 5}),
            (self.action(A.FIRE, parameters={'fire_mode': 'INDIRECT', 'target_hex': 4129, 'weapon_id': 72}),
             {'type': 8, 'jm_pos': 4129, 'weapon_id': 72}),
            (self.action(A.FIRE, target_id=2, parameters={'fire_mode': 'GUIDED', 'shooter_id': 7, 'weapon_id': 72}),
             {'type': 9, 'target_obj_id': 2, 'weapon_id': 72, 'guided_obj_id': 7}),
            (self.action(A.STOP_MOVE), {'type': 10}),
            (self.action(A.WEAPON_LOCK, parameters={'weapon_id': 36}), {'type': 11}),
            (self.action(A.WEAPON_UNFOLD), {'type': 12}),
        ]
        for action, fields in cases:
            with self.subTest(kind=action.action_type, params=action.parameters):
                result = self.export(action)
                self.assertEqual(result['message'], {'actor': 'p11', 'obj_id': 1, **fields})
                self.assertEqual(result['cur_step'], 2272)
                self.assertEqual(result['extra']['native']['action'], action.to_dict())
                self.assertEqual(result['extra']['native']['source_projection']['status'], 'MAPPED')
                self.assertNotIn('completed', str(result).lower())
                self.assertNotIn('side', result)
                if action.action_type is A.WEAPON_LOCK:
                    self.assertEqual(result['extra']['native']['source_projection']['native_extensions'],
                                     ['WEAPON_SPECIFIC_LOCK'])
        end = EngineAction('red', None, A.END_DEPLOYMENT)
        self.assertEqual(self.export(end)['message'], {'actor': 'p11', 'type': 333})

    def test_zero_identifiers_are_preserved_and_booleans_are_not_ids(self):
        units = tuple(replace(unit, obj_id=0, owner=0) if unit.obj_id == 1 else unit
                      for unit in self.template.operators)
        template = replace(self.template, operators=units)
        action = EngineAction('red', 0, A.SHOOT, target_id=0, parameters={'weapon_id': 0})
        record = export_source_action(action, cur_step=0, template=template)
        self.assertEqual(record['message'], {'actor': 0, 'obj_id': 0,
                         'type': 2, 'target_obj_id': 0, 'weapon_id': 0})
        bool_action = EngineAction('red', False, A.MOVE, parameters={'path': [102]})
        self.assertNotIn('message', export_source_action(bool_action, cur_step=0, template=template))

    def test_unverified_actions_and_shapes_are_lossless_not_fabricated(self):
        cases = [self.action(kind, parameters={'custom': {'opaque': [2, 3]}})
                 for kind in (A.WEAPON_UNLOCK, A.ENTER_CONCEALMENT, A.WAIT, A.DEPLOY)]
        cases += [self.action(A.CHANGE_STATE, parameters={'state': state})
                  for state in ('MARCHING', 'NORMAL', 'CHARGE_1', 'CHARGE_2', 1)]
        cases += [self.action(A.SHOOT, parameters={'target_hex': 106, 'weapon_id': 36}),
                  self.action(A.MOVE, target_position=(1, 2)),
                  self.action(A.FIRE, target_id=2, parameters={'fire_mode': 'GUIDED', 'weapon_id': 36}),
                  self.action(A.SHOOT, target_id=2, parameters={'weapon_id': True})]
        for action in cases:
            with self.subTest(action=action):
                result = self.export(action)
                self.assertNotIn('message', result)
                self.assertEqual(result['extra']['native']['source_projection']['status'], 'UNMAPPED')
                self.assertEqual(result['extra']['native']['action'], action.to_dict())
        value = self.export(cases[0])
        value['extra']['native']['action']['parameters']['custom']['opaque'].append(4)
        self.assertEqual(cases[0].parameters['custom']['opaque'], [2, 3])

    def test_identity_missing_ambiguous_or_wrong_side_never_becomes_color_or_seat(self):
        for action in (EngineAction('blue', 1, A.STOP_MOVE),
                       EngineAction('red', 999, A.STOP_MOVE)):
            self.assertNotIn('message', self.export(action))
        red = [unit for unit in self.template.all_units if unit.color == 0]
        object.__setattr__(red[0], 'owner', None)
        self.assertNotIn('message', self.export(self.action(A.STOP_MOVE)))
        self.assertNotIn('message', self.export(EngineAction('red', None, A.END_DEPLOYMENT)))

    def test_own_projection_uses_native_side_and_owned_unit_not_message_actor(self):
        record = self.export(self.action(A.MOVE, parameters={'path': [102]}))
        self.assertTrue(owned_source_action(record, 'red', {1}))
        self.assertFalse(owned_source_action(record, 'blue', {2}))
        record['message']['actor'] = 'blue'
        self.assertFalse(owned_source_action(record, 'blue', {2}))
        record['extra']['native']['action']['side'] = 'blue'
        self.assertFalse(owned_source_action(record, 'blue', {2}))
        self.assertFalse(owned_source_action(self.export(self.action(A.STOP_MOVE), accepted=False), 'red', {1}))
        self.assertFalse(owned_source_action({'side': 'red', 'accepted': True}, 'red', {1}))

    def environment(self, contract=SOURCE_ACTION_CONTRACT):
        session = GameSession(NativeEngineBackend(self.scenario))
        self.addCleanup(session.close)
        return SourceCompatibleEnvironment(session, template=self.template, source_contract=contract)

    def test_actual_move_intent_precedes_state_and_does_not_claim_arrival(self):
        env = self.environment()
        env.setup_views(42)
        action = self.action(A.MOVE, parameters={'path': [102, 103], 'movement_mode': 'NORMAL'})
        views, _ = env.step_views_ms([action], delta_time_ms=1000)
        record = views.green['actions'][0]
        self.assertEqual(record['cur_step'], 0)
        self.assertEqual(views.green['time']['cur_step'], 1)
        self.assertEqual(record['message']['move_path'], [102, 103])
        unit = next(unit for unit in views.green['operators'] if unit['obj_id'] == 1)
        self.assertEqual(unit['cur_hex'], 101)
        self.assertGreater(unit['cur_pos'], 0)
        self.assertLess(unit['cur_pos'], 1)
        self.assertEqual(unit['move_path'], [102, 103])
        self.assertEqual(views.red['actions'], [record])
        self.assertEqual(views.blue['actions'], [])
        self.assertEqual(env.last_step_record['ordered_actions'], [action.to_dict()])
        for _ in range(160):
            views, _ = env.step_views_ms([], delta_time_ms=1000)
            unit = next(unit for unit in views.green['operators'] if unit['obj_id'] == 1)
            self.assertEqual(views.green['actions'], [])
            if not unit['move_path']:
                break
        self.assertEqual(unit['cur_hex'], 103)
        self.assertEqual(unit['cur_pos'], 0)
        self.assertEqual(unit['move_path'], [])

    def test_old_contract_action_fields_and_movement_are_unchanged(self):
        state_hashes = []
        action = self.action(A.MOVE, parameters={'path': [102], 'movement_mode': 'NORMAL'})
        for contract in (LEGACY_SOURCE_CONTRACT, CURRENT_SOURCE_CONTRACT, ANALYSIS_SOURCE_CONTRACT, SOURCE_ACTION_CONTRACT):
            env = self.environment(contract)
            env.setup_views(42)
            views, _ = env.step_views_ms([action], delta_time_ms=1000)
            state_hashes.append(env.last_step_record['expected_state_hash'])
            if contract != SOURCE_ACTION_CONTRACT:
                self.assertEqual(views.green['actions'], [action.to_dict()])
                self.assertTrue(all(unit['move_state'] in (0, 1) for unit in views.green['operators']))
            else:
                self.assertIn('message', views.green['actions'][0])
        self.assertEqual(len(set(state_hashes)), 1)

    def test_old_source_archives_strict_replay_without_projection_migration(self):
        from wargame_native_engine.gateway.session_manager import _recorder_metadata
        from wargame_native_engine.io.replay import replay_source_archive
        from wargame_native_engine.replay.source_frame_recorder import SourceFrameRecorder
        from wargame_native_engine.io.serialization import canonical_hash
        for contract in (LEGACY_SOURCE_CONTRACT, CURRENT_SOURCE_CONTRACT, ANALYSIS_SOURCE_CONTRACT):
            with self.subTest(contract=contract), tempfile.TemporaryDirectory() as temporary:
                env = self.environment(contract)
                # Replay the original exact1.1 policy, not today's upgraded identity contract.
                from wargame_native_engine.rules.perspective import load_perspective_policy
                from wargame_native_engine.observations.perspective import PerspectiveProjector
                env.projector = PerspectiveProjector(load_perspective_policy(version='1.1.0'))
                views = env.setup_views(42)
                self.assertTrue(all('contact_identity' not in u['extra']['native']
                                    for u in views.green['operators'] + views.green['passengers']))
                recorder = SourceFrameRecorder(temporary)
                recorder.start(_recorder_metadata(env, views.green, 42))
                recorder.record_initial(views.green, initial_state_hash=env.initial_state_hash)
                for actions in ([self.action(A.MOVE, parameters={'path': [102, 103]})],
                                [], [self.action(A.STOP_MOVE)],
                                [self.action(A.SHOOT, parameters={'weapon_id': 999, 'target_hex': 106})]):
                    views, _ = env.step_views_ms(actions, delta_time_ms=1000)
                    recorder.record_step(views.green, env.last_step_record)
                recorder.finalize(env.session.result().to_dict(),
                    final_state_hash=canonical_hash(env.session.snapshot()['world']))
                self.assertTrue(replay_source_archive(self.scenario, recorder.package_zip(), strict=True).matches)

    def test_analysis_model_switch_does_not_change_live_source_facts_or_actions(self):
        from wargame_native_engine.data import ScenarioBundle
        from wargame_native_engine.operations import ScenarioMatchFactory
        with tempfile.TemporaryDirectory() as temporary:
            frames = []
            for model in ('source', 'native'):
                match = ScenarioMatchFactory(ScenarioBundle.from_scenario(self.scenario),
                    Path(temporary) / model, 'red', 'blue', record=False,
                    decision_interval_ms=1000, frame_interval_ms=1000,
                    analysis_model=model)(42, 1)
                self.addCleanup(match.close)
                manager = match.gateway.manager
                frame0 = manager.controller_view(match.session_id, 'red').source_frame
                manager.advance_ms(match.session_id,
                    [self.action(A.MOVE, parameters={'path': [102, 103]})], delta_time_ms=1000)
                frame1 = manager.controller_view(match.session_id, 'red').source_frame
                frames.append((frame0, frame1))
            self.assertEqual(frames[0], frames[1])
            self.assertEqual(frames[0][1]['actions'][0]['message']['type'], 1)
            self.assertEqual(frames[0][1]['extra']['native']['source_frame_compatibility_version'], SOURCE_ACTION_CONTRACT)

    def test_move_enum_and_unknown_state_export_roundtrip_are_explicit(self):
        env = self.environment()
        env.setup_views(42)
        world = env.session.backend.engine._require_world()
        unit = world.operators[1]
        for domain, mode, expected in [('VEHICLE', 'NORMAL', 0), ('VEHICLE', 'MARCHING', 1),
                ('PERSONNEL', 'NORMAL', 2), ('AIR', 'NORMAL', 3),
                ('PERSONNEL', 'CHARGE_1', None), ('PERSONNEL', 'CHARGE_2', None),
                ('UNKNOWN', 'NORMAL', None), ('AIR', 'MARCHING', None)]:
            unit.operator_type, unit.movement_mode = domain, mode
            self.assertEqual(source_move_state(unit), expected)
        unit.operator_type, unit.movement_mode = 'PERSONNEL', 'CHARGE_1'
        frame = env._export_green(world, actions=[], judge_info=[])
        raw = next(item for item in frame['operators'] if item['obj_id'] == 1)
        self.assertIsNone(raw['move_state'])
        parsed = SourceFrameImporter().from_dict(frame, source_hash='test')
        self.assertTrue(any('UNKNOWN' in warning for warning in parsed.warnings))
        self.assertIsNone(parsed.operators[0].raw['move_state'])
        with self.assertRaisesRegex(ValueError, 'unknown Source move_state'):
            SourceFrameImporter().to_world(parsed)
        for contract in (LEGACY_SOURCE_CONTRACT, CURRENT_SOURCE_CONTRACT, ANALYSIS_SOURCE_CONTRACT):
            old = deepcopy(frame)
            old['extra']['native']['source_frame_compatibility_version'] = contract
            with self.assertRaises((TypeError, ValueError)):
                SourceFrameImporter().from_dict(old, source_hash='test')
        del raw['extra']['native']['movement_projection']
        with self.assertRaisesRegex(ValueError, 'UNMAPPED'):
            SourceFrameImporter().from_dict(frame, source_hash='test')


if __name__ == '__main__':
    unittest.main()
