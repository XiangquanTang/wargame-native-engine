"""Loopback-only HTTP adapter over the in-process CommandGateway."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from wargame_native_engine.gateway import CommandGateway, CommandGatewayError
from wargame_native_engine.viewer.live_control import controller_view_record


_STATIC_ROOT = Path(__file__).with_name("static")
_STATIC_FILES = {
    "/": ("live_index.html", "text/html; charset=utf-8"),
    "/static/map_renderer.js": ("map_renderer.js", "text/javascript; charset=utf-8"),
    "/static/live_app.js": ("live_app.js", "text/javascript; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/static/live_styles.css": ("live_styles.css", "text/css; charset=utf-8"),
}
_MAX_BODY_BYTES = 64 * 1024


class LiveControlHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        gateway: CommandGateway,
        session_id: str,
        controller_id: str,
        opponent_controller_id: str,
        perspective: str,
        map_data: Mapping[str, object],
        output_dir: Path,
    ) -> None:
        self.gateway = gateway
        self.session_id = session_id
        self.controller_id = controller_id
        self.opponent_controller_id = opponent_controller_id
        self.perspective = perspective
        self.map_data = map_data
        self.output_dir = output_dir
        super().__init__(address, LiveControlRequestHandler)


class LiveControlRequestHandler(BaseHTTPRequestHandler):
    server: LiveControlHTTPServer

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlsplit(self.path).path)
        if path in _STATIC_FILES:
            filename, content_type = _STATIC_FILES[path]
            self._send_bytes(
                HTTPStatus.OK,
                (_STATIC_ROOT / filename).read_bytes(),
                content_type,
            )
            return
        if path == "/api/session":
            descriptor = self.server.gateway.manager.descriptor(
                self.server.session_id
            )
            self._send_json(HTTPStatus.OK, {
                **descriptor.to_record(),
                "perspective": self.server.perspective.upper(),
                "control_mode": "side_observation_only",
                "opponent_policy": "NoOp",
                "referee_analysis_exposed": False,
            })
            return
        if path == "/api/map":
            self._send_json(HTTPStatus.OK, self.server.map_data)
            return
        if path == "/api/view":
            self._send_json(HTTPStatus.OK, self._current_view())
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = unquote(urlsplit(self.path).path)
        try:
            body = self._read_json_body()
            if path == "/api/command":
                self._handle_command(body)
                return
            if path == "/api/publish":
                self._handle_publish(body)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except CommandGatewayError as exc:
            status = (
                HTTPStatus.CONFLICT
                if exc.code in {
                    "stale_expected_step", "request_id_conflict",
                    "side_already_submitted", "session_terminal",
                    "session_finalized", "pending_joint_action",
                }
                else HTTPStatus.BAD_REQUEST
            )
            self._send_json(status, {"error": str(exc), "code": exc.code})
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    do_PATCH = do_PUT
    do_DELETE = do_PUT

    def log_message(self, format: str, *args: object) -> None:
        return

    def _handle_command(self, body: Mapping[str, Any]) -> None:
        allowed = {"request_id", "expected_step", "valid_action_id"}
        if set(body) != allowed:
            raise ValueError(
                f"command fields must be exactly {sorted(allowed)!r}"
            )
        request_id = body["request_id"]
        expected_step = body["expected_step"]
        action_id = body["valid_action_id"]
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must be a non-empty string")
        if self.server.perspective == "red":
            self.server.gateway.submit(
                self.server.session_id,
                self.server.controller_id,
                request_id=str(request_id),
                expected_step=expected_step,
                valid_action_id=action_id,
            )
            self.server.gateway.submit(
                self.server.session_id,
                self.server.opponent_controller_id,
                request_id=f"opponent:{request_id}",
                expected_step=expected_step,
                valid_action_id=None,
            )
        else:
            self.server.gateway.submit(
                self.server.session_id,
                self.server.opponent_controller_id,
                request_id=f"opponent:{request_id}",
                expected_step=expected_step,
                valid_action_id=None,
            )
            self.server.gateway.submit(
                self.server.session_id,
                self.server.controller_id,
                request_id=str(request_id),
                expected_step=expected_step,
                valid_action_id=action_id,
            )
        self._send_json(HTTPStatus.OK, {
            "command": {
                "request_id": request_id,
                "accepted": True,
                "advanced": True,
                "step_index": self.server.gateway.manager.descriptor(
                    self.server.session_id
                ).step_index,
            },
            "current": self._current_view(),
        })

    def _handle_publish(self, body: Mapping[str, Any]) -> None:
        if set(body) != {"truncated"} or type(body["truncated"]) is not bool:
            raise ValueError("publish body must contain exact bool truncated")
        publication = self.server.gateway.publish_session(
            self.server.session_id,
            self.server.controller_id,
            str(self.server.output_dir),
            truncated=body["truncated"],
        )
        self._send_json(HTTPStatus.OK, {
            "output_dir": str(publication.output_dir),
            "replay_path": str(publication.replay_path),
            "analysis_path": str(publication.analysis_path),
            "frame_count": publication.frame_count,
            "step_count": publication.step_count,
            "terminal": publication.terminal,
            "truncated": publication.truncated,
        })

    def _current_view(self) -> dict[str, object]:
        view = self.server.gateway.view(
            self.server.session_id,
            self.server.controller_id,
        )
        return controller_view_record(view)

    def _read_json_body(self) -> Mapping[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None or not raw_length.isdigit():
            raise ValueError("Content-Length is required")
        length = int(raw_length)
        if not 0 <= length <= _MAX_BODY_BYTES:
            raise ValueError("request body is too large")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("request body must be a JSON object")
        return value

    def _method_not_allowed(self) -> None:
        self._send_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            {"error": "unsupported method"},
        )

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
        self.wfile.write(payload)


def create_live_control_server(
    gateway: CommandGateway,
    *,
    session_id: str,
    controller_id: str,
    opponent_controller_id: str,
    perspective: str,
    map_data: Mapping[str, object],
    output_dir: str | Path,
    host: str = "127.0.0.1",
    port: int = 8001,
) -> LiveControlHTTPServer:
    """Create, but do not start, one local side-control server."""

    side = str(perspective).lower()
    if side not in {"red", "blue"}:
        raise ValueError("perspective must be RED or BLUE")
    return LiveControlHTTPServer(
        (host, port),
        gateway=gateway,
        session_id=session_id,
        controller_id=controller_id,
        opponent_controller_id=opponent_controller_id,
        perspective=side,
        map_data=map_data,
        output_dir=Path(output_dir),
    )
