"""Keep shared metadata compatible without coupling the Engine to the Viewer."""

from dataclasses import FrozenInstanceError
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

from wargame_native_engine.shareddata import operator_registry as shared
from wargame_native_engine.viewer import operator_registry as legacy


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "config/source_compat/operator_registry_v1.json"
PUBLIC_NAMES = (
    "OPERATOR_DESCRIPTOR_SCHEMA",
    "OPERATOR_REGISTRY_SCHEMA",
    "OperatorDescriptor",
    "OperatorIdentity",
    "OperatorRegistry",
    "OperatorRegistryError",
    "OperatorSymbol",
    "WeaponDescriptor",
    "default_operator_registry_path",
    "load_operator_registry",
    "resolve_operator",
)


class OperatorRegistryCompatibilityTests(unittest.TestCase):
    def test_legacy_public_names_are_the_same_shared_objects(self):
        self.assertEqual(set(legacy.__all__), set(PUBLIC_NAMES))
        for name in PUBLIC_NAMES:
            with self.subTest(name=name):
                self.assertIs(getattr(legacy, name), getattr(shared, name))

    def test_default_path_and_cached_registry_are_unchanged(self):
        self.assertEqual(shared.default_operator_registry_path(), REGISTRY_PATH)
        self.assertEqual(legacy.default_operator_registry_path(), REGISTRY_PATH)
        registry = shared.load_operator_registry()
        self.assertIs(registry, legacy.load_operator_registry())
        self.assertIs(registry, legacy.load_operator_registry(str(REGISTRY_PATH)))
        self.assertIsInstance(registry, legacy.OperatorRegistry)

    def test_explicit_path_aliases_reuse_one_loader_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            path.write_bytes(REGISTRY_PATH.read_bytes())
            registry = shared.load_operator_registry(path)
            # A cached path retains its original immutable value, even when the
            # file changes. Both import paths must share that existing behavior.
            path.write_text("not JSON", encoding="utf-8")
            alias = path.parent / ".." / path.parent.name / path.name
            self.assertIs(registry, legacy.load_operator_registry(str(alias)))
            self.assertIs(registry, shared.load_operator_registry(path))

    def test_legacy_exception_type_catches_shared_validation_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(legacy.OperatorRegistryError):
                shared.load_operator_registry(path)

    def test_descriptors_stay_immutable_and_records_stay_detached(self):
        registry = shared.load_operator_registry()
        operator = next(iter(registry.operators.values()))
        weapon = next(iter(registry.weapon_profiles.values()))
        self.assertIsInstance(operator, legacy.OperatorDescriptor)
        self.assertIsInstance(operator.identity, legacy.OperatorIdentity)
        self.assertIsInstance(operator.symbol, legacy.OperatorSymbol)
        self.assertIsInstance(weapon, legacy.WeaponDescriptor)
        with self.assertRaises(FrozenInstanceError):
            operator.display_name = "changed"
        with self.assertRaises(TypeError):
            registry.operators[operator.key] = operator
        with self.assertRaises(TypeError):
            registry.weapon_profiles[weapon.weapon_id] = weapon
        with self.assertRaises(TypeError):
            registry.provenance["changed"] = "changed"
        record = registry.to_record()
        record["operators"][operator.key]["symbol"]["modifiers"].append("changed")
        record["weapon_profiles"][str(weapon.weapon_id)]["name"] = "changed"
        self.assertEqual(shared.load_operator_registry().to_record(), registry.to_record())
        self.assertNotEqual(record, registry.to_record())
        self.assertNotIn("changed", operator.symbol.modifiers)
        self.assertNotEqual(weapon.name, "changed")

    def test_engine_catalog_runs_without_importing_any_viewer_module(self):
        # A clean interpreter prevents earlier UI tests from masking a cycle or
        # satisfying a forbidden import from their process-wide module cache.
        script = textwrap.dedent("""
            import importlib.abc
            from pathlib import Path
            import sys

            root = Path(sys.argv[1])
            sys.path.insert(0, str(root / "src"))

            class RejectViewerImports(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "wargame_native_engine.viewer" or fullname.startswith(
                        "wargame_native_engine.viewer."
                    ):
                        raise AssertionError(f"Engine imported Viewer module: {fullname}")
                    return None

            sys.meta_path.insert(0, RejectViewerImports())
            from wargame_native_engine.core.engine import NativeEngine
            from wargame_native_engine.io.scenario import load_scenario

            engine = NativeEngine(load_scenario(root / "examples/source_movement_scenario.json"))
            try:
                engine.reset(seed=42)
                catalog = engine.get_weapon_catalog()
                assert catalog["weapons"], "Engine weapon catalog must not be empty"
                assert catalog["sources"]["operator_registry_id"]
                assert not any(
                    name == "wargame_native_engine.viewer"
                    or name.startswith("wargame_native_engine.viewer.")
                    for name in sys.modules
                ), "Engine must not initialize Viewer modules"
            finally:
                engine.close()
        """)
        result = subprocess.run(
            [sys.executable, "-I", "-c", script, str(ROOT)],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
