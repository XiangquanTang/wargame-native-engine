"""Bounded, read-only repository diagnostics and function-call adapter.

No application modules are imported. Paths are confined to the selected checkout.
The adapter intentionally has no shell, network, simulator or action tools.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import stat
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10, installed by the development extra.
    import tomli as tomllib

MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_XML_ELEMENTS = 100_000


@dataclass(frozen=True)
class Finding:
    code: str
    path: str
    message: str


def _relative_path(root: Path, relative: str) -> Path:
    """Resolve a portable checkout-relative path without allowing escapes."""
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("Expected a nonempty POSIX relative path")
    part = PurePosixPath(relative)
    if part.is_absolute() or ".." in part.parts:
        raise ValueError("Path must stay within the checkout")
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError("Symlink escapes the checkout")
    return candidate


def _read_bounded(path: Path) -> bytes:
    """Read at most the limit plus one byte, including if the file grows."""
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Expected a regular file")
    with path.open("rb") as stream:
        content = stream.read(MAX_INPUT_BYTES + 1)
    if len(content) > MAX_INPUT_BYTES:
        raise ValueError(f"Input exceeds {MAX_INPUT_BYTES} bytes")
    return content


def inspect_release(root: Path) -> dict[str, Any]:
    """Check declared console entry points and package/data-file presence.

    This is a source-tree check, not installation or runtime certification.
    """
    root = root.resolve()
    manifest = tomllib.loads(
        _read_bounded(_relative_path(root, "pyproject.toml")).decode("utf-8")
    )
    findings: list[Finding] = []
    project = manifest.get("project", {})
    tool = manifest.get("tool", {})
    if not isinstance(project, dict) or not isinstance(tool, dict):
        raise TypeError("project and tool must be TOML tables")
    setuptools = tool.get("setuptools", {})
    if not isinstance(setuptools, dict):
        raise TypeError("tool.setuptools must be a TOML table")
    package_config = setuptools.get("packages", {})
    if not isinstance(package_config, dict):
        raise TypeError(
            "Release check currently supports setuptools packages.find only"
        )
    find_config = package_config.get("find", {})
    if not isinstance(find_config, dict):
        raise TypeError("packages.find must be a TOML table")
    source_roots = find_config.get("where", ["."])
    if not isinstance(source_roots, list) or not all(
        isinstance(item, str) for item in source_roots
    ):
        raise ValueError("setuptools packages.find.where must be a list of paths")
    checked_entries = 0
    checked_patterns = 0
    checked_data_files = 0

    def fail(code: str, path: str, message: str) -> None:
        findings.append(Finding(code, path, message))

    scripts = project.get("scripts", {})
    package_data = setuptools.get("package-data", {})
    data_files = setuptools.get("data-files", {})
    if not all(
        isinstance(table, dict) for table in (scripts, package_data, data_files)
    ):
        raise TypeError("scripts, package-data and data-files must be TOML tables")
    for table in (package_data, data_files):
        if not all(
            isinstance(items, list) and all(isinstance(item, str) for item in items)
            for items in table.values()
        ):
            raise TypeError("Resource declarations must be lists of strings")

    for name, target in sorted(scripts.items()):
        checked_entries += 1
        if not isinstance(target, str) or target.count(":") != 1:
            fail("invalid_entry_point", str(name), "Expected module:function")
            continue
        module, function = target.split(":")
        if (
            not all(segment.isidentifier() for segment in module.split("."))
            or not function.isidentifier()
        ):
            fail(
                "invalid_entry_point",
                str(name),
                "Expected a module and top-level callable name",
            )
            continue
        candidates = []
        for source in source_roots:
            base = f"{source}/{module.replace('.', '/')}"
            candidates.extend(
                [
                    _relative_path(root, base + ".py"),
                    _relative_path(root, base + "/__init__.py"),
                ]
            )
        path = next(
            (candidate for candidate in candidates if candidate.is_file()), None
        )
        if path is None:
            fail("missing_entry_module", str(name), f"Module not found: {module}")
            continue
        display = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(_read_bounded(path), filename=display)
        except (SyntaxError, UnicodeError, ValueError) as error:
            fail("invalid_entry_source", display, str(error))
            continue
        declared = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        # Re-exported callables cannot be verified without importing application code.
        if function not in declared:
            fail(
                "unresolved_entry_callable",
                display,
                f"No top-level definition for {function}; inspect re-exports manually",
            )

    for package, patterns in sorted(package_data.items()):
        for pattern in patterns:
            checked_patterns += 1
            if (
                PurePosixPath(pattern).is_absolute()
                or ".." in PurePosixPath(pattern).parts
                or "\\" in pattern
            ):
                fail(
                    "unsafe_package_pattern",
                    str(package),
                    "Pattern must stay within its package",
                )
                continue
            matched = False
            for source in source_roots:
                package_dir = _relative_path(
                    root, f"{source}/{package.replace('.', '/')}"
                )
                for match in package_dir.glob(pattern):
                    resolved = _relative_path(root, match.relative_to(root).as_posix())
                    if resolved.is_file():
                        matched = True
                        break
                if matched:
                    break
            if not matched:
                fail("empty_package_pattern", str(package), f"No files match {pattern}")

    for destination, files in sorted(data_files.items()):
        for relative in files:
            checked_data_files += 1
            if not _relative_path(root, relative).is_file():
                fail("missing_data_file", relative, f"Declared for {destination}")

    return {
        "schema_version": 1,
        "check": "source_release_declarations",
        "ok": not findings,
        "project": {"name": project.get("name"), "version": project.get("version")},
        "checked": {
            "entry_points": checked_entries,
            "package_patterns": checked_patterns,
            "data_files": checked_data_files,
        },
        "findings": [asdict(finding) for finding in findings],
        "limitations": [
            "Does not import application code, run tests, build a wheel or certify runtime behavior."
        ],
    }


def summarize_test_report(root: Path, report: str) -> dict[str, Any]:
    """Summarize actual JUnit testcase elements, not unverified suite totals."""
    path = _relative_path(root.resolve(), report)
    if path.suffix.lower() != ".xml":
        raise ValueError("Expected a JUnit .xml report")
    data = _read_bounded(path)
    # Reject declarations before parsing; UTF-8-only also prevents UTF-16 bypasses.
    document = data.decode("utf-8-sig")
    if "\x00" in document:
        raise ValueError("NUL bytes and non-UTF-8 XML are not supported")
    declaration = re.match(
        r"<\?xml\s+[^?]*encoding\s*=\s*['\"]([^'\"]+)['\"]", document, re.IGNORECASE
    )
    if declaration and declaration.group(1).lower() not in {
        "utf-8",
        "utf8",
        "us-ascii",
    }:
        raise ValueError("XML declaration must specify UTF-8 or US-ASCII")
    if "<!DOCTYPE" in document.upper() or "<!ENTITY" in document.upper():
        raise ValueError("DTD and entity declarations are not supported")
    counts = {"tests": 0, "passed": 0, "failures": 0, "errors": 0, "skipped": 0}
    elements = 0
    for event, element in ET.iterparse(io.StringIO(document), events=("start", "end")):
        if event == "start":
            if elements == 0 and element.tag not in {"testsuite", "testsuites"}:
                raise ValueError("Expected a JUnit testsuite or testsuites root")
            elements += 1
            if elements > MAX_XML_ELEMENTS:
                raise ValueError("XML contains too many elements")
            continue
        if element.tag != "testcase":
            continue
        counts["tests"] += 1
        tags = {child.tag for child in element}
        # Each testcase counts once, even if a producer adds multiple outcomes.
        outcome = next(
            (tag for tag in ("error", "failure", "skipped") if tag in tags), None
        )
        category = {
            "error": "errors",
            "failure": "failures",
            "skipped": "skipped",
            None: "passed",
        }[outcome]
        counts[category] += 1
        element.clear()
    return {
        "schema_version": 1,
        "report": report,
        "counts": counts,
        "has_testcases": counts["tests"] > 0,
        "all_passed": counts["tests"] > 0 and counts["passed"] == counts["tests"],
        "limitations": [
            "Reports recorded outcomes only; does not execute tests or establish report freshness/coverage."
        ],
    }


def function_schemas() -> list[dict[str, Any]]:
    """Return fresh JSON-compatible function definitions for read-only host tools."""
    return [
        {
            "type": "function",
            "name": "inspect_release",
            "description": "Inspect source release declarations without executing application code.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "summarize_test_report",
            "description": "Read a bounded checkout-relative JUnit XML report; never run tests.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "report": {
                        "type": "string",
                        "description": "Relative POSIX path to a JUnit XML report.",
                    }
                },
                "required": ["report"],
                "additionalProperties": False,
            },
        },
    ]


def dispatch(root: Path, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Allowlist function calls; the trusted host selects the checkout root."""
    if not isinstance(arguments, dict):
        raise TypeError("Arguments must be a JSON object")
    if name == "inspect_release" and not arguments:
        return inspect_release(root)
    if name == "summarize_test_report" and set(arguments) == {"report"}:
        return summarize_test_report(root, arguments["report"])
    raise ValueError("Unknown function or unexpected arguments")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path.cwd(), help="Trusted checkout root"
    )
    parser.add_argument("command", choices=["release", "junit", "schemas"])
    parser.add_argument("--report", help="Checkout-relative JUnit XML path")
    options = parser.parse_args(argv)
    try:
        if options.command == "schemas":
            result: Any = function_schemas()
        elif options.command == "release":
            result = inspect_release(options.root)
        else:
            if options.report is None:
                parser.error("junit requires --report")
            result = summarize_test_report(options.root, options.report)
    except (OSError, ValueError, TypeError, ET.ParseError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if options.command == "release":
        return 0 if result["ok"] else 1
    if options.command == "junit":
        return 0 if result["all_passed"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
