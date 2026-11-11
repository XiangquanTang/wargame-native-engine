"""Workspace compatibility remains explicit, isolated and source preserving."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import pickle
import struct
from zipfile import ZipFile
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.run_maps import main
from wargame_native_engine.data import map_workspace as workspace


class MapWorkspaceCompatibilityTests(unittest.TestCase):
    def basic(self, root, **metadata):
        path = root / 'basic.json'
        raw = {'map_id': 'tiny', 'coordinate_system': 'odd-r', 'ele_grade': 10,
               'map_data': [[{'pos': 0, 'cond': 0, 'elev': 0, 'neighbors': [-1] * 6,
                              'roads': [0] * 6, 'rivers': [0] * 6}]], **metadata}
        path.write_text(json.dumps(raw))
        return path

    def test_terrain_preview_ignores_runtime_and_compatibility_and_scale(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            basic = self.basic(root)
            (root / 'cost.pickle').write_bytes(b'broken')
            (root / 'tinysee.npz').write_bytes(b'broken')
            (root / 'map-compatibility.json').write_text('broken')
            before = basic.read_bytes()
            with patch.object(workspace, 'convert_benchmark_basic_map', side_effect=AssertionError('must not convert')):
                report = workspace.check_workspace(root / 'maps', source=basic, terrain_only=True)
            self.assertEqual(report['passed'], 1, report)
            self.assertEqual(report['maps'][0]['runtime']['status'], 'skipped')
            self.assertTrue(Path(report['maps'][0]['preview']).is_file())
            self.assertEqual(basic.read_bytes(), before)

    def test_missing_scale_uses_shared_200_and_preserves_source_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            basic = self.basic(root)
            before = basic.read_bytes()
            report = workspace.check_workspace(root / 'maps', source=basic)
            self.assertEqual(report['passed'], 1, report)
            entry = report['maps'][0]
            self.assertEqual(entry['hex_size_m'], 200)
            self.assertEqual(entry['hex_size_source'], 'shared_map123_default')
            native = json.loads((Path(entry['structured_map']).parent / 'native-map.json').read_text())
            self.assertEqual(native['hex_size_m'], 200)
            structured = json.loads(Path(entry['structured_map']).read_text())
            self.assertIsNone(structured['physical']['hex_size_m'])
            self.assertEqual(basic.read_bytes(), before)

    def test_declared_scale_preserved_and_invalid_not_repaired(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            basic = self.basic(root, scale=100)
            report = workspace.check_workspace(root / 'maps', source=basic)
            self.assertEqual(report['maps'][0]['hex_size_m'], 100)
            self.assertEqual(report['maps'][0]['hex_size_source'], 'source_scale')
            basic = self.basic(root, scale=0)
            self.assertEqual(workspace.check_workspace(root / 'maps', source=basic)['failed'], 1)

    def test_sidecar_and_cli_agreement_and_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            basic = self.basic(root)
            sidecar = root / 'map-compatibility.json'
            sidecar.write_text(json.dumps({'visibility_modes': [0, 3, 4]}))
            self.assertEqual(workspace._map_compatibility(basic, None), ((0, 3, 4), 'map-compatibility.json'))
            self.assertEqual(workspace._map_compatibility(basic, (0, 3, 4)), ((0, 3, 4), 'cli'))
            report = workspace.check_workspace(root / 'maps', source=basic, visibility_modes=(0, 1, 2))
            self.assertEqual(report['failed'], 1)
            self.assertIn('conflicts', report['maps'][0]['error'])
            self.assertTrue(Path(report['maps'][0]['preview']).is_file())

    def test_invalid_mapping_and_sidecar_symlink_are_rejected(self):
        for modes in ([], [0, 0], [10], [-1], [True], ['0'], list(range(11))):
            with self.subTest(modes=modes), self.assertRaises(ValueError):
                workspace._visibility_modes(modes, label='test')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            basic = self.basic(root)
            external = root / 'external.json'
            external.write_text('{"visibility_modes":[0]}')
            (root / 'map-compatibility.json').symlink_to(external)
            with self.assertRaisesRegex(ValueError, 'symlink'):
                workspace._map_compatibility(basic, None)

    def test_cli_passes_mapping_and_rejects_invalid_ids(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with patch.object(workspace, 'check_workspace', return_value={'total': 1, 'failed': 0}) as check:
                self.assertEqual(main(['check', '--workspace', directory, '--visibility-modes', '0,3,4']), 0)
                self.assertEqual(check.call_args.kwargs['visibility_modes'], (0, 3, 4))
            for text in ('', '0,0', '0,10', 'a', '0,,1'):
                with self.subTest(text=text), self.assertRaises(SystemExit):
                    main(['check', '--workspace', directory, '--visibility-modes', text])

    def test_real_partial_runtime_sidecar_and_cache_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            basic = self.basic(root)
            (root / 'cost.pickle').write_bytes(pickle.dumps([[[{}]]] * 4, protocol=3))
            header = repr({'descr': '|b1', 'fortran_order': False, 'shape': (3, 1, 1, 1, 1)}).encode()
            header += b' ' * ((64 - (10 + len(header) + 1) % 64) % 64) + b'\n'
            payload = b'\x93NUMPY\x01\x00' + struct.pack('<H', len(header)) + header + bytes([1, 0, 1])
            with ZipFile(root / 'tinysee.npz', 'w') as archive:
                archive.writestr('data.npy', payload)
            sidecar = root / 'map-compatibility.json'
            sidecar.write_text(json.dumps({'visibility_modes': [0, 3, 4]}))
            first = workspace.check_workspace(root / 'maps', source=basic, require_runtime=True)
            self.assertEqual(first['passed'], 1, first)
            runtime = first['maps'][0]['runtime']
            self.assertEqual(runtime['visibility_modes'], [0, 3, 4])
            self.assertFalse(runtime['cache_hit'])
            second = workspace.check_workspace(root / 'maps', source=basic, require_runtime=True)
            self.assertTrue(second['maps'][0]['runtime']['cache_hit'])
            self.assertEqual(second['maps'][0]['runtime']['identity'], runtime['identity'])
            manifest_path = Path(runtime['path']) / 'map_runtime_manifest.native.json'
            manifest = json.loads(manifest_path.read_text())
            from wargame_native_engine.data.map_runtime import VISIBILITY_MODE_RECORDS
            manifest['visibility']['mode_order'] = [
                {'mode': mode, 'first_height_m': first, 'second_height_m': second, 'name': name}
                for mode, first, second, name in VISIBILITY_MODE_RECORDS[:3]]
            manifest_path.write_text(json.dumps(manifest))
            tampered = workspace.check_workspace(root / 'maps', source=basic, require_runtime=True)
            self.assertEqual(tampered['failed'], 1)
            self.assertIn('mapping differs', tampered['maps'][0]['error'])
            sidecar.write_text(json.dumps({'visibility_modes': [0, 1, 2]}))
            third = workspace.check_workspace(root / 'maps', source=basic, require_runtime=True)
            self.assertEqual(third['passed'], 1, third)
            self.assertNotEqual(third['maps'][0]['runtime']['path'], runtime['path'])
            self.assertNotEqual(third['maps'][0]['runtime']['identity']['manifest_sha256'], runtime['identity']['manifest_sha256'])

    def test_missing_coordinate_defaults_and_invalid_declaration_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            basic = self.basic(root)
            raw = json.loads(basic.read_text())
            del raw['coordinate_system']
            raw['map_data'][0][0]['neighbors'][5] = 100
            raw['map_data'].append([{'pos': 100, 'cond': 0, 'elev': 0,
                'neighbors': [-1, -1, 0, -1, -1, -1], 'roads': [0] * 6, 'rivers': [0] * 6}])
            basic.write_text(json.dumps(raw))
            report = workspace.check_workspace(root / 'maps', source=basic)
            self.assertEqual(report['passed'], 1, report)
            self.assertEqual(report['maps'][0]['coordinate_system_source'], 'shared_odd_r_convention')
            self.assertEqual(report['maps'][0]['coordinate_geometry']['checked_diagonal_edges'], 2)
            cli = workspace.check_workspace(root / 'maps', source=basic, coordinate_system='odd-r')
            self.assertEqual(cli['maps'][0]['coordinate_system_source'], 'cli')
            for invalid in ('even-r', '', False, 0):
                raw['coordinate_system'] = invalid
                basic.write_text(json.dumps(raw))
                invalid_report = workspace.check_workspace(root / 'maps', source=basic, coordinate_system='odd-r')
                self.assertEqual(invalid_report['failed'], 1, invalid_report)

    def test_missing_coordinate_without_parity_evidence_requires_explicit_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            basic = self.basic(root)
            raw = json.loads(basic.read_text())
            del raw['coordinate_system']
            basic.write_text(json.dumps(raw))
            report = workspace.check_workspace(root / 'maps', source=basic, terrain_only=True)
            self.assertEqual(report['failed'], 1)
            self.assertIn('diagonal-neighbor evidence', report['maps'][0]['error'])
            explicit = workspace.check_workspace(root / 'maps', source=basic, terrain_only=True, coordinate_system='odd-r')
            self.assertEqual(explicit['passed'], 1)
            self.assertEqual(explicit['maps'][0]['coordinate_system_source'], 'cli')

    def test_odd_r_neighbor_geometry_checked_even_in_terrain_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            basic = self.basic(root)
            raw = json.loads(basic.read_text())
            del raw['coordinate_system']
            cell = raw['map_data'][0][0]
            cell['neighbors'][0] = 100  # East must stay on the same row.
            basic.write_text(json.dumps(raw))
            report = workspace.check_workspace(root / 'maps', source=basic, terrain_only=True)
            self.assertEqual(report['failed'], 1)
            self.assertIn('neighbor geometry', report['maps'][0]['error'])
            cell['neighbors'][0] = 1
            raw['coordinate_system'] = 'odd-r'
            raw['map_data'][0].append({'pos': 1, 'cond': 0, 'elev': 0,
                'neighbors': [-1, -1, -1, 0, -1, -1], 'roads': [0] * 6, 'rivers': [0] * 6})
            basic.write_text(json.dumps(raw))
            report = workspace.check_workspace(root / 'maps', source=basic)
            self.assertEqual(report['passed'], 1, report)
            self.assertEqual(report['maps'][0]['coordinate_geometry']['checked_directed_edges'], 2)

    def test_cache_mapping_identity_and_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            basic = self.basic(root, scale=200)
            (root / 'cost.pickle').write_bytes(b'movement')
            (root / 'tinysee.npz').write_bytes(b'visibility')
            native = workspace.convert_benchmark_basic_map(basic).native_map
            loaded = SimpleNamespace(identity_record=lambda: {'test': True},
                                     visibility=SimpleNamespace(mode_order=(0, 3, 4), close=lambda: None))
            def publish(*args, **kwargs):
                kwargs['output_dir'].mkdir(parents=True)
                loaded.visibility.mode_order = kwargs['visibility_modes'] or (0, 1, 2)
                loaded.visibility.shape = (3, 1, 1, 1, 1)
            with patch.object(workspace, 'publish_map_runtime_data', side_effect=publish) as publisher, \
                 patch.object(workspace.MapRuntimeData, 'load', return_value=loaded):
                first = workspace._runtime(native, basic, root / 'maps', visibility_modes=(0, 3, 4), mapping_source='cli')
                same = workspace._runtime(native, basic, root / 'maps', visibility_modes=(0, 3, 4), mapping_source='map-compatibility.json')
                other = workspace._runtime(native, basic, root / 'maps', visibility_modes=(0, 1, 2))
                default = workspace._runtime(native, basic, root / 'maps')
                self.assertFalse(first['cache_hit'])
                self.assertTrue(same['cache_hit'])
                self.assertEqual(first['path'], same['path'])
                self.assertEqual(len({first['path'], other['path'], default['path']}), 3)
                self.assertEqual(first['visibility_modes'], [0, 3, 4])
                self.assertEqual(first['missing_visibility_modes'], [1, 2, 5, 6, 7, 8, 9])
                self.assertEqual(publisher.call_args_list[0].kwargs['visibility_modes'], (0, 3, 4))


if __name__ == '__main__':
    unittest.main()
