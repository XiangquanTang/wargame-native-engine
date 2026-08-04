from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.io.serialization import json_clone


def write_snapshot(path: str | Path, value: Mapping[str, Any]) -> None:
    Path(path).write_text(json.dumps(json_clone(value), ensure_ascii=False,
                                    sort_keys=True, indent=2) + "\n",
                          encoding="utf-8")


def read_snapshot(path: str | Path) -> Mapping[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("snapshot root must be an object")
    return value
