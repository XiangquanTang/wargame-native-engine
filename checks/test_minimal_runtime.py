"""Reward algebra and public objective potential contracts."""
from types import SimpleNamespace as N
import unittest
from wargame_native_engine.training.minimal_runtime import ApproachPotential, OWN, OBJ, RewardConfig, reward_components

class MinimalRewardTests(unittest.TestCase):
    def test_terminal_zero_and_truncation_bootstrap(self):
        cfg=RewardConfig(gamma=.9)
        live=reward_components((.2,0.,.3),(.4,.5,.6),base_reward=0.,terminated=False,config=cfg)
        end=reward_components((.2,0.,.3),(.4,.5,.6),base_reward=1.,terminated=True,config=cfg)
        self.assertAlmostEqual(live['distance_raw'],.16)
        self.assertAlmostEqual(live['eight_factor_raw'],.24)
        self.assertEqual(end['distance_raw'],-.2)
        self.assertEqual(end['eight_factor_raw'],-.3)
        self.assertEqual(end['win'],1.)
        self.assertEqual(live['capture_raw'],.5)
        repeated=reward_components((.4,.5,.6),(.4,.5,.6),base_reward=0.,terminated=False,config=cfg)
        self.assertEqual(repeated['capture'],0.)
    def test_missing_nonfinite_reward_rejected(self):
        with self.assertRaises(ValueError):
            reward_components((0,0,0),(0,0,float('nan')),base_reward=0,terminated=False,config=RewardConfig())
    def test_ground_only_and_owned_target_retained(self):
        cells=[N(hex_id=h,neighbors=tuple(n for n in (h-1,h+1) if 101<=n<=105)) for h in range(101,106)]
        potential=ApproachPotential(N(map=N(hexes=cells),objectives=[N(objective_id=7,x=5,y=1)]))
        own=[0.]*len(OWN)
        for key in ('alive','spatially_present','current_hex_present'): own[OWN[key]]=1.
        own[OWN['current_hex']]=104; own[OWN['domain']]=3.
        objective=[0.]*len(OBJ)
        objective[OBJ['objective_reference']]=7;objective[OBJ['value']]=1.
        binding=N(observation=N(own_operator_features=[own],own_operator_mask=[True],objective_features=[objective],objective_mask=[True]))
        self.assertEqual(potential.state(binding),(0.,0.))
        own[OWN['domain']]=2.
        self.assertEqual(potential.state(binding),(1.,0.))
        objective[OBJ['control_relation']]=1.
        self.assertEqual(potential.state(binding),(1.,1.))
        own[OWN['on_board']]=1.
        objective[OBJ['control_relation']]=0.
        self.assertEqual(potential.state(binding),(0.,0.))

if __name__=='__main__': unittest.main()
