"""Offline contract tests. All fixture sources are synthetic and never imported."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from devtools.health import (
    dispatch,
    function_schemas,
    inspect_release,
    main,
    summarize_test_report,
)

MANIFEST = """[project]
name = "fixture"
version = "1.0"
[project.scripts]
hello = "sample.cli:main"
[tool.setuptools.packages.find]
where = ["src"]
[tool.setuptools.package-data]
"sample" = ["static/*.txt"]
[tool.setuptools.data-files]
"config" = ["config/settings.json"]
"""


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.write("pyproject.toml", MANIFEST)
        self.write(
            "src/sample/cli.py",
            "raise RuntimeError('must not execute')\ndef main(): pass\n",
        )
        self.write("src/sample/static/readme.txt", "fixture")
        self.write("config/settings.json", "{}")

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return path

    def codes(self):
        return {finding["code"] for finding in inspect_release(self.root)["findings"]}

    def test_valid_release_without_importing_application(self):
        result = inspect_release(self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["checked"],
            {"entry_points": 1, "package_patterns": 1, "data_files": 1},
        )

    def test_missing_module(self):
        (self.root / "src/sample/cli.py").unlink()
        self.assertIn("missing_entry_module", self.codes())

    def test_package_entry_module(self):
        (self.root / "src/sample/cli.py").unlink()
        self.write("src/sample/cli/__init__.py", "def main(): pass")
        self.assertTrue(inspect_release(self.root)["ok"])

    def test_invalid_syntax(self):
        self.write("src/sample/cli.py", "def main(")
        self.assertIn("invalid_entry_source", self.codes())

    def test_reexport_is_not_assumed_valid(self):
        self.write("src/sample/cli.py", "from somewhere import main")
        self.assertIn("unresolved_entry_callable", self.codes())

    def test_invalid_target(self):
        self.write("pyproject.toml", MANIFEST.replace("sample.cli:main", "sample.cli"))
        self.assertIn("invalid_entry_point", self.codes())

    def test_missing_static_asset(self):
        (self.root / "src/sample/static/readme.txt").unlink()
        self.assertIn("empty_package_pattern", self.codes())

    def test_missing_data_file(self):
        (self.root / "config/settings.json").unlink()
        self.assertIn("missing_data_file", self.codes())

    def test_unsafe_pattern(self):
        self.write("pyproject.toml", MANIFEST.replace("static/*.txt", "../*.txt"))
        self.assertIn("unsafe_package_pattern", self.codes())

    def test_manifest_size_bound(self):
        with (
            patch("devtools.health.MAX_INPUT_BYTES", 10),
            self.assertRaises(ValueError),
        ):
            inspect_release(self.root)

    def test_report_counts_not_suite_claim(self):
        self.write(
            "result.xml",
            '<testsuites tests="999"><testsuite><testcase/><testcase><failure/></testcase><testcase><error/></testcase><testcase><skipped/></testcase></testsuite></testsuites>',
        )
        result = summarize_test_report(self.root, "result.xml")
        self.assertEqual(
            result["counts"],
            {"tests": 4, "passed": 1, "failures": 1, "errors": 1, "skipped": 1},
        )
        self.assertFalse(result["all_passed"])

    def test_all_passed(self):
        self.write("result.xml", "<testsuite><testcase/></testsuite>")
        self.assertTrue(summarize_test_report(self.root, "result.xml")["all_passed"])

    def test_empty_report_is_not_a_pass(self):
        self.write("result.xml", "<testsuite tests='5' failures='0'/>")
        self.assertFalse(summarize_test_report(self.root, "result.xml")["all_passed"])

    def test_skipped_only_is_not_all_passed(self):
        self.write(
            "result.xml", "<testsuite><testcase><skipped/></testcase></testsuite>"
        )
        self.assertFalse(summarize_test_report(self.root, "result.xml")["all_passed"])

    def test_conflicting_outcomes_count_once(self):
        self.write(
            "result.xml",
            "<testsuite><testcase><failure/><error/><skipped/></testcase></testsuite>",
        )
        counts = summarize_test_report(self.root, "result.xml")["counts"]
        self.assertEqual(counts["errors"], 1)
        self.assertEqual(counts["failures"], 0)

    def test_reject_entity_declarations(self):
        self.write("result.xml", '<!DOCTYPE a [<!ENTITY x "bad">]><testsuite/>')
        with self.assertRaises(ValueError):
            summarize_test_report(self.root, "result.xml")

    def test_reject_utf16(self):
        (self.root / "result.xml").write_bytes("<testsuite/>".encode("utf-16"))
        with self.assertRaises(ValueError):
            summarize_test_report(self.root, "result.xml")

    def test_reject_bomless_utf16_entities(self):
        payload = '<!DOCTYPE a [<!ENTITY x "expanded">]><testsuite><testcase name="&x;"/></testsuite>'
        for encoding in ("utf-16le", "utf-16be"):
            with self.subTest(encoding=encoding):
                (self.root / "result.xml").write_bytes(payload.encode(encoding))
                with self.assertRaises(ValueError):
                    summarize_test_report(self.root, "result.xml")

    def test_reject_non_utf8_declaration(self):
        self.write(
            "result.xml", '<?xml version="1.0" encoding="ISO-8859-1"?><testsuite/>'
        )
        with self.assertRaises(ValueError):
            summarize_test_report(self.root, "result.xml")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO requires POSIX")
    def test_reject_fifo_without_blocking(self):
        os.mkfifo(self.root / "result.xml")
        with self.assertRaises(ValueError):
            summarize_test_report(self.root, "result.xml")

    def test_explicit_package_list_reports_unsupported(self):
        self.write(
            "pyproject.toml",
            '[project]\nname="fixture"\n[tool.setuptools]\npackages=["sample"]',
        )
        with self.assertRaisesRegex(TypeError, "packages.find"):
            inspect_release(self.root)

    def test_reject_wrong_root(self):
        self.write("result.xml", "<other/>")
        with self.assertRaises(ValueError):
            summarize_test_report(self.root, "result.xml")

    def test_xml_element_bound(self):
        self.write("result.xml", "<testsuite><testcase/><testcase/></testsuite>")
        with (
            patch("devtools.health.MAX_XML_ELEMENTS", 2),
            self.assertRaises(ValueError),
        ):
            summarize_test_report(self.root, "result.xml")

    def test_reject_escape_and_wrong_types(self):
        for path in (
            "../result.xml",
            "/tmp/result.xml",
            "a\\b.xml",
            "",
            3,
            "result.json",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                summarize_test_report(self.root, path)

    def test_reject_symlink_escape(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "result.xml"
            target.write_text("<testsuite/>")
            (self.root / "result.xml").symlink_to(target)
            with self.assertRaises(ValueError):
                summarize_test_report(self.root, "result.xml")

    def test_function_allowlist_and_argument_contract(self):
        self.assertTrue(dispatch(self.root, "inspect_release", {})["ok"])
        for name, args in (
            ("run", {}),
            ("inspect_release", {"command": "anything"}),
            ("inspect_release", []),
            ("summarize_test_report", {}),
            ("summarize_test_report", {"report": "result.xml", "root": "/"}),
        ):
            with (
                self.subTest(name=name, args=args),
                self.assertRaises((ValueError, TypeError)),
            ):
                dispatch(self.root, name, args)

    def test_schemas_are_fresh_json_and_strict(self):
        schemas = function_schemas()
        json.dumps(schemas)
        self.assertEqual(len(schemas), 2)
        schemas[0]["parameters"]["properties"]["unsafe"] = {}
        self.assertEqual(function_schemas()[0]["parameters"]["properties"], {})
        for schema in function_schemas():
            self.assertTrue(schema["strict"])
            self.assertFalse(schema["parameters"]["additionalProperties"])

    def test_cli_exit_codes_and_json(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--root", str(self.root), "release"]), 0)
        self.assertTrue(json.loads(output.getvalue())["ok"])
        (self.root / "config/settings.json").unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--root", str(self.root), "release"]), 1)
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(
                main(["--root", str(self.root), "junit", "--report", "missing.xml"]), 2
            )
        self.assertIn("error", json.loads(errors.getvalue()))


if __name__ == "__main__":
    unittest.main()
