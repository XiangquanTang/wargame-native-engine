"""Static previews retain map facts and use the existing shared renderer."""

from copy import deepcopy
import json
from pathlib import Path
import re
import unittest

from wargame_native_engine.io.map_preview import render_map_preview
from wargame_native_engine.io.structured_map import load_structured_map

ROOT = Path(__file__).resolve().parents[1]


def embedded_map(html):
    return json.loads(re.search(r'<script id="map-data" type="application/json">(.*?)</script>',
                               html, re.S).group(1))


class MapPreviewTests(unittest.TestCase):
    def setUp(self):
        self.document = load_structured_map(ROOT / "examples/map123/scenario.json",
                                            input_format="native-scenario")

    def test_renderer_is_shared_and_facts_remain_unchanged(self):
        before = deepcopy(self.document)
        html = render_map_preview(self.document)
        projected = embedded_map(html)
        self.assertEqual(projected["rows"], 100)
        self.assertEqual(projected["columns"], 100)
        self.assertEqual(len(projected["cells"]), self.document["cell_count"])
        self.assertEqual(projected["elevation_kind"], self.document["elevation"]["kind"])
        self.assertEqual(projected["elevation_unit_m"], self.document["elevation"]["unit_m"])
        for source, cell in zip(self.document["cells"], projected["cells"]):
            self.assertEqual((cell["column"], cell["row"], cell["elevation"]),
                             (source[0], source[1], source[6]))
            self.assertEqual(cell["roadblock"], source[9])
        renderer = (ROOT / "src/wargame_native_engine/viewer/static/map_renderer.js").read_text()
        self.assertIn(renderer, html)
        self.assertNotIn('<script src=', html)
        self.assertEqual(self.document, before)

    def test_html_escaping_and_no_cascading_substitution(self):
        self.document["map_id"] = '</script><img src=x onerror="alert(1)">__SHARED_RENDERER__'
        html = render_map_preview(self.document, title="<img onerror=x> __MAP_JSON__")
        self.assertNotIn('<img', html)
        self.assertIn('&lt;img onerror=x&gt; __MAP_JSON__', html)
        self.assertEqual(embedded_map(html)["map_id"], self.document["map_id"])

    def test_schema_and_rows_rejected(self):
        for mutate in (lambda d: d.update(schema_version="bad"),
                       lambda d: d.update(cells=[]),
                       lambda d: d.update(coordinates=None),
                       lambda d: d.update(elevation=None),
                       lambda d: d["cells"].append(d["cells"][0]),
                       lambda d: d["cells"][0].__setitem__(6, float("nan")),
                       lambda d: d["cells"][0].__setitem__(11, [True] * 6)):
            with self.subTest(mutate=mutate):
                document = deepcopy(self.document)
                mutate(document)
                with self.assertRaises(ValueError):
                    render_map_preview(document)

    def test_sparse_bounds_and_null_elevation(self):
        self.document["cells"] = [self.document["cells"][501]]
        self.document["cells"][0][6] = None
        result = embedded_map(render_map_preview(self.document))
        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["columns"], 1)
        self.assertEqual(result["bounds"], {"min_row": 5, "max_row": 5,
                                            "min_column": 1, "max_column": 1})
        self.assertEqual(result["elevation"]["mode"], "unavailable")


if __name__ == "__main__":
    unittest.main()
