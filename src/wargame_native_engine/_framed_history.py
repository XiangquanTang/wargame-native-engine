"""Transactional framed-pickle I/O for process-private compatibility history.

These files are never a public replay format and must not be loaded from an
uploaded archive.  They exist only while their owning session is alive.
"""

from __future__ import annotations

import os
import pickle
import struct
import hashlib
import zlib
from typing import BinaryIO, TypeVar


_HEADER = struct.Struct(">Q")
_T = TypeVar("_T")


class FramedHistoryRollbackError(RuntimeError):
    """The last append failed and its committed prefix could not be secured."""


def append_private_record(stream: BinaryIO, value: object) -> tuple[int, int]:
    """Append one durable frame or restore the exact prior file length."""

    payload = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    return _append_payload(stream, payload)


def _append_payload(stream: BinaryIO, payload: bytes) -> tuple[int, int]:
    offset = stream.seek(0, os.SEEK_END)
    try:
        _write_all(stream, _HEADER.pack(len(payload)))
        _write_all(stream, payload)
        stream.flush()
        os.fsync(stream.fileno())
    except BaseException as append_error:
        try:
            stream.seek(offset)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException as rollback_error:
            raise FramedHistoryRollbackError(
                "private history append failed and rollback was not durable"
            ) from rollback_error
        raise append_error
    return offset, len(payload)


def read_private_record(
    stream: BinaryIO,
    offset: int,
    expected_size: int,
) -> object:
    stream.seek(offset)
    header = stream.read(_HEADER.size)
    if len(header) != _HEADER.size or _HEADER.unpack(header)[0] != expected_size:
        raise RuntimeError("private history record header is corrupt")
    payload = stream.read(expected_size)
    if len(payload) != expected_size:
        raise RuntimeError("private history record is truncated")
    return pickle.loads(payload)


def clear_private_history(stream: BinaryIO) -> None:
    """Discard an old episode before its in-memory index is cleared."""

    stream.seek(0)
    stream.truncate()
    stream.flush()
    os.fsync(stream.fileno())


def _write_all(stream: BinaryIO, payload: bytes) -> None:
    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = stream.write(view[written:])
        if count is None or count <= 0:
            raise OSError("private history write made no progress")
        written += count


# Gateway explicitly opts in. Other framed-history consumers retain their format.
# Raw size is also retained in the trusted in-process index, never trusted from
# disk. This preserves existing legal record capacity without an arbitrary cap.
_COMPACT = struct.Struct(">8sQ32s")
_COMPACT_MAGIC = b"WGZIP01\0"


def append_compact_private_record(stream: BinaryIO, value: object) -> tuple[int, int, int]:
    """One independently compressed gateway record; no cross-record memo."""
    raw = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    compressed = zlib.compress(raw, level=1)
    payload = _COMPACT.pack(_COMPACT_MAGIC, len(raw), hashlib.sha256(raw).digest()) + compressed
    offset, size = _append_payload(stream, payload)
    return offset, size, len(raw)


def read_compact_private_record(stream: BinaryIO, offset: int, expected_size: int,
                                expected_raw_size: int) -> object:
    """Read only same-process writer records; not an external pickle API.

    Allocation bounds come from the trusted writer index and actual file extent.
    Decode at most raw_size+1 bytes; reject bombs, tails and unsupported versions
    before unpickling. Temporary byte buffers are bounded to this single record.
    """
    if (type(offset) is not int or type(expected_size) is not int or
            type(expected_raw_size) is not int or offset < 0 or
            expected_size <= _COMPACT.size or expected_raw_size <= 0):
        raise RuntimeError("invalid compact private history index")
    end = stream.seek(0, os.SEEK_END)
    if offset > end or expected_size > end-offset-_HEADER.size:
        raise RuntimeError("compact private history record is truncated")
    stream.seek(offset)
    header = stream.read(_HEADER.size)
    if len(header) != _HEADER.size or _HEADER.unpack(header)[0] != expected_size:
        raise RuntimeError("compact private history record header is corrupt")
    compact = stream.read(_COMPACT.size)
    if len(compact) != _COMPACT.size:
        raise RuntimeError("compact private history header is truncated")
    magic, raw_size, checksum = _COMPACT.unpack(compact)
    if magic != _COMPACT_MAGIC or raw_size != expected_raw_size:
        raise RuntimeError("unsupported or corrupt compact private history record")
    compressed = stream.read(expected_size-_COMPACT.size)
    if len(compressed) != expected_size-_COMPACT.size:
        raise RuntimeError("compact private history record is truncated")
    decoder = zlib.decompressobj()
    raw = decoder.decompress(compressed, expected_raw_size+1)
    if (len(raw) != expected_raw_size or not decoder.eof or decoder.unused_data or
            decoder.unconsumed_tail or hashlib.sha256(raw).digest() != checksum):
        raise RuntimeError("compact private history payload is corrupt")
    return pickle.loads(raw)
