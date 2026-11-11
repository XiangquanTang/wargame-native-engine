"""Warm-start is a new experiment, not a relaxed resume. Real Torch, no Ray needed."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    import torch
except ImportError:
    torch = None

class UnsafePayload:
    def __reduce__(self):
        return (eval, ("__import__('builtins').print('UNSAFE_PAYLOAD_EXECUTED')",))

@unittest.skipIf(torch is None, 'optional torch dependency unavailable')
class WarmStartTests(unittest.TestCase):
    def setUp(self):
        from wargame_native_engine.training.minimal_ppo import MinimalPPO, PPOConfig, SCHEMA, CAPTURE_SCHEMA
        self.PPO, self.Config, self.old, self.new = MinimalPPO, PPOConfig, SCHEMA, CAPTURE_SCHEMA
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.PPO(seed=42)
        self.source.update_count = 30
        self.path = self.root/'parent.pt'
        from scripts.run_rl import save_checkpoint
        save_checkpoint(self.path, self.source, {'schema':self.old,'reward':{'preset':'progressive-v2'}})
    def tearDown(self):
        self.temp.cleanup()
    def test_weights_exact_critic_optimizer_rng_counters_fresh(self):
        from scripts.run_rl import warm_start_checkpoint
        learner = self.PPO(seed=7, encoder_schema=self.new)
        fresh = self.PPO(seed=7, encoder_schema=self.new)
        before = self.path.read_bytes()
        report = warm_start_checkpoint(self.path, learner, {'preset':'capture-pressure-v3'})
        for name, weight in learner.model.state_dict().items():
            expected = fresh.model.state_dict()[name] if name.startswith('critic.') else self.source.model.state_dict()[name]
            self.assertTrue(torch.equal(weight, expected), name)
        self.assertTrue(any(not torch.equal(v,self.source.model.state_dict()[n]) for n,v in learner.model.state_dict().items() if n.startswith('critic.')))
        self.assertEqual(learner.update_count, 0)
        self.assertEqual(learner.optimizer.state_dict()['state'], {})
        self.assertTrue(torch.equal(learner.generator.get_state(),fresh.generator.get_state()))
        self.assertEqual(learner.shuffle.getstate(),fresh.shuffle.getstate())
        self.assertFalse(report['exact_resume'])
        self.assertEqual(report['parent_sha256'],hashlib.sha256(before).hexdigest())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(report['encoder_transition'], {'from':self.old,'to':self.new})
    def test_cli_defaults_and_exclusion(self):
        from scripts.run_rl import parser, prepare_training_args
        args=parser().parse_args(['train','--output','new','--warm-start','parent.pt','--reward-preset','capture-pressure-v3'])
        prepare_training_args(args)
        self.assertEqual(args.bc_steps,0)
        self.assertEqual(args.encoder_schema,self.new)
        args=parser().parse_args(['train','--output','new','--warm-start','parent.pt','--bc-steps','3'])
        prepare_training_args(args)
        self.assertEqual(args.bc_steps,3)
        args=parser().parse_args(['train','--output','new'])
        prepare_training_args(args)
        self.assertEqual(args.bc_steps,128)
        with self.assertRaises(SystemExit):
            parser().parse_args(['train','--output','new','--warm-start','p','--resume','p'])
    def test_shape_schema_and_finite_rejections(self):
        from scripts.run_rl import warm_start_checkpoint
        for kind in ('shape','schema','finite','keys','metadata'):
            payload=torch.load(self.path,weights_only=True)
            if kind=='shape':payload['learner']['model']['policy.0.weight']=torch.zeros(1)
            if kind=='schema':payload['learner']['schema']='unknown'
            if kind=='finite':payload['learner']['model']['policy.0.weight'][0,0]=float('nan')
            if kind=='keys':payload['learner']['model']['extra']=torch.zeros(1)
            if kind=='metadata':payload['metadata']['encoder_schema']=self.new
            bad=self.root/(kind+'.pt');torch.save(payload,bad)
            with self.subTest(kind=kind),self.assertRaises(ValueError):
                warm_start_checkpoint(bad,self.PPO(encoder_schema=self.new),{})
    def test_saved_new_encoder_load_and_strict_legacy_rejection(self):
        from scripts.run_rl import load_checkpoint,save_checkpoint
        learner=self.PPO(encoder_schema=self.new)
        path=self.root/'new.pt'
        save_checkpoint(path,learner,{'schema':self.old,'encoder_schema':self.new})
        restored,metadata=load_checkpoint(path,'cpu')
        self.assertEqual(restored.encoder_schema,self.new)
        with self.assertRaises(ValueError):self.source.load_state_dict(learner.state_dict())
    def test_unsafe_pickle_rejected_without_execution(self):
        from scripts.run_rl import warm_start_checkpoint
        import io
        import contextlib
        path=self.root/'unsafe.pt';torch.save(UnsafePayload(),path)
        output=io.StringIO()
        with contextlib.redirect_stdout(output),self.assertRaises(Exception):
            warm_start_checkpoint(path,self.PPO(encoder_schema=self.new),{})
        self.assertNotIn('UNSAFE_PAYLOAD_EXECUTED',output.getvalue())
    def test_worker_serial_encoder_parity(self):
        from checks.test_ray_rollout import RayRolloutTests
        from scripts.run_rl import encoder_for
        from wargame_native_engine.training.ray_rollout import RolloutWorker
        fixture=RayRolloutTests();fixture.setUp()
        args={**fixture.args,'reward_preset':'capture-pressure-v3','encoder_schema':self.new}
        worker=RolloutWorker(fixture.worker.bundle,args,0)
        try:
            worker._open(0)
            binding=worker.env.current_decision_binding
            expected=encoder_for(worker.env,self.new).encode(binding)
            self.assertEqual(expected,worker.encoder.encode(binding))
            self.assertEqual(worker.encoder.schema,self.new)
        finally:
            worker.close();fixture.tearDown()
    def test_mock_ray_warm_start_and_strict_resume(self):
        from checks.test_ray_rollout import RayRolloutTests
        from checks.test_ray_training import MockTransport
        from scripts.run_rl import parser,load_checkpoint
        from wargame_native_engine.training.ray_training import train_parallel
        fixture=RayRolloutTests();fixture.setUp()
        try:
            scenario=self.root/'scenario.json';scenario.write_text(json.dumps(fixture.record))
            output=self.root/'run';output.mkdir()
            args=parser().parse_args(['train','--output',str(output),'--num-workers','1',
                '--warm-start',str(self.path),'--reward-preset','capture-pressure-v3',
                '--updates','1','--rollout-steps','1','--steps-per-episode','2',
                '--epochs','1','--minibatch-size','1','--opponent','no-op'])
            args.bundle_descriptor={'scenario_path':str(scenario),'runtime_path':None}
            transport=MockTransport()
            with patch.dict('sys.modules',{'ray':transport}),patch('scripts.run_rl.evaluate'),patch('os.sched_getaffinity',return_value=set(range(4))):
                train_parallel(fixture.worker.bundle,args,output)
                self.assertEqual(transport.teacher_calls,0)
                restored,metadata=load_checkpoint(output/'final.pt','cpu')
                self.assertEqual(restored.update_count,1)
                self.assertEqual(metadata['policy_version'],1)
                self.assertEqual(metadata['environment_steps'],1)
                self.assertEqual(metadata['encoder_schema'],self.new)
                self.assertFalse(metadata['warm_start']['exact_resume'])
                args.warm_start=None;args.resume=output/'final.pt';args.encoder_schema=self.old
                with self.assertRaisesRegex(ValueError,'encoder_schema'):
                    train_parallel(fixture.worker.bundle,args,self.root)
                args.encoder_schema=self.new;args.reward_preset='progressive-v2'
                with self.assertRaisesRegex(ValueError,'reward'):
                    train_parallel(fixture.worker.bundle,args,self.root)
        finally:fixture.tearDown()
    def test_actual_parent_if_available(self):
        from scripts.run_rl import warm_start_checkpoint
        path=Path(os.environ.get('CAPTURE_PARENT_CHECKPOINT',Path(__file__).resolve().parents[3]/'capture-review-checkpoint'/'final(1).pt'))
        if not path.exists():self.skipTest('private parent checkpoint not distributed')
        learner=self.PPO(seed=77,encoder_schema=self.new)
        original=path.read_bytes()
        report=warm_start_checkpoint(path,learner,{'preset':'capture-pressure-v3'})
        self.assertEqual(report['parent_sha256'],'280a98c490f6156bb0cf34771ea44e726e026606e7782b5f890fd76faac763dc')
        payload=torch.load(path,weights_only=True,map_location='cpu')
        for name in report['copied_parameters']:
            self.assertTrue(torch.equal(learner.model.state_dict()[name],payload['learner']['model'][name]),name)
        self.assertEqual(path.read_bytes(),original)

if __name__=='__main__':unittest.main()
