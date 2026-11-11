"""Command loss gates locking independently of suppression; no dynamics inferred."""
import unittest
from test_direct_fire_range import fixture_record
from wargame_native_engine.core.action import ActionType, EngineAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.data.source_scenario import _validate_fresh_source_state, SourceScenarioConversionError


class CommandLossLockGateTests(unittest.TestCase):
    def engine(self, raw=None, **fields):
        record = fixture_record()
        record['operators'][0].setdefault('raw_attributes', {}).update(raw or {})
        record['operators'][0].update(fields)
        engine = NativeEngine(scenario_from_dict(record))
        engine.reset(seed=42)
        self.addCleanup(engine.close)
        return engine

    def test_exact_migration_precedence_and_roundtrip(self):
        for raw, expected in [({}, False), ({'keep': 1}, False),
            ({'source_frame': {'lose_control': 1}}, True),
            ({'lose_control': False, 'source_frame': {'lose_control': 1}}, False),
            ({'lose_control': 1}, True)]:
            actor = self.engine(raw)._require_world().operators[1]
            self.assertIs(actor.command_lost, expected)
            self.assertIs(OperatorState.from_dict(actor.to_dict()).command_lost, expected)
        actor = self.engine({'lose_control': 1}, command_lost=False)._require_world().operators[1]
        self.assertFalse(actor.command_lost)
        self.assertFalse(OperatorState.from_dict(actor.to_dict()).command_lost)
        self.assertNotIn('command_lost', self.engine()._require_world().operators[1].to_dict())

    def test_invalid_flags_do_not_coerce_truthiness(self):
        for value in ('false', 2, 0.0, None, [], {}):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                self.engine({'lose_control': value})
        with self.assertRaises(TypeError): self.engine(command_lost=1)

    def test_lock_unlock_loss_gate_execution_and_actions(self):
        for locked in (False, True):
            engine = self.engine(lose_control=1)
            actor = engine._require_world().operators[1]
            self.assertTrue(actor.command_lost)
            actor.weapon_locked = locked
            actor.locked_weapon_id = 36 if locked else None
            actor.weapon_cooldown_remaining_ms = 5000
            actions = [entry.action.action_type for entry in engine.valid_actions('red')
                       if entry.action.actor_id == 1]
            self.assertNotIn(ActionType.WEAPON_LOCK, actions)
            self.assertNotIn(ActionType.WEAPON_UNLOCK, actions)
            kind = ActionType.WEAPON_UNLOCK if locked else ActionType.WEAPON_LOCK
            params = {} if locked else {'weapon_id': 36}
            result = engine.step([EngineAction('red', 1, kind, parameters=params)], delta_time_ms=1)
            self.assertEqual(result.decisions[0].code, 'ACTOR_CONTROL_LOST')
            actor = engine._require_world().operators[1]
            self.assertEqual(actor.weapon_locked, locked)
            self.assertGreater(actor.weapon_cooldown_remaining_ms, 0)
            self.assertTrue(actor.command_lost)

    def test_suppression_is_not_command_loss(self):
        engine = self.engine({'keep': 1}, suppression_remaining_ms=5000)
        actor = engine._require_world().operators[1]
        self.assertFalse(actor.command_lost)
        result = engine.step([EngineAction('red', 1, ActionType.WEAPON_LOCK,
            parameters={'weapon_id': 36})], delta_time_ms=1)
        self.assertNotEqual(result.decisions[0].code, 'ACTOR_CONTROL_LOST')

    def test_authoritative_loss_crosses_owned_live_and_recorded_views(self):
        from dataclasses import replace
        from wargame_native_engine.backends.native import NativeEngineBackend
        from wargame_native_engine.session.game_session import GameSession
        from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment
        from wargame_native_engine.compat.source_frame.template import source_frame_template_for_scenario
        from wargame_native_engine.compat.source_frame.mapping import SourceMappings
        from wargame_native_engine.viewer.live_control import _viewer_unit as live_unit
        from wargame_native_engine.viewer.replay_view import _viewer_unit as replay_unit
        for canonical, legacy in ((True, False), (False, True)):
            record = fixture_record()
            record['operators'][0]['command_lost'] = canonical
            record['operators'][0].setdefault('raw_attributes', {})['lose_control'] = legacy
            scenario = scenario_from_dict(record)
            template = source_frame_template_for_scenario(scenario)
            # Retain conflicting original Source input to exercise explicit false.
            source = template.all_units[0]
            source.raw['lose_control'] = legacy
            env = SourceCompatibleEnvironment(GameSession(NativeEngineBackend(scenario)), template=template)
            views = env.setup_views(42)
            for frame in (views.green, views.red):
                unit = next(item for item in frame['operators'] if item['obj_id'] == 1)
                self.assertIs(unit['lose_control'], legacy)
                self.assertIs(unit['extra']['native']['command_lost'], canonical)
                self.assertIs(live_unit(unit, 'operator', 'RED')['lose_control'], canonical)
                self.assertIs(replay_unit(replace(source, raw=unit), 'operator', SourceMappings())['lose_control'], canonical)
            for unit in views.blue['operators']:
                if unit['obj_id'] == 1:
                    self.assertNotIn('command_lost', unit.get('extra', {}).get('native', {}))

    def test_source_initial_active_state_boundary_unchanged(self):
        with self.assertRaises(SourceScenarioConversionError):
            _validate_fresh_source_state({'operators': [{'obj_id': 1, 'lose_control': 1}]})


if __name__ == '__main__': unittest.main()
