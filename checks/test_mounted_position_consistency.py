"""Carried location is authoritative; old records stay byte-for-byte historical."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.importer import SourceFrameImporter
from wargame_native_engine.compat.source_frame.template import source_frame_template_for_scenario
from wargame_native_engine.core.action import ActionType as A, EngineAction
from wargame_native_engine.core.destruction import destroy_source_operator
from wargame_native_engine.gateway.session_manager import _recorder_metadata
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.scenario import load_scenario, scenario_from_dict
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.replay.compact import CompactReplayReader, CompactReplayWriter, json_equal
from wargame_native_engine.replay.source_frame_recorder import SourceFrameRecorder
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment
from wargame_native_engine.viewer.replay_view import _frame_record
from wargame_native_engine.viewer.transport_positions import attach_mounted_positions

ROOT = Path(__file__).resolve().parents[1]
POLICY = 'native-mounted-position-sync/1.1'


class MountedPositionTests(unittest.TestCase):
    def environment(self, policy=POLICY, scenario=None):
        scenario = scenario or load_scenario(ROOT / 'examples/maps/scenarios/boarding.json')
        env = SourceCompatibleEnvironment(GameSession(NativeEngineBackend(scenario, mounted_position_policy=policy)),
            template=source_frame_template_for_scenario(scenario))
        self.addCleanup(env.session.close)
        return env

    def assert_carried(self, env, bundle):
        # The committed public snapshot has the actual carrier anchor/progress;
        # it deliberately has no independent occupancy or executable MOVE task.
        world = env.session.snapshot()['world']
        units = {u['operator_id']: u for u in world['operators'].values()}
        p, c = units[101], units[102]
        self.assertEqual((p['x'], p['y'], p.get('cur_pos_millionths',0)),
                         (c['x'], c['y'], c['cur_pos_millionths']))
        self.assertIsNone(p.get('current_hex'))
        self.assertFalse(p['deployed'])
        self.assertEqual(p.get('move_path',[]), [])
        self.assertIsNone(p.get('segment_to_hex'))
        self.assertIsNone(p.get('movement_action_id'))
        for frame in (bundle.green, bundle.red):
            raw = {u['obj_id']: u for u in frame['operators'] + frame['passengers']}
            self.assertEqual(raw[101]['cur_hex'], raw[102]['cur_hex'])
            self.assertEqual(raw[101]['cur_pos'], raw[102]['cur_pos'])
            self.assertEqual(raw[101]['move_path'], [])
        blue_ids = {u['obj_id'] for u in bundle.blue['operators'] + bundle.blue['passengers']}
        self.assertNotIn(101, blue_ids)
        record = _frame_record(0, '0.json', SourceFrameImporter().from_dict(bundle.green, source_hash='test'))
        render = {u['operator_id']:u for u in record['viewer_units']}
        self.assertEqual(render[101]['current_hex'], render[102]['current_hex'])
        self.assertEqual(render[101]['movement_progress'], render[102]['movement_progress'])
        self.assertFalse(render[101]['map_visible'])
        self.assertEqual(render[102]['carried_units'][0]['operator_id'], 101)

    def chain(self, env):
        initial = env.setup_views(42)
        frames, steps = [initial.green], []
        actions = [([next(a.action for a in env.session.valid_actions('red') if a.action.action_type == A.BOARD)],75000),
                   ([EngineAction('red',102,A.MOVE,parameters={'path':[102]})],1000),
                   ([],5000), ([EngineAction('red',102,A.STOP_MOVE)],1000), ([],90000)]
        for order, ms in actions:
            bundle, _ = env.step_views_ms(order, delta_time_ms=ms)
            self.assertTrue(all(item['accepted'] for item in env.last_action_results), env.last_action_results)
            self.assert_carried(env, bundle)
            frames.append(bundle.green); steps.append(env.last_step_record)
        self.assertEqual(next(u for u in frames[-1]['operators'] if u['obj_id']==102)['cur_hex'],102)
        self.assertGreater(next(u for u in frames[2]['passengers'] if u['obj_id']==101)['cur_pos'],0)
        bundle,_ = env.step_views_ms([next(a.action for a in env.session.valid_actions('red') if a.action.action_type == A.UNLOAD)],delta_time_ms=75000)
        frames.append(bundle.green); steps.append(env.last_step_record)
        units = {u['operator_id']:u for u in env.session.snapshot()['world']['operators'].values()}
        self.assertIsNone(units[101].get('mounted_on'))
        self.assertTrue(units[101]['deployed'])
        self.assertEqual(units[101]['current_hex'], units[102]['current_hex'])
        self.assertEqual((units[101]['x'], units[101]['y']), (units[102]['x'], units[102]['y']))
        self.assertEqual(units[101]['cur_pos_millionths'], 0)
        return frames, steps

    def test_public_board_move_stop_unload_authoritative_and_raw(self):
        env = self.environment()
        self.chain(env)

    def test_new_defaults_enable_policy_and_roundtrip_snapshot(self):
        s=load_scenario(ROOT / 'examples/maps/scenarios/boarding.json')
        e=NativeEngineBackend(s)
        self.addCleanup(e.close)
        self.assertEqual(e.mounted_position_policy, POLICY)
        e.reset(42)
        snap=e.snapshot(); self.assertEqual(snap['mounted_position_policy'], POLICY)
        self.assertEqual(e.restore(snap).to_dict(),snap['world'])
        other=NativeEngineBackend(s,mounted_position_policy=None)
        self.addCleanup(other.close)
        with self.assertRaisesRegex(ValueError,'mounted position policy mismatch'):
            other.restore(snap)

    def test_legacy_none_and_1_0_keep_historical_state_and_export(self):
        for policy in (None, 'native-mounted-position-sync/1.0'):
            with self.subTest(policy=policy):
                env=self.environment(policy);env.setup_views(42)
                board=next(a.action for a in env.session.valid_actions('red') if a.action.action_type==A.BOARD)
                env.step_views_ms([board],delta_time_ms=75000)
                bundle,_=env.step_views_ms([EngineAction('red',102,A.MOVE,parameters={'path':[102]})],delta_time_ms=1000)
                raw={u['obj_id']:u for u in bundle.green['operators']+bundle.green['passengers']}
                self.assertEqual(raw[101]['cur_pos'],0)
                self.assertGreater(raw[102]['cur_pos'],0)
                self.assertNotIn('mounted_position',raw[101]['extra']['native'])
                self.assertNotIn('mounted_position_policy',bundle.green['extra']['native'])
                bundle,_=env.step_views_ms([],delta_time_ms=20000)
                units=env.session.snapshot()['world']['operators']
                self.assertEqual(units['102']['x'],2)
                self.assertEqual(units['101']['x'],1 if policy is None else 2)

    def test_compact_is_exact_and_new_legacy_debug_reexecutes(self):
        env=self.environment(); frames,steps=self.chain(env)
        with tempfile.TemporaryDirectory() as temporary:
            writer=CompactReplayWriter(Path(temporary)/'compact',checkpoint_interval=2)
            writer.start()
            for frame in frames: writer.append(frame)
            writer.finalize()
            reader=CompactReplayReader(writer.package_zip())
            self.assertTrue(all(json_equal(frame,reader.raw_frame(i)) for i,frame in enumerate(frames)))
            # Record a fresh execution through the supported recorder protocol.
            fresh=self.environment(); initial=fresh.setup_views(42)
            recorder=SourceFrameRecorder(Path(temporary)/'debug')
            recorder.start(_recorder_metadata(fresh,initial.green,42))
            recorder.record_initial(initial.green,initial_state_hash=fresh.initial_state_hash)
            for step in steps:
                action=[EngineAction.from_dict(value) for value in step['ordered_actions']]
                bundle,_=fresh.step_views_ms(action,delta_time_ms=step['delta_time_ms'])
                recorder.record_step(bundle.green,fresh.last_step_record)
            recorder.finalize(fresh.session.result().to_dict(),final_state_hash=canonical_hash(fresh.session.snapshot()['world']))
            self.assertTrue(replay_source_archive(fresh.session.scenario,recorder.package_zip(),strict=True).matches)

    def test_historical_adapter_is_read_only_and_requires_safe_relation(self):
        env=self.environment();frames,_=self.chain(env)
        raw=deepcopy(frames[3]); p=raw['passengers'][0]; p['cur_hex']=101;p['cur_pos']=0.0
        original=deepcopy(raw)
        record=_frame_record(3,'3.json',SourceFrameImporter().from_dict(raw,source_hash='recorded'))
        self.assertEqual(raw,original)
        self.assertEqual(record['passengers'],original['passengers'])
        units={u['operator_id']:u for u in record['viewer_units']}
        self.assertFalse(units[101]['map_visible'])
        self.assertGreater(units[101]['movement_progress'],0)
        for change in ('missing','cross-side','unrelated','enemy-contact'):
            display=deepcopy(record['viewer_units']); source=deepcopy(raw['operators']+raw['passengers'])
            for item in display:
                item.pop('position_source',None);item['map_visible']=True
            if change=='missing': display=[u for u in display if u['operator_id']!=102]
            if change=='cross-side': next(u for u in display if u['operator_id']==102)['side']='blue'
            if change=='unrelated': next(u for u in source if u['obj_id']==102)['passenger_ids']=[]
            attach_mounted_positions(display,source,perspective='BLUE' if change=='enemy-contact' else 'GREEN')
            self.assertTrue(next(u for u in display if u['operator_id']==101)['map_visible'],change)

    def test_legacy_initial_passenger_unload_does_not_reuse_stale_template_relation(self):
        import json
        from wargame_native_engine.compat.source_frame.contract import LEGACY_SOURCE_CONTRACT
        document=json.loads((ROOT/'examples/maps/scenarios/boarding.json').read_text())
        passenger,carrier=document['operators'][:2]
        passenger.update(mounted_on=102,deployed=False,current_hex=None,state='ON_BOARD',
                         boarding_profile_id='GROUND_PERSONNEL_SINGLE_V1')
        carrier['passenger_id']=101
        scenario=scenario_from_dict(document)
        env=self.environment(None,scenario)
        env.source_contract=LEGACY_SOURCE_CONTRACT
        initial=env.setup_views(42)
        initial_render=_frame_record(0,'0.json',SourceFrameImporter().from_dict(initial.green,source_hash='old'))
        self.assertFalse(next(u for u in initial_render['viewer_units'] if u['operator_id']==101)['map_visible'])
        unload=next(a.action for a in env.session.valid_actions('red') if a.action.action_type==A.UNLOAD)
        bundle,_=env.step_views_ms([unload],delta_time_ms=75000)
        source=deepcopy(bundle.green)
        raw=next(u for u in source['passengers'] if u['obj_id']==101)
        self.assertTrue(raw['on_board'])  # Historical template is intentionally preserved.
        self.assertIsNone(raw['extra']['native']['mounted_on'])
        rendered=_frame_record(1,'1.json',SourceFrameImporter().from_dict(source,source_hash='old'))
        p=next(u for u in rendered['viewer_units'] if u['operator_id']==101)
        self.assertTrue(p.get('map_visible',True))
        self.assertNotIn('position_source',p)
        self.assertEqual(bundle.green,source)
        # Either explicit Native denial is sufficient; absence is not denial.
        for remove_passenger,remove_carrier,hidden in ((True,False,False),(False,True,False),(True,True,True)):
            variant=deepcopy(source)
            passenger=next(u for u in variant['passengers'] if u['obj_id']==101)
            carrier=next(u for u in variant['operators'] if u['obj_id']==102)
            if remove_passenger: passenger['extra']['native'].pop('mounted_on')
            if remove_carrier: carrier['extra']['native'].pop('passenger_id')
            display=_frame_record(1,'1.json',SourceFrameImporter().from_dict(variant,source_hash='old'))
            p=next(u for u in display['viewer_units'] if u['operator_id']==101)
            self.assertEqual(p.get('map_visible',True),not hidden)

    def test_tools_and_live_render_follow_carrier_then_unload(self):
        from checks.test_all_game_tool_operations import AllGameToolOperationsTests
        from wargame_native_engine.viewer.live_control import controller_view_record
        helper = AllGameToolOperationsTests()
        self.addCleanup(helper.doCleanups)
        scenario, session, adapter, directory = helper.make_session('maps/scenarios/boarding.json')
        helper.execute(session, adapter, {'operation_type':'BOARD','actor_ids':[101],'target_id':102})
        for _ in range(75): session.advance_tick()
        helper.execute(session, adapter, {'operation_type':'MOVE','actor_ids':[102],'destination_hex':102})
        match=session._match
        def render():
            view=match.gateway.manager.controller_view(match.session_id,'red')
            return {u['operator_id']:u for u in controller_view_record(view)['frame']['viewer_units']}
        for _ in range(6):
            units=render()
            self.assertFalse(units[101]['map_visible'])
            self.assertEqual(units[101]['current_hex'],units[102]['current_hex'])
            self.assertEqual(units[101]['movement_progress'],units[102]['movement_progress'])
            session.advance_tick()
        helper.execute(session, adapter, {'operation_type':'STOP_MOVE','actor_ids':[102]})
        for _ in range(95): session.advance_tick()
        helper.execute(session, adapter, {'operation_type':'UNLOAD','actor_ids':[102],'target_id':101})
        for _ in range(75): session.advance_tick()
        units=render()
        self.assertTrue(units[101]['map_visible'])
        self.assertEqual(units[101]['current_hex'],102)
        self.assertFalse(units[101]['on_board'])
        helper.verify_replay(scenario,session,directory)

    def test_carrier_destruction_cascade_stays_destroyed_off_map(self):
        env=self.environment();env.setup_views(42)
        env.step_views_ms([next(a.action for a in env.session.valid_actions('red') if a.action.action_type == A.BOARD)],delta_time_ms=75000)
        env.step_views_ms([EngineAction('red',102,A.MOVE,parameters={'path':[102]})],delta_time_ms=30000)
        world=env.session.backend.engine._require_world()
        destroy_source_operator(world,world.operators[102])
        bundle,_=env.step_views_ms([],delta_time_ms=1000)
        p=env.session.snapshot()['world']['operators']['101']
        self.assertFalse(p['alive']);self.assertFalse(p['deployed']);self.assertIsNone(p.get('current_hex'))
        record=_frame_record(0,'0.json',SourceFrameImporter().from_dict(bundle.green,source_hash='x'))
        self.assertFalse(next(u for u in record['viewer_units'] if u['operator_id']==101)['map_visible'])


if __name__ == '__main__': unittest.main()
