"""Prepare final-source-with-stdlib-copy ablation without changing a live checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

_RELATIVE_HELPER = Path("src/wargame_native_engine/io/detached.py")
_FAST_RETURN = "    return _copy_value(value, {})\n"
_STDLIB_RETURN = "    return deepcopy(value)\n"


def package_hash(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("."))
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    destination = args.destination.resolve()
    if destination.exists():
        parser.error("destination must not exist; existing data is never overwritten")
    if source == destination or source in destination.parents:
        parser.error("destination must be outside the source checkout")
    helper = (source / _RELATIVE_HELPER).read_text()
    if helper.count(_FAST_RETURN) != 1:
        parser.error("copy helper changed; review the ablation recipe before continuing")
    destination.mkdir(parents=True)
    for component in ("src", "config", "docs", "examples"):
        shutil.copytree(
            source / component,
            destination / component,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    copied_helper = destination / _RELATIVE_HELPER
    copied_helper.write_text(helper.replace(_FAST_RETURN, _STDLIB_RETURN))
    manifest = {
        "ablation": "final source with detached_copy delegating to stdlib deepcopy",
        "note": "Includes final lifecycle/replay/interface fixes, not a JSON-only patch.",
        "changed_file": _RELATIVE_HELPER.as_posix(),
        "exact_replacement": {"before": _FAST_RETURN, "after": _STDLIB_RETURN},
        "original_helper_sha256": hashlib.sha256(helper.encode()).hexdigest(),
        "ablation_helper_sha256": hashlib.sha256(copied_helper.read_bytes()).hexdigest(),
        "final_source_tree_sha256": package_hash(source / "src/wargame_native_engine"),
        "ablation_source_tree_sha256": package_hash(
            destination / "src/wargame_native_engine"
        ),
    }
    (destination / "ablation.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
