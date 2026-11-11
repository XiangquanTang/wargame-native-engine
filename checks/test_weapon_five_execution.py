"""KB ID 5 ground-portable missile binding, resources and strict replay."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from checks.test_direct_fire_range import fixture_record, shoot
from wargame_native_engine.adjudication import AdjudicationTableSet
from wargame_native_engine.agents.realtime import ManualRealtimeClock, RealtimeAgentSession
from wargame_native_engine.agents.seat import SeatOperation
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.data.source_scenario_profiles import build_source_scenario_profile
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.operations import ScenarioMatchFactory

ROOT = Path(__file__).resolve().parents[1]

def profile():
    return build_source_scenario_profile({'scenario_id':'weapon5-test',
        'time':{'cur_step':0,'tick':1,'max_time':1800}, 'operators':[
        {'obj_id':1,'type':1,'sub_type':2,'name':'步兵小队',
         'carry_weapon_ids':[5],'remain_bullet_nums':{'100':4}}]}, 'test')['weapon_profiles']['5']

def record():
    r = fixture_record()
    r['metadata']['source_compatible']['weapons'] = {'5':profile()}
    r['operators'][0].update(operator_type='PERSONNEL', subtype='source-1-2', weapon_ids=[5])
    r['operators'][0]['raw_attributes']['ammunition'] = {'100':4}
    r['operators'][1].update(operator_type='VEHICLE', subtype='fixture-vehicle', blood=3,max_blood=3)
    r['operators'][1]['raw_attributes']['armor_class'] = 'MEDIUM'
    return r

class WeaponFiveExecutionTests(unittest.TestCase):
    def engine(self, r=None):
        e=NativeEngine(scenario_from_dict(record() if r is None else r))
        e.reset(seed=42);self.addCleanup(e.close);return e

    def test_profile_and_all_exact_table_cells(self):
        p=profile()
        self.assertEqual((p['weapon_class'],p['maximum_range'],p['target_domains'],p['ammunition_key']),
                         ('portable_missile_ground',10,['VEHICLE'],'100'))
        self.assertFalse(p['can_fire_while_moving'])
        tables=AdjudicationTableSet(ROOT/'config/adjudication/source_tables_v1/native_adjudication_tables_final.json')
        for distance in range(11):
            a=tables.attack_level('direct_fire_vehicle_attack_levels','portable_missile_ground',3,distance)
            b=tables.attack_level('direct_fire_vehicle_attack_levels','portable_missile',3,distance)
            self.assertEqual(a,b);self.assertEqual(a.value,7 if distance<=2 else 8)
        with self.assertRaises(ValueError):
            tables.attack_level('direct_fire_vehicle_attack_levels','portable_missile_ground',3,11)
        with self.assertRaises(ValueError):
            tables.attack_level('direct_fire_personnel_attack_levels','portable_missile_ground',3,1)

    def test_actual_fire_consumes_real_pool_cools_and_resolves_vehicle(self):
        e=self.engine();result=e.step([shoot(weapon_id=5)],delta_time_ms=1)
        self.assertTrue(result.decisions[0].accepted)
        world=e.snapshot()['world']['operators']
        self.assertEqual(world['1']['raw_attributes']['ammunition'],{'100':3})
        self.assertEqual(world['1']['weapon_cooldown_remaining_ms'],74999)
        self.assertLess(world['2']['blood'],3)
        levels=[x.to_dict()['data'] for x in result.events if x.event_type.value=='attack_level_selected']
        self.assertEqual(levels[0]['value'],8)
        self.assertEqual(levels[0]['weapon_type'],'portable_missile_ground')
        self.assertIn('weapon=portable_missile;',levels[0]['table_cell'])

    def test_no_ammo_cooldown_unsupported_domain_and_mounted_rejected(self):
        for change in ('ammo','cooldown','personnel','air','mounted'):
            with self.subTest(change=change):
                r=record()
                if change=='ammo':r['operators'][0]['raw_attributes']['ammunition']['100']=0
                elif change=='cooldown':r['operators'][0]['weapon_cooldown_remaining_ms']=5000
                elif change=='personnel':r['operators'][1]['operator_type']='PERSONNEL'
                elif change=='air':r['operators'][1]['operator_type']='AIR'
                else:
                    carrier=deepcopy(r['operators'][1]);carrier.update(operator_id=3,side='red',
                        subtype='source-2-1',current_hex=101,x=1,y=1,passenger_id=1)
                    r['operators'].append(carrier)
                    r['deployment']['positions']['red']['3']=[1,1]
                    r['operators'][0].update(current_hex=None,deployed=False,mounted_on=3,state='ON_BOARD')
                e=self.engine(r);before=deepcopy(e.snapshot()['world']['operators']['1']['raw_attributes']['ammunition'])
                result=e.step([shoot(weapon_id=5)],delta_time_ms=1)
                self.assertFalse(result.decisions[0].accepted)
                self.assertEqual(e.snapshot()['world']['operators']['1']['raw_attributes']['ammunition'],before)

    def test_same_rng_and_effect_as_canonical_portable_vehicle_row(self):
        r=record();a=self.engine(r)
        r['metadata']['source_compatible']['weapons']['5']['weapon_class']='portable_missile'
        b=self.engine(r)
        a.step([shoot(weapon_id=5)],delta_time_ms=1);b.step([shoot(weapon_id=5)],delta_time_ms=1)
        self.assertEqual(a.snapshot()['rng'],b.snapshot()['rng'])
        self.assertEqual(a.snapshot()['world']['operators'],b.snapshot()['world']['operators'])

    def test_out_of_range_consumes_resources_and_moving_requires_lock(self):
        from dataclasses import replace
        from wargame_native_engine.adjudication import DirectFireAdjudicator, DiceRoller
        from wargame_native_engine.core.rng import NativeRNG
        e=self.engine();world=e._world
        request=e._direct_fire_request(shoot(weapon_id=5),world.operators[1],world.operators[2],world=world)
        tables=AdjudicationTableSet(ROOT/'config/adjudication/source_tables_v1/native_adjudication_tables_final.json')
        rng=NativeRNG(42);before=rng.snapshot()
        result=DirectFireAdjudicator(tables,DiceRoller(rng)).stage(replace(request,distance=11))
        self.assertTrue(result.legality.legal)
        self.assertTrue(result.outcome.no_effect)
        self.assertEqual((result.ammunition_after,result.cooldown_after),(3,75))
        self.assertEqual(rng.snapshot(),before)
        from wargame_native_engine.adjudication.fire_readiness import weapon_posture_failure
        for locked in (False,True):
            self.assertEqual(weapon_posture_failure(moving=True,locked=locked,unfolded=True,
                requires_unfolded=False,can_fire_while_moving=profile()['can_fire_while_moving'],
                ordinary_direct=True),None if locked else 'ATTACKER_MUST_STOP')

    def test_public_operation_and_strict_replay(self):
        scenario=scenario_from_dict(record())
        with TemporaryDirectory() as temporary:
            d=Path(temporary)
            s=RealtimeAgentSession(ScenarioMatchFactory(bundle=ScenarioBundle.from_scenario(scenario),
                storage_root=d/'storage',red_controller_id='red',blue_controller_id='blue',
                decision_interval_ms=1000,frame_interval_ms=1000,replay_mode='legacy-debug'),run_namespace='weapon5',
                tick_interval_ms=1000,clock=ManualRealtimeClock())
            self.addCleanup(s.close);binding=s.reset(seed=42).red_binding
            version=s.latest_view(binding).observation_version
            op=SeatOperation('SHOOT',actor_ids=(1,),target_id=2,weapon_choices=((1,5),))
            preview=s.preview_operation(binding,op,observation_version=version,request_id='w5')
            self.assertEqual(preview.executable_count,1)
            self.assertTrue(s.submit_operation(binding,operation=op,observation_version=version).accepted)
            s.advance_tick();s.publish(d/'published')
            self.assertTrue(replay_source_archive(scenario,d/'published/replay.zip',strict=True).matches)

if __name__=='__main__':unittest.main()
