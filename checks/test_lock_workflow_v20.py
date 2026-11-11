"""Public preview/confirm/apply lock workflow, including retained cooldown."""
from pathlib import Path
import tempfile
import unittest
import test_moving_fire_interfaces as moving
from wargame_native_engine.agents.seat import SeatOperation
from wargame_native_engine.agents.tool_calling import GameToolAdapter
from wargame_native_engine.io.replay import replay_source_archive

class LockWorkflowV20Tests(unittest.TestCase):
    make_session = moving.MovingFireInterfaceTests.make_session
    actor = moving.MovingFireInterfaceTests.actor
    begin_move = moving.MovingFireInterfaceTests.begin_move
    def test_confirm_lock_static_move_arrival_unlock_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            scenario, session, binding=self.make_session(root, locked=False, capable=False, replay_mode='legacy-debug')
            adapter=GameToolAdapter(session,binding)
            def submit(kind, **args):
                version=adapter.dispatch('game_observation',{})['observation_version']
                operation={'operation_type':kind,'actor_ids':[1],**args}
                preview=adapter.dispatch('preview_game_operation',{'operation':operation,
                    'observation_version':version,'request_id':f'p-{kind}-{version}'})['preview']
                self.assertTrue(preview['can_execute'],preview)
                receipt=adapter.dispatch('submit_game_operation',{'preview_token':preview['preview_token'],
                    'request_id':f's-{kind}-{version}','confirm_partial':False})
                self.assertTrue(receipt['accepted'],receipt)
                session.advance_tick()
                result=adapter.dispatch('game_command_result',{'command_sequence':receipt['command_sequence']})
                self.assertEqual(result['command']['status'],'APPLIED',result)
            def wait_until(predicate):
                for _ in range(300):
                    if predicate(self.actor(session,binding)):return
                    session.advance_tick()
                self.fail('state did not converge within fixture horizon')
            submit('WEAPON_LOCK',weapon_choices=[[1,36]])
            wait_until(lambda a:a['transition_kind'] is None)
            submit('SHOOT',weapon_choices=[[1,36]],target_hex=106)
            self.assertGreater(self.actor(session,binding)['weapon_cooldown_remaining_ms'],0)
            wait_until(lambda a:a['weapon_cooldown_remaining_ms']==0)
            self.begin_move(session,binding)
            submit('SHOOT',weapon_choices=[[1,36]],target_hex=106)
            self.assertTrue(self.actor(session,binding)['has_remaining_movement_task'])
            wait_until(lambda a:a['stopped'] and a['transition_kind'] is None and a['move_to_stop_remaining_ms']==0)
            wait_until(lambda a:a['weapon_cooldown_remaining_ms']==0)
            submit('SHOOT',weapon_choices=[[1,36]],target_hex=106)
            before=self.actor(session,binding)['weapon_cooldown_remaining_ms']
            submit('WEAPON_UNLOCK')
            self.assertEqual(self.actor(session,binding)['weapon_cooldown_remaining_ms'],max(0,before-1000))
            # Unlock does not invent unfolded posture; explicit unfold remains available.
            submit('WEAPON_UNFOLD')
            wait_until(lambda a:a['transition_kind'] is None and a['weapon_cooldown_remaining_ms']==0)
            submit('SHOOT',weapon_choices=[[1,36]],target_hex=106)
            session.publish(root/'published')
            self.assertTrue(replay_source_archive(scenario,root/'published/replay.zip',strict=True).matches)
