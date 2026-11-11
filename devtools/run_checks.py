"""Run the maintained regression suites from any working directory.

This is a developer command, not an Agent tool. It does not train models.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    search_paths = [str(root / "src"), str(root)]
    if environment.get("PYTHONPATH"):
        search_paths.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(search_paths)
    failed = False
    for suite in ("devtools/checks", "checks"):
        print(f"\n=== {suite} ===", flush=True)
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", suite, "-v"],
            cwd=root,
            env=environment,
            check=False,
        )
        failed |= result.returncode != 0
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
