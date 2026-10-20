"""Loopback-only continuous RED/BLUE realtime Web control adapter.

The HTTP layer admits only high-level :class:`SeatOperation` descriptions.
It never stages frozen action identifiers and never advances the simulation.
``RealtimeAgentSession`` owns command admission and its background thread is
the sole fixed-tick authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from wargame_native_engine.agents.realtime import (
    CommandStatus,
    RealtimeAgentSession,
    RealtimeAgentSessionError,
    RealtimeLifecycleState,
    RealtimeSeatBinding,
    RealtimeSeatView,
    SeatCommandReceipt,
)
from wargame_native_engine.agents.seat import SeatDecision, SeatOperation
from wargame_native_engine.gateway import CommandGatewayError
from wargame_native_engine.gateway.models import ControllerView
from wargame_native_engine.rendering import RenderScene
from wargame_native_engine.viewer.live_control import controller_view_record
from wargame_native_engine.viewer.human_capabilities import (
    build_human_capabilities,
)
from wargame_native_engine.viewer.static_resources import (
    load_symbol_static_resource,
)


_STATIC_ROOT = Path(__file__).with_name("static")
_STATIC_FILES = {
    "/static/dual_control.js": ("dual_control.js", "text/javascript; charset=utf-8"),
    "/static/capability_ui.js": ("capability_ui.js", "text/javascript; charset=utf-8"),
    "/static/map_renderer.js": ("map_renderer.js", "text/javascript; charset=utf-8"),
    "/static/unit_symbol_renderer.js": ("unit_symbol_renderer.js", "text/javascript; charset=utf-8"),
    "/static/unit_symbol_resolver.js": ("unit_symbol_resolver.js", "text/javascript; charset=utf-8"),
    "/static/live_actions.js": ("live_actions.js", "text/javascript; charset=utf-8"),
    "/static/live_styles.css": ("live_styles.css", "text/css; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
}
_INDEX = "dual_control_index.html"
_MAX_BODY_BYTES = 128 * 1024


@dataclass(frozen=True, slots=True)
class _StoredRealtimePreview:
    binding_id: str
    side: str
    observation_version: int
    decision: SeatDecision
    partial_confirmation_required: bool


class RealtimeDualControlHTTPServer(ThreadingHTTPServer):
    """One local browser adapter over an already-reset realtime session."""

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        session: RealtimeAgentSession,
        red_binding: RealtimeSeatBinding,
        blue_binding: RealtimeSeatBinding,
        red_token: str,
        blue_token: str,
        map_data: Mapping[str, object],
        weapon_profiles: Mapping[str | int, object] | None = None,
    ) -> None:
        if not isinstance(session, RealtimeAgentSession):
            raise TypeError("session must be a RealtimeAgentSession")
        self.realtime_session = session
        self.bindings = {"red": red_binding, "blue": blue_binding}
        self.tokens = {
            _token(red_token, "red_token"): "red",
            _token(blue_token, "blue_token"): "blue",
        }
        if len(self.tokens) != 2:
            raise ValueError("RED and BLUE tokens must differ")
        self.map_data = dict(map_data)
        self.weapon_profiles = deepcopy(dict(weapon_profiles or {}))
        self._previews: dict[str, _StoredRealtimePreview] = {}
        self._execute_requests: dict[
            str, tuple[str, dict[str, object]]
        ] = {}
        self._executed_previews: dict[str, str] = {}
        self._adapter_lock = RLock()
        super().__init__(address, RealtimeDualControlRequestHandler)

    def binding(self, token: object) -> tuple[str, RealtimeSeatBinding]:
        if not isinstance(token, str):
            raise CommandGatewayError("invalid_token", "seat token is required")
        side = self.tokens.get(token)
        if side is None:
            raise CommandGatewayError("invalid_token", "seat token is invalid")
        return side, self.bindings[side]

    def status(self, side: str, binding: RealtimeSeatBinding) -> dict[str, object]:
        seat_view, controller_view = self.realtime_session.controller_snapshot(
            binding
        )
        return self.status_from_snapshot(side, seat_view, controller_view)

    def status_from_snapshot(
        self,
        side: str,
        seat_view: RealtimeSeatView,
        controller_view: ControllerView,
    ) -> dict[str, object]:
        events = tuple(
            value for value in self.realtime_session.command_events
            if value.seat == side
        )
        pending = tuple(
            value for value in events if value.status is CommandStatus.PENDING
        )
        last = events[-1] if events else None
        ended = seat_view.terminated or seat_view.truncated
        can_operate = (
            not ended
            and self.realtime_session.lifecycle
            is RealtimeLifecycleState.ACCEPTING
        )
        if ended:
            state = "TERMINAL"
        elif pending:
            state = "COMMAND_PENDING"
        elif last is not None and last.status is CommandStatus.REJECTED_AT_APPLY:
            state = "COMMAND_REJECTED"
        else:
            state = "WAITING_SELECTION"
        return {
            "product_state": state,
            "perspective": side.upper(),
            "frame_index": seat_view.frame_index,
            "sim_time_ms": seat_view.sim_time_ms,
            "tick_index": seat_view.tick_index,
            "observation_version": seat_view.observation_version,
            "terminal": ended,
            "terminated": seat_view.terminated,
            "truncated": seat_view.truncated,
            "winner": controller_view.winner,
            "outcome": controller_view.outcome,
            "can_operate": can_operate,
            "pending_command_count": len(pending),
            "last_command": (
                None
                if last is None else {
                    "status": last.status.value,
                    "target_tick": last.target_tick,
                    "applied_frame_index": last.applied_frame_index,
                    "error_code": last.error_code,
                }
            ),
        }

    def store_preview(
        self,
        *,
        preview_token: str,
        binding: RealtimeSeatBinding,
        observation_version: int,
        decision: SeatDecision,
        partial_confirmation_required: bool,
    ) -> None:
        value = _StoredRealtimePreview(
            binding.binding_id,
            binding.seat,
            observation_version,
            decision,
            partial_confirmation_required,
        )
        with self._adapter_lock:
            existing = self._previews.get(preview_token)
            if existing is not None and existing != value:
                raise CommandGatewayError(
                    "request_id_conflict",
                    "preview token was reused with another realtime command",
                )
            self._previews[preview_token] = value

    def execute_preview(
        self,
        *,
        binding: RealtimeSeatBinding,
        preview_token: str,
        request_id: str,
        confirm_partial: bool,
    ) -> dict[str, object]:
        with self._adapter_lock:
            cached = self._execute_requests.get(request_id)
            if cached is not None:
                if cached[0] != preview_token:
                    raise CommandGatewayError(
                        "request_id_conflict",
                        "execute request_id was reused with another preview",
                    )
                return dict(cached[1])
            executed_request = self._executed_previews.get(preview_token)
            if executed_request is not None:
                raise CommandGatewayError(
                    "preview_already_executed",
                    "realtime operation preview was already submitted",
                )
            try:
                stored = self._previews[preview_token]
            except KeyError as exc:
                raise CommandGatewayError(
                    "preview_not_found", "realtime operation preview is unavailable"
                ) from exc
            if stored.binding_id != binding.binding_id or stored.side != binding.seat:
                raise CommandGatewayError(
                    "controller_not_bound", "preview belongs to another seat"
                )
            if stored.partial_confirmation_required and not confirm_partial:
                raise CommandGatewayError(
                    "partial_confirmation_required",
                    "confirm execution of the valid subset before continuing",
                )
            operation = stored.decision.operations[0]
            operation = replace(operation, confirm_partial=confirm_partial)
            receipt = self.realtime_session.submit_operation(
                binding,
                observation_version=stored.observation_version,
                operation=operation,
            )
            record = _receipt_record(receipt)
            record.update({
                "control_mode": "realtime",
                "product_state": (
                    "COMMAND_PENDING" if receipt.accepted else "COMMAND_REJECTED"
                ),
                "can_operate": receipt.accepted,
            })
            self._execute_requests[request_id] = (preview_token, record)
            self._executed_previews[preview_token] = request_id
            return dict(record)


class RealtimeDualControlRequestHandler(BaseHTTPRequestHandler):
    server: RealtimeDualControlHTTPServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            if path in {"/red", "/blue"}:
                side, _binding = self._seat(_one(query, "token"))
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
            resource = load_symbol_static_resource(path)
            if resource is not None:
                self._send_bytes(
                    HTTPStatus.OK, resource.payload, resource.content_type,
                )
                return
            if path == "/api/product/bootstrap":
                side, binding = self._seat(_one(query, "token"))
                include_map = _bootstrap_include_map(query)
                self._send_json(
                    HTTPStatus.OK,
                    self._bootstrap(side, binding, include_map=include_map),
                )
                return
            if path == "/api/product/status":
                side, binding = self._seat(_one(query, "token"))
                self._send_json(
                    HTTPStatus.OK, self.server.status(side, binding),
                )
                return
            if path == "/api/realtime/events":
                side, _binding = self._seat(_one(query, "token"))
                self._send_json(HTTPStatus.OK, {
                    "side": side,
                    "events": [
                        value.to_record()
                        for value in self.server.realtime_session.command_events
                        if value.seat == side
                    ],
                })
                return
            if path == "/api/map":
                self._seat(_one(query, "token"))
                self._send_json(HTTPStatus.OK, self.server.map_data)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except CommandGatewayError as exc:
            self._gateway_error(exc)
        except RealtimeAgentSessionError as exc:
            self._realtime_error(exc)
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
                _side, binding = self._seat(body["token"])
                seat_view, controller_view, base = (
                    self.server.realtime_session.operation_capabilities_snapshot(
                        binding, body["actor_ids"]
                    )
                )
                record = build_human_capabilities(
                    base,
                    controller_view,
                    body["actor_ids"],
                    weapon_profiles=self.server.weapon_profiles,
                )
                record["observation_version"] = seat_view.observation_version
                record["frame_index"] = seat_view.frame_index
                self._send_json(
                    HTTPStatus.OK, record,
                )
                return
            if path == "/api/product/tactical-overlay":
                if set(body) != {"token", "observation_version", "unit_id"}:
                    raise ValueError(
                        "tactical overlay requires exactly token, "
                        "observation_version and unit_id"
                    )
                side, binding = self._seat(body["token"])
                version = _exact_nonnegative_int(
                    body["observation_version"], "observation_version"
                )
                unit_id = _exact_nonnegative_int(body["unit_id"], "unit_id")
                overlay = self.server.realtime_session.tactical_overlay(
                    binding,
                    observation_version=version,
                    unit_id=unit_id,
                )
                seat_view, controller_view = (
                    self.server.realtime_session.controller_snapshot(binding)
                )
                if seat_view.observation_version != version:
                    raise RealtimeAgentSessionError(
                        "tactical_overlay_observation_stale",
                        "tactical overlay requires the latest observation version",
                    )
                record = overlay.to_record()
                record["observation_version"] = version
                record["frame_index"] = seat_view.frame_index
                record["confirmed_contacts"] = _confirmed_contacts(
                    side,
                    unit_id,
                    controller_view,
                )
                self._send_json(HTTPStatus.OK, record)
                return
            if path == "/api/product/preview":
                allowed = {
                    "token", "request_id", "observation_version", "actor_ids",
                    "operation_type", "destination_hex", "target_hex", "target_id",
                    "movement_mode", "weapon_choices", "whole_side_wait",
                    "fire_mode", "guider_id", "shooter_id", "weapon_id",
                }
                required = {
                    "token", "request_id", "observation_version", "actor_ids",
                    "operation_type",
                }
                if not required.issubset(body) or not set(body).issubset(allowed):
                    raise ValueError(
                        "realtime operation preview has missing or unsupported fields"
                    )
                _side, binding = self._seat(body["token"])
                version = _exact_nonnegative_int(
                    body["observation_version"], "observation_version"
                )
                operation = _seat_operation(body)
                preview = self.server.realtime_session.preview_operation(
                    binding,
                    operation,
                    observation_version=version,
                    request_id=_nonempty_text(body["request_id"], "request_id"),
                )
                self.server.store_preview(
                    preview_token=preview.preview_token,
                    binding=binding,
                    observation_version=version,
                    decision=SeatDecision.single(operation),
                    partial_confirmation_required=(
                        preview.partial_confirmation_required
                    ),
                )
                record = preview.to_record()
                record["observation_version"] = version
                self._send_json(HTTPStatus.OK, record)
                return
            if path == "/api/product/execute":
                allowed = {
                    "token", "preview_token", "request_id", "confirm_partial",
                }
                required = {"token", "preview_token", "request_id"}
                if not required.issubset(body) or not set(body).issubset(allowed):
                    raise ValueError(
                        "realtime operation execute has missing or unsupported fields"
                    )
                _side, binding = self._seat(body["token"])
                confirm = body.get("confirm_partial", False)
                if type(confirm) is not bool:
                    raise ValueError("confirm_partial must be bool")
                record = self.server.execute_preview(
                    binding=binding,
                    preview_token=_nonempty_text(
                        body["preview_token"], "preview_token"
                    ),
                    request_id=_nonempty_text(body["request_id"], "request_id"),
                    confirm_partial=confirm,
                )
                if not record["accepted"]:
                    self._send_json(HTTPStatus.CONFLICT, {
                        **record,
                        "error": record["error_code"] or "command rejected",
                    })
                else:
                    self._send_json(HTTPStatus.ACCEPTED, record)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except CommandGatewayError as exc:
            self._gateway_error(exc)
        except RealtimeAgentSessionError as exc:
            self._realtime_error(exc)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    do_PATCH = do_PUT
    do_DELETE = do_PUT

    def log_message(self, format: str, *args: object) -> None:
        return

    def _bootstrap(
        self,
        side: str,
        binding: RealtimeSeatBinding,
        *,
        include_map: bool = True,
    ) -> dict[str, object]:
        seat_view, controller_view = self.server.realtime_session.controller_snapshot(
            binding
        )
        scene = self._scene(side, controller_view, include_map=include_map)
        frame = scene.get("frame")
        if not isinstance(frame, Mapping):
            raise RuntimeError("render scene lacks a frame record")
        scene["frame"] = {
            key: frame[key]
            for key in ("frame_index", "time", "cities", "viewer_units")
        }
        scene["overlays"] = {
            "selected_unit_ids": [],
            "planned_paths": [],
            "shoot_target_ids": [],
            "indirect_fire_active_areas": deepcopy(
                seat_view.capabilities.operation_summary.get(
                    "indirect_fire", {}
                ).get("active_areas", [])
            ),
            "indirect_fire_mission_areas": deepcopy(
                seat_view.capabilities.operation_summary.get(
                    "indirect_fire", {}
                ).get("mission_areas", [])
            ),
        }
        return {
            "session_id": self.server.realtime_session.episode_id,
            "side": side,
            "control_mode": "realtime",
            "referee_data_exposed": False,
            "observation_version": seat_view.observation_version,
            "tick_index": seat_view.tick_index,
            "tick_interval_ms": self.server.realtime_session.tick_interval_ms,
            "scene": scene,
            "product_status": self.server.status_from_snapshot(
                side, seat_view, controller_view,
            ),
        }

    def _scene(
        self, side: str, view: ControllerView, *, include_map: bool = True,
    ) -> dict[str, object]:
        record = controller_view_record(view)
        if not include_map:
            return {
                "mode": f"realtime-{side}",
                "perspective": side.upper(),
                "map": None,
                "frame": record["frame"],
                "overlays": {
                    "selected_unit_id": None,
                    "planned_path": [],
                    "planned_destination": None,
                    "proposal_id": None,
                    "proposal_action_id": None,
                    "shoot_target_ids": [],
                    "adjudication_events": [],
                },
                "timeline": None,
            }
        return RenderScene(
            mode=f"realtime-{side}",
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
        ).to_record()
    def _seat(self, raw_token: object) -> tuple[str, RealtimeSeatBinding]:
        return self.server.binding(raw_token)

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
            "request_id_conflict", "preview_not_found",
            "preview_already_executed",
            "partial_confirmation_required", "side_already_submitted",
            "decision_window_closed", "decision_window_expired",
        }
        self._send_json(
            HTTPStatus.CONFLICT if exc.code in conflict else HTTPStatus.FORBIDDEN,
            {"error": str(exc), "code": exc.code},
        )

    def _realtime_error(self, exc: RealtimeAgentSessionError) -> None:
        self._send_json(
            HTTPStatus.CONFLICT,
            {"error": exc.message, "code": exc.code},
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
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:",
            )
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            # Browsers routinely cancel an obsolete bootstrap/status request
            # during refresh or navigation.  The response is read-only and no
            # command has been admitted, so this is a normal client disconnect
            # rather than an Engine or action failure.
            return


def _bootstrap_include_map(query: Mapping[str, list[str]]) -> bool:
    values = query.get("include_map")
    if values is None:
        return True
    if values == ["1"]:
        return True
    if values == ["0"]:
        return False
    raise ValueError("include_map must be exactly 0 or 1")


def _confirmed_contacts(
    side: str,
    unit_id: int,
    view: ControllerView,
) -> dict[str, object]:
    """Project only contacts already present in one frozen side observation."""

    if view.perspective.lower() != side:
        raise ValueError("confirmed-contact perspective mismatch")
    friendlies = view.observation.get("friendly_operators")
    enemies = view.observation.get("visible_enemies")
    if not isinstance(friendlies, list) or not isinstance(enemies, list):
        raise ValueError("side observation lacks operator arrays")
    selected = tuple(
        value for value in friendlies
        if isinstance(value, Mapping) and value.get("operator_id") == unit_id
    )
    if len(selected) != 1:
        raise ValueError("selected unit is absent from the side observation")
    seen = selected[0].get("observed_enemy_ids", [])
    if not isinstance(seen, list):
        raise ValueError("selected unit contact IDs must be an array")
    visible = {
        _exact_nonnegative_int(value.get("operator_id"), "visible enemy ID"): value
        for value in enemies
        if isinstance(value, Mapping)
    }
    confirmed_ids = tuple(sorted({
        _exact_nonnegative_int(value, "confirmed contact ID")
        for value in seen
        if _exact_nonnegative_int(value, "confirmed contact ID") in visible
    }))
    projected_units = {
        _exact_nonnegative_int(value.get("obj_id"), "source contact ID"): value
        for field in ("operators", "passengers")
        for value in view.source_frame.get(field, [])
        if isinstance(value, Mapping)
    }
    targets = []
    for target_id in confirmed_ids:
        source = projected_units.get(target_id)
        if source is None:
            raise ValueError("confirmed contact is absent from side source projection")
        targets.append({
            "target_id": target_id,
            "hex_id": _exact_nonnegative_int(
                source.get("cur_hex"), "confirmed contact hex"
            ),
            "target_domain": str(
                visible[target_id].get("operator_type", "UNKNOWN")
            ).upper(),
        })
    return {
        "semantics": "selected_unit_current_observed_enemies",
        "hex_ids": sorted({value["hex_id"] for value in targets}),
        "targets": targets,
    }


def create_realtime_dual_control_server(
    session: RealtimeAgentSession,
    *,
    red_binding: RealtimeSeatBinding,
    blue_binding: RealtimeSeatBinding,
    red_token: str,
    blue_token: str,
    map_data: Mapping[str, object],
    weapon_profiles: Mapping[str | int, object] | None = None,
    host: str = "127.0.0.1",
    port: int = 8003,
) -> RealtimeDualControlHTTPServer:
    return RealtimeDualControlHTTPServer(
        (host, port),
        session=session,
        red_binding=red_binding,
        blue_binding=blue_binding,
        red_token=red_token,
        blue_token=blue_token,
        map_data=map_data,
        weapon_profiles=weapon_profiles,
    )


def _seat_operation(value: Mapping[str, Any]) -> SeatOperation:
    raw_actors = value["actor_ids"]
    if isinstance(raw_actors, (str, bytes)) or not isinstance(raw_actors, list):
        raise ValueError("actor_ids must be an array")
    raw_choices = value.get("weapon_choices", {})
    if not isinstance(raw_choices, Mapping):
        raise ValueError("weapon_choices must be an object")
    choices = tuple(sorted(
        (
            _exact_nonnegative_int(unit_id, "weapon unit_id"),
            _exact_nonnegative_int(weapon_id, "weapon_id"),
        )
        for unit_id, weapon_id in raw_choices.items()
    ))
    whole_side_wait = value.get("whole_side_wait", False)
    if type(whole_side_wait) is not bool:
        raise ValueError("whole_side_wait must be bool")
    destination = value.get("destination_hex")
    target_hex = value.get("target_hex")
    target = value.get("target_id")
    operation_type = _nonempty_text(
        value["operation_type"], "operation_type"
    ).upper()
    guided = operation_type == "GUIDED_FIRE"
    indirect = operation_type == "INDIRECT_FIRE"
    return SeatOperation(
        operation_type=operation_type,
        actor_ids=tuple(
            _exact_nonnegative_int(item, "actor_id")
            for item in raw_actors
        ),
        destination_hex=(
            None if destination is None
            else _exact_nonnegative_int(destination, "destination_hex")
        ),
        target_hex=(
            None if target_hex is None
            else _exact_nonnegative_int(target_hex, "target_hex")
        ),
        target_id=(
            None if target is None
            else _exact_nonnegative_int(target, "target_id")
        ),
        movement_mode=_nonempty_text(
            value.get("movement_mode", "NORMAL"), "movement_mode"
        ),
        weapon_choices=choices,
        whole_side_wait=whole_side_wait,
        fire_mode=(
            _nonempty_text(value.get("fire_mode"), "fire_mode")
            if guided or indirect else None
        ),
        guider_id=(
            _exact_nonnegative_int(value.get("guider_id"), "guider_id")
            if guided else None
        ),
        shooter_id=(
            _exact_nonnegative_int(value.get("shooter_id"), "shooter_id")
            if guided or indirect else None
        ),
        weapon_id=(
            _exact_nonnegative_int(value.get("weapon_id"), "weapon_id")
            if guided or indirect else None
        ),
    )


def _receipt_record(value: SeatCommandReceipt) -> dict[str, object]:
    return {
        "command_sequence": value.command_sequence,
        "side": value.seat,
        "accepted": value.accepted,
        "target_tick": value.target_tick,
        "status": value.status.value,
        "error_code": value.error_code,
    }


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
