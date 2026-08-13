from __future__ import annotations

from copy import deepcopy
import json

import pytest

from wargame_native_engine.compat.source_frame.serialization import (
    canonical_green_frame_payload,
)
from wargame_native_engine.io.serialization import json_clone


def _parent_recorder_payload(value: object) -> bytes:
    """Independent expectation from the pre-helper Recorder implementation."""

    return (
        json.dumps(
            json_clone(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def test_canonical_green_frame_payload_matches_parent_recorder_boundaries(
) -> None:
    frame = {
        "标题": "中文复盘",
        "nested": {
            "items": [1, {"enabled": True, "missing": None}],
            "disabled": False,
        },
        "operators": [],
    }
    before = deepcopy(frame)

    payload = canonical_green_frame_payload(frame)

    assert payload == _parent_recorder_payload(frame)
    assert payload.decode("utf-8").startswith('{"nested"')
    assert "中文复盘".encode("utf-8") in payload
    assert payload.endswith(b"\n")
    assert not payload.endswith(b"\n\n")
    assert frame == before


def test_canonical_green_frame_payload_ignores_mapping_insertion_order() -> None:
    first = {"z": 1, "a": {"y": 2, "x": 3}}
    second = {"a": {"x": 3, "y": 2}, "z": 1}

    assert canonical_green_frame_payload(first) == _parent_recorder_payload(first)
    assert canonical_green_frame_payload(second) == _parent_recorder_payload(second)
    assert canonical_green_frame_payload(first) == canonical_green_frame_payload(
        second
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_green_frame_payload_rejects_non_finite_numbers(
    value: float,
) -> None:
    frame = {"value": value}

    with pytest.raises(ValueError, match="Out of range float values"):
        canonical_green_frame_payload(frame)
    with pytest.raises(ValueError, match="Out of range float values"):
        _parent_recorder_payload(frame)

    assert frame["value"] is value


def test_canonical_green_frame_payload_rejects_unserializable_objects() -> None:
    class Unsupported:
        pass

    frame = {"value": Unsupported()}

    with pytest.raises(TypeError, match="not JSON serializable: Unsupported"):
        canonical_green_frame_payload(frame)
    with pytest.raises(TypeError, match="not JSON serializable: Unsupported"):
        _parent_recorder_payload(frame)
    assert isinstance(frame["value"], Unsupported)
