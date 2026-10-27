"""Opt-in detached copies of plain snapshot records, without pickle machinery.

The registry contains exact, owned dataclass types only. It is not a global
``copy`` dispatch modification. Unknown objects and record subclasses retain
their standard ``deepcopy`` behavior, including custom copy protocols.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import fields, is_dataclass
from typing import Any, TypeVar

_T = TypeVar("_T")
_RECORD_FIELDS: dict[type, tuple[str, ...]] = {}
_ATOMIC_TYPES = frozenset({str, int, float, bool, bytes, complex, type(None)})


def detached_record(cls: type[_T]) -> type[_T]:
    """Opt a plain frozen/slotted record into field-wise detached copying.

    Copying does not run constructors, validation, hashes or other side effects,
    exactly like the default deepcopy behavior for these dataclasses. A future
    custom copy/reduction implementation must remove this opt-in first.
    """
    if (
        not is_dataclass(cls)
        or not cls.__dataclass_params__.frozen
        or cls.__dictoffset__ != 0
        or cls.__reduce_ex__ is not object.__reduce_ex__
        or cls.__reduce__ is not object.__reduce__
        or cls.__new__ is not object.__new__
        or hasattr(cls, "__deepcopy__")
        or getattr(cls.__getstate__, "__module__", None) != "dataclasses"
        or getattr(cls.__setstate__, "__module__", None) != "dataclasses"
    ):
        raise TypeError("detached_record requires a plain frozen slotted dataclass")
    names = tuple(field.name for field in fields(cls))
    if set(names) != set(cls.__slots__):
        raise TypeError("detached_record requires slots containing exactly its fields")
    _RECORD_FIELDS[cls] = names
    return cls


def detached_copy(value: _T) -> _T:
    """Deep-copy a snapshot, preserving shared references and container cycles.

    This is not JSON normalization: dictionaries keep key order and tuples stay
    tuples. Even frozen records are copied because their payloads may be mutable.
    """
    return _copy_value(value, {})


def _copy_value(value: Any, memo: dict[int, Any]) -> Any:
    cls = type(value)
    if cls in _ATOMIC_TYPES:
        return value
    identity = id(value)
    if identity in memo:
        return memo[identity]
    if cls is list:
        result = []
        memo[identity] = result
        result.extend(_copy_value(item, memo) for item in value)
        return result
    if cls is dict:
        result = {}
        memo[identity] = result
        for key, item in value.items():
            result[_copy_value(key, memo)] = _copy_value(item, memo)
        return result
    if cls is tuple:
        items = [_copy_value(item, memo) for item in value]
        # Recursive tuple/list structures may have installed this tuple while
        # copying a child. Match deepcopy's alias handling in that case.
        if identity in memo:
            return memo[identity]
        result = value if all(a is b for a, b in zip(items, value)) else tuple(items)
        memo[identity] = result
        return result
    names = _RECORD_FIELDS.get(cls)
    if names is not None:
        result = object.__new__(cls)
        memo[identity] = result
        for name in names:
            object.__setattr__(result, name, _copy_value(getattr(value, name), memo))
        return result
    return deepcopy(value, memo)
