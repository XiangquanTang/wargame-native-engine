"""Opt-in capture pressure: bounded monotone shaping and finite damage/death ledgers."""
from dataclasses import replace
from types import SimpleNamespace as N
import unittest
from wargame_native_engine.training.minimal_runtime import (
    ApproachPotential, LossLedger, OWN, OBJ, RewardConfig, RewardState,
    REWARD_COMPONENT_NAMES, reward_components)

CFG = RewardConfig.for_preset('capture-pressure-v3')

def state(**kwargs):
    return replace(RewardState(0., 0., 0., 0., 0., 0), **kwargs)

def parts(a, b, **kwargs):
    return reward_components(a, b, config=CFG, base_reward=kwargs.get('base_reward', 0.), terminated=kwargs.get('terminated', False))

class CapturePressureTests(unittest.TestCase):
    def setUp(self):
        cells=[N(hex_id=h, neighbors=tuple(n for n in (h-1,h+1) if 101<=n<=109)) for h in range(101,110)]
        self.scenario=N(map=N(hexes=cells), objectives=[N(objective_id=7, x=9, y=1, value=2),N(objective_id=8,x=9,y=1,value=1)])
        self.potential=ApproachPotential(self.scenario,tolerate_missing_objectives=True)
        self.unit=[0.]*len(OWN)
        for key in ('alive','spatially_present','current_hex_present'): self.unit[OWN[key]]=1.
        self.unit[OWN['domain']]=2.; self.unit[OWN['current_hex']]=108
        self.rows=[]
        for key,value in ((7,2),(8,1)):
            row=[0.]*len(OBJ); row[OBJ['objective_reference']]=key; row[OBJ['value']]=value
            self.rows.append(row)
        self.binding=N(observation=N(own_operator_features=[self.unit],own_operator_mask=[True],objective_features=self.rows,objective_mask=[True,True]))
    def rate(self): return self.potential.pressure_rate(self.binding,CFG)
    def test_explicit_mapping_distance_floor_and_ownership(self):
        self.assertEqual(self.rate(),2.25) # Major 1.5 + minor .75 even standing on target.
        self.unit[OWN['current_hex']]=101
        far=self.rate(); self.assertGreater(far,2.25); self.assertLess(far,4.5)
        self.unit[OWN['current_hex']]=107
        self.assertLess(self.rate(),far)
        for row in self.rows: row[OBJ['control_relation']]=1.
        self.assertEqual(self.rate(),0.)
        for row in self.rows: row[OBJ['control_relation']]=2.
        enemy=self.rate()
        for row in self.rows: row[OBJ['control_relation']]=0.
        self.assertEqual(enemy,2*self.rate())
        self.assertGreater(self.rate(),0.) # Losing ownership restores pressure.
    def test_no_air_passenger_dead_absent_or_unreachable_credit(self):
        for field,value in [('on_board',1),('domain',3),('alive',0),('destroyed',1),('spatially_present',0),('current_hex_present',0),('current_hex',999)]:
            original=self.unit[OWN[field]]; self.unit[OWN[field]]=value
            self.assertEqual(self.rate(),4.5,(field,value)); self.unit[OWN[field]]=original
        self.binding.observation.own_operator_mask=[False]
        self.assertEqual(self.rate(),4.5)
    def test_missing_objectives_fail_closed_and_static_values_not_names(self):
        self.binding.observation.objective_mask=[False,False]
        self.assertEqual(self.rate(),4.5)
        self.scenario.objectives[0].x=99
        self.potential=ApproachPotential(self.scenario,tolerate_missing_objectives=True)
        self.binding.observation.objective_mask=[True,True]
        self.assertEqual(self.rate(),3.75)
        with self.assertRaises(ValueError): ApproachPotential(self.scenario)
    def test_time_partition_ownership_and_terminal(self):
        a=state(pressure_rate=4.5)
        b=state(pressure_rate=4.5,sim_time_ms=60000)
        self.assertEqual(parts(a,b)['objective_pressure'],-4.5)
        split=sum(parts(state(pressure_rate=4.5,sim_time_ms=t),state(pressure_rate=4.5,sim_time_ms=t+1000))['objective_pressure'] for t in range(0,60000,1000))
        self.assertAlmostEqual(split,-4.5)
        owned=state(pressure_rate=0.,owned=1.,sim_time_ms=60000)
        self.assertEqual(parts(a,owned)['objective_pressure'],-4.5)
        self.assertEqual(parts(owned,replace(owned,sim_time_ms=120000))['objective_pressure'],0.)
        self.assertEqual(parts(a,b,terminated=True)['objective_pressure'],-4.5)
        self.assertEqual(parts(a,a)['objective_pressure'],0.)
    def test_capture_loss_recapture_and_approach_cycles_no_farm(self):
        a=state(distance=.3,pressure_rate=4.5)
        b=state(distance=1.,owned=1.,pressure_rate=0.)
        c=replace(a,sim_time_ms=1000)
        self.assertEqual(parts(a,b)['capture'],5.)
        self.assertEqual(parts(b,b)['capture'],0.)
        self.assertAlmostEqual(parts(a,b)['capture']+parts(b,c)['capture'],0.)
        self.assertAlmostEqual(parts(a,b)['distance']+parts(b,c)['distance'],0.)
        self.assertEqual(parts(a,b,terminated=True,base_reward=1)['win'],10.)
    def test_config_and_legacy_metadata_exact(self):
        self.assertEqual((CFG.damage_weight,CFG.own_damage_weight,CFG.kill_weight,CFG.time_weight),(3.,1.,1.,0.))
        self.assertEqual(RewardConfig(**CFG.metadata()),CFG)
        self.assertEqual(set(RewardConfig.for_preset('progressive-v2').metadata()), {'gamma','win_weight','capture_weight','distance_weight','eight_factor_weight','preset','damage_weight','own_damage_weight','time_weight','approach_scale'})
        for change in ({'pressure_floor':0},{'pressure_floor':1.1},{'pressure_distance_scale':0},{'pressure_enemy_multiplier':.5}):
            with self.assertRaises(ValueError): replace(CFG,**change)

class KillLedgerTests(unittest.TestCase):
    def test_partial_damage_healing_death_missing_and_reinforcement(self):
        roster=[N(operator_id=i,side=s,health=h,alive=h>0) for i,s,h in [(1,'red',100),(2,'blue',100),(3,'blue',100),(4,'blue',0)]]
        ledger=LossLedger(roster)
        def row(health,alive=True): return {'side':'blue','health':health,'alive':alive}
        self.assertEqual(ledger.update({},'red'),(0.,0.)); self.assertEqual(ledger.enemy_kill_fraction('red'),0.)
        self.assertEqual(ledger.update({2:row(50)},'red'),(.25,0.))
        self.assertEqual(ledger.update({2:row(100)},'red'),(.25,0.))
        self.assertEqual(ledger.update({2:row(50)},'red'),(.25,0.))
        self.assertEqual(ledger.update({2:row(0,False)},'red'),(.5,0.))
        self.assertEqual(ledger.enemy_kill_fraction('red'),.5)
        for snap in ({},{2:row(100)},{2:row(0,False)},{3:row(100)},{4:row(0,False)}):
            self.assertEqual(ledger.update(snap,'red'),(.5,0.)); self.assertEqual(ledger.enemy_kill_fraction('red'),.5)
        p=parts(state(),state(enemy_loss=.5,enemy_kills=.5,own_loss=.1))
        self.assertEqual(p['enemy_damage'],1.5); self.assertEqual(p['enemy_kill'],.5); self.assertEqual(p['own_damage'],-.1)
        self.assertAlmostEqual(p['total'],sum(p[k] for k in REWARD_COMPONENT_NAMES))
        ledger.update({3:row(0,False)},'red')
        self.assertEqual(ledger.enemy_kill_fraction('red'),1.)
        self.assertEqual(parts(state(enemy_kills=1.),state(enemy_kills=1.))['enemy_kill'],0.)
        with self.assertRaises(ValueError): parts(state(enemy_kills=1.),state())

class CapturePressureEnvironmentTests(unittest.TestCase):
    def test_native_time_pressure_and_reset(self):
        from checks.test_reward_upgrade_v2 import ProgressiveIntegrationTests
        from wargame_native_engine.training.minimal_runtime import MinimalTrainingEnv
        fixture=ProgressiveIntegrationTests(); fixture.setUp()
        env=MinimalTrainingEnv(fixture.bundle,opponent='no-op',horizon=2,reward_config=CFG)
        try:
            env.reset(42)
            before=env.reward_state(env.current_decision_binding)
            result=env.step(None)
            after=env.reward_state(env.current_decision_binding)
            p=parts(before,after,base_reward=result.reward,terminated=result.terminated)
            self.assertEqual(after.sim_time_ms-before.sim_time_ms,1000)
            self.assertAlmostEqual(p['objective_pressure'],-before.pressure_rate/60.)
            self.assertLess(p['objective_pressure'],0.)
            env.reset(43)
            fresh=env.reward_state(env.current_decision_binding)
            self.assertEqual((fresh.sim_time_ms,fresh.enemy_loss,fresh.enemy_kills),(0,0.,0.))
        finally: env.close()

if __name__ == '__main__': unittest.main()
