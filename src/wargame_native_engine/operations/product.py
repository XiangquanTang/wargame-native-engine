"""Human-facing grouped operation previews over one SideController.

This module composes existing frozen actions and server route proposals.  It
never creates an adjudication rule or advances the environment outside the
existing SideController -> CommandGateway path.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from typing import Any, Mapping, Sequence

from wargame_native_engine.gateway.models import (
    CommandGatewayError,
    CommandResult,
)
from wargame_native_engine.io.serialization import canonical_hash, json_clone
from wargame_native_engine.operations.controller import SideController


PRODUCT_OPERATION_TYPES = (
    "MOVE",
    "SHOOT",
    "STOP_MOVE",
    "WEAPON_UNFOLD",
    "WEAPON_LOCK",
    "WAIT",
)


@dataclass(frozen=True, slots=True)
class UnitOperationPreview:
    unit_id: int
    display_name: str
    accepted_for_preview: bool
    human_readable_summary: str
    estimated_duration_ms: int | None = None
    path: tuple[int, ...] = ()
    weapon_options: tuple[Mapping[str, object], ...] = ()
    reject_code: str | None = None
    reject_message: str | None = None

    def to_record(self) -> dict[str, object]:
        return {
            "unit_id": self.unit_id,
            "display_name": self.display_name,
            "accepted_for_preview": self.accepted_for_preview,
            "human_readable_summary": self.human_readable_summary,
            "estimated_duration_ms": self.estimated_duration_ms,
            "path": list(self.path),
            "weapon_options": json_clone(self.weapon_options),
            "reject_code": self.reject_code,
            "reject_message": self.reject_message,
        }


@dataclass(frozen=True, slots=True)
class GroupOperationPreview:
    preview_token: str
    operation_type: str
    selected_count: int
    executable_count: int
    rejected_count: int
    per_unit_results: tuple[UnitOperationPreview, ...]
    destination_hex: int | None
    target_id: int | None
    estimated_duration_ms: int | None
    warnings: tuple[str, ...]
    expires_with_window: int
    can_execute: bool
    partial_confirmation_required: bool
    whole_side_wait: bool = False

    def to_record(self) -> dict[str, object]:
        return {
            "preview_token": self.preview_token,
            "operation_type": self.operation_type,
            "selected_count": self.selected_count,
            "executable_count": self.executable_count,
            "rejected_count": self.rejected_count,
            "per_unit_results": [item.to_record() for item in self.per_unit_results],
            "destination_hex": self.destination_hex,
            "target_id": self.target_id,
            "estimated_duration_ms": self.estimated_duration_ms,
            "warnings": list(self.warnings),
            "expires_with_window": self.expires_with_window,
            "can_execute": self.can_execute,
            "partial_confirmation_required": self.partial_confirmation_required,
            "whole_side_wait": self.whole_side_wait,
        }


@dataclass(frozen=True, slots=True)
class ProductExecutionResult:
    preview_token: str
    request_id: str
    accepted: bool
    submitted: bool
    advanced: bool
    product_state: str
    executed_count: int
    skipped_count: int
    frame_index: int
    sim_time_ms: int
    terminal: bool
    winner: str | None
    outcome: str | None
    message: str

    def to_record(self) -> dict[str, object]:
        return {
            "preview_token": self.preview_token,
            "request_id": self.request_id,
            "accepted": self.accepted,
            "submitted": self.submitted,
            "advanced": self.advanced,
            "product_state": self.product_state,
            "executed_count": self.executed_count,
            "skipped_count": self.skipped_count,
            "frame_index": self.frame_index,
            "sim_time_ms": self.sim_time_ms,
            "terminal": self.terminal,
            "winner": self.winner,
            "outcome": self.outcome,
            "message": self.message,
        }


@dataclass(slots=True)
class _StoredPreview:
    preview: GroupOperationPreview
    action_ids: tuple[str, ...]
    executed_request_id: str | None = None
    execution_result: ProductExecutionResult | None = None


class ProductOperationService:
    """One side's window-scoped preview and execute registry."""

    def __init__(self, controller: SideController) -> None:
        self.controller = controller
        self._previews: dict[str, _StoredPreview] = {}
        self._preview_requests: dict[str, tuple[tuple[object, ...], str]] = {}
        self._execute_requests: dict[
            str, tuple[str, ProductExecutionResult]
        ] = {}
        self._active_window: int | None = None
        self._lock = RLock()

    def status(self) -> dict[str, object]:
        window, view = self.controller.state()
        with self._lock:
            self._sync_window(window.decision_window_index)
        if view.terminal:
            state = "TERMINAL"
        elif window.submitted:
            state = "WAITING_OPPONENT"
        else:
            state = "WAITING_SELECTION"
        return {
            "product_state": state,
            "perspective": view.perspective,
            "frame_index": view.frame_index,
            "sim_time_ms": view.sim_time_ms,
            "terminal": view.terminal,
            "winner": view.winner,
            "outcome": view.outcome,
            "can_operate": state == "WAITING_SELECTION",
        }

    def capabilities(self, actor_ids: Sequence[int]) -> dict[str, object]:
        """Summarize toolbar applicability without exposing frozen IDs."""

        actors = _actor_ids(actor_ids)
        operations = {
            kind: {"applicable_count": 0, "unavailable": []}
            for kind in PRODUCT_OPERATION_TYPES
        }
        summaries = []
        shoot_targets: set[int] = set()
        for unit_id in actors:
            try:
                menu = self.controller.get_unit_commands(unit_id)
            except CommandGatewayError as exc:
                for value in operations.values():
                    value["unavailable"].append({
                        "unit_id": unit_id,
                        "code": exc.code,
                        "message": exc.message,
                    })
                continue
            summaries.append({
                "unit_id": unit_id,
                "display_name": _display_name(menu.unit, unit_id),
                "operator_type": menu.unit.get("operator_type"),
                "subtype": menu.unit.get("subtype"),
                "current_hex": menu.unit.get("current_hex"),
                "health": menu.unit.get("health", menu.unit.get("blood")),
                "state": menu.unit.get("state"),
            })
            for kind, value in operations.items():
                available = bool(menu.available.get(kind))
                if kind == "MOVE":
                    available = menu.move_planning_available
                if available:
                    value["applicable_count"] += 1
                    if kind == "SHOOT":
                        for item in menu.available.get("SHOOT", ()):
                            target = item.get("action", {}).get("target_id")
                            if type(target) is int:
                                shoot_targets.add(target)
                else:
                    value["unavailable"].append({
                        "unit_id": unit_id,
                        "code": "operation_unavailable",
                        "message": _unavailable_message(kind),
                    })
        return {
            "selected_count": len(actors),
            "selected_units": summaries,
            "operations": operations,
            "shoot_target_ids": sorted(shoot_targets),
        }

    def preview_operation(
        self,
        actor_ids: Sequence[int],
        operation_type: str,
        *,
        request_id: str,
        destination_hex: int | None = None,
        target_id: int | None = None,
        movement_mode: str = "NORMAL",
        weapon_choices: Mapping[int | str, int] | None = None,
        whole_side_wait: bool = False,
    ) -> GroupOperationPreview:
        request = _nonempty(request_id, "request_id")
        operation = str(operation_type).upper()
        if operation not in PRODUCT_OPERATION_TYPES:
            raise CommandGatewayError(
                "unsupported_operation", f"unsupported operation {operation!r}"
            )
        actors = _actor_ids(actor_ids)
        if whole_side_wait:
            if operation != "WAIT" or actors:
                raise CommandGatewayError(
                    "invalid_whole_side_wait",
                    "whole-side wait requires WAIT and no selected units",
                )
        elif not actors:
            raise CommandGatewayError(
                "selection_required", "select at least one friendly unit"
            )
        if operation == "MOVE" and type(destination_hex) is not int:
            raise CommandGatewayError(
                "destination_required", "MOVE requires an exact destination hex"
            )
        if operation == "SHOOT" and type(target_id) is not int:
            raise CommandGatewayError(
                "target_required", "SHOOT requires an exact visible target id"
            )
        choices = _weapon_choices(weapon_choices)
        window = self.controller.window()
        if window.submitted:
            raise CommandGatewayError(
                "side_already_submitted", "this side is already waiting"
            )
        signature: tuple[object, ...] = (
            window.decision_window_index,
            actors,
            operation,
            destination_hex,
            target_id,
            str(movement_mode).upper(),
            tuple(sorted(choices.items())),
            whole_side_wait,
        )
        with self._lock:
            self._sync_window(window.decision_window_index)
            cached = self._preview_requests.get(request)
            if cached is not None:
                if cached[0] != signature:
                    raise CommandGatewayError(
                        "request_id_conflict",
                        "preview request_id was reused with another operation",
                    )
                return deepcopy(self._previews[cached[1]].preview)

            results: list[UnitOperationPreview] = []
            action_ids: list[str] = []
            if not whole_side_wait:
                for unit_id in actors:
                    result, action_id = self._preview_unit(
                        unit_id,
                        operation,
                        request_id=request,
                        destination_hex=destination_hex,
                        target_id=target_id,
                        movement_mode=movement_mode,
                        weapon_choice=choices.get(unit_id),
                    )
                    results.append(result)
                    if action_id is not None:
                        action_ids.append(action_id)
            executable = len(action_ids)
            rejected = len(results) - executable
            can_execute = whole_side_wait or executable > 0
            durations = [
                item.estimated_duration_ms
                for item in results
                if item.estimated_duration_ms is not None
            ]
            token_record = {
                "session_id": self.controller.session_id,
                "controller_id": self.controller.controller_id,
                "window": window.decision_window_index,
                "request_id": request,
                "signature": signature,
                "action_ids": action_ids,
            }
            token = "operation-" + canonical_hash(token_record)[:32]
            warnings = ()
            if rejected and executable:
                warnings = (f"将执行{executable}个，跳过{rejected}个",)
            preview = GroupOperationPreview(
                token,
                operation,
                len(actors),
                executable,
                rejected,
                tuple(results),
                destination_hex,
                target_id,
                max(durations) if durations else None,
                warnings,
                window.decision_window_index,
                can_execute,
                bool(rejected and executable),
                whole_side_wait,
            )
            self._previews[token] = _StoredPreview(preview, tuple(action_ids))
            self._preview_requests[request] = (signature, token)
            self.controller.record_operation_preview(
                preview_token=token,
                operation_type=operation,
                selected_unit_ids=actors,
                executable_count=executable,
                rejected=tuple({
                    "unit_id": item.unit_id,
                    "reject_code": item.reject_code,
                    "reject_message": item.reject_message,
                } for item in results if not item.accepted_for_preview),
            )
            return deepcopy(preview)

    def preview(self, preview_token: str) -> GroupOperationPreview:
        with self._lock:
            self._sync_window(self.controller.window().decision_window_index)
            try:
                return deepcopy(self._previews[str(preview_token)].preview)
            except KeyError as exc:
                raise CommandGatewayError(
                    "preview_not_found", "operation preview is unavailable or expired"
                ) from exc

    def execute_preview(
        self,
        preview_token: str,
        *,
        request_id: str,
        confirm_partial: bool = False,
    ) -> ProductExecutionResult:
        request = _nonempty(request_id, "request_id")
        token = _nonempty(preview_token, "preview_token")
        if type(confirm_partial) is not bool:
            raise CommandGatewayError(
                "invalid_partial_confirmation", "confirm_partial must be bool"
            )
        with self._lock:
            cached_request = self._execute_requests.get(request)
            if cached_request is not None:
                if cached_request[0] != token:
                    raise CommandGatewayError(
                        "request_id_conflict",
                        "execute request_id was reused with another preview",
                    )
                return deepcopy(cached_request[1])
            try:
                stored = self._previews[token]
            except KeyError as exc:
                raise CommandGatewayError(
                    "preview_not_found", "operation preview is unavailable or expired"
                ) from exc
            window = self.controller.window()
            preview = stored.preview
            if preview.expires_with_window != window.decision_window_index:
                self._sync_window(window.decision_window_index)
                raise CommandGatewayError(
                    "preview_expired", "this operation belongs to an older cycle"
                )
            if stored.execution_result is not None:
                if stored.executed_request_id == request:
                    return deepcopy(stored.execution_result)
                raise CommandGatewayError(
                    "preview_already_executed", "this operation was already executed"
                )
            if not preview.can_execute:
                raise CommandGatewayError(
                    "preview_not_executable", "no selected unit can execute this operation"
                )
            if preview.partial_confirmation_required and not confirm_partial:
                raise CommandGatewayError(
                    "partial_confirmation_required",
                    "confirm execution of the valid subset before continuing",
                )
            self.controller.clear_staged()
            try:
                for index, action_id in enumerate(stored.action_ids):
                    self.controller.stage_action(
                        action_id,
                        request_id=f"{request}:stage:{index}",
                        expected_window=preview.expires_with_window,
                    )
                command = self.controller.submit_batch(
                    request_id=request,
                    expected_window=preview.expires_with_window,
                )
            except BaseException:
                self.controller.clear_staged()
                raise
            result = _product_result(preview, request, command)
            stored.executed_request_id = request
            stored.execution_result = result
            self._execute_requests[request] = (token, result)
            return deepcopy(result)

    def _preview_unit(
        self,
        unit_id: int,
        operation: str,
        *,
        request_id: str,
        destination_hex: int | None,
        target_id: int | None,
        movement_mode: str,
        weapon_choice: int | None,
    ) -> tuple[UnitOperationPreview, str | None]:
        try:
            menu = self.controller.get_unit_commands(unit_id)
        except CommandGatewayError as exc:
            return _rejected(unit_id, str(unit_id), exc.code, exc.message), None
        name = _display_name(menu.unit, unit_id)
        if operation == "MOVE":
            if not menu.move_planning_available:
                return _rejected(
                    unit_id, name, "move_unavailable", _unavailable_message("MOVE")
                ), None
            proposal = self.controller.plan_move(
                unit_id,
                int(destination_hex),
                request_id=f"{request_id}:move:{unit_id}",
                movement_mode=movement_mode,
                expected_window=menu.decision_window_index,
            )
            if not proposal.reachable or proposal.action_id is None:
                code = proposal.blocked_reason or "destination_unreachable"
                return _rejected(unit_id, name, code, _reason_message(code)), None
            return UnitOperationPreview(
                unit_id,
                name,
                True,
                f"机动至格{proposal.destination_hex}，{len(proposal.ordered_path)}段",
                proposal.estimated_duration_ms,
                proposal.ordered_path,
            ), proposal.action_id

        candidates = tuple(menu.available.get(operation, ()))
        if operation == "SHOOT":
            candidates = tuple(
                item
                for item in candidates
                if item.get("action", {}).get("target_id") == target_id
            )
        candidates = tuple(sorted(candidates, key=lambda item: str(item["action_id"])))
        if not candidates:
            code = "target_not_shootable" if operation == "SHOOT" else (
                "operation_unavailable"
            )
            return _rejected(
                unit_id, name, code, _unavailable_message(operation)
            ), None
        options = tuple(_weapon_option(item) for item in candidates)
        chosen = None
        if operation == "SHOOT" and len(candidates) > 1:
            if weapon_choice is None:
                return UnitOperationPreview(
                    unit_id,
                    name,
                    False,
                    "存在多个合法武器，请选择武器",
                    weapon_options=options,
                    reject_code="weapon_choice_required",
                    reject_message="存在多个合法武器，请为该单位选择",
                ), None
            matching = [
                item for item in candidates
                if item.get("action", {}).get("parameters", {}).get("weapon_id")
                == weapon_choice
            ]
            if len(matching) != 1:
                return _rejected(
                    unit_id, name, "weapon_choice_invalid", "所选武器当前不可用",
                    options,
                ), None
            chosen = matching[0]
        elif len(candidates) > 1:
            return _rejected(
                unit_id,
                name,
                "operation_choice_ambiguous",
                "该操作存在多个服务器选项，无法自动选择",
            ), None
        else:
            chosen = candidates[0]
        summary = _operation_summary(operation, chosen)
        return UnitOperationPreview(
            unit_id,
            name,
            True,
            summary,
            weapon_options=options if operation == "SHOOT" else (),
        ), str(chosen["action_id"])

    def _sync_window(self, index: int) -> None:
        if self._active_window is None:
            self._active_window = index
            return
        if self._active_window != index:
            self._previews.clear()
            self._preview_requests.clear()
            self._active_window = index


def _product_result(
    preview: GroupOperationPreview,
    request_id: str,
    command: CommandResult,
) -> ProductExecutionResult:
    view = command.view
    if view.terminal:
        state = "TERMINAL"
        message = "对局已终局"
    elif command.advanced:
        state = "WAITING_SELECTION"
        message = "双方操作已处理，请选择下一步"
    else:
        state = "WAITING_OPPONENT"
        message = "本方操作已提交，等待对方"
    return ProductExecutionResult(
        preview.preview_token,
        request_id,
        command.accepted,
        True,
        command.advanced,
        state,
        preview.executable_count,
        preview.rejected_count,
        view.frame_index,
        view.sim_time_ms,
        view.terminal,
        view.winner,
        view.outcome,
        message,
    )


def _actor_ids(values: Sequence[int]) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise CommandGatewayError("invalid_actor_ids", "actor_ids must be an array")
    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        if type(value) is not int or value < 0:
            raise CommandGatewayError(
                "invalid_actor_ids", "actor_ids must contain non-negative exact ints"
            )
        if value in seen:
            raise CommandGatewayError(
                "duplicate_actor", f"unit {value} was selected more than once"
            )
        seen.add(value)
        result.append(value)
    return tuple(result)


def _weapon_choices(values: Mapping[int | str, int] | None) -> dict[int, int]:
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise CommandGatewayError(
            "invalid_weapon_choices", "weapon_choices must be an object"
        )
    result: dict[int, int] = {}
    for raw_unit, weapon in values.items():
        try:
            unit_id = int(raw_unit)
        except (TypeError, ValueError) as exc:
            raise CommandGatewayError(
                "invalid_weapon_choices", "weapon choice unit id is invalid"
            ) from exc
        if type(weapon) is not int or weapon < 0 or unit_id < 0:
            raise CommandGatewayError(
                "invalid_weapon_choices", "weapon ids must be non-negative exact ints"
            )
        result[unit_id] = weapon
    return result


def _display_name(unit: Mapping[str, Any], unit_id: int) -> str:
    value = unit.get("name") or unit.get("subtype") or unit.get("operator_type")
    return f"{value or '单位'} {unit_id}"


def _weapon_option(item: Mapping[str, Any]) -> Mapping[str, object]:
    action = item.get("action", {})
    parameters = action.get("parameters", {}) if isinstance(action, Mapping) else {}
    return {
        "weapon_id": parameters.get("weapon_id"),
        "target_id": action.get("target_id") if isinstance(action, Mapping) else None,
        "summary": f"武器{parameters.get('weapon_id', 'UNKNOWN')}",
    }


def _operation_summary(operation: str, item: Mapping[str, Any]) -> str:
    action = item.get("action", {})
    if operation == "SHOOT":
        weapon = action.get("parameters", {}).get("weapon_id")
        return f"射击目标{action.get('target_id')}，武器{weapon}"
    return {
        "STOP_MOVE": "停止当前移动",
        "WEAPON_UNFOLD": "展开武器",
        "WEAPON_LOCK": "锁定武器",
        "WAIT": "本轮不下达新动作",
    }.get(operation, operation)


def _rejected(
    unit_id: int,
    name: str,
    code: str,
    message: str,
    weapon_options: tuple[Mapping[str, object], ...] = (),
) -> UnitOperationPreview:
    return UnitOperationPreview(
        unit_id,
        name,
        False,
        message,
        weapon_options=weapon_options,
        reject_code=code,
        reject_message=message,
    )


def _unavailable_message(operation: str) -> str:
    return {
        "MOVE": "当前不能机动",
        "SHOOT": "当前没有对该目标的合法射击动作",
        "STOP_MOVE": "当前未在移动或不能停止",
        "WEAPON_UNFOLD": "当前不能展开武器",
        "WEAPON_LOCK": "当前不能锁定武器",
        "WAIT": "当前不能等待",
    }.get(operation, "当前不能执行该操作")


def _reason_message(code: str) -> str:
    return {
        "DESTINATION_UNREACHABLE": "目标不可达",
        "UNKNOWN_DESTINATION": "目标格不存在",
        "ALREADY_AT_DESTINATION": "单位已在目标格",
        "OPERATOR_DESTROYED": "单位已被摧毁",
        "OPERATOR_CANNOT_MOVE": "单位当前不能移动",
        "OPERATOR_LOST_CONTROL": "单位当前失控",
    }.get(str(code), str(code))


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CommandGatewayError(f"invalid_{name}", f"{name} must be non-empty")
    return value
