"""Canonical decision-aligned training transition serialization."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
from typing import Sequence

from wargame_native_engine.compat.source_frame.serialization import (
    canonical_source_json,
)
from wargame_native_engine.training.models import DecisionTransition


DECISION_TRANSITIONS_NAME = "decision_transitions.native.jsonl"
DECISION_TRANSITION_SCHEMA = "native-decision-transition/1.0"


def decision_transitions_to_jsonl(
    values: Sequence[DecisionTransition],
) -> str:
    if not all(isinstance(value, DecisionTransition) for value in values):
        raise TypeError("values must contain only DecisionTransition records")
    records = tuple(value.to_record() for value in values)
    return "".join(canonical_source_json(value) + "\n" for value in records)


def write_decision_transitions_jsonl(
    path: str | Path,
    values: Sequence[DecisionTransition],
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = decision_transitions_to_jsonl(values).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target
