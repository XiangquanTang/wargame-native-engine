"""Loopback-only RED/BLUE decision-window HTTP controller."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from wargame_native_engine.gateway import (
    CommandGateway,
    CommandGatewayError,
    DeadlineMode,
)
from wargame_native_engine.operations import ProductOperationService
from wargame_native_engine.operations.controller import SideController
from wargame_native_engine.rendering import RenderScene
from wargame_native_engine.viewer.live_control import controller_view_record


_STATIC_ROOT = Path(__file__).with_name("static")
_STATIC_FILES = {
    "/static/dual_control.js": ("dual_control.js", "text/javascript; charset=utf-8"),
    "/static/map_renderer.js": ("map_renderer.js", "text/javascript; charset=utf-8"),
    "/static/live_actions.js": ("live_actions.js", "text/javascript; charset=utf-8"),
    "/static/live_styles.css": ("live_styles.css", "text/css; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
_INDEX = "dual_control_index.html"
_MAX_BODY_BYTES = 128 * 1024


class DualSideControlHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        gateway: CommandGateway,
        session_id: str,
        red_controller_id: str,
        blue_controller_id: str,
        red_token: str,
        blue_token: str,
        map_data: Mapping[str, object],
        output_dir: Path,
    ) -> None:
        self.gateway = gateway
        self.session_id = str(session_id)
        self.controllers = {
            "red": str(red_controller_id),
            "blue": str(blue_controller_id),
        }
        self.tokens = {
            _token(red_token, "red_token"): "red",
            _token(blue_token, "blue_token"): "blue",
        }
        if len(self.tokens) != 2:
            raise ValueError("RED and BLUE tokens must differ")
        self.map_data = map_data
        self.output_dir = output_dir
        self.side_controllers = {
            side: SideController(gateway, self.session_id, controller)
            for side, controller in self.controllers.items()
        }
        self.product_services = {
            side: ProductOperationService(controller)
            for side, controller in self.side_controllers.items()
        }
        super().__init__(address, DualSideControlRequestHandler)


class DualSideControlRequestHandler(BaseHTTPRequestHandler):
    server: DualSideControlHTTPServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            if path in {"/red", "/blue"}:
                side, _ = self._seat(_one(query, "token"))
                if path != f"/{side}":
                    raise CommandGatewayError(
                        "controller_not_bound", "token does not own this seat page"
                    )
                self._send_bytes(
                    HTTPStatus.OK,
                    (_STATIC_ROOT / _INDEX).read_bytes(),
                    "text/html; charset=utf-8",
                )
                return
            if path in _STATIC_FILES:
                filename, content_type = _STATIC_FILES[path]
                self._send_bytes(
                    HTTPStatus.OK,
                    (_STATIC_ROOT / filename).read_bytes(),
                    content_type,
                )
                return
            if path == "/api/bootstrap":
                side, controller = self._seat(_one(query, "token"))
                self._send_json(
                    HTTPStatus.OK, self._bootstrap(side, controller),
                )
                return
            if path == "/api/product/bootstrap":
                side, controller = self._seat(_one(query, "token"))
                self._send_json(
                    HTTPStatus.OK, self._product_bootstrap(side, controller),
                )
                return
            if path == "/api/product/status":
                side, _ = self._seat(_one(query, "token"))
                self._send_json(
                    HTTPStatus.OK, self.server.product_services[side].status(),
                )
                return
            if path == "/api/product/preview":
                side, _ = self._seat(_one(query, "token"))
                preview = self.server.product_services[side].preview(
                    _one(query, "preview_token")
                )
                self._send_json(HTTPStatus.OK, preview.to_record())
                return
            if path == "/api/view":
                _, controller = self._seat(_one(query, "token"))
                self._send_json(HTTPStatus.OK, controller_view_record(
                    self.server.gateway.view(self.server.session_id, controller)
                ))
                return
            if path == "/api/scene":
                side, controller = self._seat(_one(query, "token"))
                _, view = self.server.gateway.controller_state(
                    self.server.session_id, controller
                )
                self._send_json(HTTPStatus.OK, self._scene(side, view))
                return
            if path == "/api/commands":
                side, _ = self._seat(_one(query, "token"))
                unit_id = _exact_nonnegative_int(_one(query, "unit_id"), "unit_id")
                menu = self.server.side_controllers[side].get_unit_commands(unit_id)
                self._send_json(HTTPStatus.OK, menu.to_record())
                return
            if path == "/api/proposal":
                side, _ = self._seat(_one(query, "token"))
                proposal = self.server.side_controllers[side].proposal(
                    _one(query, "proposal_id")
                )
                self._send_json(HTTPStatus.OK, proposal.to_record())
                return
            if path == "/api/window":
                _, controller = self._seat(_one(query, "token"))
                window = self.server.gateway.poll_window(
                    self.server.session_id, controller,
                )
                self._send_json(HTTPStatus.OK, self._window_record(window))
                return
            if path == "/api/submission":
                _, controller = self._seat(_one(query, "token"))
                request_id = _one(query, "request_id")
                result = self.server.gateway.request_result(
                    self.server.session_id, controller, request_id,
                )
                self._send_json(HTTPStatus.OK, {
                    "found": result is not None,
                    "result": None if result is None else result.to_record(),
                })
                return
            if path == "/api/map":
                self._seat(_one(query, "token"))
                self._send_json(HTTPStatus.OK, self.server.map_data)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except CommandGatewayError as exc:
            self._gateway_error(exc)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        path = unquote(urlsplit(self.path).path)
        try:
            body = self._read_json_body()
            if path == "/api/product/capabilities":
                if set(body) != {"token", "actor_ids"}:
                    raise ValueError(
                        "capabilities requires exactly token and actor_ids"
                    )
                side, _ = self._seat(body["token"])
                self._send_json(
                    HTTPStatus.OK,
                    self.server.product_services[side].capabilities(
                        body["actor_ids"]
                    ),
                )
                return
            if path == "/api/product/preview":
                allowed = {
                    "token", "request_id", "actor_ids", "operation_type",
                    "destination_hex", "target_id", "movement_mode",
                    "weapon_choices", "whole_side_wait",
                }
                required = {"token", "request_id", "actor_ids", "operation_type"}
                if not required.issubset(body) or not set(body).issubset(allowed):
                    raise ValueError(
                        "operation preview contains missing or unsupported fields"
                    )
                side, _ = self._seat(body["token"])
                preview = self.server.product_services[side].preview_operation(
                    body["actor_ids"],
                    body["operation_type"],
                    request_id=_nonempty_text(body["request_id"], "request_id"),
                    destination_hex=body.get("destination_hex"),
                    target_id=body.get("target_id"),
                    movement_mode=body.get("movement_mode", "NORMAL"),
                    weapon_choices=body.get("weapon_choices"),
                    whole_side_wait=body.get("whole_side_wait", False),
                )
                self._send_json(HTTPStatus.OK, preview.to_record())
                return
            if path == "/api/product/execute":
                allowed = {
                    "token", "preview_token", "request_id", "confirm_partial",
                }
                required = {"token", "preview_token", "request_id"}
                if not required.issubset(body) or not set(body).issubset(allowed):
                    raise ValueError(
                        "operation execute contains missing or unsupported fields"
                    )
                side, _ = self._seat(body["token"])
                result = self.server.product_services[side].execute_preview(
                    _nonempty_text(body["preview_token"], "preview_token"),
                    request_id=_nonempty_text(body["request_id"], "request_id"),
                    confirm_partial=body.get("confirm_partial", False),
                )
                self._send_json(HTTPStatus.OK, result.to_record())
                return
            if path == "/api/batch":
                allowed = {
                    "token", "request_id", "expected_window",
                    "ordered_action_ids",
                }
                if set(body) != allowed:
                    raise ValueError(
                        f"batch fields must be exactly {sorted(allowed)!r}"
                    )
                side, controller = self._seat(body["token"])
                result = self.server.gateway.submit_batch(
                    self.server.session_id,
                    controller,
                    request_id=body["request_id"],
                    expected_window=body["expected_window"],
                    ordered_action_ids=body["ordered_action_ids"],
                )
                self._send_json(HTTPStatus.OK, {
                    "side": side,
                    "result": result.to_record(),
                })
                return
            if path == "/api/plan-move":
                allowed = {
                    "token", "request_id", "expected_window", "unit_id",
                    "destination_hex", "movement_mode",
                }
                if set(body) != allowed:
                    raise ValueError(
                        f"plan-move fields must be exactly {sorted(allowed)!r}"
                    )
                side, _ = self._seat(body["token"])
                expected_window = _exact_nonnegative_int(
                    body["expected_window"], "expected_window"
                )
                proposal = self.server.side_controllers[side].plan_move(
                    _exact_nonnegative_int(body["unit_id"], "unit_id"),
                    _exact_nonnegative_int(
                        body["destination_hex"], "destination_hex"
                    ),
                    request_id=_nonempty_text(body["request_id"], "request_id"),
                    movement_mode=_nonempty_text(
                        body["movement_mode"], "movement_mode"
                    ),
                    expected_window=expected_window,
                )
                self._send_json(HTTPStatus.OK, proposal.to_record())
                return
            if path == "/api/batch/stage":
                allowed = {
                    "token", "request_id", "expected_window", "action_id",
                }
                if set(body) != allowed:
                    raise ValueError(
                        f"stage fields must be exactly {sorted(allowed)!r}"
                    )
                side, _ = self._seat(body["token"])
                staged = self.server.side_controllers[side].stage_action(
                    _nonempty_text(body["action_id"], "action_id"),
                    request_id=_nonempty_text(body["request_id"], "request_id"),
                    expected_window=_exact_nonnegative_int(
                        body["expected_window"], "expected_window"
                    ),
                )
                self._send_json(HTTPStatus.OK, {
                    "staged_actions": [item.to_record() for item in staged],
                })
                return
            if path == "/api/batch/submit":
                allowed = {"token", "request_id", "expected_window"}
                if set(body) != allowed:
                    raise ValueError(
                        f"batch submit fields must be exactly {sorted(allowed)!r}"
                    )
                side, _ = self._seat(body["token"])
                result = self.server.side_controllers[side].submit_batch(
                    request_id=_nonempty_text(body["request_id"], "request_id"),
                    expected_window=_exact_nonnegative_int(
                        body["expected_window"], "expected_window"
                    ),
                )
                self._send_json(HTTPStatus.OK, {
                    "side": side,
                    "result": result.to_record(),
                })
                return
            if path == "/api/publish":
                if set(body) != {"token", "truncated"}:
                    raise ValueError("publish requires token and truncated")
                if type(body["truncated"]) is not bool:
                    raise ValueError("truncated must be bool")
                _, controller = self._seat(body["token"])
                publication = self.server.gateway.publish_session(
                    self.server.session_id,
                    controller,
                    str(self.server.output_dir),
                    truncated=body["truncated"],
                )
                self._send_json(HTTPStatus.OK, {
                    "replay_path": str(publication.replay_path),
                    "analysis_path": str(publication.analysis_path),
                    "control_audit_path": str(publication.control_audit_path),
                    "frame_count": publication.frame_count,
                    "step_count": publication.step_count,
                    "terminal": publication.terminal,
                    "truncated": publication.truncated,
                })
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except CommandGatewayError as exc:
            self._gateway_error(exc)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    do_PATCH = do_PUT

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if not path.startswith("/api/batch/"):
            self._method_not_allowed()
            return
        try:
            query = parse_qs(parsed.query, keep_blank_values=True)
            side, _ = self._seat(_one(query, "token"))
            expected = _exact_nonnegative_int(
                _one(query, "expected_window"), "expected_window"
            )
            window = self.server.side_controllers[side].window()
            if window.decision_window_index != expected:
                raise CommandGatewayError(
                    "stale_expected_step",
                    f"expected {expected}, current {window.decision_window_index}",
                )
            unit_text = path.removeprefix("/api/batch/")
            unit_id = None if unit_text == "global" else _exact_nonnegative_int(
                unit_text, "unit_id"
            )
            staged = self.server.side_controllers[side].unstage(unit_id)
            self._send_json(HTTPStatus.OK, {
                "request_id": _one(query, "request_id"),
                "staged_actions": [item.to_record() for item in staged],
            })
        except CommandGatewayError as exc:
            self._gateway_error(exc)
        except (KeyError, TypeError, ValueError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def log_message(self, format: str, *args: object) -> None:
        return

    def _bootstrap(self, side: str, controller: str) -> dict[str, object]:
        window, view = self.server.gateway.controller_state(
            self.server.session_id, controller,
        )
        return {
            "session_id": self.server.session_id,
            "side": side,
            "control_contract": "frozen-valid-action-id-batch/1.0",
            "referee_data_exposed": False,
            "view": controller_view_record(view),
            "scene": self._scene(side, view),
            "window": self._window_record(window),
            "staged_actions": [
                item.to_record()
                for item in self.server.side_controllers[side].staged_actions
            ],
        }

    def _product_bootstrap(
        self, side: str, controller: str,
    ) -> dict[str, object]:
        _, view = self.server.gateway.controller_state(
            self.server.session_id, controller,
        )
        scene = self._scene(side, view)
        scene["overlays"] = {
            "selected_unit_ids": [],
            "planned_paths": [],
            "shoot_target_ids": [],
        }
        return {
            "session_id": self.server.session_id,
            "side": side,
            "referee_data_exposed": False,
            "scene": scene,
            "product_status": self.server.product_services[side].status(),
        }

    def _scene(self, side: str, view) -> dict[str, object]:
        record = controller_view_record(view)
        scene = RenderScene(
            mode=f"live-{side}",
            perspective=side.upper(),
            map=self.server.map_data,
            frame=record["frame"],
            overlays={
                "selected_unit_id": None,
                "planned_path": [],
                "planned_destination": None,
                "proposal_id": None,
                "proposal_action_id": None,
                "shoot_target_ids": [],
                "adjudication_events": [],
            },
        )
        return scene.to_record()

    def _window_record(self, window) -> dict[str, object]:
        record = window.to_record()
        deadline = window.deadline_at_server_monotonic
        remaining = None
        if (
            deadline is not None
            and self.server.gateway.deadline_mode
            is DeadlineMode.SERVER_MONOTONIC
        ):
            remaining = max(
                0,
                round((deadline - self.server.gateway.clock.now()) * 1000),
            )
        record["deadline_remaining_ms"] = remaining
        return record

    def _seat(self, raw_token: object) -> tuple[str, str]:
        if not isinstance(raw_token, str):
            raise CommandGatewayError("invalid_token", "seat token is required")
        side = self.server.tokens.get(raw_token)
        if side is None:
            raise CommandGatewayError("invalid_token", "seat token is invalid")
        return side, self.server.controllers[side]

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

    def _gateway_error(self, exc: CommandGatewayError) -> None:
        conflict = {
            "stale_expected_step", "request_id_conflict",
            "side_already_submitted", "decision_window_closed",
            "decision_window_expired", "session_terminal", "session_finalized",
            "preview_not_found", "preview_expired", "preview_already_executed",
            "partial_confirmation_required", "side_already_submitted",
        }
        self._send_json(
            HTTPStatus.CONFLICT if exc.code in conflict else HTTPStatus.FORBIDDEN,
            {"error": str(exc), "code": exc.code},
        )

    def _method_not_allowed(self) -> None:
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "unsupported method"})

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
        self, status: HTTPStatus, payload: bytes, content_type: str,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'",
        )
        self.end_headers()
        self.wfile.write(payload)


def create_dual_side_control_server(
    gateway: CommandGateway,
    *,
    session_id: str,
    red_controller_id: str,
    blue_controller_id: str,
    red_token: str,
    blue_token: str,
    map_data: Mapping[str, object],
    output_dir: str | Path,
    host: str = "127.0.0.1",
    port: int = 8002,
) -> DualSideControlHTTPServer:
    return DualSideControlHTTPServer(
        (host, port),
        gateway=gateway,
        session_id=session_id,
        red_controller_id=red_controller_id,
        blue_controller_id=blue_controller_id,
        red_token=red_token,
        blue_token=blue_token,
        map_data=map_data,
        output_dir=Path(output_dir),
    )


def _token(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) < 8:
        raise ValueError(f"{field} must contain at least eight characters")
    return value


def _one(query: Mapping[str, list[str]], field: str) -> str:
    values = query.get(field)
    if values is None or len(values) != 1 or not values[0]:
        raise ValueError(f"exactly one {field} is required")
    return values[0]


def _nonempty_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _exact_nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a non-negative exact int")
    return value
