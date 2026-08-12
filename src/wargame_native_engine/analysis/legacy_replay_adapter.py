"""Read legacy serialized replay ZIP members for derived analysis only."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any
from zipfile import ZipFile, ZipInfo

from wargame_native_engine.analysis.analyzer import GreenFrameAnalysisInput


LEGACY_SERIALIZED_SOURCE_FORMAT = "historical-serialized"
_NUMERIC_MEMBER_BASENAME = re.compile(r"^([0-9]+)(?:\.json)?$")


@dataclass(frozen=True, slots=True)
class LegacySerializedReplaySelection:
    """Selected final state for each ``cur_step`` in one legacy archive."""

    session_prefix: str
    inputs: tuple[GreenFrameAnalysisInput, ...]
    selected_member_names: tuple[str, ...]
    raw_record_count: int

    @property
    def selected_frame_count(self) -> int:
        return len(self.inputs)

    @property
    def collapsed_record_count(self) -> int:
        return self.raw_record_count - self.selected_frame_count


@dataclass(frozen=True, slots=True)
class _LegacyMember:
    source_sequence: int
    session_prefix: str
    info: ZipInfo


def has_legacy_serialized_members(infos: Sequence[ZipInfo]) -> bool:
    """Return whether *infos* contain a nested numeric legacy data member."""

    return any(_legacy_member(info) is not None for info in infos)


def load_legacy_serialized_replay(
    source: str | Path,
) -> LegacySerializedReplaySelection:
    """Load and collapse one legacy serialized replay without writing files.

    Member basenames define source order.  Every payload is strictly decoded
    and parsed, while only the last source record for each ``cur_step`` is
    retained for analysis.
    """

    path = Path(source)
    with ZipFile(path) as archive:
        members = _collect_members(archive.infolist())
        selected_by_step: dict[
            int, tuple[int, str, Mapping[str, Any], bytes]
        ] = {}
        previous_step: int | None = None

        for member in members:
            payload = archive.read(member.info)
            frame = _frame_from_payload(payload, member.info.filename)
            cur_step = _cur_step(frame, member.info.filename)
            if previous_step is not None and cur_step < previous_step:
                raise ValueError(
                    "legacy replay cur_step must not regress in numeric source "
                    f"order: member {member.info.filename!r} has {cur_step} "
                    f"after {previous_step}"
                )
            previous_step = cur_step
            selected_by_step[cur_step] = (
                member.source_sequence,
                member.info.filename,
                frame,
                payload,
            )

    selected = tuple(sorted(
        selected_by_step.items(),
        key=lambda item: item[1][0],
    ))
    inputs = tuple(
        GreenFrameAnalysisInput(
            frame_index=cur_step,
            frame=frame,
            frame_hash=hashlib.sha256(payload).hexdigest(),
            scenario_hash=None,
        )
        for cur_step, (_, _, frame, payload) in selected
    )
    return LegacySerializedReplaySelection(
        session_prefix=members[0].session_prefix,
        inputs=inputs,
        selected_member_names=tuple(item[1][1] for item in selected),
        raw_record_count=len(members),
    )


def _collect_members(infos: Sequence[ZipInfo]) -> tuple[_LegacyMember, ...]:
    members: list[_LegacyMember] = []
    invalid_locations: list[str] = []
    for info in infos:
        if info.is_dir():
            continue
        path = PurePosixPath(info.filename)
        match = _NUMERIC_MEMBER_BASENAME.fullmatch(path.name)
        if match is None:
            continue
        if len(path.parts) != 2 or path.parts[0] in {"", ".", ".."}:
            invalid_locations.append(info.filename)
            continue
        members.append(_LegacyMember(
            source_sequence=int(match.group(1)),
            session_prefix=path.parts[0],
            info=info,
        ))

    if invalid_locations:
        raise ValueError(
            "legacy replay numeric data members must be inside one non-empty "
            f"session directory: {invalid_locations[0]!r}"
        )
    if not members:
        raise ValueError("legacy serialized replay has no numeric data members")

    prefixes = sorted({member.session_prefix for member in members})
    if len(prefixes) != 1:
        raise ValueError(
            "legacy serialized replay must contain exactly one session prefix; "
            f"got {prefixes!r}"
        )

    by_sequence: dict[int, str] = {}
    for member in members:
        prior = by_sequence.get(member.source_sequence)
        if prior is not None:
            raise ValueError(
                "legacy replay source sequence conflict between "
                f"{prior!r} and {member.info.filename!r}: "
                f"{member.source_sequence}"
            )
        by_sequence[member.source_sequence] = member.info.filename

    members.sort(key=lambda item: item.source_sequence)
    return tuple(members)


def _legacy_member(info: ZipInfo) -> _LegacyMember | None:
    if info.is_dir():
        return None
    path = PurePosixPath(info.filename)
    if len(path.parts) != 2 or path.parts[0] in {"", ".", ".."}:
        return None
    match = _NUMERIC_MEMBER_BASENAME.fullmatch(path.name)
    if match is None:
        return None
    return _LegacyMember(int(match.group(1)), path.parts[0], info)


def _frame_from_payload(
    payload: bytes,
    member_name: str,
) -> Mapping[str, Any]:
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"legacy replay member {member_name!r} is not valid UTF-8"
        ) from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"legacy replay member {member_name!r} is not valid JSON"
        ) from exc
    if not isinstance(value, Mapping):
        raise ValueError(
            f"legacy replay member {member_name!r} root must be an object"
        )
    return value


def _cur_step(frame: Mapping[str, Any], member_name: str) -> int:
    time = frame.get("time")
    if not isinstance(time, Mapping):
        raise ValueError(
            f"legacy replay member {member_name!r} time must be an object"
        )
    if "cur_step" not in time:
        raise ValueError(
            f"legacy replay member {member_name!r} is missing time.cur_step"
        )
    value = time["cur_step"]
    if type(value) is not int:
        raise ValueError(
            f"legacy replay member {member_name!r} time.cur_step must be a "
            "non-negative exact int"
        )
    if value < 0:
        raise ValueError(
            f"legacy replay member {member_name!r} time.cur_step must be "
            "non-negative"
        )
    return value
