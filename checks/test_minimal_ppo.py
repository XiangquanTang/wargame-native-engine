import math
"""Minimal structured PPO contract and optimizer tests."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import unittest

try:
    import torch
except ImportError:
    torch = None

if torch is not None:
    from wargame_native_engine.training.minimal_ppo import (
        CompactEncoder, PolicyInput, MinimalPPO, PPOConfig, PPOTransition,
        compute_gae, masked_distribution,
    )
    from wargame_native_engine.training.decision_encoder import (
        SNAPSHOT_FEATURES, OWN_OPERATOR_FEATURES, OBSERVED_ENEMY_FEATURES,
        OBJECTIVE_FEATURES, INVENTORY_UNIT_FEATURES, WEAPON_INVENTORY_FEATURES,
        ACTION_CANDIDATE_FEATURES,
    )


def row(names, **values):
    return tuple(float(values.get(k,0)) for k in names)


def binding(padding=0, owner=11):
    def group(rows,width):
        return tuple(rows)+((0.,)*width,)*padding, (True,)*len(rows)+(False,)*padding
    o=SimpleNamespace(catalog_hash="catalog", snapshot_features=row(SNAPSHOT_FEATURES,stage=2,sim_time_ms=1000))
    values = {
        "own_operator": ([row(OWN_OPERATOR_FEATURES,operator_reference=owner,alive=1,health_ratio=1,current_hex_present=1,current_hex=1,spatially_present=1,domain=2)], OWN_OPERATOR_FEATURES),
        "observed_enemy": ([row(OBSERVED_ENEMY_FEATURES,operator_reference=21,x=5,y=6,alive=1,domain=2)],OBSERVED_ENEMY_FEATURES),
        "objective": ([row(OBJECTIVE_FEATURES,objective_reference=99,x=10,y=10,value=2)], OBJECTIVE_FEATURES),
        "inventory_unit": ([row(INVENTORY_UNIT_FEATURES,operator_reference=owner,weapon_count=1,locked_weapon_present=1,locked_weapon_reference=69)], INVENTORY_UNIT_FEATURES),
        "weapon_inventory": ([row(WEAPON_INVENTORY_FEATURES,owner_reference=owner,weapon_reference=69,ammunition_present=1,ammunition=10,direct_mode=1)],WEAPON_INVENTORY_FEATURES),
        "action_candidate": ([row(ACTION_CANDIDATE_FEATURES,catalog_index=0,actor_present=1,actor_reference=owner,action_type=3,path_endpoint_present=1,path_endpoint=2,path_length_present=1,path_length=2),row(ACTION_CANDIDATE_FEATURES,catalog_index=1,actor_present=1,actor_reference=owner,action_type=6,weapon_present=1,weapon_reference=69,weapon_owner_reference=owner,target_present=1,target_reference=21,fire_mode=1)],ACTION_CANDIDATE_FEATURES),
    }
    for name,(rows,names) in values.items():
        rows,mask=group(rows,len(names));setattr(o,name+"_features",rows);setattr(o,name+"_mask",mask)
    return SimpleNamespace(observation=o,action_space=SimpleNamespace(catalog_hash="catalog",action_ids=("move","shoot"),mask=(True,True)+(False,)*padding))


@unittest.skipIf(torch is None,"optional torch learner dependency unavailable")
class MinimalPPOTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(1)

    def setUp(self):
        self.encoder=CompactEncoder({1:(0,0),2:(2,2)},20)
        self.obs=self.encoder.encode(binding())
        self.learner=MinimalPPO(PPOConfig(epochs=2,minibatch_size=2),seed=12)

    def test_feature_shapes_and_padding_invariance(self):
        padded=self.encoder.encode(binding(10))
        self.assertEqual(self.obs,padded)
        self.assertEqual(len(self.obs.globals),8)
        self.assertEqual(len(self.obs.own[0]),18)
        self.assertEqual(len(self.obs.candidates[0]),70)
        self.assertLess(self.learner.parameter_count,20000)

    def test_public_weapon_profiles_distinguish_range(self):
        short=CompactEncoder({1:(0,0),2:(2,2)},20,{69:{"maximum_range":4,"target_domains":["VEHICLE"]}}).encode(binding())
        long=CompactEncoder({1:(0,0),2:(2,2)},20,{69:{"maximum_range":12,"target_domains":["VEHICLE"]}}).encode(binding())
        self.assertEqual(short.candidates[0],long.candidates[0])
        self.assertNotEqual(short.candidates[1],long.candidates[1])
        self.assertNotEqual(short.candidates[1],self.obs.candidates[1])

    def test_identifiers_are_join_keys_not_features(self):
        renamed=self.encoder.encode(binding(owner=12345678))
        self.assertEqual(self.obs,renamed)

    def test_masked_probability_and_illegal_transition(self):
        dist=masked_distribution(torch.tensor([100.,0.,3.]),(False,True,True))
        self.assertEqual(float(dist.probs[0]),0.)
        with self.assertRaises(ValueError):
            masked_distribution(torch.tensor([1.]),(False,))
        invalid=replace(self.obs,mask=(False,True))
        for _ in range(10):
            self.assertEqual(self.learner.act(invalid).local_index,1)
        with self.assertRaises(ValueError):
            PPOTransition(invalid,0,0,0,0,0,True,True)

    def test_candidate_permutation_equivariance(self):
        perm=replace(self.obs,candidates=self.obs.candidates[::-1],mask=self.obs.mask[::-1],action_ids=self.obs.action_ids[::-1],action_indices=self.obs.action_indices[::-1])
        a,v=self.learner.model(self.obs);b,w=self.learner.model(perm)
        torch.testing.assert_close(a,b.flip(0));torch.testing.assert_close(v,w)
        self.assertEqual(self.learner.act(self.obs,deterministic=True).action_index,self.learner.act(perm,deterministic=True).action_index)

    def test_empty_catalog_critic_bootstrap(self):
        empty=replace(self.obs,candidates=(),mask=(),action_ids=(),action_indices=())
        self.assertTrue(torch.isfinite(torch.tensor(self.learner.value(empty))))
        with self.assertRaises(ValueError): self.learner.act(empty)

    def test_gae_terminal_and_truncation(self):
        transitions=[PPOTransition(self.obs,0,-.69,.3,0,.4,True,True),PPOTransition(self.obs,0,-.69,.4,0,.5,True,True),PPOTransition(self.obs,0,-.69,.5,1,999,False,False)]
        a,v=compute_gae(transitions,.99,.95)
        self.assertAlmostEqual(a[0],.627617625)
        self.assertAlmostEqual(a[1],.56525)
        self.assertAlmostEqual(a[2],.5)
        self.assertAlmostEqual(v[2],1.)
        truncated=PPOTransition(self.obs,0,-.69,.5,0,.7,True,False)
        a,_=compute_gae([truncated,transitions[2]],.99,.95)
        self.assertAlmostEqual(a[0],.193)

    def test_real_optimizer_update_and_checkpoint_roundtrip(self):
        before={k:v.clone() for k,v in self.learner.model.state_dict().items()}
        transitions=[]
        for i in range(4):
            a=self.learner.act(self.obs)
            transitions.append(PPOTransition(self.obs,a.local_index,a.log_prob,a.value,float(i%2),.3,i!=3,i!=3))
        metrics=self.learner.update(transitions)
        self.assertEqual(metrics["optimizer_steps"],4)
        self.assertGreater(metrics["parameter_l2_delta"],0)
        self.assertTrue(math.isfinite(metrics["parameter_l2_delta"]))
        self.assertTrue(any(not torch.equal(before[k],v) for k,v in self.learner.model.state_dict().items()))
        state=deepcopy(self.learner.state_dict())
        other=MinimalPPO(self.learner.config,seed=999)
        other.load_state_dict(state)
        self.assertEqual(self.learner.act(self.obs),other.act(self.obs))
        with self.assertRaises(ValueError):
            MinimalPPO(PPOConfig(hidden_size=32)).load_state_dict(state)

    def test_behavior_cloning_uses_legal_teacher_without_fake_ppo(self):
        logits,_=self.learner.model(self.obs)
        before=float(masked_distribution(logits,self.obs.mask).probs[0].detach())
        critic_before={k:v.clone() for k,v in self.learner.model.critic.state_dict().items()}
        result=self.learner.behavior_clone([(self.obs,0)]*4,epochs=10,minibatch_size=2)
        logits,_=self.learner.model(self.obs)
        after=float(masked_distribution(logits,self.obs.mask).probs[0].detach())
        self.assertGreater(after,before)
        self.assertEqual(result["optimizer_steps"],20)
        self.assertGreater(result["parameter_l2_delta"],0)
        self.assertTrue(math.isfinite(result["parameter_l2_delta"]))
        for key in ("teacher_accuracy_before", "teacher_accuracy_after"):
            self.assertTrue(math.isfinite(result[key]))
            self.assertGreaterEqual(result[key],0)
            self.assertLessEqual(result[key],1)
        self.assertEqual(result["teacher_accuracy_after"],
                         float(self.learner.act(self.obs,deterministic=True).local_index == 0))
        self.assertEqual(self.learner.update_count,0)
        self.assertTrue(all(torch.equal(critic_before[k],v) for k,v in self.learner.model.critic.state_dict().items()))
        with self.assertRaises(ValueError):
            self.learner.behavior_clone([(replace(self.obs,mask=(False,True)),0)])

    def test_input_detaches_mutable_values(self):
        values=list(self.obs.globals)
        copy=replace(self.obs,globals=values)
        values[0]=999
        self.assertNotEqual(copy.globals[0],999)

    def test_geometry_and_catalog_fail_closed(self):
        with self.assertRaises(ValueError): CompactEncoder({},20).encode(binding())
        b=binding();b.action_space.catalog_hash="other"
        with self.assertRaises(ValueError):self.encoder.encode(b)


if __name__ == "__main__": unittest.main()
