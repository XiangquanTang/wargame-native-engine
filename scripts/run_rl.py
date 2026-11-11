#!/usr/bin/env python3
"""Primary minimal baseline: actual rule imitation, then fresh on-policy PPO."""
from __future__ import annotations
from wargame_native_engine.training.candidate_limits import MAX_NEW_CANDIDATES, validate_new_candidate_limit
import argparse
from collections import Counter
from dataclasses import asdict
import json
import hashlib
import os
from pathlib import Path
import time

from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.training.minimal_runtime import MinimalTrainingEnv, RewardConfig, reward_components, REWARD_COMPONENT_NAMES
from wargame_native_engine.training.minimal_ppo import CompactEncoder, MinimalPPO, PPOConfig, PPOTransition, torch, SCHEMA, CAPTURE_SCHEMA


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n',encoding='utf-8')


def load_bundle(args):
    if args.input:
        if not args.scenario_input:
            raise ValueError('--input requires --scenario-input')
        from wargame_native_engine.data.scenario_workspace import prepare_source_game
        report = prepare_source_game(args.input,args.scenario_input,args.workspace)
        path, runtime = report['scenario_path'],report['runtime']['path']
    else:
        if args.scenario_input:
            raise ValueError('--scenario-input requires --input')
        path, runtime = args.scenario_bundle,args.map_runtime_data
        if path is None:
            raise ValueError('provide --input and --scenario-input, or --scenario-bundle')
    args.bundle_descriptor={'scenario_path':str(Path(path).resolve()),
                            'runtime_path':str(Path(runtime).resolve()) if runtime else None}
    bundle = ScenarioBundle.from_path(path)
    if runtime:
        bundle = bundle.with_map_runtime_data(runtime)
    print(f'LOADED: map={bundle.scenario.map.map_id} scenario={bundle.scenario.scenario_id}',flush=True)
    print(f'SCENARIO: {Path(path).resolve()}',flush=True)
    print(f'RUNTIME: {Path(runtime).resolve() if runtime else "native declared map"}',flush=True)
    return bundle


def reward_config(args):
    """One reward resolver shared by serial, Ray driver, and rollout workers."""
    fields = ('gamma', 'win_weight', 'capture_weight', 'distance_weight',
              'eight_factor_weight', 'damage_weight', 'own_damage_weight',
              'time_weight', 'approach_scale', 'pressure_major_rate', 'pressure_minor_rate',
              'pressure_major_value', 'pressure_distance_scale', 'pressure_floor',
              'pressure_enemy_multiplier', 'kill_weight')
    overrides = {key: getattr(args, key) for key in fields
                 if getattr(args, key, None) is not None}
    return RewardConfig.for_preset(getattr(args, 'reward_preset', 'legacy-v1'), **overrides)


def environment(bundle,args,horizon,rewards=None):
    return MinimalTrainingEnv(bundle,side=args.side,horizon=horizon,decision_ms=args.decision_ms,
                              candidate_limit=args.candidate_limit,opponent=args.opponent,
                              reward_config=rewards if rewards is not None else reward_config(args))


def encoder_schema(args):
    explicit = getattr(args, 'encoder_schema', None)
    metadata = getattr(args, 'checkpoint_metadata', None)
    schema = explicit or (metadata.get('encoder_schema', SCHEMA) if metadata else
                          CAPTURE_SCHEMA if getattr(args, 'reward_preset', None) == 'capture-pressure-v3' else SCHEMA)
    if schema not in (SCHEMA, CAPTURE_SCHEMA):
        raise ValueError('unsupported encoder schema')
    return schema


class _CandidateLimitAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        namespace._candidate_limit_explicit = True


def prepare_training_args(args):
    # An omitted resume flag inherits the checkpoint's exact capacity. Read
    # metadata safely on CPU without constructing a model or changing weights.
    if (getattr(args, 'resume', None)
            and getattr(args, '_candidate_limit_explicit', True) is False):
        payload = torch.load(args.resume, map_location='cpu', weights_only=True)
        capacity = payload.get('metadata', {}).get('candidate_limit')
        if type(capacity) is not int or not 1 <= capacity <= 256:
            raise ValueError('resume checkpoint has invalid candidate_limit metadata')
        args.candidate_limit = capacity
        args._candidate_limit_explicit = True  # resolved once; strict comparison remains
    # Strict resume retains explicitly selected checkpoint capacity. Warm-start
    # starts a new experiment, so it uses the new <=255 candidate budget.
    if not getattr(args, 'resume', None):
        validate_new_candidate_limit(args.candidate_limit)
    if getattr(args, 'bc_steps', None) is None:
        args.bc_steps = 0 if getattr(args, 'warm_start', None) else 128
    args.encoder_schema = encoder_schema(args)


def encoder_for(env, schema=SCHEMA):
    kwargs = {}
    # Static public catalog is deliberately separate from GREEN reward analysis.
    profiles = env._manager.get_weapon_catalog(env._require_session_id())
    kwargs['weapon_profiles'] = {int(v['weapon_id']):v for v in profiles['weapons']}
    return CompactEncoder(env.approach.positions,env.approach.spatial_scale,schema=schema,**kwargs)


def save_checkpoint(path,learner,metadata):
    path=Path(path)
    temporary=path.with_name(path.name+'.tmp')
    try:
        torch.save({'learner':learner.state_dict(),'metadata':metadata},temporary)
        os.replace(temporary,path)
    finally:
        if temporary.exists(): temporary.unlink()


def load_checkpoint(path,device):
    payload = torch.load(path,map_location=device,weights_only=True)
    learner = MinimalPPO(PPOConfig(**payload['learner']['config']),seed=0,device=device,
                         encoder_schema=payload['learner'].get('encoder_schema', SCHEMA))
    if payload['metadata'].get('encoder_schema', SCHEMA) != learner.encoder_schema:
        raise ValueError('checkpoint encoder metadata mismatch')
    learner.load_state_dict(payload['learner'])
    return learner,payload['metadata']


def warm_start_checkpoint(path, learner, target_reward):
    """Copy actor/shared weights only, never parent optimizer, critic, RNG or counters."""
    path = Path(path)
    # Hash the same bytes safely loaded, including when the source path changes concurrently.
    import io
    data = path.read_bytes()
    payload = torch.load(io.BytesIO(data), map_location='cpu', weights_only=True)
    state, metadata = payload['learner'], payload['metadata']
    source_schema = state.get('encoder_schema', SCHEMA)
    if state.get('schema') != SCHEMA or source_schema not in (SCHEMA, CAPTURE_SCHEMA):
        raise ValueError('unsupported warm-start architecture or encoder schema')
    if metadata.get('encoder_schema', SCHEMA) != source_schema:
        raise ValueError('warm-start encoder metadata mismatch')
    source_config = PPOConfig(**state['config'])
    if (source_config.entity_size, source_config.hidden_size) != (learner.config.entity_size, learner.config.hidden_size):
        raise ValueError('warm-start architecture dimensions differ')
    expected = learner.model.state_dict()
    weights = state['model']
    if set(weights) != set(expected):
        raise ValueError('warm-start model keys differ')
    for name, value in weights.items():
        if not isinstance(value, torch.Tensor) or value.shape != expected[name].shape or value.dtype != expected[name].dtype:
            raise ValueError('warm-start parameter shape/dtype differs: ' + name)
        if not bool(torch.isfinite(value).all()):
            raise ValueError('non-finite warm-start parameter: ' + name)
    copied = [name for name in expected if not name.startswith('critic.')]
    reset = [name for name in expected if name.startswith('critic.')]
    learner.model.load_state_dict({name: weights[name] if name in copied else value
                                  for name, value in expected.items()}, strict=True)
    return {'parent_path': str(path.resolve()), 'parent_sha256': hashlib.sha256(data).hexdigest(),
            'parent_reward': metadata.get('reward'), 'parent_config': state['config'],
            'target_reward': target_reward, 'target_config': asdict(learner.config),
            'encoder_transition': {'from': source_schema, 'to': learner.encoder_schema},
            'copied_parameters': copied, 'reset_parameters': reset,
            'copied_modules': ['own_encoder', 'enemy_encoder', 'objective_encoder', 'policy'],
            'reset_modules': ['critic', 'optimizer', 'sampling_rng', 'shuffle_rng', 'update_count', 'episodes'],
            'exact_resume': False,
            'semantic_change': source_schema != learner.encoder_schema,
            'note': 'Actor tensors copied exactly; changed CAPTURE encoding does not preserve CAPTURE logits.'}


def add_components(total,components):
    for k in (*REWARD_COMPONENT_NAMES, 'total'):
        total[k] += components[k]


def collect_teacher(bundle,args,learner,output):
    from wargame_native_engine.training.minimal_teacher import SimpleCaptureTeacher
    samples, kinds = [],Counter()
    skipped = 0
    episode=0
    env=environment(bundle,args,args.steps_per_episode)
    try:
        env.reset(args.seed)
        encoder=encoder_for(env,encoder_schema(args))
        teacher=SimpleCaptureTeacher()
        for _ in range(args.bc_steps):
            selection=teacher.select(env)
            if selection.action_index is None:
                result=env.step(None)
                skipped+=1
            else:
                obs=encoder.encode(selection.binding)
                local=obs.action_indices.index(selection.action_index)
                samples.append((obs,local))
                kinds[selection.operation_type]+=1
                result=env.step_bound_action(selection.action_index,selection.binding)
            if result.terminated or result.truncated:
                episode+=1
                env.close()
                env=environment(bundle,args,args.steps_per_episode)
                env.reset(args.seed+episode)
                encoder=encoder_for(env,encoder_schema(args))
                teacher=SimpleCaptureTeacher()
        if not samples:
            raise RuntimeError('rule teacher produced no matched labels; BC cannot proceed')
        metrics=learner.behavior_clone(samples,epochs=args.bc_epochs)
        report={**metrics,'teacher':'simple-capture-single-command-round-robin',
                'decision_steps':args.bc_steps,'unlabeled_idle_steps':skipped,
                'operation_counts':dict(kinds),'episodes_started':episode+1,
                'value_head_supervised':False,'ppo_transitions_from_demonstrations':0}
        write_json(output/'imitation-summary.json',report)
        print(f'BC: samples={len(samples)} idle={skipped} loss={metrics["loss"]:.6f} actions={dict(kinds)}',flush=True)
        return report
    finally:
        env.close()


def evaluate(bundle,args,learner,rewards,output,*,seed):
    env=environment(bundle,args,args.eval_steps,rewards)
    totals=Counter(); kinds=Counter(); trajectory=[]
    started=time.monotonic()
    try:
        env.reset(seed)
        encoder=encoder_for(env,encoder_schema(args))
        for step in range(args.eval_steps):
            binding=env.current_decision_binding
            before=env.reward_state(binding)
            obs=encoder.encode(binding)
            action=learner.act(obs,deterministic=True)
            record=binding.action_space.actions[action.action_index]
            result=env.step_bound_action(action.action_index,binding)
            after=env.reward_state(env.current_decision_binding)
            parts=reward_components(before,after,base_reward=result.reward,terminated=result.terminated,config=rewards)
            add_components(totals,parts)
            kind=record.get('action',{}).get('action_type','UNKNOWN')
            kinds[kind]+=1
            if (step+1)%100==0:
                print(f'EVAL: decisions={step+1}/{args.eval_steps}',flush=True)
            trajectory.append({'step':step,'action_index':action.action_index,'operation':kind,
                               'reward':parts['total'],'reward_components':parts,'terminated':result.terminated,'truncated':result.truncated})
            if result.terminated or result.truncated:
                break
        publication=env.publish(output/'replay',replay_mode='compact')
        report={'schema':'native-minimal-rl-evaluation/1.0','seed':seed,'side':args.side,
                'opponent':args.opponent,'decisions':len(trajectory),'frames':publication.frame_count,
                'terminated':result.terminated,'truncated':result.truncated,'winner':result.info.get('winner'),
                'reward_components':dict(totals),'action_counts':dict(kinds),
                'elapsed_seconds':time.monotonic()-started,'replay':str(publication.replay_path),
                'deterministic_argmax':True,'trajectory':trajectory,
                'reward_config':rewards.metadata(),'decision_ms':args.decision_ms,
                'candidate_limit':args.candidate_limit,
                'checkpoint_metadata':getattr(args,'checkpoint_metadata',None)}
        write_json(output/'evaluation.json',report)
        print(f'EVALUATED: decisions={len(trajectory)} reward={totals["total"]:.6f} replay={publication.replay_path}',flush=True)
        return report
    finally:
        env.close()


def train(bundle,args,output):
    prepare_training_args(args)
    rewards=reward_config(args)
    learner=MinimalPPO(PPOConfig(gamma=args.gamma,learning_rate=args.learning_rate,epochs=args.epochs,
                                minibatch_size=args.minibatch_size),seed=args.seed,device=args.device,encoder_schema=encoder_schema(args))
    metadata={'schema':SCHEMA,'encoder_schema':encoder_schema(args),'map':bundle.scenario.map.map_id,'scenario':bundle.scenario.scenario_id,
              'reward':rewards.metadata(),'side':args.side,'decision_ms':args.decision_ms,
              'candidate_limit':args.candidate_limit,'opponent':args.opponent,'seed':args.seed,
              'parameters':sum(p.numel() for p in learner.model.parameters()),
              'actor_information':'side projection + public static map/weapon catalog',
              'reward_information':'authoritative damage/time and GREEN Native eight-factor, never actor input'}
    if getattr(args, 'warm_start', None):
        metadata['warm_start']=warm_start_checkpoint(args.warm_start,learner,rewards.metadata())
    write_json(output/'run.json',metadata)
    save_checkpoint(output/'initial.pt',learner,metadata)
    print(f'MODEL: entity-candidate PPO parameters={metadata["parameters"]}; device={args.device}',flush=True)
    print(f'REWARD: {rewards.metadata()}; gamma per decision; native terminal rules unchanged',flush=True)
    if args.bc_steps:
        collect_teacher(bundle,args,learner,output)
        save_checkpoint(output/'imitation.pt',learner,metadata)
    # Demonstrations are never placed in the PPO rollout; start fresh episodes.
    episode=0
    env=environment(bundle,args,args.steps_per_episode)
    total_steps=0
    started=time.monotonic()
    try:
        env.reset(args.seed+100000)
        encoder=encoder_for(env,encoder_schema(args))
        for update in range(args.updates):
            transitions=[]; totals=Counter(); kinds=Counter(); raw_parts=[]
            for i in range(args.rollout_steps):
                binding=env.current_decision_binding
                obs=encoder.encode(binding)
                before=env.reward_state(binding)
                action=learner.act(obs)
                record=binding.action_space.actions[action.action_index]
                result=env.step_bound_action(action.action_index,binding)
                next_binding=env.current_decision_binding
                after=env.reward_state(next_binding)
                parts=reward_components(before,after,base_reward=result.reward,terminated=result.terminated,config=rewards)
                next_value=0. if result.terminated else learner.value(encoder.encode(next_binding))
                end=result.terminated or result.truncated
                transitions.append(PPOTransition(obs,action.local_index,action.log_prob,action.value,
                    parts['total'],next_value,not result.terminated,not end and i+1<args.rollout_steps))
                total_steps+=1
                add_components(totals,parts)
                raw_parts.append(parts)
                kinds[record.get('action',{}).get('action_type','UNKNOWN')]+=1
                if end:
                    episode+=1
                    env.close()
                    env=environment(bundle,args,args.steps_per_episode)
                    env.reset(args.seed+100000+episode)
                    encoder=encoder_for(env,encoder_schema(args))
            metrics=learner.update(transitions)
            metrics.update({'environment_steps':total_steps,'episodes_completed':episode,
                            'reward_components':dict(totals),'action_counts':dict(kinds),
                            'reward_transition_components':raw_parts,
                            'elapsed_seconds':time.monotonic()-started})
            with (output/'training.jsonl').open('a',encoding='utf-8') as f:
                f.write(json.dumps(metrics,ensure_ascii=False,allow_nan=False)+'\n')
            save_checkpoint(output/'final.pt',learner,metadata)
            print(f'PPO: update={update+1}/{args.updates} steps={total_steps} loss={metrics["loss"]:.6f} rewards={dict(totals)}',flush=True)
    finally:
        env.close()
    # Reload the saved checkpoint rather than evaluating in-memory parameters.
    restored,args.checkpoint_metadata=load_checkpoint(output/'final.pt',args.device)
    evaluate(bundle,args,restored,rewards,output,seed=args.seed+200000)
    print(f'SAVED: {output / "final.pt"}',flush=True)


def parser():
    p=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    sub=p.add_subparsers(dest='command',required=True)
    for name in ('train','evaluate'):
        q=sub.add_parser(name,allow_abbrev=False)
        q.add_argument('--input',type=Path)
        q.add_argument('--scenario-input',type=Path)
        q.add_argument('--workspace',type=Path,default=Path('local_data/maps'))
        q.add_argument('--scenario-bundle',type=Path)
        q.add_argument('--map-runtime-data',type=Path)
        q.add_argument('--output',type=Path,required=True)
        q.add_argument('--side',choices=('red','blue'),default='red' if name=='train' else None)
        q.add_argument('--opponent',choices=('random-legal','no-op'),default='random-legal' if name=='train' else None)
        q.add_argument('--seed',type=int,default=42)
        q.add_argument('--device',default='cpu')
        q.add_argument('--decision-ms',type=int,default=1000 if name=='train' else None)
        q.set_defaults(_candidate_limit_explicit=False)
        q.add_argument('--candidate-limit',type=int,action=_CandidateLimitAction,default=MAX_NEW_CANDIDATES if name=='train' else None)
        q.add_argument('--eval-steps',type=int,default=1500)
        if name=='evaluate':
            q.add_argument('--checkpoint',type=Path,required=True)
        else:
            q.add_argument('--num-workers',type=int,default=0,help='0: legacy serial; positive: real Ray CPU actors (suggest 4)')
            q.add_argument('--ray-object-store-mb',type=int,default=1024,help='Bound local Ray object store size (MiB)')
            q.add_argument('--ray-check',action='store_true',help='Real Ray preflight: requires >=2 workers; one environment decision each, no training/eval')
            initialization=q.add_mutually_exclusive_group()
            initialization.add_argument('--resume',type=Path,help='Ray path: strict resume into a new output directory')
            initialization.add_argument('--warm-start',type=Path,help='Copy actor/shared weights, reset critic and optimizer into a NEW experiment')
            q.add_argument('--checkpoint-every',type=int,default=10)
            q.add_argument('--updates',type=int,default=10)
            q.add_argument('--rollout-steps',type=int,default=128)
            q.add_argument('--steps-per-episode',type=int,default=1500)
            q.add_argument('--bc-steps',type=int,default=None,help='Default 128; warm-start default 0; explicit value overrides')
            q.add_argument('--bc-epochs',type=int,default=4)
            q.add_argument('--epochs',type=int,default=4)
            q.add_argument('--minibatch-size',type=int,default=32)
            q.add_argument('--learning-rate',type=float,default=3e-4)
            q.add_argument('--gamma',type=float,default=.99)
            q.add_argument('--reward-preset',choices=('legacy-v1','progressive-v2','capture-pressure-v3'),default='legacy-v1',
                           help='Versioned reward semantics; progressive-v2 is for a NEW experiment, not old-checkpoint resume')
            for coefficient in ('win-weight','capture-weight','distance-weight','eight-factor-weight',
                                'damage-weight','own-damage-weight','time-weight','approach-scale',
                                'pressure-major-rate','pressure-minor-rate','pressure-major-value',
                                'pressure-distance-scale','pressure-floor','pressure-enemy-multiplier','kill-weight'):
                q.add_argument('--'+coefficient,type=float,default=None,
                               help='Override the selected reward preset default')
    return p


def main(argv=None):
    args=parser().parse_args(argv)
    for key in ('decision_ms','candidate_limit','eval_steps','updates','rollout_steps','steps_per_episode','bc_epochs','epochs','minibatch_size'):
        if hasattr(args,key) and getattr(args,key) is not None and getattr(args,key)<=0:
            raise ValueError(f'{key} must be positive')
    if args.command=='train':
        prepare_training_args(args)
    if (getattr(args,'bc_steps',0) or 0)<0:
        raise ValueError('bc_steps must be nonnegative')
    if getattr(args,'num_workers',0)<0 or getattr(args,'checkpoint_every',1)<=0:
        raise ValueError('num-workers must be nonnegative and checkpoint-every positive')
    if getattr(args,'ray_object_store_mb',1024)<80:
        raise ValueError('ray-object-store-mb must be at least 80')
    if getattr(args,'ray_check',False) and args.num_workers<2:
        raise ValueError('--ray-check requires --num-workers >= 2')
    if getattr(args,'resume',None) and not args.num_workers:
        raise ValueError('--resume requires --num-workers > 0')
    if args.output.exists():
        raise FileExistsError(f'refusing to overwrite output: {args.output}')
    torch.set_num_threads(1)
    args.output.mkdir(parents=True)
    try:
        bundle=load_bundle(args)
        if args.command=='train':
            if args.num_workers:
                from wargame_native_engine.training.ray_training import train_parallel
                train_parallel(bundle,args,args.output)
            else:
                train(bundle,args,args.output)
        else:
            learner,metadata=load_checkpoint(args.checkpoint,args.device)
            for key in ('side','opponent','decision_ms','candidate_limit'):
                if getattr(args,key) is None:
                    setattr(args,key,metadata[key])
                elif getattr(args,key)!=metadata[key]:
                    raise ValueError(f'evaluation {key} must match training checkpoint')
            args.checkpoint_metadata=metadata
            evaluate(bundle,args,learner,RewardConfig(**metadata['reward']),args.output,seed=args.seed)
    except BaseException as exc:
        write_json(args.output/'failure.json',{'type':type(exc).__name__,'message':str(exc)})
        raise
    return 0


if __name__=='__main__':
    raise SystemExit(main())
