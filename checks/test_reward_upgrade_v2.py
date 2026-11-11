"""Versioned progressive reward contracts; no long training or Ray service required."""
from dataclasses import replace
import math
from types import SimpleNamespace as N
import unittest

from wargame_native_engine.training.minimal_runtime import (
    ApproachPotential, LossLedger, OWN, OBJ, RewardConfig, RewardState,
    REWARD_COMPONENT_NAMES, reward_components,
)


def state(distance=0., owned=0., eight=0., enemy_loss=0., own_loss=0., sim_time_ms=0):
    return RewardState(distance, owned, eight, enemy_loss, own_loss, sim_time_ms)


def parts(before, after, *, terminated=False, base_reward=0., config=None):
    return reward_components(before, after, base_reward=base_reward,
                             terminated=terminated,
                             config=config or RewardConfig.for_preset('progressive-v2'))


def unit(identifier, side, health=100.):
    return N(operator_id=identifier, side=side, health=health, max_health=100., alive=health > 0,
             raw_attributes={})


def snapshot(*units):
    return {u.operator_id: {'side': u.side, 'health': u.health, 'alive': u.alive} for u in units}


class ProgressiveRewardTests(unittest.TestCase):
    def test_default_remains_legacy_and_old_metadata_roundtrips(self):
        legacy = dict(gamma=.99, win_weight=1., capture_weight=1., distance_weight=.1, eight_factor_weight=.03)
        self.assertEqual(RewardConfig().metadata(), legacy)
        self.assertEqual(RewardConfig(**legacy), RewardConfig())
        self.assertEqual(RewardConfig.for_preset('legacy-v1').metadata(), legacy)
        cfg = RewardConfig.for_preset('progressive-v2')
        self.assertEqual(RewardConfig(**cfg.metadata()), cfg)
        self.assertEqual((cfg.win_weight, cfg.capture_weight, cfg.distance_weight), (10., 5., 1.))
        self.assertEqual((cfg.damage_weight, cfg.own_damage_weight, cfg.time_weight), (1., 1., .005))
        self.assertEqual(cfg.eight_factor_weight, 0.)
        with self.assertRaises(ValueError): RewardConfig.for_preset('unknown')

    def test_capture_is_large_and_loss_recapture_not_free_reward(self):
        before = state(.9, 0.)
        captured = state(1., 1.)
        a = parts(before, captured)
        self.assertEqual(a['capture'], 5.)
        self.assertAlmostEqual(a['distance'], .1)
        b = parts(captured, before)
        self.assertAlmostEqual(a['total'] + b['total'], 0.)
        self.assertEqual(parts(captured, captured)['capture'], 0.)
        self.assertEqual(parts(before, state(1., .25))['capture'], 1.25)

    def test_stationary_and_closed_approach_cycle_zero(self):
        near, far = state(.8), state(.2)
        self.assertEqual(parts(near, near)['distance'], 0.)
        self.assertAlmostEqual(parts(far, near)['distance'] + parts(near, far)['distance'], 0.)
        self.assertAlmostEqual(sum(parts(state(a),state(b))['distance'] for a,b in [(0.,.1),(.1,.7),(.7,1.)]),1.)

    def test_terminal_does_not_cancel_v2_approach_and_win_is_ten(self):
        before, after = state(.8), state(1., 1., sim_time_ms=1000)
        live, terminal = parts(before,after), parts(before,after,terminated=True,base_reward=1.)
        self.assertAlmostEqual(terminal['distance'], .2)
        self.assertEqual(terminal['distance'],live['distance'])
        self.assertEqual(terminal['capture'],5.)
        self.assertEqual(terminal['win'],10.)
        self.assertEqual(parts(before,after,terminated=True,base_reward=-1.)['win'],-10.)

    def test_actual_time_and_fixed_horizon_cost_partition_invariant(self):
        whole = parts(state(sim_time_ms=3000),state(sim_time_ms=8000))
        split = [parts(state(sim_time_ms=t),state(sim_time_ms=t+250)) for t in range(3000,8000,250)]
        self.assertAlmostEqual(whole['time'],-.025)
        self.assertAlmostEqual(sum(p['time'] for p in split),whole['time'])
        self.assertEqual(parts(state(),state())['time'],0.)
        self.assertEqual(parts(state(),state(sim_time_ms=1000),terminated=True)['time'],-.005)
        with self.assertRaises(ValueError): parts(state(sim_time_ms=1000),state())

    def test_damage_linear_and_self_damage_symmetric_logged_sum(self):
        p = parts(state(),state(enemy_loss=.3,own_loss=.2,sim_time_ms=1000))
        self.assertAlmostEqual(p['enemy_damage'],.3)
        self.assertAlmostEqual(p['own_damage'],-.2)
        self.assertAlmostEqual(p['total'],sum(p[key] for key in REWARD_COMPONENT_NAMES))
        self.assertAlmostEqual(parts(state(),state(enemy_loss=.6))['enemy_damage'],2*p['enemy_damage'])
        self.assertEqual(parts(state(enemy_loss=.3),state(enemy_loss=.3))['enemy_damage'],0.)

    def test_nonlinear_approach_equal_distance_gain_larger_near_target(self):
        cells=[N(hex_id=h,neighbors=tuple(n for n in (h-1,h+1) if 101<=n<=109)) for h in range(101,110)]
        potential=ApproachPotential(N(map=N(hexes=cells),objectives=[N(objective_id=7,x=9,y=1)]))
        own=[0.]*len(OWN)
        for key in ('alive','spatially_present','current_hex_present'): own[OWN[key]]=1.
        own[OWN['domain']]=2.
        objective=[0.]*len(OBJ)
        objective[OBJ['objective_reference']]=7; objective[OBJ['value']]=1.
        binding=N(observation=N(own_operator_features=[own],own_operator_mask=[True],objective_features=[objective],objective_mask=[True]))
        def phi(h):
            own[OWN['current_hex']]=h
            return potential.state(binding,nonlinear_scale=3.)[0]
        self.assertGreater(phi(108)-phi(107), phi(102)-phi(101))
        self.assertAlmostEqual(phi(101),math.exp(-7/3))
        self.assertEqual(phi(108),1.)
        objective[OBJ['control_relation']]=1.
        self.assertEqual(phi(101),1.)


class LossLedgerTests(unittest.TestCase):
    def setUp(self):
        self.red=unit(1,'red'); self.blue=unit(2,'blue'); self.reserve=unit(3,'blue')
        self.ledger=LossLedger([self.red,self.blue,self.reserve])

    def test_actual_partial_damage_death_and_repeat_no_double_count(self):
        self.assertEqual(self.ledger.update(snapshot(self.red,self.blue), 'red'),(0.,0.))
        self.blue.health=60.
        self.assertEqual(self.ledger.update(snapshot(self.red,self.blue),'red'),(.2,0.))
        self.blue.alive=False
        self.assertEqual(self.ledger.update(snapshot(self.red,self.blue),'red'),(.5,0.))
        self.assertEqual(self.ledger.update(snapshot(self.red,self.blue),'red'),(.5,0.))

    def test_missing_or_delayed_reserve_is_not_damage_denominator_immutable(self):
        self.assertEqual(self.ledger.update(snapshot(self.red),'red'),(0.,0.))
        self.blue.health=50.
        self.assertEqual(self.ledger.update(snapshot(self.blue),'red'),(.25,0.))
        self.assertEqual(self.ledger.update(snapshot(self.reserve),'red'),(.25,0.))
        self.assertEqual(self.ledger.update({},'red'),(.25,0.))
        self.reserve.health=50.
        self.assertEqual(self.ledger.update(snapshot(self.reserve),'red'),(.5,0.))

    def test_healing_does_not_reopen_paid_damage_and_self_loss(self):
        self.blue.health=50.
        self.ledger.update(snapshot(self.blue),'red')
        self.blue.health=100.
        self.assertEqual(self.ledger.update(snapshot(self.blue),'red'),(.25,0.))
        self.blue.health=60.; self.red.health=70.
        self.assertEqual(self.ledger.update(snapshot(self.blue,self.red),'red'),(.25,.3))
        self.blue.health=20.
        self.assertEqual(self.ledger.update(snapshot(self.blue,self.red),'red'),(.4,.3))

    def test_unknown_roster_fails_closed(self):
        with self.assertRaises((ValueError,RuntimeError)):
            self.ledger.update(snapshot(unit(99,'blue')),'red')

    def test_authoritative_indirect_hit_health_sync_and_no_repeat_reward(self):
        from checks.test_indirect_explicit_target import fixture
        from wargame_native_engine.core.engine import NativeEngine
        from wargame_native_engine.io.scenario import scenario_from_dict
        scenario=scenario_from_dict(fixture())
        ledger=LossLedger(scenario.operators)
        engine=NativeEngine(scenario)
        try:
            world=engine.reset(seed=1)
            self.assertEqual(ledger.update(snapshot(*world.operators.values()),'red'),(0.,0.))
            action=engine.indirect_fire_action(engine.get_indirect_fire_capabilities('red')[0],106,target_id=2)
            engine.step([action],delta_time_ms=1)
            self.assertEqual(ledger.update(snapshot(*engine._require_world().operators.values()),'red'),(0.,0.))
            engine.step([],delta_time_ms=150000)
            world=engine._require_world()
            enemy=world.operators[2]
            self.assertEqual(enemy.blood,2)
            self.assertAlmostEqual(enemy.health/enemy.max_health,enemy.blood/enemy.max_blood)
            paid=ledger.update(snapshot(*world.operators.values()),'red')
            self.assertAlmostEqual(paid[0],1/3)
            self.assertEqual(paid[1],0.)
            self.assertEqual(ledger.update(snapshot(*world.operators.values()),'red'),paid)
        finally:
            engine.close()

    def test_preexisting_damage_is_not_reward(self):
        blue=unit(2,'blue',40.)
        ledger=LossLedger([self.red,blue])
        self.assertEqual(ledger.update(snapshot(blue),'red'),(0.,0.))
        blue.health=20.
        self.assertEqual(ledger.update(snapshot(blue),'red'),(.5,0.))


try:
    import torch
except ImportError:
    torch = None


@unittest.skipIf(torch is None, 'optional torch dependency unavailable')
class ProgressiveIntegrationTests(unittest.TestCase):
    def setUp(self):
        import json
        from pathlib import Path
        from wargame_native_engine.data import ScenarioBundle
        from wargame_native_engine.io.scenario import scenario_from_dict
        from wargame_native_engine.compat.source_frame.template import source_frame_template_for_scenario
        from scripts.run_rl import parser
        record=json.loads((Path(__file__).resolve().parents[1]/'examples/source_movement_scenario.json').read_text())
        record['objectives']=[{'objective_id':7,'x':4,'y':1}]
        template=source_frame_template_for_scenario(scenario_from_dict(record))
        sources={unit.obj_id:unit.raw for unit in template.all_units}
        for unit in record['operators']:
            unit['raw_attributes']['source_frame']={**sources[unit['operator_id']],'value':2}
        self.bundle=ScenarioBundle.from_scenario(scenario_from_dict(record))
        self.args=parser().parse_args(['train','--output','unused','--opponent','no-op',
                                     '--steps-per-episode','2','--reward-preset','progressive-v2'])
        torch.set_num_threads(1)

    def test_worker_serial_config_parity_and_actual_environment_time(self):
        from scripts.run_rl import reward_config, environment
        from wargame_native_engine.training.ray_rollout import RolloutWorker
        from wargame_native_engine.training.minimal_ppo import MinimalPPO, PPOConfig
        config=reward_config(self.args)
        env=environment(self.bundle,self.args,2)
        worker=RolloutWorker(self.bundle,vars(self.args),0)
        try:
            self.assertEqual(worker.rewards,config)
            env.reset(42)
            before=env.reward_state(env.current_decision_binding)
            self.assertIsInstance(before,RewardState)
            result=env.step(None)
            after=env.reward_state(env.current_decision_binding)
            p=parts(before,after,terminated=result.terminated,base_reward=result.reward)
            self.assertEqual(after.sim_time_ms-before.sim_time_ms,1000)
            self.assertEqual(p['time'],-.005)
            self.assertEqual(after.enemy_loss,0.)
            env.reset(43)
            self.assertEqual(env.reward_state(env.current_decision_binding).sim_time_ms,0)
            learner=MinimalPPO(PPOConfig(epochs=1),seed=42)
            batch=worker.collect(learner.model.state_dict(),learner.state_dict()['config'],0,3)
            self.assertEqual(batch['episodes_completed'],1)
            for transition in batch['reward_transition_components']:
                self.assertAlmostEqual(transition['total'],sum(transition[k] for k in REWARD_COMPONENT_NAMES))
                self.assertEqual(transition['time'],-.005)
            self.assertAlmostEqual(batch['reward_components']['total'],sum(batch['reward_components'][k] for k in REWARD_COMPONENT_NAMES))
        finally:
            worker.close(); env.close()

    def test_old_checkpoint_load_and_reward_change_resume_rejected_before_ray_start(self):
        import sys
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from scripts.run_rl import save_checkpoint, load_checkpoint, SCHEMA
        from wargame_native_engine.training.minimal_ppo import MinimalPPO, PPOConfig
        from wargame_native_engine.training.ray_training import train_parallel
        learner=MinimalPPO(PPOConfig(epochs=1),seed=42)
        metadata={'schema':SCHEMA,'map':self.bundle.scenario.map.map_id,'scenario':self.bundle.scenario.scenario_id,
                  'reward':RewardConfig().metadata(),'side':self.args.side,'decision_ms':self.args.decision_ms,
                  'candidate_limit':self.args.candidate_limit,'opponent':self.args.opponent}
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); checkpoint=root/'old.pt'
            save_checkpoint(checkpoint,learner,metadata)
            old_bytes=checkpoint.read_bytes()
            restored,loaded=load_checkpoint(checkpoint,'cpu')
            self.assertEqual(RewardConfig(**loaded['reward']),RewardConfig())
            self.assertEqual(restored.update_count,learner.update_count)
            self.args.resume=checkpoint; self.args.num_workers=1
            # Resume validation precedes all Ray initialization or output writes.
            with patch.dict(sys.modules,{'ray':N()}):
                with self.assertRaisesRegex(ValueError,'resume reward differs'):
                    train_parallel(self.bundle,self.args,root/'new-run')
            self.assertEqual(checkpoint.read_bytes(),old_bytes)
            self.assertFalse((root/'new-run').exists())


if __name__ == '__main__':
    unittest.main()
