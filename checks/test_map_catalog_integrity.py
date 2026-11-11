"""Release map declarations stay synchronized with their actual scenario assets."""
import unittest
from unittest.mock import patch

from scripts import run_maps


class MapCatalogIntegrityTests(unittest.TestCase):
    def test_all_assets_and_scenario_identities_match_catalog(self):
        catalog = run_maps._catalog()
        self.assertEqual(run_maps.verify(catalog, catalog['maps']), catalog['data_file_count'])
        self.assertEqual(sum(item['bytes'] for item in catalog['files']), catalog['data_bytes'])
        self.assertEqual(sum((run_maps.ROOT / item['path']).stat().st_size
                             for item in catalog['files']), catalog['data_bytes'])

    def test_map123_run_dispatch_survives_catalog_preflight(self):
        catalog = run_maps._catalog()
        map123 = next(item for item in catalog['maps'] if item['id'] == 'map123')
        with patch('scripts.run_map123.main', return_value=0) as launch:
            self.assertEqual(run_maps.main(['run', 'map123', '--port', '8765']), 0)
            launch.assert_called_once_with(['--scenario', map123['default_variant'], '--port', '8765'])


if __name__ == '__main__':
    unittest.main()
