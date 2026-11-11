"""Independent adversarial checks; only locally authored fake policy code runs."""
from copy import deepcopy
import random
import unittest
from wargame_native_engine.agents.miaosuan import MiaosuanBaselineAdapter
from wargame_native_engine.core.action import ActionType, EngineAction, ValidAction

class Fake:
    def __init__(self, result=None): self.result = [] if result is None else result
    def setup(self, setup): self.setup_input = setup
    def step(self, observation): self.input = observation; return self.result
    def reset(self): pass

def frame():
    return {'extra': {'native': {'perspective': 'RED'}},
            'operators': [{'obj_id': 1, 'color': 0}, {'obj_id': 9, 'color': 1}],
            'passengers': [], 'time': {'cur_step': 3, 'stage': 2}}

def ready(fake, seed=11):
    adapter = MiaosuanBaselineAdapter(fake, side='red', seed=seed)
    adapter.setup({'seat': 1, 'faction': 0, 'role': 1})
    return adapter

def legal(actor=1, side='red'):
    return ValidAction(EngineAction(side, actor, ActionType.MOVE, parameters={'path': [101]}), 1)

class IndependentMiaosuanContracts(unittest.TestCase):
    def test_boolean_seat_is_not_integer_identity(self):
        fake = Fake([{'actor': True, 'obj_id': 1, 'type': 1, 'move_path': [101]}])
        batch = ready(fake).step(frame(), [legal()])
        self.assertFalse(batch.actions)
        self.assertEqual(batch.trace[0]['status'], 'rejected')

    def test_boolean_unit_is_not_integer_identity(self):
        fake = Fake([{'actor': 1, 'obj_id': True, 'type': 1, 'move_path': [101]}])
        self.assertFalse(ready(fake).step(frame(), [legal()]).actions)

    def test_catalogue_excludes_opponent_and_unknown_owned_ids(self):
        fake=Fake(); ready(fake).step(frame(), [legal(), legal(9,'blue'), legal(77)])
        self.assertEqual(set(fake.input['valid_actions']), {1})
        self.assertEqual(fake.input['role_and_grouping_info'][1]['operators'], [1])

    def test_wrong_perspective_and_stale_route_rejected(self):
        fake=Fake([{'actor':1,'obj_id':1,'type':1,'move_path':[202]}]); adapter=ready(fake)
        self.assertFalse(adapter.step(frame(),[legal()]).actions)
        other=frame();other['extra']['native']['perspective']='GREEN'
        with self.assertRaises(ValueError): adapter.step(other,[legal()])

    def test_input_mutation_and_rng_do_not_leak(self):
        class Mutator(Fake):
            def step(self, observation):
                observation['operators'].clear(); random.random(); return []
        original=frame();before=deepcopy(original);state=random.getstate()
        ready(Mutator()).step(original,[legal()])
        self.assertEqual(original,before); self.assertEqual(random.getstate(),state)

    def test_rng_restored_on_failure(self):
        class Failure(Fake):
            def step(self, observation): random.random(); raise RuntimeError('intentional')
        adapter=ready(Failure());before=random.getstate()
        with self.assertRaises(RuntimeError): adapter.step(frame(),[legal()])
        self.assertEqual(before,random.getstate())

    def test_hidden_target_not_in_projected_candidates(self):
        fake=Fake([{'actor':1,'obj_id':1,'type':2,'target_obj_id':88,'weapon_id':3}])
        attack=ValidAction(EngineAction('red',1,ActionType.SHOOT,target_id=88,parameters={'weapon_id':3}),0)
        batch=ready(fake).step(frame(),[attack])
        self.assertFalse(batch.actions)
        self.assertNotIn(1,fake.input['valid_actions'])

    def test_unknown_fields_duplicates_and_nonjson_are_auditable(self):
        good={'actor':1,'obj_id':1,'type':1,'move_path':[101]}
        fake=Fake([{**good,'target_state':'unexpected'},good,good,{'unsafe':object()}])
        batch=ready(fake).step(frame(),[legal()])
        self.assertEqual(len(batch.actions),1)
        self.assertEqual([row['status'] for row in batch.trace],['rejected','translated','rejected','rejected'])
        import json
        json.dumps(list(batch.trace),allow_nan=False)

    def test_guided_shooter_identity_preserved(self):
        own_frame=frame();own_frame['operators'].append({'obj_id':2,'color':0})
        guided=ValidAction(EngineAction('red',1,ActionType.FIRE,target_id=9,
                         parameters={'fire_mode':'GUIDED','weapon_id':3,'shooter_id':2}),0)
        record={'actor':1,'obj_id':1,'type':9,'target_obj_id':9,'guided_obj_id':2,'weapon_id':3}
        fake=Fake([record]);batch=ready(fake).step(own_frame,[guided])
        self.assertEqual(batch.actions,(guided.action,))
        self.assertEqual(fake.input['valid_actions'][1][9][0]['guided_obj_id'],2)

    def test_indirect_authority_scope_and_injection_rejections(self):
        from wargame_native_engine.core.fire_capability import IndirectFireCapability, IndirectFireCapabilityStatus
        from wargame_native_engine.rules.indirect_fire import ArtilleryClass, IndirectRangePolicy, IndirectTargetDomain
        capability=IndirectFireCapability(1,7,ArtilleryClass.LIGHT,'profile',
            IndirectRangePolicy.BOUNDED_HEX_DISTANCE,0,20,IndirectTargetDomain.AREA,
            IndirectFireCapabilityStatus.AVAILABLE,())
        base={'actor':1,'obj_id':1,'type':8,'weapon_id':7,'jm_pos':101}
        fake=Fake([{**base,'obj_id':9},{**base,'weapon_id':8},
                   {**base,'parameters':{'authority_hash':'stale'}},base])
        calls=[]
        def rejecting_builder(capability,target):
            calls.append((capability,target));raise ValueError('stale capability')
        batch=ready(fake).step(frame(),[],indirect_capabilities=[capability],
                                indirect_action_builder=rejecting_builder)
        self.assertFalse(batch.actions);self.assertEqual(len(calls),1)
        self.assertEqual([r['reason'] for r in batch.trace],['actor_not_owned',
            'indirect_capability_unavailable','unexpected_source_action_fields',
            'native_indirect_builder_rejected'])
        self.assertNotIn('indirect_action_builder',fake.input)

    def test_same_seed_instances_do_not_share_rng_progress(self):
        class Sampling(Fake):
            def step(self, observation): self.sample=random.random();return []
        a,b=Sampling(),Sampling();aa,bb=ready(a,37),ready(b,37)
        aa.step(frame(),[legal()]); first=a.sample
        aa.step(frame(),[legal()]); second=a.sample
        bb.step(frame(),[legal()]);self.assertEqual(first,b.sample)
        bb.step(frame(),[legal()]);self.assertEqual(second,b.sample)

if __name__=='__main__': unittest.main()
