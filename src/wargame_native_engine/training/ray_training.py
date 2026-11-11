"""Synchronous local Ray rollout actors; one centralized PPO learner."""
from collections import Counter
from dataclasses import asdict
import json
import hashlib
import os
from pathlib import Path
import time


def wait_workers(ray, refs, stage):
    """Keep long BC/rollout waits visible; surface any failed actor promptly."""
    pending=list(enumerate(refs)); results=[None]*len(refs); started=time.monotonic()
    while pending:
        ready,_=ray.wait([ref for _,ref in pending],num_returns=1,timeout=60)
        if not ready:
            print(f'{stage}: workers complete={len(refs)-len(pending)}/{len(refs)} elapsed={time.monotonic()-started:.0f}s; waiting',flush=True)
            continue
        for ref in ready:
            found=next((i for i,(_,item) in enumerate(pending) if item is ref),None)
            if found is None:
                found=next(i for i,(_,item) in enumerate(pending) if item==ref)
            index,_=pending.pop(found)
            results[index]=ray.get(ref)
            print(f'{stage}: workers complete={len(refs)-len(pending)}/{len(refs)} elapsed={time.monotonic()-started:.0f}s',flush=True)
    return results


def train_parallel(bundle, args, output):
    try:
        import ray
    except ImportError as exc:
        raise RuntimeError("Ray is required: install the project's [parallel] extra; no serial fallback") from exc
    from scripts.run_rl import (RewardConfig, MinimalPPO, PPOConfig, SCHEMA, write_json,
                                save_checkpoint, load_checkpoint, evaluate, reward_config,
                                prepare_training_args, encoder_schema, warm_start_checkpoint)
    from wargame_native_engine.training.ray_rollout import RolloutWorker
    prepare_training_args(args)
    workers_count=args.num_workers
    cpu_count=len(os.sched_getaffinity(0)) if hasattr(os,'sched_getaffinity') else (os.cpu_count() or 1)
    if workers_count > max(1,cpu_count-1):
        raise ValueError(f'{workers_count} workers exceeds safe CPU budget {max(1,cpu_count-1)}; reduce --num-workers')
    rewards=reward_config(args)
    metadata={'schema':SCHEMA,'encoder_schema':encoder_schema(args),'map':bundle.scenario.map.map_id,'scenario':bundle.scenario.scenario_id,
              'reward':rewards.metadata(),'side':args.side,'decision_ms':args.decision_ms,
              'candidate_limit':args.candidate_limit,'opponent':args.opponent,'seed':args.seed,
              'num_workers':workers_count,'environment_steps':0,'policy_version':0,'stage':'initial',
              'actor_information':'side projection + public static map/weapon catalog',
              'reward_information':'authoritative damage/time and GREEN Native eight-factor, never actor input'}
    if args.resume:
        learner, previous=load_checkpoint(args.resume,args.device)
        if previous.get('encoder_schema', SCHEMA) != metadata['encoder_schema']:
            raise ValueError('resume encoder_schema differs from checkpoint')
        for key in ('map','scenario','reward','side','decision_ms','candidate_limit','opponent'):
            if metadata[key]!=previous[key]:
                raise ValueError(f'resume {key} differs from checkpoint')
        metadata.update(environment_steps=previous.get('environment_steps',0),
                        policy_version=previous.get('policy_version',learner.update_count),
                        stage=previous.get('stage','ppo'),resume_from=str(args.resume),
                        resume_environment='fresh episodes; no in-flight trajectory restored')
    else:
        learner=MinimalPPO(PPOConfig(gamma=args.gamma,learning_rate=args.learning_rate,
                           epochs=args.epochs,minibatch_size=args.minibatch_size),seed=args.seed,device=args.device,encoder_schema=encoder_schema(args))
        if getattr(args, 'warm_start', None):
            metadata['warm_start']=warm_start_checkpoint(args.warm_start,learner,rewards.metadata())
    metadata['parameters']=learner.parameter_count
    metadata['learner_config']=asdict(learner.config)
    write_json(output/'run.json',metadata)
    save_checkpoint(output/'initial.pt',learner,metadata)
    save_checkpoint(output/'final.pt',learner,metadata)
    print(f'RAY: workers={workers_count} CPU-only; learner={args.device}; parameters={learner.parameter_count}; synchronous PPO',flush=True)
    print(f'REWARD: {rewards.metadata()}; gamma per decision; native terminal rules unchanged',flush=True)
    print(f'BATCH: {workers_count} x {args.rollout_steps} decisions/update; BC {args.bc_steps} decisions/worker; evaluation {args.eval_steps}',flush=True)
    actors=[];started=time.monotonic()
    try:
        ray.init(address='local',num_cpus=workers_count,include_dashboard=False,log_to_driver=False,
                 _node_ip_address='127.0.0.1',object_store_memory=args.ray_object_store_mb*1024*1024)
        remote=ray.remote(num_cpus=1,num_gpus=0,max_restarts=0)(RolloutWorker)
        worker_args=vars(args).copy()
        # Resume starts new disjoint episodes rather than replaying initial seeds.
        worker_args['seed']=args.seed+metadata['environment_steps']+metadata['policy_version']*100003
        actors=[remote.remote(args.bundle_descriptor,worker_args,i) for i in range(workers_count)]
        if args.bc_steps and not args.ray_check and (not args.resume or metadata['stage']=='initial'):
            refs=[a.collect_teacher.remote(args.bc_steps) for a in actors]
            reports=wait_workers(ray,refs,'BC collecting')
            samples=[sample for report in reports for sample in report['samples']]
            if not samples:
                raise RuntimeError('parallel rule teacher produced no labels')
            metrics=learner.behavior_clone(samples,epochs=args.bc_epochs)
            kinds=Counter()
            for report in reports:kinds.update(report['operation_counts'])
            summary={**metrics,'teacher':'simple-capture-single-command-round-robin',
                     'operation_counts':dict(kinds),'decision_steps':args.bc_steps*workers_count,
                     'unlabeled_idle_steps':sum(r['unlabeled_idle_steps'] for r in reports),
                     'workers':[{k:v for k,v in r.items() if k!='samples'} for r in reports],
                     'ppo_transitions_from_demonstrations':0,'value_head_supervised':False}
            write_json(output/'imitation-summary.json',summary)
            metadata['stage']='imitation'
            save_checkpoint(output/'imitation.pt',learner,metadata)
            save_checkpoint(output/'final.pt',learner,metadata)
            print(f'BC: samples={len(samples)} loss={metrics["loss"]:.6f} actions={dict(kinds)}',flush=True)
            del samples,reports
        for update in range(1 if args.ray_check else args.updates):
            version=metadata['policy_version']
            weights={k:v.detach().cpu().clone() for k,v in learner.model.state_dict().items()}
            digest=hashlib.sha256()
            for name,value in sorted(weights.items()):
                digest.update(name.encode());digest.update(value.numpy().tobytes())
            expected_digest=digest.hexdigest()
            weight_ref=ray.put(weights)
            refs=[a.collect.remote(weight_ref,asdict(learner.config),version,1 if args.ray_check else args.rollout_steps) for a in actors]
            reports=wait_workers(ray,refs,f'PPO collecting version={version}')  # synchronous barrier
            if len({r['pid'] for r in reports})!=workers_count:
                raise RuntimeError('Ray workers must have distinct process IDs')
            if any(r['weight_digest']!=expected_digest for r in reports):
                raise RuntimeError('rollout weights mismatch')
            if any(r.get('encoder_schema', SCHEMA)!=learner.encoder_schema for r in reports):
                raise RuntimeError('rollout encoder schema mismatch')
            if any(r['version']!=version for r in reports):
                raise RuntimeError('rollout policy version mismatch')
            transitions=[];totals=Counter();kinds=Counter()
            for report in reports:
                chunk=report['transitions']
                if not chunk or chunk[-1].trace_continues:
                    raise RuntimeError('worker trajectory boundary missing')
                transitions.extend(chunk)
                totals.update(report['reward_components']);kinds.update(report['action_counts'])
            if args.ray_check:
                write_json(output/'ray-check.json',{'passed':True,'workers':workers_count,
                    'distinct_pids':[r['pid'] for r in reports],'policy_version':version,
                    'identical_weights_verified':True,'actual_environment_decisions':len(transitions),
                    'learner_device':str(learner.device),'ray_version':ray.__version__})
                ray.get([actor.close.remote() for actor in actors])
                print(f'RAY CHECK PASSED: {workers_count} distinct CPU processes; same weights; real environment steps',flush=True)
                return
            metrics=learner.update(transitions)
            metadata['environment_steps']+=len(transitions)
            metadata['policy_version']+=1
            metadata['stage']='ppo'
            metrics.update(environment_steps=metadata['environment_steps'],policy_version=metadata['policy_version'],
                           sampling_policy_version=version,reward_components=dict(totals),action_counts=dict(kinds),
                           workers=[{k:v for k,v in r.items() if k!='transitions'} for r in reports],
                           elapsed_seconds=time.monotonic()-started)
            with (output/'training.jsonl').open('a',encoding='utf8') as f:
                f.write(json.dumps(metrics,ensure_ascii=False,allow_nan=False)+'\n');f.flush()
            save_checkpoint(output/'final.pt',learner,metadata)
            if metadata['policy_version']%args.checkpoint_every==0:
                save_checkpoint(output/f'checkpoint-{metadata["policy_version"]:06d}.pt',learner,metadata)
            print(f'PPO: update={update+1}/{args.updates} version={metadata["policy_version"]} decisions={metadata["environment_steps"]} loss={metrics["loss"]:.6f} reward={totals["total"]:.6f} saved={output / "final.pt"}',flush=True)
        # Release sampling processes before serial full-length evaluation.
        ray.get([actor.close.remote() for actor in actors])
        for actor in actors:ray.kill(actor,no_restart=True)
        actors=[]
        ray.shutdown()
        restored,args.checkpoint_metadata=load_checkpoint(output/'final.pt',args.device)
        evaluate(bundle,args,restored,rewards,output,seed=args.seed+200000)
    except BaseException as exc:
        write_json(output/'interrupted.json',{'type':type(exc).__name__,'message':str(exc),
                   'safe_checkpoint':str(output/'final.pt'),
                   'note':'Only atomic completed-stage checkpoint is safe; restart fresh episodes with --resume.'})
        print(f'STOPPED: last safe checkpoint {output / "final.pt"}; no partial batch applied on resume',flush=True)
        raise
    finally:
        if ray.is_initialized():
            for actor in actors:
                ray.kill(actor,no_restart=True)
            ray.shutdown()
