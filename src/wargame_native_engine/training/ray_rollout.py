"""CPU rollout actor implementation; Ray orchestration belongs to the driver.

Only model parameters cross from the learner. Each actor owns its environment,
policy sampling RNG, and episode seeds. Returned PPO records retain the exact
sampled candidate features and masks; the last record always cuts the GAE trace.
"""
from argparse import Namespace
from collections import Counter
import time
import os
import hashlib

from wargame_native_engine.training.minimal_ppo import MinimalPPO, PPOConfig, PPOTransition, torch
from wargame_native_engine.training.minimal_runtime import reward_components, REWARD_COMPONENT_NAMES
from wargame_native_engine.training.minimal_teacher import SimpleCaptureTeacher


class RolloutWorker:
    def __init__(self, bundle, args_dict, worker_index):
        if type(worker_index) is not int or worker_index < 0:
            raise ValueError('worker_index must be nonnegative integer')
        torch.set_num_threads(1)
        if isinstance(bundle,dict):
            from wargame_native_engine.data import ScenarioBundle
            loaded=ScenarioBundle.from_path(bundle['scenario_path'])
            if bundle.get('runtime_path'):
                loaded=loaded.with_map_runtime_data(bundle['runtime_path'])
            bundle=loaded
        self.bundle = bundle
        self.args = Namespace(**args_dict)
        self.worker_index = worker_index
        self.seed = self.args.seed + worker_index * 1000000
        self.env = None
        self.encoder = None
        self.policy = None
        self.episode = 0
        self.total_decisions = 0
        self.last_version = None
        from scripts.run_rl import reward_config
        self.rewards = reward_config(self.args)

    def _open(self, stage_offset):
        from scripts.run_rl import environment, encoder_for, encoder_schema
        self.env = environment(self.bundle,self.args,self.args.steps_per_episode,self.rewards)
        self.env.reset(self.seed + stage_offset + self.episode)
        self.encoder = encoder_for(self.env, encoder_schema(self.args))

    def _next_episode(self, stage_offset):
        self.close()
        self.episode += 1
        self._open(stage_offset)

    def collect(self, policy_state, configdict, version, steps):
        if type(steps) is not int or steps <= 0:
            raise ValueError('rollout steps must be positive integer')
        if type(version) is not int or version < 0:
            raise ValueError('policy version must be nonnegative integer')
        if self.last_version is not None and version <= self.last_version:
            raise ValueError('worker requires a new synchronous policy version')
        config = PPOConfig(**configdict)
        if self.policy is None:
            from scripts.run_rl import encoder_schema
            self.policy = MinimalPPO(config,seed=self.seed+200000,device='cpu',encoder_schema=encoder_schema(self.args))
        elif self.policy.config != config:
            raise ValueError('worker policy configuration changed')
        self.policy.model.load_state_dict(policy_state,strict=True)
        digest=hashlib.sha256()
        for name,value in sorted(policy_state.items()):
            digest.update(name.encode());digest.update(value.detach().cpu().numpy().tobytes())
        weight_digest=digest.hexdigest()
        self.last_version = version
        started = time.monotonic()
        if self.env is None:
            self.episode = 0
            self._open(100000)
        records=[]; totals=Counter(); kinds=Counter(); raw=[]
        completed=0
        for index in range(steps):
            binding=self.env.current_decision_binding
            obs=self.encoder.encode(binding)
            before=self.env.reward_state(binding)
            action=self.policy.act(obs)
            operation=binding.action_space.actions[action.action_index]
            result=self.env.step_bound_action(action.action_index,binding)
            next_binding=self.env.current_decision_binding
            after=self.env.reward_state(next_binding)
            parts=reward_components(before,after,base_reward=result.reward,
                                   terminated=result.terminated,config=self.rewards)
            next_value=0. if result.terminated else self.policy.value(self.encoder.encode(next_binding))
            done=result.terminated or result.truncated
            records.append(PPOTransition(obs,action.local_index,action.log_prob,action.value,
                parts['total'],next_value,not result.terminated,not done and index+1<steps))
            for key in (*REWARD_COMPONENT_NAMES, 'total'):
                totals[key]+=parts[key]
            raw.append(parts)
            kinds[operation.get('action',{}).get('action_type','UNKNOWN')]+=1
            self.total_decisions+=1
            if done:
                completed+=1
                self._next_episode(100000)
        return {'encoder_schema':self.policy.encoder_schema,'transitions':records,'version':version,'weight_digest':weight_digest,'worker_index':self.worker_index,'pid':os.getpid(),
                'reward_components':dict(totals),'reward_transition_components':raw,
                'action_counts':dict(kinds),'episodes_completed':completed,
                'worker_episodes_completed':self.episode,'decision_steps':steps,
                'total_decisions':self.total_decisions,'elapsed_seconds':time.monotonic()-started}

    def collect_teacher(self, steps):
        if type(steps) is not int or steps <= 0:
            raise ValueError('teacher steps must be positive integer')
        if self.policy is not None or self.last_version is not None:
            raise ValueError('teacher collection must precede on-policy rollout')
        self.close()
        self.episode=0
        started=time.monotonic()
        samples=[]; kinds=Counter(); idle=0
        teacher=SimpleCaptureTeacher()
        try:
            self._open(0)
            for _ in range(steps):
                selection=teacher.select(self.env)
                if selection.action_index is None:
                    result=self.env.step(None)
                    idle+=1
                else:
                    obs=self.encoder.encode(selection.binding)
                    local=obs.action_indices.index(selection.action_index)
                    samples.append((obs,local))
                    kinds[selection.operation_type]+=1
                    result=self.env.step_bound_action(selection.action_index,selection.binding)
                if result.terminated or result.truncated:
                    teacher.close()
                    self._next_episode(0)
                    teacher=SimpleCaptureTeacher()
            return {'samples':samples,'operation_counts':dict(kinds),'unlabeled_idle_steps':idle,
                    'decision_steps':steps,'episodes_started':self.episode+1,
                    'worker_index':self.worker_index,'pid':os.getpid(),'elapsed_seconds':time.monotonic()-started}
        finally:
            teacher.close()
            self.close()
            self.episode=0

    def close(self):
        if self.env is not None:
            self.env.close()
            self.env=None
            self.encoder=None
