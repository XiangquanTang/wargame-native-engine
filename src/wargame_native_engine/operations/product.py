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

from wargame_native_engine.agents.seat import SeatDecision
from wargame_native_engine.gateway.models import (
    CommandGatewayError,
    CommandResult,
    ControllerView,
    DecisionWindowView,
)
from wargame_native_engine.io.serialization import canonical_hash, json_clone
from wargame_native_engine.operations.controller import SideController
from wargame_native_engine.projection.decision_semantic import (
    CapabilityAvailabilityStatus,
    DecisionCapabilityFamily,
)


PRODUCT_OPERATION_TYPES = (
    "MOVE",
    "SHOOT",
    "GUIDED_FIRE",
    "INDIRECT_FIRE",
    "BOARD",
    "UNLOAD",
    "ENTER_CONCEALMENT",
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


@dataclass(frozen=True, slots=True)
class SeatDecisionPreview:
    """Opaque product preview for one complete side decision."""

    preview_token: str
    decision_window_index: int
    operation_previews: tuple[GroupOperationPreview, ...]
    operation_count: int
    executable_count: int
    skipped_count: int
    can_execute: bool

    def to_record(self) -> dict[str, object]:
        return {
            "preview_token": self.preview_token,
            "decision_window_index": self.decision_window_index,
            "operation_previews": [
                item.to_record() for item in self.operation_previews
            ],
            "operation_count": self.operation_count,
            "executable_count": self.executable_count,
            "skipped_count": self.skipped_count,
            "can_execute": self.can_execute,
        }


@dataclass(frozen=True, slots=True)
class SeatDecisionExecutionResult:
    preview_token: str
    request_id: str
    accepted: bool
    submitted: bool
    advanced: bool
    product_state: str
    operation_count: int
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
            "operation_count": self.operation_count,
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


@dataclass(slots=True)
class _StoredDecisionPreview:
    preview: SeatDecisionPreview
    component_tokens: tuple[str, ...]
    action_ids: tuple[str, ...]
    executed_request_id: str | None = None
    execution_result: SeatDecisionExecutionResult | None = None


class ProductOperationService:
    """One side's window-scoped preview and execute registry."""

    def __init__(self, controller: SideController) -> None:
        self.controller = controller
        self._previews: dict[str, _StoredPreview] = {}
        self._preview_requests: dict[str, tuple[tuple[object, ...], str]] = {}
        self._execute_requests: dict[
            str, tuple[str, ProductExecutionResult]
        ] = {}
        self._decision_previews: dict[str, _StoredDecisionPreview] = {}
        self._decision_preview_requests: dict[
            str, tuple[tuple[object, ...], str]
        ] = {}
        self._decision_execute_requests: dict[
            str, tuple[str, SeatDecisionExecutionResult]
        ] = {}
        self._active_window: int | None = None
        self._lock = RLock()

    def status(self) -> dict[str, object]:
        window, view = self.controller.state()
        return self.status_from_snapshot(window, view)

    def decision_core(self) -> dict[str, object]:
        """Return the explicit side-safe machine payload for Human clients."""

        _, view = self.controller.state()
        return view.decision_record()

    def status_from_snapshot(
        self,
        window: DecisionWindowView,
        view: ControllerView,
    ) -> dict[str, object]:
        """Map one already-consistent controller snapshot to product language."""

        with self._lock:
            self._sync_window(window.decision_window_index)
        decision = view.decision_semantic
        catalog = view.action_catalog
        if decision is None or catalog is None:
            raise CommandGatewayError(
                "decision_core_unavailable", "normal Human view requires Decision core"
            )
        snapshot = decision.snapshot
        if snapshot.terminated or snapshot.truncated:
            state = "TERMINAL"
        elif window.submitted:
            state = "WAITING_OPPONENT"
        else:
            state = "WAITING_SELECTION"
        return {
            "product_state": state,
            "perspective": snapshot.side.value.upper(),
            "frame_index": snapshot.frame_index,
            "sim_time_ms": snapshot.sim_time_ms,
            "terminal": view.terminal,
            "winner": view.winner,
            "outcome": view.outcome,
            "can_operate": state == "WAITING_SELECTION",
            "decision_semantic_hash": decision.semantic_hash,
            "action_catalog_hash": catalog.catalog_hash,
        }

    def capabilities(
        self,
        actor_ids: Sequence[int],
        *,
        view: ControllerView | None = None,
    ) -> dict[str, object]:
        """Summarize toolbar applicability without exposing frozen IDs."""

        actors = _actor_ids(actor_ids)
        active_view = self.controller.observe() if view is None else deepcopy(view)
        decision = active_view.decision_semantic
        catalog = active_view.action_catalog
        if decision is None or catalog is None:
            raise CommandGatewayError(
                "decision_core_unavailable",
                "normal Human capability view requires Decision core",
            )
        machine = {
            value.operator_reference: value
            for value in decision.capabilities.operators
        }
        operations = {
            kind: {"applicable_count": 0, "unavailable": []}
            for kind in PRODUCT_OPERATION_TYPES
        }
        summaries = []
        shoot_targets: set[int] = set()
        guided_targets: set[int] = set()
        for unit_id in actors:
            try:
                menu = self.controller.get_unit_commands(unit_id, view=active_view)
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
                candidates = _operation_candidates(menu, kind)
                family = _product_capability_family(kind)
                status = (
                    None
                    if family is None
                    else next(
                        (
                            item
                            for item in machine[unit_id].families
                            if item.family is family
                        ),
                        None,
                    )
                )
                available = (
                    bool(candidates)
                    if status is None
                    else status.status is CapabilityAvailabilityStatus.AVAILABLE
                )
                if available:
                    value["applicable_count"] += 1
                    if kind == "SHOOT":
                        for item in candidates:
                            target = item.get("action", {}).get("target_id")
                            if type(target) is int:
                                shoot_targets.add(target)
                    elif kind == "GUIDED_FIRE":
                        for item in candidates:
                            target = item.get("action", {}).get("target_id")
                            if type(target) is int:
                                guided_targets.add(target)
                else:
                    code, message = (
                        _capability_unavailable(kind, menu)
                        if status is None
                        or status.status is CapabilityAvailabilityStatus.UNKNOWN
                        else _machine_capability_unavailable(status)
                    )
                    value["unavailable"].append({
                        "unit_id": unit_id,
                        "code": code,
                        "message": message,
                    })
        return {
            "selected_count": len(actors),
            "selected_units": summaries,
            "operations": operations,
            "shoot_target_ids": sorted(shoot_targets),
            "guided_fire_target_ids": sorted(guided_targets),
            "decision_semantic_hash": decision.semantic_hash,
            "action_catalog_hash": catalog.catalog_hash,
            "machine_capabilities": [
                value
                for value in decision.capabilities.to_dict()["operators"]
                if value["operator_reference"] in set(actors)
            ],
        }

    def preview_operation(
        self,
        actor_ids: Sequence[int],
        operation_type: str,
        *,
        request_id: str,
        destination_hex: int | None = None,
        target_hex: int | None = None,
        target_id: int | None = None,
        movement_mode: str = "NORMAL",
        weapon_choices: Mapping[int | str, int] | None = None,
        whole_side_wait: bool = False,
        fire_mode: str | None = None,
        guider_id: int | None = None,
        shooter_id: int | None = None,
        weapon_id: int | None = None,
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
        if operation in {"BOARD", "UNLOAD"} and type(target_id) is not int:
            raise CommandGatewayError(
                "target_required",
                f"{operation} requires an exact server-issued target id",
            )
        if operation == "GUIDED_FIRE":
            if fire_mode != "GUIDED":
                raise CommandGatewayError(
                    "guided_fire_mode_required",
                    "GUIDED_FIRE requires fire_mode GUIDED",
                )
            if len(actors) != 1 or actors[0] != guider_id:
                raise CommandGatewayError(
                    "guided_fire_guider_mismatch",
                    "GUIDED_FIRE selection must contain exactly guider_id",
                )
            for name, value in (
                ("guider_id", guider_id),
                ("shooter_id", shooter_id),
                ("weapon_id", weapon_id),
                ("target_id", target_id),
            ):
                if type(value) is not int or value < 0:
                    raise CommandGatewayError(
                        f"invalid_{name}",
                        f"{name} must be a non-negative exact int",
                    )
        if operation == "INDIRECT_FIRE":
            if fire_mode != "INDIRECT":
                raise CommandGatewayError(
                    "indirect_fire_mode_required",
                    "INDIRECT_FIRE requires fire_mode INDIRECT",
                )
            if len(actors) != 1 or actors[0] != shooter_id:
                raise CommandGatewayError(
                    "indirect_fire_shooter_mismatch",
                    "INDIRECT_FIRE selection must contain exactly shooter_id",
                )
            for name, value in (
                ("shooter_id", shooter_id),
                ("weapon_id", weapon_id),
                ("target_hex", target_hex),
            ):
                if type(value) is not int or value < 0:
                    raise CommandGatewayError(
                        f"invalid_{name}",
                        f"{name} must be a non-negative exact int",
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
            target_hex,
            target_id,
            str(movement_mode).upper(),
            tuple(sorted(choices.items())),
            whole_side_wait,
            fire_mode,
            guider_id,
            shooter_id,
            weapon_id,
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
                stored_cached = self._previews.get(cached[1])
                if stored_cached is None:
                    raise CommandGatewayError(
                        "preview_superseded",
                        "this operation was replaced by a newer selection",
                    )
                return deepcopy(stored_cached.preview)

            results: list[UnitOperationPreview] = []
            action_ids: list[str] = []
            if not whole_side_wait:
                for unit_id in actors:
                    result, action_id = self._preview_unit(
                        unit_id,
                        operation,
                        request_id=request,
                        destination_hex=destination_hex,
                        target_hex=target_hex,
                        target_id=target_id,
                        movement_mode=movement_mode,
                        weapon_choice=choices.get(unit_id),
                        fire_mode=fire_mode,
                        guider_id=guider_id,
                        shooter_id=shooter_id,
                        guided_weapon_id=weapon_id,
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
            self._replace_overlapping_previews(preview)
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

    def preview_decision(
        self,
        decision: SeatDecision,
        *,
        request_id: str,
    ) -> SeatDecisionPreview:
        """Validate every operation before authorizing one atomic side batch."""

        if not isinstance(decision, SeatDecision):
            raise TypeError("decision must be a SeatDecision")
        request = _nonempty(request_id, "request_id")
        window = self.controller.window()
        if window.submitted:
            raise CommandGatewayError(
                "side_already_submitted", "this side is already waiting"
            )
        signature = (
            window.decision_window_index,
            tuple(
                canonical_hash(operation.preview_arguments())
                for operation in decision.operations
            ),
        )
        with self._lock:
            self._sync_window(window.decision_window_index)
            cached = self._decision_preview_requests.get(request)
            if cached is not None:
                if cached[0] != signature:
                    raise CommandGatewayError(
                        "request_id_conflict",
                        "decision preview request_id was reused with another decision",
                    )
                stored = self._decision_previews.get(cached[1])
                if stored is None:
                    raise CommandGatewayError(
                        "decision_preview_superseded",
                        "this decision preview is no longer available",
                    )
                return deepcopy(stored.preview)

            previews: list[GroupOperationPreview] = []
            component_tokens: list[str] = []
            action_ids: list[str] = []
            try:
                for index, operation in enumerate(decision.operations):
                    preview = self.preview_operation(
                        operation.actor_ids,
                        operation.operation_type,
                        request_id=f"{request}:operation:{index}",
                        destination_hex=operation.destination_hex,
                        target_hex=operation.target_hex,
                        target_id=operation.target_id,
                        movement_mode=operation.movement_mode,
                        weapon_choices=dict(operation.weapon_choices),
                        whole_side_wait=operation.whole_side_wait,
                        fire_mode=operation.fire_mode,
                        guider_id=operation.guider_id,
                        shooter_id=operation.shooter_id,
                        weapon_id=operation.weapon_id,
                    )
                    previews.append(preview)
                    component_tokens.append(preview.preview_token)
                    if not preview.can_execute:
                        raise CommandGatewayError(
                            "preview_not_executable",
                            "one decision operation has no executable actors",
                        )
                    if (
                        preview.partial_confirmation_required
                        and not operation.confirm_partial
                    ):
                        raise CommandGatewayError(
                            "partial_confirmation_required",
                            "confirm every partial operation before submission",
                        )
                    stored_operation = self._previews[preview.preview_token]
                    action_ids.extend(stored_operation.action_ids)
            except BaseException:
                for token in component_tokens:
                    self._previews.pop(token, None)
                for index in range(len(decision.operations)):
                    self._preview_requests.pop(
                        f"{request}:operation:{index}", None
                    )
                raise

            token = "seat-decision-" + canonical_hash({
                "session_id": self.controller.session_id,
                "controller_id": self.controller.controller_id,
                "window": window.decision_window_index,
                "request_id": request,
                "signature": signature,
                "component_tokens": component_tokens,
            })[:32]
            executable = sum(item.executable_count for item in previews)
            skipped = sum(item.rejected_count for item in previews)
            value = SeatDecisionPreview(
                token,
                window.decision_window_index,
                tuple(previews),
                len(decision.operations),
                executable,
                skipped,
                True,
            )
            self._decision_previews[token] = _StoredDecisionPreview(
                value,
                tuple(component_tokens),
                tuple(action_ids),
            )
            self._decision_preview_requests[request] = (signature, token)
            return deepcopy(value)

    def execute_decision(
        self,
        preview_token: str,
        *,
        request_id: str,
    ) -> SeatDecisionExecutionResult:
        """Stage a fully prevalidated decision and submit the side once."""

        request = _nonempty(request_id, "request_id")
        token = _nonempty(preview_token, "preview_token")
        with self._lock:
            cached = self._decision_execute_requests.get(request)
            if cached is not None:
                if cached[0] != token:
                    raise CommandGatewayError(
                        "request_id_conflict",
                        "decision execute request_id was reused with another preview",
                    )
                return deepcopy(cached[1])
            try:
                stored = self._decision_previews[token]
            except KeyError as exc:
                raise CommandGatewayError(
                    "decision_preview_not_found",
                    "seat decision preview is unavailable or expired",
                ) from exc
            preview = stored.preview
            window = self.controller.window()
            if preview.decision_window_index != window.decision_window_index:
                self._sync_window(window.decision_window_index)
                raise CommandGatewayError(
                    "decision_preview_expired",
                    "seat decision belongs to an older cycle",
                )
            if stored.execution_result is not None:
                if stored.executed_request_id == request:
                    return deepcopy(stored.execution_result)
                raise CommandGatewayError(
                    "decision_preview_already_executed",
                    "seat decision was already executed",
                )
            if any(token not in self._previews for token in stored.component_tokens):
                raise CommandGatewayError(
                    "decision_preview_superseded",
                    "one component operation was replaced",
                )
            self.controller.clear_staged()
            try:
                for index, action_id in enumerate(stored.action_ids):
                    self.controller.stage_action(
                        action_id,
                        request_id=f"{request}:stage:{index}",
                        expected_window=preview.decision_window_index,
                    )
                command = self.controller.submit_batch(
                    request_id=request,
                    expected_window=preview.decision_window_index,
                )
            except BaseException:
                self.controller.clear_staged()
                raise
            result = _decision_product_result(preview, request, command)
            stored.executed_request_id = request
            stored.execution_result = result
            self._decision_execute_requests[request] = (token, result)
            for component in stored.component_tokens:
                self._previews.pop(component, None)
            return deepcopy(result)

    def _replace_overlapping_previews(
        self, preview: GroupOperationPreview,
    ) -> None:
        incoming = {item.unit_id for item in preview.per_unit_results}
        for token, stored in tuple(self._previews.items()):
            if stored.execution_result is not None:
                continue
            existing = {
                item.unit_id for item in stored.preview.per_unit_results
            }
            if (
                preview.whole_side_wait
                or stored.preview.whole_side_wait
                or incoming.intersection(existing)
            ):
                del self._previews[token]

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
        target_hex: int | None,
        target_id: int | None,
        movement_mode: str,
        weapon_choice: int | None,
        fire_mode: str | None,
        guider_id: int | None,
        shooter_id: int | None,
        guided_weapon_id: int | None,
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

        if operation == "INDIRECT_FIRE":
            if unit_id != shooter_id:
                return _rejected(
                    unit_id,
                    name,
                    "indirect_fire_shooter_mismatch",
                    "所选单位不是指定的间接火力发射平台",
                ), None
            try:
                proposal = self.controller.plan_indirect_fire(
                    unit_id,
                    int(guided_weapon_id),
                    int(target_hex),
                    request_id=f"{request_id}:indirect:{unit_id}",
                    expected_window=menu.decision_window_index,
                )
            except (CommandGatewayError, TypeError, ValueError) as exc:
                code = getattr(exc, "code", "indirect_fire_plan_rejected")
                message = getattr(exc, "message", str(exc))
                return _rejected(unit_id, name, code, message), None
            return UnitOperationPreview(
                unit_id,
                name,
                True,
                f"间接火力计划格{target_hex}，武器{guided_weapon_id}",
                weapon_options=({
                    "weapon_id": guided_weapon_id,
                    "target_hex": target_hex,
                    "summary": f"武器{guided_weapon_id}",
                },),
            ), proposal.action_id

        candidates = _operation_candidates(menu, operation)
        if operation == "SHOOT":
            candidates = tuple(
                item
                for item in candidates
                if item.get("action", {}).get("target_id") == target_id
            )
        elif operation in {"BOARD", "UNLOAD"}:
            candidates = tuple(
                item
                for item in candidates
                if item.get("action", {}).get("target_id") == target_id
            )
        elif operation == "GUIDED_FIRE":
            candidates = tuple(
                item for item in candidates
                if _matches_guided_fire_choice(
                    item,
                    fire_mode=fire_mode,
                    guider_id=guider_id,
                    shooter_id=shooter_id,
                    weapon_id=guided_weapon_id,
                    target_id=target_id,
                )
            )
        candidates = tuple(sorted(candidates, key=lambda item: str(item["action_id"])))
        if not candidates:
            code = (
                "target_not_shootable" if operation == "SHOOT"
                else "guided_fire_capability_unavailable"
                if operation == "GUIDED_FIRE"
                else "operation_unavailable"
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
            self._decision_previews.clear()
            self._decision_preview_requests.clear()
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


def _decision_product_result(
    preview: SeatDecisionPreview,
    request_id: str,
    command: CommandResult,
) -> SeatDecisionExecutionResult:
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
    return SeatDecisionExecutionResult(
        preview.preview_token,
        request_id,
        command.accepted,
        True,
        command.advanced,
        state,
        preview.operation_count,
        preview.executable_count,
        preview.skipped_count,
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
    raw_attributes = unit.get("raw_attributes")
    source_frame = (
        raw_attributes.get("source_frame")
        if isinstance(raw_attributes, Mapping)
        else None
    )
    source_name = (
        source_frame.get("name")
        if isinstance(source_frame, Mapping)
        else None
    )
    value = (
        source_name
        or unit.get("name")
        or unit.get("subtype")
        or unit.get("operator_type")
    )
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
    if operation == "GUIDED_FIRE":
        parameters = action.get("parameters", {})
        return (
            f"引导单位{parameters.get('guider_id')}为发射平台"
            f"{parameters.get('shooter_id')}引导射击目标"
            f"{action.get('target_id')}，武器{parameters.get('weapon_id')}"
        )
    if operation == "BOARD":
        return f"人员{action.get('actor_id')}搭载载具{action.get('target_id')}"
    if operation == "UNLOAD":
        return f"载具{action.get('actor_id')}卸载人员{action.get('target_id')}"
    if operation == "ENTER_CONCEALMENT":
        return f"单位{action.get('actor_id')}进入隐蔽姿态"
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
        "GUIDED_FIRE": "当前没有与该引导单位匹配的合法引导射击机会",
        "BOARD": "当前没有合法搭载机会",
        "UNLOAD": "当前没有合法下车机会",
        "ENTER_CONCEALMENT": "当前不满足主动隐蔽条件",
        "STOP_MOVE": "当前未在移动或不能停止",
        "WEAPON_UNFOLD": "当前不能展开武器",
        "WEAPON_LOCK": "当前不能锁定武器",
        "WAIT": "当前不能等待",
    }.get(operation, "当前不能执行该操作")


def _product_capability_family(
    operation: str,
) -> DecisionCapabilityFamily | None:
    value = "DIRECT_FIRE" if operation == "SHOOT" else operation
    try:
        return DecisionCapabilityFamily(value)
    except ValueError:
        return None


def _machine_capability_unavailable(status: Any) -> tuple[str, str]:
    if status.status is CapabilityAvailabilityStatus.UNKNOWN:
        return (
            "CAPABILITY_UNKNOWN",
            "当前未提供完整能力状态；具体操作仍需正式预览",
        )
    if status.status is CapabilityAvailabilityStatus.MISSING:
        return "CAPABILITY_MISSING", "当前能力证据缺失"
    if status.status is CapabilityAvailabilityStatus.NOT_APPLICABLE:
        return "CAPABILITY_NOT_APPLICABLE", "该单位不适用此能力"
    blockers = tuple(
        f"{value.producer_namespace}:{value.code}"
        for value in status.blocker_codes
    )
    return (
        blockers[0] if blockers else "CAPABILITY_BLOCKED",
        "当前能力被权威状态阻塞",
    )


def _capability_unavailable(
    operation: str,
    menu: Any,
) -> tuple[str, str]:
    """Explain only conditions already evidenced by the frozen side menu."""

    available = set(menu.available)
    state = str(menu.unit.get("state", "")).upper()
    moving = "STOP_MOVE" in available or state == "MOVING"
    transitioning = state == "TRANSITIONING"
    if operation == "SHOOT":
        visible = menu.unit.get("observed_enemy_ids", ())
        if not visible:
            return "no_visible_enemy", "当前未发现可见敌军"
        if moving:
            return "actor_moving", "单位正在移动；当前武器不能移动射击"
        if transitioning:
            return "actor_transitioning", "单位正在进行状态转换"
        if "WEAPON_UNFOLD" in available:
            return "weapon_not_unfolded", "武器尚未展开"
        return (
            "no_legal_shoot",
            "可见目标不满足射程、弹药、冷却、武器或目标类型条件",
        )
    if operation == "GUIDED_FIRE":
        return (
            "no_guided_fire_capability",
            "当前权威状态没有合法的引导单位、发射平台、武器和目标组合",
        )
    if operation in {"WEAPON_LOCK", "WEAPON_UNFOLD"}:
        label = "锁定" if operation == "WEAPON_LOCK" else "展开"
        if moving:
            return "actor_must_be_stopped", f"单位移动中；停止后才能{label}武器"
        if transitioning:
            return "transition_in_progress", "武器状态转换正在进行"
        if operation == "WEAPON_LOCK" and "WEAPON_UNFOLD" in available:
            return "weapon_already_locked", "武器已经锁定"
        if operation == "WEAPON_UNFOLD" and "WEAPON_LOCK" in available:
            return "weapon_already_unfolded", "武器已经展开"
    return "operation_unavailable", _unavailable_message(operation)


def _operation_candidates(menu: Any, operation: str) -> tuple[Mapping[str, Any], ...]:
    """Return only Engine-issued candidates for one Product operation."""

    key = "FIRE" if operation == "GUIDED_FIRE" else operation
    values = tuple(menu.available.get(key, ()))
    if operation != "GUIDED_FIRE":
        return values
    return tuple(
        item for item in values
        if isinstance(item, Mapping)
        and isinstance(item.get("action"), Mapping)
        and isinstance(item["action"].get("parameters"), Mapping)
        and item["action"]["parameters"].get("fire_mode") == "GUIDED"
    )


def _matches_guided_fire_choice(
    item: Mapping[str, Any],
    *,
    fire_mode: str | None,
    guider_id: int | None,
    shooter_id: int | None,
    weapon_id: int | None,
    target_id: int | None,
) -> bool:
    action = item.get("action")
    if not isinstance(action, Mapping):
        return False
    parameters = action.get("parameters")
    if not isinstance(parameters, Mapping):
        return False
    return (
        fire_mode == "GUIDED"
        and action.get("action_type") == "FIRE"
        and action.get("actor_id") == guider_id
        and action.get("target_id") == target_id
        and parameters.get("fire_mode") == fire_mode
        and parameters.get("guider_id") == guider_id
        and parameters.get("shooter_id") == shooter_id
        and parameters.get("weapon_id") == weapon_id
    )


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
