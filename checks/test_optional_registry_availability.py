"""Optional historical catalogs must not advertise unshipped certifications."""

import json
from pathlib import Path
import tempfile
import unittest

from wargame_native_engine.capability_registry import (
    CAPABILITY_REGISTRY_SCHEMA,
    CapabilityRegistry,
    load_default_capability_registry,
)
from wargame_native_engine.demo_registry import (
    DEMO_EXPECTED_SCHEMA,
    DEMO_REGISTRY_SCHEMA,
    DemoRegistry,
    DemoRegistryError,
    _default_known_capability_ids,
    load_default_demo_registry,
)


ROOT = Path(__file__).resolve().parents[1]


class OptionalRegistryAvailabilityTests(unittest.TestCase):
    def test_default_catalog_advertises_no_unshipped_demo_certifications(self):
        path = ROOT / "config/capabilities/native_capability_registry_v1.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record, {
            "schema": CAPABILITY_REGISTRY_SCHEMA,
            "capabilities": [],
        })
        self.assertEqual(load_default_capability_registry().descriptors, ())
        self.assertEqual(CapabilityRegistry.load(path).descriptors, ())
        # No demo certification is not a declaration of zero playable maps.
        catalog = json.loads((ROOT / "examples/maps/catalog.json").read_text())
        self.assertTrue(catalog["maps"])

    def test_unshipped_default_demo_library_has_explicit_domain_error(self):
        self.assertFalse((ROOT / "examples/demos/registry.json").exists())
        with self.assertRaisesRegex(DemoRegistryError, "not included in this release"):
            load_default_demo_registry()

    def test_explicit_external_demo_registry_remains_supported(self):
        self._check_external_registry("custom", ("custom",))

    def test_external_demo_catalog_retains_implicit_legacy_vocabulary(self):
        expected = (
            "boarding", "concealment", "direct_fire", "drone", "guided_fire",
            "indirect_fire", "loitering_munition", "objective_control", "visibility",
        )
        self.assertEqual(_default_known_capability_ids(), expected)
        for capability in expected:
            with self.subTest(capability=capability):
                self._check_external_registry(capability, None)

    def _check_external_registry(self, capability, known):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scenario.json").write_text("{}", encoding="utf-8")
            (root / "README.md").write_text("Custom demo", encoding="utf-8")
            (root / "expected.json").write_text(json.dumps({
                "schema": DEMO_EXPECTED_SCHEMA, "demo_id": "custom",
            }), encoding="utf-8")
            path = root / "registry.json"
            path.write_text(json.dumps({
                "schema": DEMO_REGISTRY_SCHEMA,
                "demos": [{
                    "id": "custom", "name": "Custom", "purpose": "Test adapter",
                    "scenario": "scenario.json", "expected_behavior": "expected.json",
                    "readme": "README.md", "features": ["CUSTOM"],
                    "capability_ids": [capability], "expected": {},
                }],
            }), encoding="utf-8")
            registry = DemoRegistry.load(path, known_capability_ids=known)
            self.assertEqual(registry.resolve("custom").demo_id, "custom")
            self.assertEqual(registry.resolve("custom").capability_ids, (capability,))
            self.assertEqual(registry.scenario_path("custom"), root / "scenario.json")


if __name__ == "__main__":
    unittest.main()
