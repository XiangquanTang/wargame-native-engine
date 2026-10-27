"""Optional JSON boundary for model hosts; no provider SDK or network access.

The engine adapter remains an exception-raising Python API. This transport adds
strict JSON parsing and a versioned success/error envelope for external model
runners. It does not catch unexpected programming or infrastructure failures.
"""

from __future__ import annotations

import json
from hashlib import sha256
from math import isfinite
from typing import Any

from wargame_native_engine.agents.realtime import RealtimeAgentSessionError
from wargame_native_engine.agents.tool_calling import (
    GameToolAdapter,
    GameToolError,
    function_schemas,
)
from wargame_native_engine.gateway.models import CommandGatewayError
from wargame_native_engine.version import (
    ADJUDICATION_SEMANTICS_VERSION,
    ENGINE_VERSION,
)

TOOL_TRANSPORT_VERSION = "game-tools-json/1.0"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GameToolError("invalid_json", "duplicate object keys are forbidden")
        result[key] = value
    return result


def _nonfinite_constant(value: str) -> None:
    raise GameToolError("invalid_json", "nonfinite JSON numbers are forbidden")


def _recovery(code: str) -> str:
    """Machine-readable advice, not an automatic retry policy."""
    if code in {
        "stale_observation",
        "observation_version_stale",
        "observation_version_expired_or_unknown",
        "preview_superseded",
    }:
        return "refresh_observation"
    if code in {
        "episode_changed",
        "realtime_binding_invalid",
        "realtime_binding_unknown",
    }:
        return "create_adapter_for_current_episode"
    if code in {"response_too_large", "request_capacity", "command_unavailable"}:
        return "host_intervention"
    return "review_request"


class GameToolTransport:
    """Parse bounded JSON arguments and return explicit, detached envelopes.

    ``invoke`` catches only the documented domain errors. An unexpected error
    must be logged by the host; blindly retrying a mutating request is unsafe.
    Submission response errors do not prove that no command was queued. Keep
    the original request ID and adapter for reconciliation, never generate a
    new submission ID merely because a response was lost.
    """

    def __init__(
        self, adapter: GameToolAdapter, *, max_argument_bytes: int = 64 * 1024
    ) -> None:
        if not isinstance(adapter, GameToolAdapter):
            raise TypeError("adapter must be a GameToolAdapter")
        if type(max_argument_bytes) is not int or max_argument_bytes <= 0:
            raise ValueError("max_argument_bytes must be positive")
        self._adapter = adapter
        self._max_argument_bytes = max_argument_bytes

    def describe(self) -> dict[str, Any]:
        """Fresh metadata that the host can persist beside an evaluation run."""
        schemas = function_schemas()
        encoded = json.dumps(schemas, sort_keys=True, separators=(",", ":"))
        return {
            "transport_version": TOOL_TRANSPORT_VERSION,
            "engine_version": ENGINE_VERSION,
            "adjudication_semantics_version": ADJUDICATION_SEMANTICS_VERSION,
            "schema_sha256": sha256(encoded.encode("utf-8")).hexdigest(),
            "freshness_policy": "strict_current_observation",
            "max_argument_bytes": self._max_argument_bytes,
            "max_json_depth": 64,
            "functions": schemas,
        }

    def invoke(self, name: str, arguments_json: str) -> dict[str, Any]:
        """Invoke one allowlisted tool; transport errors have ``ok=False``.

        Input size is checked before decoding. Duplicate keys, non-JSON values,
        excessive parser nesting, non-object roots and invalid UTF-8 surrogate
        strings fail closed. No eval, imports, paths or tool execution happen
        during parsing. The host still owns authentication and tick scheduling.
        """
        try:
            arguments = self._parse(arguments_json)
            result = self._adapter.dispatch(name, arguments)
        except (GameToolError, RealtimeAgentSessionError, CommandGatewayError) as exc:
            message = getattr(exc, "message", str(exc))
            return {
                "transport_version": TOOL_TRANSPORT_VERSION,
                "ok": False,
                "error": {
                    "code": exc.code,
                    "message": message,
                    "recovery": _recovery(exc.code),
                },
            }
        return {
            "transport_version": TOOL_TRANSPORT_VERSION,
            "ok": True,
            "result": result,
        }

    def _parse(self, source: str) -> dict[str, Any]:
        if type(source) is not str:
            raise GameToolError("invalid_json", "arguments must be a JSON string")
        # A UTF-8 encoding cannot be shorter than the number of code points.
        # Reject obvious oversize strings without creating another large copy.
        if len(source) > self._max_argument_bytes:
            raise GameToolError("arguments_too_large", "arguments exceed host limit")
        try:
            if len(source.encode("utf-8")) > self._max_argument_bytes:
                raise GameToolError(
                    "arguments_too_large", "arguments exceed host limit"
                )
            value = json.loads(
                source,
                object_pairs_hook=_unique_object,
                parse_constant=_nonfinite_constant,
            )
        except (
            UnicodeEncodeError,
            json.JSONDecodeError,
            RecursionError,
            ValueError,
        ) as exc:
            if isinstance(exc, GameToolError):
                raise
            raise GameToolError(
                "invalid_json", "arguments are not valid bounded JSON"
            ) from exc
        if type(value) is not dict:
            raise GameToolError("invalid_json", "arguments must be a JSON object")
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            if depth > 64:
                raise GameToolError("invalid_json", "JSON nesting exceeds 64 levels")
            if isinstance(item, dict):
                pending.extend((key, depth + 1) for key in item)
                pending.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
            elif isinstance(item, str):
                try:
                    item.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise GameToolError(
                        "invalid_json", "unpaired Unicode surrogate"
                    ) from exc
            elif isinstance(item, float) and not isfinite(item):
                raise GameToolError(
                    "invalid_json", "nonfinite JSON numbers are forbidden"
                )
        return value
