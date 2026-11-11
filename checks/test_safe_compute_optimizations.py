"""Narrow allocation reductions preserve hashes, validation and isolation."""
from copy import deepcopy
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wargame_native_engine.data import map_runtime as mr
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment
from wargame_native_engine.compat.source_frame.template import source_frame_template_for_scenario
from wargame_native_engine.operations import ScenarioMatchFactory
from wargame_native_engine.training import NativeMultiSeatEnv
from wargame_native_engine.agents import SeatDecision

ROOT = Path(__file__).resolve().parents[1]

class SafeComputeOptimizationTests(unittest.TestCase):
    def movement(self, edges):
        return mr._load_canonical_movement_costs(dict(
            schema=mr.MOVEMENT_COST_SCHEMA, map_id='tiny', rows=1, columns=2,
            mode_order=list(mr.MOVEMENT_MODE_NAMES),
            factor_semantics='maximum_speed_divided_by_actual_speed', edges=edges),
            map_id='tiny', rows=1, columns=2, neighbors={0:frozenset({1}),1:frozenset({0})})

    def test_fraction_pool_is_exact_and_local(self):
        edges=[[0,0,1,2,3],[0,1,0,2,3],[1,0,1,4,6]]
        a=self.movement(edges); b=self.movement(edges)
        self.assertEqual(a,{(0,0,1):Fraction(2,3),(0,1,0):Fraction(2,3),(1,0,1):Fraction(4,6)})
        self.assertIs(a[0,0,1],a[0,1,0])
        self.assertIsNot(a[0,0,1],b[0,0,1])

    def test_fraction_pool_keeps_invalid_edge_checks(self):
        for edge in [[0,0,1,1,0],[0,0,1,0,1],[0,0,1,True,1],[0,0,0,1,1],[9,0,1,1,1]]:
            with self.subTest(edge=edge),self.assertRaises(mr.MapRuntimeDataError):
                self.movement([edge])
        with self.assertRaisesRegex(mr.MapRuntimeDataError,'strictly_sorted'):
            self.movement([[0,0,1,1,1],[0,0,1,1,1]])

    def test_step_reuses_only_current_snapshot_world_hash(self):
        scenario=load_scenario(ROOT/'examples/source_movement_scenario.json')
        session=GameSession(NativeEngineBackend(scenario)); self.addCleanup(session.close)
        env=SourceCompatibleEnvironment(session,template=source_frame_template_for_scenario(scenario))
        env.setup(seed=42)
        for _ in range(2):
            with patch('wargame_native_engine.session.source_environment.canonical_hash',wraps=canonical_hash) as hashed:
                bundle,_=env.step_views([],delta_time_ms=1000)
            self.assertEqual(hashed.call_count,1)
            snapshot=session.snapshot()
            expected=canonical_hash(snapshot['world'])
            self.assertEqual(env.last_step_record['expected_state_hash'],expected)
            self.assertEqual(bundle.green['extra']['native']['state_hash'],expected)
            self.assertEqual(env._public_metadata(snapshot)['state_hash'],expected)
        altered=deepcopy(snapshot);altered['world']['tick']+=1
        self.assertNotEqual(env._public_metadata(altered)['state_hash'],expected)

    def test_projection_or_hash_failure_invalidates_current_step(self):
        for target in ('canonical_hash', 'project'):
            scenario=load_scenario(ROOT/'examples/source_movement_scenario.json')
            session=GameSession(NativeEngineBackend(scenario))
            try:
                env=SourceCompatibleEnvironment(session,template=source_frame_template_for_scenario(scenario))
                env.setup(seed=42)
                env.step_views([],delta_time_ms=1000)
                if target == 'canonical_hash':
                    context=patch('wargame_native_engine.session.source_environment.canonical_hash',side_effect=ValueError('injected'))
                else:
                    context=patch.object(env.projector,'project',side_effect=ValueError('injected'))
                with context, self.assertRaisesRegex(ValueError,'injected'):
                    env.step_views([],delta_time_ms=1000)
                self.assertIsNone(env.last_step_record)
                self.assertEqual(env.last_action_results,())
                self.assertEqual(env.last_execution_events,())
                with self.assertRaisesRegex(RuntimeError,'desynchronized'):
                    env.step_views([],delta_time_ms=1000)
                env.setup(seed=42)
                env.step_views([],delta_time_ms=1000)
                snapshot=session.snapshot()
                env.restore_views(snapshot)
                self.assertEqual(env._public_metadata(snapshot)['state_hash'],canonical_hash(snapshot['world']))
            finally:
                session.close()

    def test_transition_constructor_retains_nested_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            factory=ScenarioMatchFactory.from_path(ROOT/'examples/source_movement_scenario.json',
                storage_root=Path(directory),red_controller_id='red-test',blue_controller_id='blue-test',
                decision_interval_ms=1000,frame_interval_ms=1000)
            env=NativeMultiSeatEnv(factory,run_namespace='safe-copy-test',max_decision_windows=3)
            try:
                env.reset(seed=42)
                result=env.step(SeatDecision(),SeatDecision())
                template=result.transitions[0]
                before=[t.to_record() for t in result.transitions]
                result.observations['red']['qa_nested']={'items':[9]}
                result.capabilities['red'].operation_summary['qa_nested']={'items':[9]}
                self.assertEqual([t.to_record() for t in result.transitions],before)
                self.assertNotIn('qa_nested',result.observations['blue'])
                observation={'nested':{'items':[1]}}
                cap=replace(template.capabilities,operation_summary={'nested':{'items':[2]}})
                transition=replace(template,observation=observation,next_observation=observation,
                    capabilities=cap,next_capabilities=cap)
                observation['nested']['items'].append(9)
                cap.operation_summary['nested']['items'].append(9)
                self.assertEqual(transition.observation,{'nested':{'items':[1]}})
                self.assertEqual(transition.capabilities.operation_summary,{'nested':{'items':[2]}})
                transition.observation['nested']['items'].append(8)
                transition.capabilities.operation_summary['nested']['items'].append(8)
                self.assertEqual(transition.next_observation,{'nested':{'items':[1]}})
                self.assertEqual(transition.next_capabilities.operation_summary,{'nested':{'items':[2]}})
            finally:
                env.close()
