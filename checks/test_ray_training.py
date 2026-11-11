"""Synchronous mocked transport tests: NOT evidence of a working Ray runtime."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from dataclasses import replace
import unittest
from unittest.mock import patch
from checks import test_ray_rollout as worker_tests
torch=worker_tests.torch


class MockTransport:
    """Executes actors locally. Synthetic PIDs test driver validation only."""
    __version__='unit-mock-not-ray'
    def __init__(self):self.active=False;self.teacher_calls=0;self.corrupt_boundary=False
    def init(self,**kwargs):self.active=True
    def shutdown(self):self.active=False
    def is_initialized(self):return self.active
    def put(self,value):return value
    def get(self,value):return value
    def wait(self,refs,**kwargs):return refs[:1],refs[1:]
    def kill(self,actor,**kwargs):actor.worker.close()
    def remote(self,**resources):
        transport=self
        def wrap(cls):
            def create(*args):
                worker=cls(*args)
                def collect(*values):
                    result=worker.collect(*values)
                    result['pid']=10000+worker.worker_index # explicit transport simulation
                    if transport.corrupt_boundary:
                        result['transitions'][-1]=replace(result['transitions'][-1],trace_continues=True)
                    return result
                def teacher(*values):
                    transport.teacher_calls+=1
                    return worker.collect_teacher(*values)
                return SimpleNamespace(worker=worker,collect=SimpleNamespace(remote=collect),
                    collect_teacher=SimpleNamespace(remote=teacher),close=SimpleNamespace(remote=worker.close))
            return SimpleNamespace(remote=create)
        return wrap


@unittest.skipIf(torch is None,'optional torch dependency unavailable')
class RayTrainingDriverTests(unittest.TestCase):
    def test_resume_skips_bc_and_advances_version(self):
        from scripts.run_rl import parser,load_checkpoint
        from wargame_native_engine.training.ray_training import train_parallel
        fixture=worker_tests.RayRolloutTests();fixture.setUp()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);scenario=root/'scenario.json';scenario.write_text(json.dumps(fixture.record))
                first=root/'first';first.mkdir()
                args=parser().parse_args(['train','--output',str(first),'--num-workers','2',
                    '--bc-steps','1','--bc-epochs','1','--updates','1','--rollout-steps','1',
                    '--steps-per-episode','2','--epochs','1','--minibatch-size','2',
                    '--opponent','no-op','--checkpoint-every','1'])
                args.bundle_descriptor={'scenario_path':str(scenario),'runtime_path':None}
                transport=MockTransport()
                with patch.dict('sys.modules',{'ray':transport}),patch('scripts.run_rl.evaluate'),patch('os.sched_getaffinity',return_value=set(range(4))):
                    train_parallel(fixture.worker.bundle,args,first)
                    self.assertEqual(transport.teacher_calls,2)
                    _,before=load_checkpoint(first/'final.pt','cpu')
                    self.assertEqual(before['policy_version'],1)
                    second=root/'second';second.mkdir();args.output=second;args.resume=first/'final.pt'
                    train_parallel(fixture.worker.bundle,args,second)
                    self.assertEqual(transport.teacher_calls,2) # no repeated BC on resume
                _,after=load_checkpoint(second/'final.pt','cpu')
                self.assertEqual(after['policy_version'],2)
                self.assertEqual(after['environment_steps'],4)
                self.assertTrue((second/'checkpoint-000002.pt').exists())
                self.assertFalse((second/'final.pt.tmp').exists())
        finally:fixture.tearDown()

    def test_missing_worker_trace_boundary_fails_before_update(self):
        from scripts.run_rl import parser,load_checkpoint
        from wargame_native_engine.training.ray_training import train_parallel
        fixture=worker_tests.RayRolloutTests();fixture.setUp()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);scenario=root/'scenario.json';scenario.write_text(json.dumps(fixture.record))
                args=parser().parse_args(['train','--output',str(root),'--num-workers','1',
                    '--bc-steps','0','--updates','1','--rollout-steps','1','--opponent','no-op'])
                args.bundle_descriptor={'scenario_path':str(scenario),'runtime_path':None}
                transport=MockTransport();transport.corrupt_boundary=True
                with patch.dict('sys.modules',{'ray':transport}),patch('scripts.run_rl.evaluate'),patch('os.sched_getaffinity',return_value=set(range(4))):
                    with self.assertRaisesRegex(RuntimeError,'boundary missing'):
                        train_parallel(fixture.worker.bundle,args,root)
                _,metadata=load_checkpoint(root/'final.pt','cpu')
                self.assertEqual(metadata['policy_version'],0)
                self.assertTrue((root/'interrupted.json').exists())
        finally:fixture.tearDown()

    def test_atomic_checkpoint_preserves_previous_on_write_failure(self):
        from scripts.run_rl import save_checkpoint,load_checkpoint
        fixture=worker_tests.RayRolloutTests();fixture.setUp()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                path=Path(temporary)/'final.pt'
                save_checkpoint(path,fixture.policy,{'sentinel':1})
                original=path.read_bytes()
                with patch('scripts.run_rl.torch.save',side_effect=OSError('simulated disk failure')):
                    with self.assertRaises(OSError):save_checkpoint(path,fixture.policy,{'sentinel':2})
                self.assertEqual(path.read_bytes(),original)
                self.assertFalse(path.with_name('final.pt.tmp').exists())
        finally:fixture.tearDown()

if __name__=='__main__':unittest.main()
