"""Survival and range annotations must not redefine authoritative attack rules."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from wargame_native_engine.agents.realtime import ManualRealtimeClock, RealtimeAgentSession
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.operations import ScenarioMatchFactory
from wargame_native_engine.viewer.human_capabilities import build_human_capabilities

ROOT = Path(__file__).resolve().parents[1]


class DestroyedTargetCapabilityTests(unittest.TestCase):
    def make_snapshot(self, directory):
        record = json.loads((ROOT / 'examples/source_movement_scenario.json').read_text())
        for cell in record['map']['hexes']:
            cell.update(elev=0, cond=0)
        # Intentionally outside range but still observable: the existing game
        # rule accepts the attempt, consumes resources and deals zero damage.
        record['metadata']['source_compatible']['weapons']['36']['maximum_range'] = 1
        dead = deepcopy(record['operators'][1])
        dead.update(operator_id=3, x=5, current_hex=105,
                    alive=False, health=0, blood=0, state='DESTROYED')
        record['operators'].append(dead)
        record['deployment']['positions']['blue']['3'] = [5, 1]
        scenario = scenario_from_dict(record)
        session = RealtimeAgentSession(ScenarioMatchFactory(
            bundle=ScenarioBundle.from_scenario(scenario), storage_root=directory,
            red_controller_id='target-red', blue_controller_id='target-blue',
            decision_interval_ms=1000, frame_interval_ms=1000,
        ), run_namespace='target-survival', tick_interval_ms=1000,
            clock=ManualRealtimeClock())
        self.addCleanup(session.close)
        binding = session.reset(seed=42).red_binding
        _, view, summary = session.operation_capabilities_snapshot(binding, (1,))
        profiles = record['metadata']['source_compatible']['weapons']
        return view, summary, profiles

    def test_core_excludes_destroyed_but_keeps_out_of_range_living_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            view, summary, profiles = self.make_snapshot(Path(temporary))
            self.assertEqual(summary['shoot_target_ids'], [2])
            model = build_human_capabilities(summary, view, (1,), weapon_profiles=profiles)
            self.assertEqual(model['direct_fire']['selectable_target_ids'], [2])
            weapon = model['direct_fire']['units'][0]['weapons'][0]
            self.assertEqual(weapon['legal_target_ids'], [2])
            self.assertEqual(weapon['in_range_target_ids'], [])
            self.assertEqual(weapon['out_of_range_target_ids'], [2])
            self.assertEqual(weapon['range_unknown_target_ids'], [])

    def test_explicit_destroyed_side_contacts_not_selectable(self):
        with tempfile.TemporaryDirectory() as temporary:
            view, summary, profiles = self.make_snapshot(Path(temporary))
            observation = deepcopy(view.observation)
            live = observation['visible_enemies'][0]
            for target_id, evidence in (
                (10, {'alive': False}), (11, {'destroyed': True}),
                (12, {'blood': 0}), (13, {'health': 0}),
            ):
                observation['visible_enemies'].append(dict(live, operator_id=target_id, **evidence))
            observation['visible_enemies'].append(dict(live))  # Duplicate contact is not another target.
            model = build_human_capabilities(summary, replace(view, observation=observation),
                                             (1,), weapon_profiles=profiles)
            self.assertEqual(model['direct_fire']['selectable_target_ids'], [2])
            self.assertEqual(model['direct_fire']['units'][0]['weapons'][0]['legal_target_ids'], [2])

    def test_missing_or_fractional_position_is_unknown_not_guessed_range(self):
        with tempfile.TemporaryDirectory() as temporary:
            view, summary, profiles = self.make_snapshot(Path(temporary))
            observation = deepcopy(view.observation)
            observation['visible_enemies'][0]['position'] = [5.5, 1]
            model = build_human_capabilities(summary, replace(view, observation=observation),
                                             (1,), weapon_profiles=profiles)
            weapon = model['direct_fire']['units'][0]['weapons'][0]
            self.assertEqual(weapon['legal_target_ids'], [2])
            self.assertEqual(weapon['range_unknown_target_ids'], [2])
            self.assertEqual(weapon['out_of_range_target_ids'], [])


if __name__ == '__main__':
    unittest.main()
