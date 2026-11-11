"""Worker-local PPO/BC contracts, independent of Ray transport."""
import json
from pathlib import Path
import unittest
import tempfile
import hashlib
import pickle

try:
    import torch
except ImportError:
    torch=None

@unittest.skipIf(torch is None,'optional torch dependency unavailable')
class RayRolloutTests(unittest.TestCase):
    def setUp(self):
        from scripts.run_rl import parser
        from wargame_native_engine.data import ScenarioBundle
        from wargame_native_engine.io.scenario import scenario_from_dict
        from wargame_native_engine.training.minimal_ppo import MinimalPPO,PPOConfig
        from wargame_native_engine.training.ray_rollout import RolloutWorker
        root=Path(__file__).resolve().parents[1]
        record=json.loads((root/'examples/source_movement_scenario.json').read_text())
        record['objectives']=[{'objective_id':7,'x':4,'y':1}]
        from wargame_native_engine.compat.source_frame.template import source_frame_template_for_scenario
        template=source_frame_template_for_scenario(scenario_from_dict(record))
        sources={unit.obj_id:unit.raw for unit in template.all_units}
        for unit in record['operators']:
            unit['raw_attributes']['source_frame']={**sources[unit['operator_id']],'value':2}
        self.record=record
        bundle=ScenarioBundle.from_scenario(scenario_from_dict(record))
        args=vars(parser().parse_args(['train','--output','unused','--opponent','no-op','--steps-per-episode','2']))
        self.args=args
        self.worker=RolloutWorker(bundle,args,2)
        self.policy=MinimalPPO(PPOConfig(epochs=1),seed=42)
        self.config=self.policy.state_dict()['config']
        self.weights={k:v.detach().cpu() for k,v in self.policy.model.state_dict().items()}
    def tearDown(self):
        self.worker.close()
    def test_rollout_boundaries_and_policy_versions(self):
        batch=self.worker.collect(self.weights,self.config,0,3)
        self.assertEqual(len(batch['transitions']),3)
        self.assertEqual(batch['worker_index'],2)
        self.assertEqual(batch['decision_steps'],3)
        self.assertEqual(batch['episodes_completed'],1)
        a,b,c=batch['transitions']
        self.assertTrue(a.trace_continues)
        self.assertFalse(b.trace_continues) # episode horizon
        self.assertTrue(b.bootstrap_allowed) # truncation is not terminal
        self.assertFalse(c.trace_continues) # worker/batch boundary
        self.assertTrue(c.bootstrap_allowed)
        with self.assertRaises(ValueError):self.worker.collect(self.weights,self.config,0,1)
        following=self.worker.collect(self.weights,self.config,1,1)
        self.assertEqual(following['version'],1)
        self.assertFalse(following['transitions'][-1].trace_continues)
    def test_teacher_fresh_then_onpolicy(self):
        report=self.worker.collect_teacher(1)
        self.assertEqual(report['operation_counts'],{'WEAPON_LOCK':1})
        self.assertEqual(len(report['samples']),1)
        self.assertIsNone(self.worker.env)
        obs,index=report['samples'][0]
        self.assertTrue(obs.mask[index])
        batch=self.worker.collect(self.weights,self.config,0,1)
        self.assertEqual(len(batch['transitions']),1)
        self.assertEqual(batch['worker_episodes_completed'],0)
        with self.assertRaises(ValueError):self.worker.collect_teacher(1)
    def test_descriptor_and_weight_digest(self):
        from wargame_native_engine.training.ray_rollout import RolloutWorker
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'scenario.json'
            path.write_text(json.dumps(self.record))
            descriptor={'scenario_path':str(path),'runtime_path':None}
            # The wire descriptor has no live locks/runtime-cache objects.
            self.assertEqual(pickle.loads(pickle.dumps(descriptor)),descriptor)
            worker=RolloutWorker(descriptor,self.args,3)
            try:
                report=worker.collect(self.weights,self.config,7,1)
                digest=hashlib.sha256()
                for name,value in sorted(self.weights.items()):
                    digest.update(name.encode());digest.update(value.numpy().tobytes())
                self.assertEqual(report['weight_digest'],digest.hexdigest())
                self.assertEqual(report['version'],7)
            finally:worker.close()
    def test_distinct_worker_seed(self):
        self.assertEqual(self.worker.seed,2000042)
        with self.assertRaises(ValueError):self.worker.collect(self.weights,self.config,0,0)

if __name__=='__main__':unittest.main()
