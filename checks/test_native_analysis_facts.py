"""Analysis projection is opt-in, authoritative, detached and historical-safe."""
from copy import deepcopy
from pathlib import Path
import unittest

from wargame_native_engine.compat.source_frame.contract import (
    ANALYSIS_SOURCE_CONTRACT, CURRENT_SOURCE_CONTRACT, LEGACY_SOURCE_CONTRACT,
)
from wargame_native_engine.compat.source_frame.exporter import SourceFrameExporter
from wargame_native_engine.compat.source_frame.template import source_frame_template_for_scenario
from wargame_native_engine.core.destruction import destroy_source_operator
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.movement.policy import load_source_movement_policy
from wargame_native_engine.movement.runtime import MovementRuntime
from wargame_native_engine.projection.analysis_facts import project_native_analysis_facts
from wargame_native_engine.projection.operator_lifecycle import project_operator_lifecycle

ROOT = Path(__file__).resolve().parents[1]


class NativeAnalysisFactsTests(unittest.TestCase):
    def setUp(self):
        self.scenario = load_scenario(ROOT / 'examples/source_movement_scenario.json')
        self.world = self.scenario.initial_world()
        self.unit = next(iter(self.world.operators.values()))
        self.template = source_frame_template_for_scenario(self.scenario)

    def facts(self):
        return project_native_analysis_facts(self.unit)

    def test_versioned_export_all_units_and_old_contracts_untouched(self):
        exporter = SourceFrameExporter()
        before = deepcopy(self.world.to_dict())
        historical_hashes = {
            LEGACY_SOURCE_CONTRACT: '3fdcebbb16a01273bfad4b5399f81db73d42e39ac64fed94c13991a65d80510e',
            CURRENT_SOURCE_CONTRACT: 'cb598a928dd1b1d5c113baa0e5d0b8bfd32939f2e1ae5aa55beae212fd426c1e',
        }
        for contract in (LEGACY_SOURCE_CONTRACT, CURRENT_SOURCE_CONTRACT):
            frame = exporter.export(self.world, template=self.template, source_contract=contract)
            # Captured from unmodified dc12299 exporter, not the new branch.
            self.assertEqual(canonical_hash(frame), historical_hashes[contract])
            for unit in frame['operators'] + frame['passengers']:
                self.assertNotIn('analysis_facts', unit['extra']['native'])
        frame = exporter.export(self.world, template=self.template,
                                source_contract=ANALYSIS_SOURCE_CONTRACT)
        units = frame['operators'] + frame['passengers']
        self.assertEqual(len(units), len(self.world.operators))
        for unit in units:
            facts = unit['extra']['native']['analysis_facts']
            self.assertEqual(facts['schema_version'], 'native-analysis-facts/1.0')
            self.assertIs(type(facts['movement_locked']), bool)
        self.assertEqual(before, self.world.to_dict())

    def test_board_unload_death_projection_does_not_retain_previous_values(self):
        self.assertIs(self.facts()['movement_locked'], False)
        self.unit.movement_locked = True
        self.unit.mounted_on = 99
        boarded = self.facts()
        self.assertTrue(boarded['boarding']['on_board'])
        self.assertTrue(boarded['movement_locked'])
        self.unit.movement_locked = False
        self.unit.mounted_on = None
        unloaded = self.facts()
        self.assertFalse(unloaded['boarding']['on_board'])
        self.assertIsNone(unloaded['boarding']['mounted_on'])
        self.assertFalse(unloaded['movement_locked'])
        self.unit.movement_locked = True
        self.unit.suppression_remaining_ms = 150_000
        destroy_source_operator(self.world, self.unit)
        dead = self.facts()
        self.assertFalse(dead['movement_locked'])
        self.assertEqual(dead['suppression'], {'active': False, 'remaining_ms': 0})
        self.assertTrue(boarded['movement_locked'])  # detached

    def test_suppression_runtime_timer_and_reset_not_stale_source(self):
        runtime = MovementRuntime(None, load_source_movement_policy())
        self.unit.state = 'SUPPRESSED'
        self.unit.suppression_remaining_ms = 150_000
        self.unit.raw_attributes['suppression_remaining_seconds'] = 999
        self.assertEqual(self.facts()['suppression']['remaining_ms'], 150_000)
        runtime.advance(self.world, 20_000, [])
        self.assertEqual(self.facts()['suppression']['remaining_ms'], 130_000)
        # Accepted suppression reapplication resets the authoritative timer.
        self.unit.suppression_remaining_ms = 150_000
        runtime.advance(self.world, 149_999, [])
        self.assertEqual(self.facts()['suppression'], {'active': True, 'remaining_ms': 1})
        runtime.advance(self.world, 1, [])
        self.assertEqual(self.facts()['suppression'], {'active': False, 'remaining_ms': 0})

    def test_fatigue_domains_and_real_recovery_use_lifecycle_authority(self):
        self.unit.operator_type = 'PERSONNEL'
        runtime = MovementRuntime(None, load_source_movement_policy())
        for level in range(3):
            self.unit.fatigue_level = level
            facts = self.facts()['fatigue']
            lifecycle = project_operator_lifecycle(self.world, self.unit.operator_id)
            self.assertEqual(facts['level'], lifecycle.fatigue.level)
            self.assertEqual(facts['level'], level)
            self.assertEqual(facts['applicability'], 'APPLICABLE')
        runtime.advance(self.world, 75_000, [])
        self.assertEqual(self.facts()['fatigue']['level'], 1)
        runtime.advance(self.world, 75_000, [])
        self.assertEqual(self.facts()['fatigue']['level'], 0)
        for domain, applicability in [('VEHICLE', 'NOT_APPLICABLE'), ('UNKNOWN', 'UNKNOWN')]:
            self.unit.operator_type = domain
            fatigue = self.facts()['fatigue']
            self.assertEqual(fatigue['applicability'], applicability)
            self.assertIsNone(fatigue['level'])
            self.assertEqual(fatigue['native_level'], 0)

    def test_close_combat_current_suppression_exported_only_once(self):
        self.unit.state = 'CLOSE_COMBAT'
        self.assertTrue(self.facts()['close_combat'])
        self.unit.state = 'STOPPED'
        self.assertFalse(self.facts()['close_combat'])
        self.assertNotIn('lose_control', self.facts())
        self.unit.suppression_remaining_ms = 1000
        self.assertEqual(self.facts()['suppression'],
                         {'active': True, 'remaining_ms': 1000})


if __name__ == '__main__':
    unittest.main()
