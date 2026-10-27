"""Provider-neutral tools for one authenticated seat in a virtual game.

The host, not model arguments, selects the session and seat. All mutations pass
through RealtimeAgentSession and its normal admission, execution and log paths.
The adapter never advances ticks. Keep one adapter per seat per episode so
request deduplication survives model retries; do not share it across sessions.
"""

from __future__ import annotations

import json
from collections.abc import Set as AbstractSet
from copy import deepcopy
from dataclasses import asdict, replace
from threading import RLock
from typing import Any

from wargame_native_engine.adjudication.fire_effect_query import (
    FireEffectQuery,
    FireEffectQueryError,
    fire_effect_query_schema,
)
from wargame_native_engine.core.region_visibility import (
    RegionVisibilityQuery,
    RegionVisibilityQueryError,
    region_visibility_query_schema,
)
from wargame_native_engine.agents.realtime import (
    RealtimeAgentSession,
    RealtimeSeatBinding,
    RealtimeSeatView,
)
from wargame_native_engine.agents.seat import SeatOperation
from wargame_native_engine.operations.product import PRODUCT_OPERATION_TYPES


class GameToolError(ValueError):
    """Stable adapter error; the host should report it as a tool error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _integer(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise GameToolError(
            "invalid_arguments", f"{name} must be a nonnegative integer"
        )
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise GameToolError(
            "invalid_arguments", f"{name} must contain 1–128 characters"
        )
    return value


def _keys(
    value: object, required: AbstractSet[str], optional: AbstractSet[str] = frozenset()
) -> dict:
    if (
        type(value) is not dict
        or not required <= value.keys()
        or value.keys() - required - optional
    ):
        raise GameToolError(
            "invalid_arguments", "missing or unsupported argument fields"
        )
    return value


def _actors(value: object) -> tuple[int, ...]:
    if type(value) is not list or len(value) > 64:
        raise GameToolError(
            "invalid_arguments", "actor_ids must be an array of at most 64 IDs"
        )
    ids = tuple(_integer(item, "actor_id") for item in value)
    if len(ids) != len(set(ids)):
        raise GameToolError("invalid_arguments", "actor_ids must be unique")
    return ids


def _operation(value: object) -> SeatOperation:
    optional = {
        "destination_hex",
        "target_hex",
        "target_id",
        "movement_mode",
        "weapon_choices",
        "whole_side_wait",
        "fire_mode",
        "guider_id",
        "shooter_id",
        "weapon_id",
    }
    value = _keys(value, {"operation_type", "actor_ids"}, optional)
    kind = value["operation_type"]
    if kind not in PRODUCT_OPERATION_TYPES:
        raise GameToolError("invalid_arguments", "unknown operation_type")
    kwargs = dict(value)
    kwargs["actor_ids"] = _actors(value["actor_ids"])
    for name in (
        "destination_hex",
        "target_hex",
        "target_id",
        "guider_id",
        "shooter_id",
        "weapon_id",
    ):
        if name in value and value[name] is not None:
            _integer(value[name], name)
    for name in ("movement_mode", "fire_mode"):
        if name in value and (name == "movement_mode" or value[name] is not None):
            _text(value[name], name)
    if "whole_side_wait" in value and type(value["whole_side_wait"]) is not bool:
        raise GameToolError("invalid_arguments", "whole_side_wait must be boolean")
    if "weapon_choices" in value:
        choices = value["weapon_choices"]
        if type(choices) is not list or len(choices) > 64:
            raise GameToolError(
                "invalid_arguments", "weapon_choices must be an array of pairs"
            )
        parsed = []
        for pair in choices:
            if type(pair) is not list or len(pair) != 2:
                raise GameToolError(
                    "invalid_arguments", "weapon choice must be [actor_id, weapon_id]"
                )
            parsed.append(tuple(_integer(x, "weapon choice") for x in pair))
        kwargs["weapon_choices"] = tuple(parsed)
    try:
        return SeatOperation(**kwargs)
    except (TypeError, ValueError) as exc:
        # The immutable domain contract owns cross-field validation. Translate
        # only construction failures, never exceptions from engine execution.
        raise GameToolError("invalid_arguments", str(exc)) from exc


class GameToolAdapter:
    """Bounded in-process tool dispatcher with seat/episode ownership.

    Preview validity is advisory: a background tick may move between preview
    and submission. The existing session is authoritative and validates the
    current version atomically on admission and the operation again on application. An
    accepted receipt means queued, NEVER that the operation succeeded.
    Strict freshness can starve slow inference on continuously advancing ticks;
    the host must own scheduling/refresh, not silently relabel old proposals.
    """

    def __init__(
        self,
        session: RealtimeAgentSession,
        binding: RealtimeSeatBinding,
        *,
        max_response_bytes: int = 512 * 1024,
        max_requests: int = 4096,
    ) -> None:
        if not isinstance(session, RealtimeAgentSession) or not isinstance(
            binding, RealtimeSeatBinding
        ):
            raise TypeError("session and binding must be realtime objects")
        for name, value in (
            ("max_response_bytes", max_response_bytes),
            ("max_requests", max_requests),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be positive")
        session.latest_view(binding)  # Resolve authentic binding before exposing tools.
        self._session, self._binding = session, binding
        self._episode = session.episode_id
        self._max_response_bytes, self._max_requests = max_response_bytes, max_requests
        self._previews: dict[str, tuple[int, SeatOperation, bool, bool]] = {}
        self._preview_requests: dict[str, tuple[int, SeatOperation]] = {}
        self._requests: dict[str, tuple[str, bool, dict]] = {}
        self._used_previews: set[str] = set()
        self._owned_sequences: dict[int, tuple[str, ...]] = {}
        self._lock = RLock()

    def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Validate JSON arguments and return a detached JSON-serializable value."""
        with self._lock:
            if self._session.episode_id != self._episode:
                raise GameToolError(
                    "episode_changed", "create a new adapter after session reset"
                )
            handlers = {
                "game_observation": self._observation,
                "game_capabilities": self._capabilities,
                "preview_game_operation": self._preview,
                "submit_game_operation": self._submit,
                "game_command_result": self._result,
                "game_region_visibility": self._region_visibility,
                "game_weapon_catalog": self._weapon_catalog,
                "game_fire_effect": self._fire_effect,
            }
            if not isinstance(name, str) or name not in handlers:
                raise GameToolError("unknown_tool", "tool name is not allowlisted")
            try:
                result = handlers[name](arguments)
            except (RegionVisibilityQueryError, FireEffectQueryError) as exc:
                # These are documented query failures, not engine exceptions.
                raise GameToolError(exc.code, str(exc)) from exc
            return self._bounded(result)

    def _bounded(self, value: dict) -> dict:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        if len(encoded.encode("utf-8")) > self._max_response_bytes:
            raise GameToolError(
                "response_too_large",
                "response exceeds host limit; no silent truncation",
            )
        return json.loads(encoded)

    def _identity(self, view: RealtimeSeatView) -> dict:
        return {
            "episode_id": self._episode,
            "side": self._binding.seat,
            "observation_version": view.observation_version,
            "tick_index": view.tick_index,
            "frame_index": view.frame_index,
            "sim_time_ms": view.sim_time_ms,
        }

    def _observation(self, args: dict) -> dict:
        _keys(args, set())
        view = self._session.latest_view(self._binding)
        return {
            **self._identity(view),
            "terminated": view.terminated,
            "truncated": view.truncated,
            "observation": view.to_record()["observation"],
        }

    def _capabilities(self, args: dict) -> dict:
        _keys(args, {"actor_ids"})
        actors = _actors(args["actor_ids"])
        view, _, capabilities = self._session.operation_capabilities_snapshot(
            self._binding, actors
        )
        if not set(actors) <= set(view.capabilities.controllable_actor_ids):
            raise GameToolError(
                "actor_not_owned", "selected actors do not belong to this seat"
            )
        return {
            **self._identity(view),
            "controllable_actor_ids": list(view.capabilities.controllable_actor_ids),
            "capabilities": capabilities,
            "semantics": "candidate operations; preview and execution validation remain authoritative",
        }

    def _region_visibility(self, args: dict) -> dict:
        _keys(
            args,
            {"observation_version", "actor_id", "region", "target_profile"},
            {"actor_assertion"},
        )
        version = _integer(args["observation_version"], "observation_version")
        actor = _integer(args["actor_id"], "actor_id")
        query = RegionVisibilityQuery.from_dict({
            name: value for name, value in args.items()
            if name not in {"observation_version", "actor_id"}
        })
        view, result = self._session.region_visibility_snapshot(
            self._binding, observation_version=version, actor_id=actor, query=query,
        )
        return {**self._identity(view), "visibility": result.to_dict()}

    def _weapon_catalog(self, args: dict) -> dict:
        _keys(args, set())
        view, catalog = self._session.weapon_catalog_snapshot(self._binding)
        return {**self._identity(view), "catalog": catalog}

    def _fire_effect(self, args: dict) -> dict:
        _keys(
            args,
            {"observation_version", "actor_id", "weapon_id"},
            {"target_id", "target_hex", "attacker_model", "target_model",
             "target_profile", "fire_mode", "guider_id", "indirect_profile"},
        )
        version = _integer(args["observation_version"], "observation_version")
        query = FireEffectQuery.from_dict({
            name: value for name, value in args.items()
            if name != "observation_version"
        })
        view, result = self._session.fire_effect_snapshot(
            self._binding, observation_version=version, query=query,
        )
        return {**self._identity(view), "effect": result}

    def _preview(self, args: dict) -> dict:
        _keys(args, {"observation_version", "operation", "request_id"})
        version = _integer(args["observation_version"], "observation_version")
        request_id = _text(args["request_id"], "request_id")
        operation = _operation(args["operation"])
        prior = self._preview_requests.get(request_id)
        if prior is not None and prior != (version, operation):
            raise GameToolError(
                "request_id_conflict", "preview request ID was used for different arguments"
            )
        if prior is None and len(self._preview_requests) >= self._max_requests:
            raise GameToolError("request_capacity", "episode preview budget exhausted")
        view = self._session.latest_view(self._binding)
        if version != view.observation_version:
            raise GameToolError(
                "stale_observation", "read the latest observation before previewing"
            )
        if not set(operation.actor_ids) <= set(
            view.capabilities.controllable_actor_ids
        ):
            raise GameToolError(
                "actor_not_owned", "selected actors do not belong to this seat"
            )
        # Reserve before entering the service: it maintains its own registry,
        # even if a tick race or response-size failure prevents token delivery.
        # Keep the reservation on failure; identical retries are still allowed.
        self._preview_requests[request_id] = (version, operation)
        preview = self._session.preview_operation(
            self._binding, operation, observation_version=version, request_id=request_id
        )
        if self._session.latest_view(self._binding).observation_version != version:
            raise GameToolError(
                "stale_observation",
                "environment advanced during preview; preview again",
            )
        result = {
            **self._identity(view),
            "preview": preview.to_record(),
            "advisory": True,
        }
        self._bounded(result)  # Never authorize a token that cannot be returned.
        self._previews[preview.preview_token] = (
            version,
            operation,
            preview.partial_confirmation_required,
            preview.can_execute,
        )
        return result

    def _submit(self, args: dict) -> dict:
        _keys(args, {"preview_token", "request_id", "confirm_partial"})
        token = _text(args["preview_token"], "preview_token")
        request_id = _text(args["request_id"], "request_id")
        confirm = args["confirm_partial"]
        if type(confirm) is not bool:
            raise GameToolError("invalid_arguments", "confirm_partial must be boolean")
        cached = self._requests.get(request_id)
        if cached is not None:
            if (token, confirm) != cached[:2]:
                raise GameToolError(
                    "request_id_conflict", "request ID was used for different arguments"
                )
            return deepcopy(cached[2])
        if len(self._requests) >= self._max_requests:
            raise GameToolError(
                "request_capacity", "episode submission budget exhausted"
            )
        if token in self._used_previews:
            raise GameToolError(
                "preview_already_submitted",
                "use original request ID to read its receipt",
            )
        if token not in self._previews:
            raise GameToolError(
                "preview_not_owned", "preview token was not issued by this adapter"
            )
        version, operation, needs_confirm, can_execute = self._previews[token]
        if not can_execute:
            raise GameToolError(
                "operation_not_executable", "preview contains no executable operation"
            )
        if needs_confirm and not confirm:
            raise GameToolError(
                "partial_confirmation_required", "explicitly confirm the valid subset"
            )
        view = self._session.latest_view(self._binding)
        if version != view.observation_version:
            raise GameToolError(
                "stale_observation", "preview again against the latest observation"
            )
        receipt = self._session.submit_operation_if_current(
            self._binding,
            observation_version=version,
            operation=replace(operation, confirm_partial=confirm),
        )
        result = {
            "episode_id": self._episode,
            "observation_version": version,
            "request_id": request_id,
            **asdict(receipt),
            "status": receipt.status.value,
            "semantics": "accepted means queued; query game_command_result for application outcome",
        }
        # Record BEFORE response-size validation: retries must never re-submit.
        self._requests[request_id] = (token, confirm, deepcopy(result))
        self._used_previews.add(token)
        if receipt.command_sequence is not None:
            self._owned_sequences[receipt.command_sequence] = receipt.execution_handles
        return result

    def _result(self, args: dict) -> dict:
        _keys(args, {"command_sequence"})
        sequence = _integer(args["command_sequence"], "command_sequence")
        if sequence not in self._owned_sequences:
            raise GameToolError(
                "command_not_owned", "command was not submitted by this adapter"
            )
        event = self._session.command_event(self._binding, sequence)
        if event is None:
            raise GameToolError("command_unavailable", "command event is unavailable")
        executions = [
            self._session.execution_status(self._binding, handle).to_record()
            for handle in self._owned_sequences[sequence]
        ]
        return {
            "episode_id": self._episode,
            "command": event.to_record(),
            "executions": executions,
            "semantics": "APPLIED means admitted to the engine, not necessarily execution completed",
        }


def function_schemas() -> list[dict[str, Any]]:
    """Fresh provider-neutral JSON Schema definitions (wrap for your provider)."""
    integer = {"type": "integer", "minimum": 0}
    text = {"type": "string", "minLength": 1, "maxLength": 128}
    actors = {"type": "array", "items": integer, "maxItems": 64, "uniqueItems": True}
    props: dict[str, Any] = {
        "operation_type": {"type": "string", "enum": list(PRODUCT_OPERATION_TYPES)},
        "actor_ids": actors,
    }
    for key in (
        "destination_hex",
        "target_hex",
        "target_id",
        "guider_id",
        "shooter_id",
        "weapon_id",
    ):
        props[key] = {"type": ["integer", "null"], "minimum": 0}
    props["target_id"]["description"] = (
        "Explicit visible enemy selection. SHOOT uses target_id OR target_hex; "
        "INDIRECT_FIRE may bind target_id with target_hex. For these two fire operations, no ID means no damage."
    )
    props["target_hex"]["description"] = (
        "Exact map hex. SHOOT with only target_hex fires an empty shot; "
        "INDIRECT_FIRE requires this field and never infers a target from occupants."
    )
    props.update(
        {
            "movement_mode": text,
            "fire_mode": {"type": ["string", "null"], "minLength": 1, "maxLength": 128},
            "whole_side_wait": {"type": "boolean"},
            "weapon_choices": {
                "description": "Explicit [actor_id, weapon_id] pairs required for SHOOT and WEAPON_LOCK. WEAPON_UNLOCK takes no weapon choice; it requires stopped movement and completed transitions.",
                "type": "array",
                "maxItems": 64,
                "items": {
                    "type": "array",
                    "items": integer,
                    "minItems": 2,
                    "maxItems": 2,
                },
            },
        }
    )
    operation = {
        "type": "object",
        "properties": props,
        "required": ["operation_type", "actor_ids"],
        "additionalProperties": False,
    }
    definitions = [
        ("game_observation", "Read only this seat's observation and provenance.", {}),
        (
            "game_capabilities",
            "Read candidate game operations; not a guarantee of execution.",
            {"actor_ids": actors},
        ),
        (
            "preview_game_operation",
            "Validate a virtual game operation without submitting it.",
            {
                "observation_version": integer,
                "request_id": text,
                "operation": operation,
            },
        ),
        (
            "submit_game_operation",
            "Queue a previously previewed game operation; does not advance time.",
            {
                "preview_token": text,
                "request_id": text,
                "confirm_partial": {"type": "boolean"},
            },
        ),
        (
            "game_command_result",
            "Read this adapter's command admission/application event.",
            {"command_sequence": integer},
        ),
    ]
    schemas = [
        {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": fields,
                "required": list(fields),
                "additionalProperties": False,
            },
        }
        for name, description, fields in definitions
    ]
    region = region_visibility_query_schema()
    region["properties"] = {
        "observation_version": integer,
        "actor_id": integer,
        **region["properties"],
    }
    region["required"] = ["observation_version", "actor_id", *region["required"]]
    effect = fire_effect_query_schema()
    effect["properties"] = {
        "observation_version": integer,
        **effect["properties"],
    }
    effect["required"] = ["observation_version", *effect["required"]]
    schemas.extend([
        {
            "name": "game_region_visibility",
            "description": (
                "Read hypothetical target visibility over a bounded map rectangle; "
                "does not reveal occupants or change the observer's state."
            ),
            "parameters": region,
        },
        {
            "name": "game_weapon_catalog",
            "description": "Read public weapon definitions, model mappings and source provenance.",
            "parameters": {
                "type": "object", "properties": {}, "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "game_fire_effect",
            "description": (
                "Estimate virtual-game damage using a visible target or an explicit "
                "hypothesis. Never fires, draws randomness or changes ammunition."
            ),
            "parameters": effect,
        },
    ])
    return deepcopy(schemas)
