"""Strict read-only resource lookup for Viewer operator symbols."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from wargame_native_engine.viewer.operator_registry import (
    default_operator_registry_path,
)


# Only the exact, reviewed mount bootstrap in the three HTML entrypoints may
# execute inline. Regression tests bind this digest to their actual contents;
# do not replace it with unsafe-inline when changing the bootstrap.
VIEWER_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'sha256-9ai124sh/zCHKSnP2Lg0ShtB2mJMgU4h3IAq7idM9+U='; "
    "style-src 'self'; img-src 'self' blob:; base-uri 'self'"
)

_SYMBOL_PATH = re.compile(
    r"^/static/assets/unit_symbols/(base|modifiers)/([a-z0-9_]+\.svg)$"
)


@dataclass(frozen=True, slots=True)
class ViewerStaticResource:
    payload: bytes
    content_type: str


def load_symbol_static_resource(
    request_path: str,
    *,
    static_root: str | Path | None = None,
    registry_path: str | Path | None = None,
) -> ViewerStaticResource | None:
    """Return one whitelisted Registry/SVG resource without path guessing."""

    if request_path == "/static/operator_registry_v1.json":
        path = (
            default_operator_registry_path()
            if registry_path is None
            else Path(registry_path)
        )
        if not path.is_file():
            return None
        return ViewerStaticResource(
            payload=path.read_bytes(),
            content_type="application/json; charset=utf-8",
        )

    matched = _SYMBOL_PATH.fullmatch(request_path)
    if matched is None:
        return None
    root = (
        Path(__file__).with_name("static")
        if static_root is None
        else Path(static_root)
    )
    category, filename = matched.groups()
    path = root / "assets" / "unit_symbols" / category / filename
    if not path.is_file():
        return None
    return ViewerStaticResource(
        payload=path.read_bytes(),
        content_type="image/svg+xml; charset=utf-8",
    )
