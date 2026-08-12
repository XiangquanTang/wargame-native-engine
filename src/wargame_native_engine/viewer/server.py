"""Standard-library HTTP server for one immutable replay view."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from wargame_native_engine.viewer.replay_view import ReplayView


_STATIC_ROOT = Path(__file__).with_name("static")
_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/map_renderer.js": ("map_renderer.js", "text/javascript; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


class ReplayViewerHTTPServer(ThreadingHTTPServer):
    """HTTP server carrying one read-only ReplayView."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], view: ReplayView) -> None:
        self.view = view
        super().__init__(address, ReplayViewerRequestHandler)


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
        if path == "/api/replay":
            self._send_json(HTTPStatus.OK, self.server.view.replay_record())
            return
        if path == "/api/timeline":
            self._send_json(HTTPStatus.OK, self.server.view.timeline_record())
            return
        if path == "/api/map":
            self._send_json(HTTPStatus.OK, self.server.view.map_record())
            return
        for prefix, getter in (
            ("/api/frame/", self.server.view.frame_record),
            ("/api/analysis/", self.server.view.analysis_record),
        ):
            if path.startswith(prefix):
                self._send_indexed(path[len(prefix):], getter)
                return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
        path = unquote(urlsplit(self.path).path)
        if path in _STATIC_FILES or path in {
            "/api/replay", "/api/timeline", "/api/map",
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

    def _send_indexed(self, raw_index: str, getter: Any) -> None:
        try:
            if not raw_index or not raw_index.isascii() or not raw_index.isdigit():
                raise ValueError("frame index must be a non-negative integer")
            value = getter(int(raw_index))
        except (TypeError, ValueError, IndexError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._send_json(HTTPStatus.OK, value)

    def _send_json(self, status: HTTPStatus, value: object) -> None:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self._send_bytes(status, payload, "application/json; charset=utf-8")

    def _send_bytes(
        self,
        status: HTTPStatus,
        payload: bytes,
        content_type: str,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)


def create_replay_viewer_server(
    view: ReplayView,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> ReplayViewerHTTPServer:
    """Create, but do not start, the local read-only HTTP server."""

    return ReplayViewerHTTPServer((host, port), view)
