"""Standard-library HTTP server for one immutable replay view."""

from __future__ import annotations

from collections import OrderedDict
import gzip
from threading import RLock
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from wargame_native_engine.viewer.replay_view import ReplayView
from wargame_native_engine.viewer.recorded_visibility import ReplayPerspectiveUnavailable
from wargame_native_engine.viewer.static_resources import (
    VIEWER_CONTENT_SECURITY_POLICY,
    load_symbol_static_resource,
)


_STATIC_ROOT = Path(__file__).with_name("static")
_STATIC_FILES = {
    "/static/viewer_urls.js": ("viewer_urls.js", "text/javascript; charset=utf-8"),
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/map_renderer.js": ("map_renderer.js", "text/javascript; charset=utf-8"),
    "/static/unit_symbol_renderer.js": ("unit_symbol_renderer.js", "text/javascript; charset=utf-8"),
    "/static/unit_symbol_resolver.js": ("unit_symbol_resolver.js", "text/javascript; charset=utf-8"),
    "/static/timeline_chart.js": ("timeline_chart.js", "text/javascript; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


class ReplayViewerHTTPServer(ThreadingHTTPServer):
    """HTTP server carrying one read-only ReplayView."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], view: ReplayView) -> None:
        self.view = view
        self._response_cache: OrderedDict[str, tuple[bytes, bytes]] = OrderedDict()
        self._response_cache_bytes = 0
        self._response_lock = RLock()
        super().__init__(address, ReplayViewerRequestHandler)

    def encoded_record(self, key: str, factory: Any) -> tuple[bytes, bytes]:
        """Bounded immutable response cache, shared safely by HTTP threads.

        Cache bytes, never mutable public records. Even a giant valid frame
        cannot grow the cache beyond its 32 MiB budget or 128 entry limit.
        """
        with self._response_lock:
            if key in self._response_cache:
                self._response_cache.move_to_end(key)
                return self._response_cache[key]
            raw = _json_bytes(factory())
            compressed = gzip.compress(raw, compresslevel=3, mtime=0)
            encoded = (raw, compressed)
            size = len(raw) + len(compressed)
            limit = 32 * 1024 * 1024
            if size <= limit:
                while self._response_cache and (
                    self._response_cache_bytes + size > limit
                    or len(self._response_cache) >= 128
                ):
                    _, removed = self._response_cache.popitem(last=False)
                    self._response_cache_bytes -= sum(map(len, removed))
                self._response_cache[key] = encoded
                self._response_cache_bytes += size
            return encoded


class ReplayViewerRequestHandler(BaseHTTPRequestHandler):
    server: ReplayViewerHTTPServer

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = unquote(urlsplit(self.path).path)
        if path in _STATIC_FILES:
            filename, content_type = _STATIC_FILES[path]
            self._send_bytes(
                HTTPStatus.OK,
                (_STATIC_ROOT / filename).read_bytes(),
                content_type,
            )
            return
        resource = load_symbol_static_resource(path)
        if resource is not None:
            self._send_bytes(HTTPStatus.OK, resource.payload, resource.content_type)
            return
        if path == "/api/replay":
            self._send_record(path, self.server.view.replay_record)
            return
        if path == "/api/perspectives":
            self._send_record(path, self.server.view.perspectives_record)
            return
        if path == "/api/timeline":
            self._send_record(path, self.server.view.timeline_record)
            return
        if path == "/api/map":
            self._send_record(path, self.server.view.map_record)
            return
        try:
            values = parse_qs(urlsplit(self.path).query, keep_blank_values=True).get("perspective", ["GREEN"])
            if len(values) != 1 or values[0] not in {"GREEN", "RED", "BLUE"}:
                raise ValueError("perspective must be GREEN, RED or BLUE")
            perspective = values[0]
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        for prefix, getter in (
            ("/api/frame-bundle/", lambda index: self.server.view.frame_bundle_record(index, perspective)),
            ("/api/frame/", lambda index: self.server.view.frame_record(index, perspective)),
            ("/api/analysis/", lambda index: self._analysis_record(index, perspective)),
        ):
            if path.startswith(prefix):
                key = path if perspective == "GREEN" else f"{path}?perspective={perspective}"
                self._send_indexed(path[len(prefix):], getter, key)
                return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
        path = unquote(urlsplit(self.path).path)
        if path in _STATIC_FILES or load_symbol_static_resource(path) is not None or path in {
            "/api/replay", "/api/perspectives", "/api/timeline", "/api/map",
        }:
            self.send_response(HTTPStatus.OK)
            self.end_headers()
            return
        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        self._send_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            {"error": "replay viewer is read-only"},
        )

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

    def log_message(self, format: str, *args: object) -> None:
        return

    def _analysis_record(self, index: int, perspective: str) -> dict[str, object]:
        if perspective != "GREEN":
            raise ReplayPerspectiveUnavailable("阵营八因子分析不可用。")
        return self.server.view.analysis_record(index)

    def _send_indexed(self, raw_index: str, getter: Any, key: str) -> None:
        try:
            if not raw_index or not raw_index.isascii() or not raw_index.isdigit():
                raise ValueError("frame index must be a non-negative integer")
            self._send_record(key, lambda: getter(int(raw_index)))
        except ReplayPerspectiveUnavailable as exc:
            self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
            return
        except (TypeError, ValueError, IndexError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

    def _send_record(self, key: str, factory: Any) -> None:
        raw, compressed = self.server.encoded_record(key, factory)
        use_gzip = _accepts_gzip(self.headers.get("Accept-Encoding", "")) and len(compressed) < len(raw)
        self._send_bytes(
            HTTPStatus.OK, compressed if use_gzip else raw,
            "application/json; charset=utf-8", compressed=use_gzip,
            vary_encoding=True,
        )

    def _send_json(self, status: HTTPStatus, value: object) -> None:
        self._send_bytes(status, _json_bytes(value), "application/json; charset=utf-8")

    def _send_bytes(
        self,
        status: HTTPStatus,
        payload: bytes,
        content_type: str,
        *, compressed: bool = False, vary_encoding: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        if compressed:
            self.send_header("Content-Encoding", "gzip")
        if vary_encoding:
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            VIEWER_CONTENT_SECURITY_POLICY,
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _accepts_gzip(header: str) -> bool:
    qualities: dict[str, float] = {}
    for entry in header.split(","):
        parts = entry.strip().lower().split(";")
        quality = 1.0
        for parameter in parts[1:]:
            if parameter.strip().startswith("q="):
                try:
                    quality = float(parameter.strip()[2:])
                except ValueError:
                    quality = 0.0
        qualities[parts[0].strip()] = quality
    return 0 < qualities.get("gzip", qualities.get("*", 0.0)) <= 1


def create_replay_viewer_server(
    view: ReplayView,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> ReplayViewerHTTPServer:
    """Create, but do not start, the local read-only HTTP server."""

    return ReplayViewerHTTPServer((host, port), view)
