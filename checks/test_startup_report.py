"""v22 launcher summaries must not leak hashes or claim readiness early."""
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import run_maps, run_map123, run_realtime_dual_control
from scripts.startup_report import print_startup
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.version import ENGINE_VERSION

ROOT = Path(__file__).resolve().parents[1]


class StartupReportTests(unittest.TestCase):
    def test_summary_reports_loaded_inputs_without_hashes(self):
        bundle = ScenarioBundle.from_path(ROOT / 'examples/source_movement_scenario.json')
        scenario = bundle.scenario
        args = SimpleNamespace(scenario_bundle=ROOT / 'examples/source_movement_scenario.json',
            map_runtime_data=None, output_dir=ROOT / 'local_data/test', tick_interval_ms=1000,
            json=False, red_token='red', blue_token='blue')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            print_startup(bundle, scenario, args, '127.0.0.1', 8003)
        text = output.getvalue()
        for required in (f'READY: v{ENGINE_VERSION}', 'SCENARIO:', 'MAP:', 'SIZE:', 'UNITS:', 'RUNTIME:', 'OUTPUT:', 'RED:', 'BLUE:'):
            self.assertIn(required, text)
        self.assertIn(str(args.scenario_bundle.resolve()), text)
        self.assertNotIn('sha256', text)
        self.assertNotIn(bundle.map_identity.sha256, text)
        self.assertIn('no external movement or LOS table', text)
        args.json = True
        with contextlib.redirect_stdout(output := io.StringIO()):
            print_startup(bundle, scenario, args, '127.0.0.1', 8003)
        report = json.loads(output.getvalue().splitlines()[0])
        self.assertEqual(report['identity']['map_identity']['sha256'], bundle.map_identity.sha256)
        self.assertEqual(report['startup']['units']['red'], sum(u.side == 'red' for u in scenario.operators))

    def test_runtime_channels_are_reported_without_reloading(self):
        from dataclasses import replace
        from fractions import Fraction
        from wargame_native_engine.data.map_runtime import MovementCostLookup
        bundle = ScenarioBundle.from_path(ROOT / 'examples/source_movement_scenario.json')
        movement = MovementCostLookup(map_id='test', rows=1, columns=2,
            source_sha256='a' * 64, payload_sha256='b' * 64,
            factors={(3, 1, 2): Fraction(1), (0, 1, 2): Fraction(2)})
        runtime = SimpleNamespace(movement=movement, visibility=SimpleNamespace(mode_order=(0, 3, 4)))
        bundle = replace(bundle, _map_runtime_data=runtime)
        args = SimpleNamespace(scenario_bundle=ROOT / 'examples/source_movement_scenario.json',
            map_runtime_data=ROOT / 'local_data/runtime', output_dir=ROOT / 'local_data/test',
            tick_interval_ms=1000, json=False, red_token='red', blue_token='blue')
        with contextlib.redirect_stdout(output := io.StringIO()):
            print_startup(bundle, bundle.scenario, args, '127.0.0.1', 8003)
        self.assertEqual(movement.mode_order, (0, 3))
        self.assertIn('movement=[0, 3] LOS=[0, 3, 4]', output.getvalue())
        self.assertNotIn(str(args.map_runtime_data.resolve()), output.getvalue())
        self.assertIn('已加载机动/视线表', output.getvalue())

    def test_prepare_json_is_opt_in_and_forwarded(self):
        report = {'map_id': '221', 'scenario_id': 'test', 'scenario_path': 'scenario.json',
                  'runtime': {'path': 'runtime'}, 'output_dir': 'out', 'run_namespace': 'run',
                  'preparation_path': 'preparation.json', 'conversion': {'warnings': ['example warning']}}
        for as_json in (False, True):
            with patch('wargame_native_engine.data.scenario_workspace.prepare_source_game', return_value=report), \
                 patch.object(run_realtime_dual_control, 'main', return_value=0) as launcher, \
                 contextlib.redirect_stdout(output := io.StringIO()):
                self.assertEqual(run_maps.main(['start', '--input', 'map.zip', '--scenario-input', 'scenario.json',
                                               *(['--json'] if as_json else [])]), 0)
            self.assertEqual('--json' in launcher.call_args.args[0], as_json)
            if as_json:
                self.assertEqual(json.loads(output.getvalue()), report)
            else:
                self.assertIn('WARNING: example warning', output.getvalue())
                self.assertNotIn('READY:', output.getvalue())

    def test_existing_launcher_flags_remain(self):
        self.assertTrue(run_map123.build_parser().parse_args(['--json', '--prepare-only']).json)
        self.assertTrue(run_realtime_dual_control.build_parser().parse_args([
            '--json', '--output-dir', 'out', '--run-namespace', 'run']).json)
