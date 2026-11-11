"""Reward CLI/checkpoint contracts; mocked Ray transport is NOT real Ray proof."""
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from checks import test_ray_rollout as worker_tests
torch = worker_tests.torch
from checks.test_ray_training import MockTransport


@unittest.skipIf(torch is None, 'optional torch dependency unavailable')
class RewardCLIContracts(unittest.TestCase):
    def test_defaults_and_overrides(self):
        from scripts.run_rl import parser, reward_config
        old = reward_config(parser().parse_args(['train','--output','unused']))
        self.assertEqual(old.metadata(), dict(gamma=.99,win_weight=1.,capture_weight=1.,
                                             distance_weight=.1,eight_factor_weight=.03))
        args = parser().parse_args(['train','--output','unused','--reward-preset','progressive-v2'])
        new = reward_config(args)
        self.assertEqual((new.win_weight,new.capture_weight,new.distance_weight,new.damage_weight,
                          new.own_damage_weight,new.time_weight,new.eight_factor_weight),
                         (10.,5.,1.,1.,1.,.005,0.))
        args = parser().parse_args(['train','--output','unused','--reward-preset','progressive-v2',
            '--capture-weight','8','--damage-weight','2','--own-damage-weight','.5',
            '--time-weight','.01','--approach-scale','4'])
        override = reward_config(args)
        self.assertEqual((override.capture_weight,override.damage_weight,override.own_damage_weight,
                          override.time_weight,override.approach_scale),(8.,2.,.5,.01,4.))

    def test_serial_worker_configuration_and_logged_sum(self):
        from scripts.run_rl import environment, parser, reward_config
        from wargame_native_engine.training.ray_rollout import RolloutWorker
        from wargame_native_engine.training.minimal_runtime import REWARD_COMPONENT_NAMES
        fixture=worker_tests.RayRolloutTests();fixture.setUp()
        try:
            args=parser().parse_args(['train','--output','unused','--opponent','no-op',
                                     '--reward-preset','progressive-v2','--time-weight','.007'])
            rewards=reward_config(args)
            worker=RolloutWorker(fixture.worker.bundle,vars(args),0)
            env=environment(fixture.worker.bundle,args,2,rewards)
            try:
                self.assertEqual(worker.rewards,rewards)
                env.reset(42)
                worker._open(0)
                self.assertEqual(env.reward_state(env.current_decision_binding),
                                 worker.env.reward_state(worker.env.current_decision_binding))
                report=worker.collect(fixture.weights,fixture.config,0,1)
                for parts in report['reward_transition_components']:
                    self.assertAlmostEqual(parts['total'],sum(parts[k] for k in REWARD_COMPONENT_NAMES))
                self.assertAlmostEqual(report['reward_components']['time'],-.007)
                self.assertAlmostEqual(report['reward_components']['total'],
                    sum(report['reward_components'][k] for k in REWARD_COMPONENT_NAMES))
            finally:
                env.close();worker.close()
        finally:fixture.tearDown()

    def test_old_checkpoint_loads_and_new_reward_resume_rejected_before_ray(self):
        from scripts.run_rl import parser, save_checkpoint, load_checkpoint, reward_config
        from wargame_native_engine.training.ray_training import train_parallel
        from wargame_native_engine.training.minimal_runtime import RewardConfig
        fixture=worker_tests.RayRolloutTests();fixture.setUp()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary); checkpoint=root/'old.pt'; output=root/'new';output.mkdir()
                args=parser().parse_args(['train','--output',str(output),'--num-workers','1',
                    '--bc-steps','0','--reward-preset','progressive-v2','--resume',str(checkpoint)])
                metadata={'map':fixture.worker.bundle.scenario.map.map_id,
                    'scenario':fixture.worker.bundle.scenario.scenario_id,
                    'reward':RewardConfig().metadata(),'side':args.side,'decision_ms':args.decision_ms,
                    'candidate_limit':args.candidate_limit,'opponent':args.opponent}
                save_checkpoint(checkpoint,fixture.policy,metadata)
                original=checkpoint.read_bytes()
                restored,loaded=load_checkpoint(checkpoint,'cpu')
                self.assertEqual(RewardConfig(**loaded['reward']),RewardConfig())
                transport=MockTransport()
                with patch.dict('sys.modules',{'ray':transport}),patch('os.sched_getaffinity',return_value=set(range(4))):
                    with self.assertRaisesRegex(ValueError,'resume reward differs from checkpoint'):
                        train_parallel(fixture.worker.bundle,args,output)
                self.assertFalse(transport.active)
                self.assertEqual(checkpoint.read_bytes(),original)
                self.assertFalse((output/'initial.pt').exists())
        finally:fixture.tearDown()

if __name__=='__main__': unittest.main()
