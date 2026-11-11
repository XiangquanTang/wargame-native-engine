"""Explicit fresh Source launch profiles retain facts and reject ambiguity."""
from copy import deepcopy
from pathlib import Path
import unittest

from wargame_native_engine.data.source_scenario import (
    SourceScenarioConversionError, _apply_initial_source_transport, _validate_fresh_source_state)
from wargame_native_engine.data.source_scenario_profiles import build_source_scenario_profile
from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.adjudication.table_loader import AdjudicationTableSet

ROOT=Path(__file__).resolve().parents[1]

class SourceScenarioLaunchProfileTests(unittest.TestCase):
    def source(self):
        return {'scenario_id':'test', 'time':{'cur_step':0,'tick':1,'max_time':1800},
                'operators':[{'obj_id':1,'color':0,'type':2,'sub_type':1,'name':'轻型战车',
                'carry_weapon_ids':[56,69],'remain_bullet_nums':{'0':40,'100':4,'101':0,'102':0},
                'cur_hex':101,'passenger_ids':[2]},
                {'obj_id':2,'color':0,'type':1,'sub_type':2,'name':'步兵小队',
                'carry_weapon_ids':[29,35,71],'remain_bullet_nums':{'0':10,'100':4,'101':0,'102':0},
                'cur_hex':101,'on_board':1,'car':1}]}

    def native(self):
        return [OperatorState(operator_id=i,side='red',operator_type=domain,
            subtype=subtype,x=1,y=1,altitude=0,heading=0,health=3,max_health=3,
            speed=0,max_speed=36,move_range=0,sensor_range=25,state='STOPPED',
            alive=True,weapon_ids=[],current_hex=101,deployed=True)
            for i,domain,subtype in [(1,'VEHICLE','source-2-1'),(2,'PERSONNEL','source-1-2')]]

    def test_explicit_profile_preserves_source_and_names(self):
        source=self.source(); before=deepcopy(source)
        p=build_source_scenario_profile(source,'221')
        self.assertEqual(source,before)
        self.assertEqual(p['operator_profiles']['1']['canonical_type'],'LIGHT_COMBAT_VEHICLE')
        self.assertEqual(p['operator_profiles']['1']['source_names'],['轻型战车'])
        self.assertEqual(p['armor_profiles'],{'0':'NONE','1':'LIGHT','2':'MEDIUM','3':'HEAVY','4':'COMPOSITE'})
        self.assertEqual(p['weapon_profiles']['69']['ammunition_key'],'100')
        self.assertEqual(p['weapon_profiles']['71']['maximum_range'],10)
        self.assertIn('Native convention',p['weapon_profiles']['69']['native_basis'])
        self.assertEqual(p['termination']['max_ticks'],1800)
        self.assertIn('operators[1].altitude',p['missing_source_fields'])
        self.assertEqual(p['initial_state_defaults']['altitude'],0)

    def test_unknown_identity_and_ambiguous_ammo_rejected(self):
        for change in ('unknown','ambiguous','empty','clock'):
            s=self.source()
            if change=='unknown':s['operators'][0]['name']='unknown'
            elif change=='ambiguous':s['operators'][0]['remain_bullet_nums']['101']=2
            elif change=='empty':s['operators'][0]['remain_bullet_nums']['100']=0
            else:s['time']['tick']=2
            with self.subTest(change=change),self.assertRaises(SourceScenarioConversionError):
                build_source_scenario_profile(s,'221')

    def test_initial_occupancy_and_dismounted_launcher(self):
        s=self.source(); units=self.native(); _apply_initial_source_transport(s,units)
        self.assertEqual((units[0].passenger_id,units[1].mounted_on),(2,1))
        self.assertFalse(units[1].deployed); self.assertEqual(units[1].state,'ON_BOARD')
        self.assertIsNone(units[1].current_hex)
        s['operators'][0]['passenger_ids']=[]
        s['operators'][0]['launch_ids']=[2]
        s['operators'][1].update(on_board=0,car=None,launcher=1)
        units=self.native();_apply_initial_source_transport(s,units)
        self.assertIsNone(units[1].mounted_on);self.assertTrue(units[1].deployed)

    def test_initial_transport_rejects_invalid_links(self):
        for change in ('dangling','reciprocal','side','hex','type','multiple','car_without','nested'):
            s=self.source();units=self.native()
            if change=='dangling':s['operators'][1]['car']=99
            elif change=='reciprocal':s['operators'][0]['passenger_ids']=[]
            elif change=='side':units[1].side='blue'
            elif change=='hex':units[1].current_hex=102
            elif change=='type':units[0].subtype='source-2-0'
            elif change=='multiple':s['operators'][0]['passenger_ids']=[2,3]
            elif change=='car_without':s['operators'][1]['on_board']=0
            elif change=='nested':s['operators'][1]['passenger_ids']=[1]
            with self.subTest(change=change),self.assertRaises(SourceScenarioConversionError):
                _apply_initial_source_transport(s,units)

    def test_nonfresh_runtime_state_is_not_silently_reset(self):
        for field,value in [('speed',1),('keep',1),('weapon_cool_time',2),('move_path',[3]),('lose_control',1)]:
            s=self.source();s['operators'][0][field]=value
            with self.subTest(field=field),self.assertRaises(SourceScenarioConversionError):
                _validate_fresh_source_state(s)

    def test_initial_mounted_template_uses_passenger_channel(self):
        from wargame_native_engine.compat.source_frame.template import source_frame_template_for_scenario
        from wargame_native_engine.io.scenario import load_scenario
        from dataclasses import replace
        base=load_scenario(ROOT/'examples/source_movement_scenario.json')
        units=self.native()
        for unit,raw in zip(units,self.source()['operators']):
            unit.raw_attributes['source_frame']={**raw, 'armor':0, 'blood':3, 'max_blood':3}
        _apply_initial_source_transport(self.source(),units)
        scenario=replace(base,operators=tuple(units),deployment_positions={'red':{1:(1.0,1.0),2:(1.0,1.0)},'blue':{}})
        template=source_frame_template_for_scenario(scenario)
        self.assertEqual([u.obj_id for u in template.operators],[1])
        self.assertEqual([u.obj_id for u in template.passengers],[2])
        self.assertTrue(template.passengers[0].on_board)
        self.assertEqual(template.passengers[0].car_id,1)
        self.assertEqual(template.passengers[0].current_hex,101)
        self.assertIsNone(units[1].current_hex)

    def test_observe_distance_mapping_is_unknown_not_dictionary_keys(self):
        from wargame_native_engine.compat.source_frame.importer import SourceFrameImporter
        raw = {**self.source()['operators'][0], 'armor': 0, 'blood': 3,
               'max_blood': 3, 'observe_distance': {'1': 10, '2': 25, '3': 1}}
        warnings = []
        unit = SourceFrameImporter._unit(raw, False, warnings)
        self.assertEqual(unit.observe_distance, ())
        self.assertEqual(unit.raw['observe_distance'], raw['observe_distance'])
        self.assertTrue(any('unsupported indices' in value for value in warnings))
        raw['observe_distance'] = [10, 25, 1]
        warnings = []
        unit = SourceFrameImporter._unit(raw, False, warnings)
        self.assertEqual(unit.observe_distance, (10.0, 25.0, 1.0))
        self.assertFalse(any('observe_distance' in value for value in warnings))

    def test_initial_mounted_session_unloads_to_completion(self):
        from checks.test_all_game_tool_operations import AllGameToolOperationsTests
        helper = AllGameToolOperationsTests()
        self.addCleanup(helper.doCleanups)
        def mounted(document):
            passenger, carrier = document['operators'][:2]
            passenger.update(mounted_on=102, deployed=False, current_hex=None,
                             state='ON_BOARD', boarding_profile_id='GROUND_PERSONNEL_SINGLE_V1')
            carrier.update(passenger_id=101, boarding_profile_id='GROUND_PERSONNEL_SINGLE_V1')
        scenario, session, adapter, directory = helper.make_session(
            'maps/scenarios/boarding.json', mounted)
        helper.execute(session, adapter, {
            'operation_type': 'UNLOAD', 'actor_ids': [102], 'target_id': 101})
        for _ in range(75):
            session.advance_tick()
        match = session._match
        world = match.gateway.manager._managed(match.session_id).environment.session.snapshot()['world']
        passenger, carrier = world['operators']['101'], world['operators']['102']
        self.assertIsNone(passenger['mounted_on'])
        self.assertIsNone(carrier['passenger_id'])
        self.assertTrue(passenger['deployed'])
        self.assertEqual(passenger['current_hex'], carrier['current_hex'])
        helper.verify_replay(scenario, session, directory)

    def test_vehicle_missile_alias_uses_exact_grouped_table(self):
        t=AdjudicationTableSet(ROOT/'config/adjudication/source_tables_v1/native_adjudication_tables_final.json')
        for distance in range(21):
            a=t.attack_level('direct_fire_vehicle_attack_levels','vehicle_missile',3,distance)
            b=t.attack_level('direct_fire_vehicle_attack_levels','vehicle_missile_gun_launched',3,distance)
            self.assertEqual(a,b)

if __name__=='__main__':unittest.main()
