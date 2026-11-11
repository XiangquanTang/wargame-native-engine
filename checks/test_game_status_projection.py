"""Lifecycle reads avoid evidence hashing without changing result semantics."""

from dataclasses import FrozenInstanceError, replace
import unittest
from unittest.mock import patch

from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.core.result import GameStatus
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.session.game_session import GameSession


class GameStatusProjectionTests(unittest.TestCase):
    def setUp(self):
        self.scenario = load_scenario('examples/public_river_valley_scenario.json')

    def test_native_parity_active_terminal_truncated_reset_restore(self):
        engine = NativeEngine(self.scenario)
        self.addCleanup(engine.close)
        engine.reset(seed=42)
        initial = engine.snapshot()
        for terminal, truncated, winner, outcome in (
            (False, False, None, 'ongoing'),
            (True, False, None, 'draw'),
            (True, False, 'red', 'win'),
            (False, True, None, 'truncated'),
            (True, True, 'blue', 'truncated'),
        ):
            with self.subTest(outcome=outcome, winner=winner):
                world = engine._require_world()
                world.terminal, world.truncated, world.winner = terminal, truncated, winner
                status = engine.game_status()
                self.assertEqual(status, GameStatus.from_result(engine.result()))
                self.assertEqual(status.outcome, outcome)
                self.assertEqual(status.final_stage, world.stage)
                with self.assertRaises(FrozenInstanceError):
                    status.winner = 'blue'
        engine.restore(initial)
        self.assertEqual(engine.game_status(), GameStatus.from_result(engine.result()))
        self.assertEqual(engine.game_status().outcome, 'ongoing')
        engine.reset(seed=7)
        self.assertEqual(engine.game_status(), GameStatus.from_result(engine.result()))

    def test_native_query_never_serializes_world(self):
        backend = NativeEngineBackend(self.scenario)
        session = GameSession(backend)
        self.addCleanup(session.close)
        session.reset(seed=42)
        world = backend.engine._require_world()
        with patch.object(type(world), 'to_dict', side_effect=AssertionError('full world copy')):
            self.assertEqual(session.game_status().outcome, 'ongoing')

    def test_legacy_backend_without_optional_hook_uses_result(self):
        reference = NativeEngineBackend(self.scenario)
        self.addCleanup(reference.close)
        reference.reset(seed=42)
        result = replace(reference.result(), outcome='legacy-custom', winner='blue')

        class LegacyBackend:
            scenario = self.scenario
            def result(self):
                return result

        session = GameSession(LegacyBackend())
        session._reset = True
        self.assertEqual(session.game_status(), GameStatus.from_result(result))

    def test_inherited_native_hook_does_not_bypass_custom_result(self):
        class CustomBackend(NativeEngineBackend):
            def result(self):
                return replace(super().result(), outcome='custom-backend')

        backend = CustomBackend(self.scenario)
        session = GameSession(backend)
        self.addCleanup(session.close)
        session.reset(seed=42)
        self.assertEqual(session.game_status().outcome, 'custom-backend')

        class CustomEngine(NativeEngine):
            def result(self):
                return replace(super().result(), outcome='custom-engine')

        native = NativeEngineBackend(self.scenario)
        native.engine.close()
        native.engine = CustomEngine(self.scenario)
        second = GameSession(native)
        self.addCleanup(second.close)
        second.reset(seed=42)
        self.assertEqual(second.game_status().outcome, 'custom-engine')

    def test_custom_status_may_delegate_to_result_without_recursion(self):
        class CustomEngine(NativeEngine):
            def game_status(self):
                return replace(GameStatus.from_result(self.result()), outcome='custom-status')

        engine = CustomEngine(self.scenario)
        self.addCleanup(engine.close)
        engine.reset(seed=42)
        self.assertEqual(engine.game_status().outcome, 'custom-status')
        self.assertEqual(engine.result().outcome, 'ongoing')

    def test_inherited_session_hook_preserves_custom_result(self):
        class CustomSession(GameSession):
            def result(self):
                return replace(super().result(), outcome='custom-session')

        session = CustomSession(NativeEngineBackend(self.scenario))
        self.addCleanup(session.close)
        session.reset(seed=42)
        self.assertEqual(session.game_status().outcome, 'custom-session')

    def test_hook_contract_and_inactive_failures_are_explicit(self):
        backend = NativeEngineBackend(self.scenario)
        session = GameSession(backend)
        self.addCleanup(session.close)
        with self.assertRaises(RuntimeError):
            session.game_status()
        session.reset(seed=42)
        with patch.object(backend, 'game_status', return_value={'outcome': 'ongoing'}):
            with self.assertRaisesRegex(TypeError, 'exact GameStatus'):
                session.game_status()

    def test_tick_limit_status_matches_full_result_at_boundary(self):
        backend = NativeEngineBackend(self.scenario, max_ticks=1)
        session = GameSession(backend)
        self.addCleanup(session.close)
        session.reset(seed=42)
        session.step([], delta_time_ms=1000)
        self.assertEqual(session.game_status(), GameStatus.from_result(session.result()))
        self.assertTrue(session.is_terminal())


if __name__ == '__main__':
    unittest.main()
