from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any


def to_json_value(value: Any) -> Any:
    # Most leaves are already JSON scalars. Exact types preserve the existing
    # Enum/dataclass dispatch for subclasses while avoiding those checks for
    # every scalar in a detached projection or canonical hash.
    if type(value) in (str, int, float, bool) or value is None:
        return value
    if type(value) is dict:
        return {str(key): to_json_value(item) for key, item in value.items()}
    if type(value) in (tuple, list):
        return [to_json_value(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {item.name: to_json_value(getattr(value, item.name))
                for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): to_json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        values = [to_json_value(item) for item in value]
        return sorted(values, key=lambda item: json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(to_json_value(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class _JsonRoundTripRequired(Exception):
    """The input is not a tree of exact, finite JSON-native values."""


def json_clone(value: Any) -> Any:
    """Return a detached, canonically ordered JSON value.

    Native JSON trees need no text encoding and parsing to detach their
    containers. Keep the exact round-trip fallback for non-native values,
    non-string keys, subclasses and unusual numbers: their normalization is
    part of the public serialization contract.

    Unlike ``deepcopy``, repeated container references are intentionally copied
    independently, tuples become lists, and object keys are sorted.
    """
    try:
        return _clone_json_tree(value)
    except (_JsonRoundTripRequired, RecursionError):
        # Fall back for the whole tree, not just the unusual leaf. This retains
        # custom-type dispatch order and normalization/encoding error precedence.
        return json.loads(canonical_json(value))


def _clone_json_tree(value: Any) -> Any:
    value_type = type(value)
    if value is None or value_type is str or value_type is bool:
        return value
    if value_type is int and value.bit_length() <= 2048:
        # Always below Python's configurable minimum decimal conversion limit.
        # Larger integers retain json's conversion-limit behavior via fallback.
        return value
    if value_type is float and math.isfinite(value):
        return value
    if value_type is list or value_type is tuple:
        return [_clone_json_tree(item) for item in value]
    if value_type is dict:
        # Validate before sorting so unusual keys retain whole-tree fallback
        # and its normalization/error precedence, without a generator per dict.
        for key in value:
            if type(key) is not str:
                raise _JsonRoundTripRequired
        return {key: _clone_json_tree(value[key]) for key in sorted(value)}
    raise _JsonRoundTripRequired


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
