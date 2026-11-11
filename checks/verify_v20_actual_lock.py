"""Actual unchanged-map lock readiness smoke; supplied preparation reports are local QA inputs."""
import json
import argparse
from pathlib import Path
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.action import ActionType,EngineAction
from wargame_native_engine.io.replay import ReplayWriter,replay_source_archive
from wargame_native_engine.agents.realtime import RealtimeAgentSession,ManualRealtimeClock
from wargame_native_engine.operations import ScenarioMatchFactory

ROOT=Path(__file__).resolve().parents[1]
WORK=ROOT.parent

def verify(name,bundle):
    engine=NativeEngine(bundle.scenario,map_runtime_data=bundle.map_runtime_data)
    try:
        world=engine.reset(seed=42)
        # Select an original deployed vehicle with loaded direct weapon; no ammunition edits.
        actor=next(a for a in world.operators.values() if a.side=='red' and a.operator_type=='VEHICLE'
            and a.current_hex is not None and a.mounted_on is None and a.raw_attributes.get('ammunition',{}).get('0',0)>=4
            and any(w in a.weapon_ids for w in (36,37,54,43)))
        wid=next(w for w in (36,37,54,43) if w in actor.weapon_ids)
        aid=actor.operator_id
        movement=next(a.action for a in engine.valid_actions('red') if a.action.actor_id==aid and a.action.action_type is ActionType.MOVE)
        destination=movement.parameters['path'][-1]
        replaydir=WORK/'actual-lock-replays'/name
        writer=ReplayWriter(replaydir,run_id=f'v20-lock-{name}',scenario=bundle.scenario,seed=42,
            agents={'red':'qa','blue':'noop'},max_ticks=1000,ruleset=engine.ruleset,project_commit=None)
        writer.record_initial_state(engine.snapshot())
        stages=[]
        def step(action=None,dt=1,label='wait'):
            nonlocal actor
            r=engine.step([] if action is None else [action],delta_time_ms=dt)
            actor=engine._require_world().operators[aid]
            writer.record_step(r,engine.snapshot())
            if action is not None:
                assert r.decisions[0].accepted,(name,label,r.decisions)
                stages.append(label)
            return r
        def cmd(kind,params=None):return EngineAction('red',aid,kind,parameters=params or {})
        def shot(label):step(cmd(ActionType.SHOOT,{'weapon_id':wid,'target_hex':destination}),label=label)
        step(cmd(ActionType.WEAPON_LOCK,{'weapon_id':wid}),engine._source_policy.transition_ms,'lock_confirmed')
        shot('stationary_fire')
        step(dt=actor.weapon_cooldown_remaining_ms)
        step(movement,label='move')
        assert actor.moving
        shot('moving_fire')
        # Advance the unchanged route and stop transition, not hand-editing any state.
        for _ in range(12):
            if actor.stopped and not actor.moving and actor.move_to_stop_remaining_ms==0:break
            step(dt=30000)
        assert actor.current_hex==destination and actor.stopped,(name,actor.current_hex,destination)
        if actor.weapon_cooldown_remaining_ms:step(dt=actor.weapon_cooldown_remaining_ms)
        shot('arrival_stationary_fire')
        before=actor.weapon_cooldown_remaining_ms
        ammo=dict(actor.raw_attributes['ammunition'])
        step(cmd(ActionType.WEAPON_UNLOCK),17,'unlock')
        assert actor.weapon_cooldown_remaining_ms==max(0,before-17)
        assert actor.raw_attributes['ammunition']==ammo
        assert not actor.weapon_locked and actor.locked_weapon_id is None
        writer.finalize({'result':engine.result().to_dict()},status='completed')
        # Separate genuine public new-game publication smoke uses the supported
        # source-archive runtime parameter. It does not re-execute the above
        # EngineAction trajectory; report these evidence scopes separately.
        session=RealtimeAgentSession(ScenarioMatchFactory(bundle=bundle,
            storage_root=WORK/'public-storage'/name,red_controller_id='qa-red',blue_controller_id='qa-blue',
            decision_interval_ms=1000,frame_interval_ms=1000,replay_mode="legacy-debug"),
            run_namespace=f'v20-{name}',tick_interval_ms=1000,clock=ManualRealtimeClock())
        try:
            session.reset(seed=42)
            session.advance_tick()
            publication=session.publish(WORK/'published'/name)
            replay=replay_source_archive(bundle.scenario,publication.replay_path,strict=True,
                                         map_runtime_data=bundle.map_runtime_data)
        finally:session.close()
        assert replay.matches,replay
        return {'map_id':name,'actor_id':aid,'weapon_id':wid,'destination':destination,'stages':stages,
                'cooldown_before_unlock':before,'cooldown_after_unlock':actor.weapon_cooldown_remaining_ms,
                'ammunition_unchanged_by_unlock':True,'original_source_unmodified':True,'public_new_game_strict_source_replay_matches':True,
                'workflow_evidence':'Native EngineAction trajectory; separate public new-game no-op replay'}
    finally:engine.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preparation',type=Path,action='append',required=True,
                        help='prepare-only JSON report for each uploaded scenario')
    parser.add_argument('--map123-runtime',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True,help='fresh verification output directory')
    args=parser.parse_args()
    WORK=args.output
    WORK.mkdir(parents=True,exist_ok=False)
    reports=[]
    for path in args.preparation:
        prep=json.loads(path.read_text())
        bundle=ScenarioBundle.from_path(prep['scenario_path']).with_map_runtime_data(prep['runtime']['path'])
        reports.append(verify(str(prep['map_id']),bundle));print(json.dumps(reports[-1]),flush=True)
    reports.append(verify('123',ScenarioBundle.from_path(ROOT/'examples/map123/scenario.json').with_map_runtime_data(args.map123_runtime)))
    (WORK/'actual-lock-report.json').write_text(json.dumps(reports,indent=2))
    print(json.dumps(reports[-1]),flush=True)
