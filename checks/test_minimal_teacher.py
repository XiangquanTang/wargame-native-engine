"""Actual simple-capture rule labels resolve to frozen native candidates."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest

from checks.test_simple_capture_policy import context, action
from wargame_native_engine.agents.protocol import AgentResetContext
from wargame_native_engine.training.minimal_teacher import SimpleCaptureTeacher


class StubEnv:
    controlled_side = 'red'
    def __init__(self, ctx):
        self.ctx = ctx
        self.records = [dict(r, action_id=f'a{i}') for i, r in enumerate(ctx.operation_capabilities.frozen_actions)]
        self.scope = 'scope-1'
        self.plans = []
        self._manager = NS(agent_reset_context=lambda *a: AgentResetContext('s','fixture','red',42,'1',a[-1]),
                           get_weapon_catalog=lambda s: ctx.operation_capabilities.operation_summary['policy_weapon_catalog'])
    def _require_session_id(self): return 's'
    @property
    def current_decision_binding(self):
        return NS(binding_scope_id=self.scope, action_space=NS(actions=tuple(deepcopy(self.records)),mask=(True,)*len(self.records)))
    def _controlled_view(self):
        c = self.ctx
        return NS(terminal=False, observation=c.observation, step_index=c.decision_index,
            frame_index=c.frame_index,sim_time_ms=c.sim_time_ms,valid_actions=tuple(self.records),
            source_frame={'operators':[{'obj_id':99,'cur_hex':101},{'obj_id':2,'cur_hex':103}]},
            own_weapon_inventory=c.operation_capabilities.operation_summary['policy_own_weapon_inventory'],
            decision_semantic=None,action_catalog=None)
    def plan_move(self, actor, destination, **kwargs):
        self.plans.append((actor,destination))
        record = action('MOVE',actor)
        record['action_id'] = f'planned-{len(self.plans)}'
        self.records.append(record)
        return NS(reachable=True,action_id=record['action_id'],blocked_reason=None)


class TeacherTests(unittest.TestCase):
    def test_exact_lock_and_idle_not_wait_label(self):
        env = StubEnv(context(locked=False,actions=[action('WEAPON_LOCK',weapon=36)]))
        teacher = SimpleCaptureTeacher()
        choice = teacher.select(env)
        self.assertEqual((choice.operation_type,choice.action_index),('WEAPON_LOCK',0))
        env.ctx.observation['friendly_operators'][0]['transition_kind'] = 'WEAPON_LOCK'
        self.assertIsNone(teacher.select(env).action_index)

    def test_move_uses_new_server_planned_binding(self):
        env = StubEnv(context(actions=[action('MOVE')]))
        choice = SimpleCaptureTeacher().select(env)
        self.assertEqual(choice.operation_type,'MOVE')
        self.assertEqual(env.plans,[(1,106)])
        self.assertEqual(choice.binding.action_space.actions[choice.action_index]['action_id'],'planned-1')

    def test_shoot_exact_visible_target_and_weapon(self):
        enemy = {'operator_id':2,'operator_type':'VEHICLE','alive':True,'position':[3,1]}
        env = StubEnv(context(enemies=[enemy],actions=[action('SHOOT',target=99,weapon=36),action('SHOOT',target=2,weapon=36)]))
        teacher = SimpleCaptureTeacher()
        choice = teacher.select(env)
        self.assertEqual((choice.operation_type,choice.action_index),('SHOOT',1))
        self.assertEqual(teacher._context(env,1).operation_capabilities.operation_summary['policy_visible_enemy_hexes'],{'2':103})

    def test_only_returned_actor_mutates_stop_dedup(self):
        actor = context().observation['friendly_operators'][0]
        actors = [dict(actor,operator_id=i,stopped=False,has_active_segment=True) for i in (1,2)]
        env = StubEnv(context(actors=actors,objectives=[{'objective_id':7,'x':2,'y':1}],actions=[action('STOP_MOVE',actor=i) for i in (1,2)]))
        teacher = SimpleCaptureTeacher()
        first = teacher.select(env)
        second = teacher.select(env)
        self.assertEqual((first.actor_id,second.actor_id),(1,2))
        self.assertEqual((first.operation_type,second.operation_type),('STOP_MOVE','STOP_MOVE'))
        self.assertIsNone(teacher.select(env).action_index)
        env.scope = 'scope-2'
        self.assertEqual(teacher.select(env).actor_id,1)

    def test_ambiguous_label_fails_instead_of_guess(self):
        env = StubEnv(context(locked=False,actions=[action('WEAPON_LOCK',weapon=36)]*2))
        with self.assertRaisesRegex(ValueError,'matches=2'):
            SimpleCaptureTeacher().select(env)

    def test_capture_label(self):
        env = StubEnv(context(objectives=[{'objective_id':7,'x':2,'y':1}],actions=[action('CAPTURE',target=7)]))
        self.assertEqual(SimpleCaptureTeacher().select(env).operation_type,'CAPTURE')

    def test_real_native_lock_move_stop_capture(self):
        from wargame_native_engine.data import ScenarioBundle
        from wargame_native_engine.io.scenario import scenario_from_dict
        from wargame_native_engine.training.minimal_runtime import MinimalTrainingEnv
        root = Path(__file__).resolve().parents[1]
        record = json.loads((root/'examples/source_movement_scenario.json').read_text())
        record['objectives'] = [{'objective_id':7,'x':4,'y':1}]
        record['operators'][0]['raw_attributes']['ammunition'] = {'0':0}
        env = MinimalTrainingEnv(ScenarioBundle.from_scenario(scenario_from_dict(record)),opponent='no-op',horizon=300)
        teacher = SimpleCaptureTeacher()
        trace = []
        try:
            env.reset(seed=42)
            for _ in range(300):
                selection = teacher.select(env)
                if selection.action_index is None:
                    step = env.step(None)
                else:
                    trace.append(selection.operation_type)
                    step = env.step_bound_action(selection.action_index,selection.binding)
                if env._controlled_view().observation['objectives'][0]['controlling_side']=='red':
                    break
                if step.terminated or step.truncated:
                    break
            self.assertEqual(env._controlled_view().observation['objectives'][0]['controlling_side'],'red')
            self.assertEqual(trace,['WEAPON_LOCK','MOVE','STOP_MOVE','CAPTURE'])
        finally:
            teacher.close()
            env.close()

if __name__ == '__main__': unittest.main()
