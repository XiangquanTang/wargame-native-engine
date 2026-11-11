"""Partial canonical LOS profiles preserve ordered, exact lookup semantics."""
import json
from pathlib import Path
import pickle
import shutil
import struct
import tempfile
import unittest
from zipfile import ZipFile, ZIP_DEFLATED

from wargame_native_engine.data.map_runtime import (
    MapRuntimeData, MapRuntimeDataError, VISIBILITY_MODE_RECORDS,
    publish_map_runtime_data, prepare_map_runtime_data,
)
from wargame_native_engine.io.scenario import NativeMap, NativeHex


class VisibilityChannelCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.native = NativeMap('tiny', 100, 100, hexes=(
            NativeHex(0, 0, 0, (1, -1, -1, -1, -1, -1)),
            NativeHex(1, 0, 0, (-1, -1, -1, 0, -1, -1))),
            hex_size_m=200, elevation_kind='METERS', elevation_unit_m=10)
        self.basic = self.root / 'basic.json'
        self.basic.write_text(json.dumps({'map_id': 'tiny'}))
        self.cost = self.root / 'cost.pickle'
        self.cost.write_bytes(pickle.dumps([[[{1: 1}, {0: 1}]]] * 4, protocol=3))

    def source(self, count, shape=None):
        path = self.root / f'{count}see.npz'
        shape = shape or (count, 1, 2, 1, 2)
        header = repr({'descr': '|b1', 'fortran_order': False, 'shape': shape}).encode()
        header += b' ' * ((64 - (10 + len(header) + 1) % 64) % 64) + b'\n'
        # Each channel: 0->0 False; 0->1 True; 1->0 False; 1->1 True.
        payload = b'\x93NUMPY\x01\x00' + struct.pack('<H', len(header)) + header + bytes([0, 1, 0, 1]) * count
        with ZipFile(path, 'w', ZIP_DEFLATED) as out:
            out.writestr('data.npy', payload)
        return path

    def publish(self, count, modes=None, name='output'):
        return publish_map_runtime_data(self.native, source_map_path=self.basic,
            movement_cost_path=self.cost, visibility_npz_path=self.source(count),
            output_dir=self.root / name, visibility_modes=modes)

    def test_all_prefix_counts_and_missing_pairs(self):
        for count in range(1, 11):
            with self.subTest(count=count):
                pub = self.publish(count, name=f'out-{count}')
                runtime = MapRuntimeData.load(pub.output_dir, self.native)
                try:
                    self.assertEqual(runtime.visibility.mode_order, tuple(range(count)))
                    for mode, first, second, _ in VISIBILITY_MODE_RECORDS:
                        kwargs = dict(observer_height_m=first, target_height_m=second)
                        if mode < count:
                            self.assertTrue(runtime.visibility.visible(0, 1, **kwargs))
                            self.assertFalse(runtime.visibility.visible(1, 0, **kwargs))
                        else:
                            with self.assertRaisesRegex(MapRuntimeDataError, 'missing_visibility_mode'):
                                runtime.visibility.visible(0, 1, **kwargs)
                finally:
                    runtime.visibility.close()

    def test_permuted_subset_and_reverse_coordinates(self):
        pub = self.publish(3, [9, 2, 0])
        runtime = MapRuntimeData.load(pub.output_dir, self.native)
        self.addCleanup(runtime.visibility.close)
        # Ground observer at 1, low target at 0 maps to channel 2, ordered 0->1.
        self.assertTrue(runtime.visibility.visible(1, 0, observer_height_m=0, target_height_m=200))
        self.assertFalse(runtime.visibility.visible(0, 1, observer_height_m=0, target_height_m=200))
        self.assertTrue(runtime.visibility.visible(0, 1, observer_height_m=500, target_height_m=500))
        evidence = runtime.visibility.line_of_sight(1, 0, observer_height_m=0, target_height_m=200)
        self.assertEqual(evidence.modifiers[0]['mode'], 2)
        self.assertEqual(evidence.modifiers[0]['first_hex'], 0)

    def test_permutation_uses_distinct_physical_channels(self):
        source = self.source(3)
        with ZipFile(source) as archive:
            payload = archive.read('data.npy')
        # Physical channel 0 (canonical 9): 0->1 true.
        # Physical channel 1 (canonical 2): 1->0 true.
        # Physical channel 2 (canonical 0): all false.
        with ZipFile(source, 'w', ZIP_DEFLATED) as archive:
            archive.writestr('data.npy', payload[:-12] + bytes([0,1,0,0, 0,0,1,0, 0,0,0,0]))
        pub = publish_map_runtime_data(self.native, source_map_path=self.basic,
            movement_cost_path=self.cost, visibility_npz_path=source,
            output_dir=self.root / 'distinct', visibility_modes=[9,2,0])
        runtime = MapRuntimeData.load(pub.output_dir, self.native)
        self.addCleanup(runtime.visibility.close)
        self.assertTrue(runtime.visibility.visible(0, 1, observer_height_m=500, target_height_m=500))
        self.assertFalse(runtime.visibility.visible(0, 1, observer_height_m=200, target_height_m=0))
        self.assertTrue(runtime.visibility.visible(0, 1, observer_height_m=0, target_height_m=200))
        self.assertFalse(runtime.visibility.visible(0, 1, observer_height_m=0, target_height_m=0))

    def test_invalid_mapping_and_shapes(self):
        for modes in ([0, 0, 2], [0, 1], [0, 1, 10], [0, 1, True], [0, 1, '2'], '012'):
            with self.subTest(modes=modes), self.assertRaisesRegex(MapRuntimeDataError, 'invalid_visibility_modes'):
                self.publish(3, modes)
            self.assertFalse((self.root / 'output').exists())
        for count in (0, 11):
            with self.subTest(count=count), self.assertRaises(MapRuntimeDataError):
                self.publish(count)

    def test_manifest_mapping_tamper_is_rejected(self):
        pub = self.publish(3)
        manifest = json.loads(pub.manifest_path.read_text())
        manifest['visibility']['mode_order'][0]['first_height_m'] = 500
        pub.manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(MapRuntimeDataError, 'visibility_manifest_mismatch'):
            MapRuntimeData.load(pub.output_dir, self.native)

    def test_cold_cache_partial_profile_and_reuse(self):
        pub = self.publish(3, [9, 2, 0])
        shutil.copy2(self.root / '3see.npz', pub.output_dir / '3see.npz')
        first = prepare_map_runtime_data(self.native, release_dir=pub.output_dir, cache_root=self.root / 'cache')
        second = prepare_map_runtime_data(self.native, release_dir=pub.output_dir, cache_root=self.root / 'cache')
        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.assertEqual(first.identity, second.identity)
        runtime = MapRuntimeData.load(first.cache_dir, self.native)
        self.addCleanup(runtime.visibility.close)
        self.assertEqual(runtime.visibility.mode_order, (9, 2, 0))

    def test_cold_cache_invalid_mode_records_have_stable_error(self):
        pub = self.publish(3)
        shutil.copy2(self.root / '3see.npz', pub.output_dir / '3see.npz')
        original = json.loads(pub.manifest_path.read_text())
        for value in (None, 7, True, {}, []):
            manifest = json.loads(json.dumps(original))
            manifest['visibility']['mode_order'] = value
            pub.manifest_path.write_text(json.dumps(manifest))
            with self.subTest(value=value), self.assertRaises(MapRuntimeDataError):
                prepare_map_runtime_data(self.native, release_dir=pub.output_dir, cache_root=self.root / 'cache')

    def test_mapping_binds_manifest_not_payload_and_legacy_identity_keys(self):
        a = self.publish(3, [0, 1, 2], 'a')
        b = self.publish(3, [9, 2, 0], 'b')
        self.assertNotEqual(a.manifest_sha256, b.manifest_sha256)
        self.assertEqual(a.visibility_path.read_bytes(), b.visibility_path.read_bytes())
        ten = self.publish(10, name='ten')
        manifest = json.loads(ten.manifest_path.read_text())
        self.assertNotIn('mapping_provenance', manifest['visibility'])
        runtime = MapRuntimeData.load(ten.output_dir, self.native)
        self.addCleanup(runtime.visibility.close)
        self.assertEqual(set(runtime.identity_record()), {'schema', 'map_id', 'map_sha256', 'manifest_sha256',
            'movement_source_sha256', 'movement_payload_sha256', 'visibility_source_sha256', 'visibility_payload_sha256'})

if __name__ == '__main__':
    unittest.main()
