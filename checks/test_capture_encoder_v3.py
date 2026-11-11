"""Typed objective joins and explicit old-checkpoint compatibility."""
import unittest
from checks.test_minimal_ppo import binding, row, torch
if torch is not None:
    from wargame_native_engine.training.minimal_ppo import CompactEncoder, MinimalPPO, SCHEMA, CAPTURE_SCHEMA
    from wargame_native_engine.training.decision_encoder import ACTION_CANDIDATE_FEATURES, OBJECTIVE_FEATURES

@unittest.skipIf(torch is None, 'torch unavailable')
class CaptureEncoderTests(unittest.TestCase):
    def encoder(self, schema):
        return CompactEncoder({1:(0,0), 2:(2,2)}, 20, schema=schema)
    def capture(self, target=99):
        b = binding()
        b.observation.action_candidate_features += (row(ACTION_CANDIDATE_FEATURES,
            catalog_index=2, action_type=11, actor_present=1, actor_reference=11,
            target_present=1, target_reference=target),)
        b.observation.action_candidate_mask += (True,)
        b.action_space.action_ids += ('capture',)
        b.action_space.mask += (True,)
        return b
    def test_only_capture_changes(self):
        old,new = [self.encoder(s).encode(self.capture()) for s in (SCHEMA,CAPTURE_SCHEMA)]
        self.assertEqual(old.candidates[:2],new.candidates[:2])
        for name in ('own','enemies','objectives','globals','mask','action_ids','action_indices'):
            self.assertEqual(getattr(old,name),getattr(new,name))
        self.assertEqual(old.candidates[2][61],0)
        self.assertEqual(new.candidates[2][61],1)
        self.assertEqual(new.candidates[2][34:36],(.5,.5))
        self.assertEqual(new.candidates[2][62:64],(.5,.5))
        self.assertEqual(new.candidates[2][38:41],(1.,0.,0.))
    def test_objective_operator_collision(self):
        b=self.capture(21)
        b.observation.objective_features=(row(OBJECTIVE_FEATURES,objective_reference=21,x=10,y=10,value=2,control_relation=2),)
        old,new=[self.encoder(s).encode(b) for s in (SCHEMA,CAPTURE_SCHEMA)]
        self.assertEqual(old.candidates[2][34:36],(.25,.3))
        self.assertEqual(new.candidates[2][34:36],(.5,.5))
        self.assertEqual(new.candidates[2][38:41],(0.,0.,1.))
    def test_missing_objective_fails_closed(self):
        with self.assertRaisesRegex(ValueError,'objective absent'):
            self.encoder(CAPTURE_SCHEMA).encode(self.capture(21))
        self.encoder(SCHEMA).encode(self.capture(21))
    def test_match_controller_dispatches_both_encoder_schemas(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace as NS
        from wargame_native_engine.matches.controllers import PPOController
        for schema in (SCHEMA,CAPTURE_SCHEMA):
            learner=MinimalPPO(seed=2,encoder_schema=schema)
            with tempfile.TemporaryDirectory() as root:
                path=Path(root)/'policy.pt'
                torch.save({'learner':learner.state_dict(),'metadata':{'encoder_schema':schema}},path)
                controller=PPOController(path)
                context=NS(public_map={'valid_hex_ids':[1,2]},weapon_catalog={'weapons':[]},binding=self.capture())
                controller.reset(context)
                self.assertEqual(controller.encoder.schema,schema)
                self.assertIn(controller.decide(context),context.binding.action_space.action_ids)
                torch.save({'learner':learner.state_dict(),'metadata':{'encoder_schema':'invalid'}},path)
                with self.assertRaises(ValueError): PPOController(path)
    def test_schema_strict_resume(self):
        old=MinimalPPO(seed=2); new=MinimalPPO(seed=2,encoder_schema=CAPTURE_SCHEMA)
        self.assertNotIn('encoder_schema',old.state_dict())
        self.assertEqual(new.state_dict()['encoder_schema'],CAPTURE_SCHEMA)
        for k,v in old.model.state_dict().items(): self.assertTrue(torch.equal(v,new.model.state_dict()[k]))
        with self.assertRaises(ValueError): new.load_state_dict(old.state_dict())
        with self.assertRaises(ValueError): old.load_state_dict(new.state_dict())
        with self.assertRaises(ValueError): self.encoder('unknown')
if __name__=='__main__': unittest.main()
