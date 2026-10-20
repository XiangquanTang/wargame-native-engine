"""Validated, provenance-bound map lookup data for Native simplified rules.

The old benchmark map helpers persist two useful *data* products:

* ``cost.pickle``: four movement cost-factor matrices;
* ``*see.npz``: ten static line-of-sight matrices.

Those legacy serialization formats are accepted only by the explicit offline
converter in this module.  Runtime code consumes the canonical files emitted
by that converter and never imports the old benchmark project.  The large LOS
matrix is kept as a read-only NPY file and queried through ``mmap``; it is not
expanded into Python objects or loaded into process RAM.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import fcntl
from fractions import Fraction
import hashlib
import json
import math
import mmap
import os
from pathlib import Path
import pickle
import shutil
import struct
import tempfile
from threading import RLock
from types import MappingProxyType
from typing import Any, BinaryIO, Mapping
from zipfile import BadZipFile, ZipFile

from wargame_native_engine.io.scenario import NativeMap, native_map_identity_hash
from wargame_native_engine.io.serialization import canonical_json
from wargame_native_engine.visibility.line_of_sight import (
    LineOfSightEvidence,
    hex_distance,
)


MAP_RUNTIME_SCHEMA = "native-map-runtime-data/1.0"
MOVEMENT_COST_SCHEMA = "native-map-movement-costs/1.0"
MAP_RUNTIME_CONVERTER_VERSION = "1.0.0"
MOVEMENT_COST_FILENAME = "movement_costs.native.json"
VISIBILITY_FILENAME = "visibility.native.npy"
MANIFEST_FILENAME = "map_runtime_manifest.native.json"

MOVEMENT_MODE_NAMES = (
    "VEHICLE_NORMAL",
    "VEHICLE_MARCHING",
    "PERSONNEL",
    "AIR",
)

# Source static-visibility matrix modes.  Mixed-height modes put the higher
# endpoint first; ``StaticVisibilityLookup`` swaps coordinates when needed.
VISIBILITY_MODE_RECORDS = (
    (0, 0, 0, "GROUND_GROUND"),
    (1, 200, 200, "LOW_LOW"),
    (2, 200, 0, "LOW_GROUND"),
    (3, 20, 0, "ULTRALOW_GROUND"),
    (4, 20, 20, "ULTRALOW_ULTRALOW"),
    (5, 200, 20, "LOW_ULTRALOW"),
    (6, 500, 0, "HIGH_GROUND"),
    (7, 500, 20, "HIGH_ULTRALOW"),
    (8, 500, 200, "HIGH_LOW"),
    (9, 500, 500, "HIGH_HIGH"),
)
_VISIBILITY_MODE_BY_HEIGHTS = {
    (first, second): mode
    for mode, first, second, _ in VISIBILITY_MODE_RECORDS
}


class MapRuntimeDataError(ValueError):
    """Stable error at the explicit map lookup-data boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"{self.code}:{self.message}")


@dataclass(frozen=True, slots=True)
class PublishedMapRuntimeData:
    output_dir: Path
    manifest_path: Path
    movement_cost_path: Path
    visibility_path: Path
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class PreparedMapRuntimeData:
    """Validated immutable cache prepared from a release asset directory."""

    cache_dir: Path
    cache_hit: bool
    manifest_sha256: str
    identity: Mapping[str, object]


class MovementCostLookup:
    """Immutable lookup of ``maximum_speed / actual_speed`` edge factors."""

    __slots__ = (
        "map_id",
        "rows",
        "columns",
        "source_sha256",
        "payload_sha256",
        "_factors",
        "_modes",
        "_sealed",
    )

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("MovementCostLookup is immutable")
        object.__setattr__(self, name, value)

    def __init__(
        self,
        *,
        map_id: str,
        rows: int,
        columns: int,
        source_sha256: str,
        payload_sha256: str,
        factors: Mapping[tuple[int, int, int], Fraction],
    ) -> None:
        self.map_id = str(map_id)
        self.rows = int(rows)
        self.columns = int(columns)
        self.source_sha256 = _sha256_text(source_sha256, "movement source sha256")
        self.payload_sha256 = _sha256_text(payload_sha256, "movement payload sha256")
        self._factors = MappingProxyType(dict(factors))
        self._modes = frozenset(key[0] for key in self._factors)
        self._sealed = True

    def factor(self, mode: int, from_hex: int, to_hex: int) -> Fraction | None:
        return self._factors.get((int(mode), int(from_hex), int(to_hex)))

    def supports_mode(self, mode: int) -> bool:
        return int(mode) in self._modes

    @property
    def edge_count(self) -> int:
        return len(self._factors)


class StaticVisibilityLookup:
    """Disk-backed exact lookup for the ten source static LOS modes."""

    __slots__ = (
        "map_id",
        "rows",
        "columns",
        "source_sha256",
        "payload_sha256",
        "path",
        "shape",
        "data_offset",
        "_stream",
        "_mapping",
        "_cache",
        "_lock",
    )

    def __init__(
        self,
        *,
        map_id: str,
        rows: int,
        columns: int,
        source_sha256: str,
        payload_sha256: str,
        path: Path,
        shape: tuple[int, ...],
        data_offset: int,
    ) -> None:
        self.map_id = str(map_id)
        self.rows = int(rows)
        self.columns = int(columns)
        self.source_sha256 = _sha256_text(source_sha256, "visibility source sha256")
        self.payload_sha256 = _sha256_text(payload_sha256, "visibility payload sha256")
        self.path = Path(path)
        self.shape = tuple(int(item) for item in shape)
        self.data_offset = int(data_offset)
        self._stream: BinaryIO | None = None
        self._mapping: mmap.mmap | None = None
        self._cache: dict[tuple[int, int, int], bool] = {}
        self._lock = RLock()

    def visible(
        self,
        observer_hex: int,
        target_hex: int,
        *,
        observer_height_m: int,
        target_height_m: int,
    ) -> bool:
        mode, first_hex, second_hex = self._resolve_mode(
            observer_hex,
            target_hex,
            observer_height_m,
            target_height_m,
        )
        return self._visible_by_mode(mode, first_hex, second_hex)

    def line_of_sight(
        self,
        observer_hex: int,
        target_hex: int,
        *,
        observer_height_m: int,
        target_height_m: int,
    ) -> LineOfSightEvidence:
        mode, first_hex, second_hex = self._resolve_mode(
            observer_hex,
            target_hex,
            observer_height_m,
            target_height_m,
        )
        visible = self._visible_by_mode(mode, first_hex, second_hex)
        return LineOfSightEvidence(
            visible=visible,
            distance=hex_distance(observer_hex, target_hex),
            blocking_hexes=(),
            samples=(),
            modifiers=({
                "rule": "source_static_visibility_lookup",
                "mode": mode,
                "first_hex": first_hex,
                "second_hex": second_hex,
                "source_sha256": self.source_sha256,
                "payload_sha256": self.payload_sha256,
            },),
        )

    def close(self) -> None:
        with self._lock:
            if self._mapping is not None:
                self._mapping.close()
                self._mapping = None
            if self._stream is not None:
                self._stream.close()
                self._stream = None
            self._cache.clear()

    def _visible_by_mode(
        self,
        mode: int,
        first_hex: int,
        second_hex: int,
    ) -> bool:
        key = (int(mode), int(first_hex), int(second_hex))
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            first_row, first_col = self._row_col(first_hex)
            second_row, second_col = self._row_col(second_hex)
            flat = (
                ((((mode * self.rows) + first_row) * self.columns + first_col)
                 * self.rows + second_row)
                * self.columns
                + second_col
            )
            mapping = self._require_mapping()
            visible = mapping[self.data_offset + flat] != 0
            # A fixed bound prevents a long-running service from caching the
            # entire 1-billion-entry matrix.  Clearing affects performance
            # only; the immutable lookup remains the result authority.
            if len(self._cache) >= 200_000:
                self._cache.clear()
            self._cache[key] = visible
            return visible

    def _row_col(self, hex_id: int) -> tuple[int, int]:
        row, col = divmod(int(hex_id), 100)
        if not (0 <= row < self.rows and 0 <= col < self.columns):
            raise MapRuntimeDataError(
                "visibility_hex_out_of_bounds",
                f"hex {hex_id} is outside {self.rows}x{self.columns}",
            )
        return row, col

    @staticmethod
    def _resolve_mode(
        observer_hex: int,
        target_hex: int,
        observer_height_m: int,
        target_height_m: int,
    ) -> tuple[int, int, int]:
        observer_height = _exact_height(observer_height_m, "observer_height_m")
        target_height = _exact_height(target_height_m, "target_height_m")
        if observer_height >= target_height:
            heights = (observer_height, target_height)
            first_hex, second_hex = int(observer_hex), int(target_hex)
        else:
            heights = (target_height, observer_height)
            first_hex, second_hex = int(target_hex), int(observer_hex)
        try:
            mode = _VISIBILITY_MODE_BY_HEIGHTS[heights]
        except KeyError as exc:
            raise MapRuntimeDataError(
                "unsupported_visibility_height_pair",
                f"no static visibility mode for heights {heights}",
            ) from exc
        return mode, first_hex, second_hex

    def _require_mapping(self) -> mmap.mmap:
        with self._lock:
            if self._mapping is None:
                stream = self.path.open("rb")
                try:
                    mapping = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
                except BaseException:
                    stream.close()
                    raise
                self._stream = stream
                self._mapping = mapping
            return self._mapping


@dataclass(frozen=True, slots=True)
class MapRuntimeData:
    """Validated runtime pair bound to one canonical ``NativeMap`` identity."""

    map_id: str
    map_sha256: str
    manifest_sha256: str
    movement: MovementCostLookup
    visibility: StaticVisibilityLookup

    @classmethod
    def load(
        cls,
        root: str | Path,
        native_map: NativeMap,
        *,
        verify_payload_hashes: bool = True,
    ) -> "MapRuntimeData":
        directory = Path(root)
        manifest_path = directory / MANIFEST_FILENAME
        manifest = _read_json_mapping(manifest_path, "runtime manifest")
        if manifest.get("schema") != MAP_RUNTIME_SCHEMA:
            raise MapRuntimeDataError(
                "unsupported_map_runtime_schema",
                str(manifest.get("schema")),
            )
        map_record = _mapping(manifest.get("map_identity"), "map_identity")
        map_id = _nonempty_text(map_record.get("map_id"), "map_identity.map_id")
        map_sha = _sha256_text(map_record.get("sha256"), "map_identity.sha256")
        actual_map_sha = native_map_identity_hash(native_map)
        if map_id != native_map.map_id or map_sha != actual_map_sha:
            raise MapRuntimeDataError(
                "map_runtime_identity_mismatch",
                f"runtime {map_id}/{map_sha} != map {native_map.map_id}/{actual_map_sha}",
            )
        actual_rows, actual_columns, neighbors = _native_map_grid(native_map)
        grid = _mapping(manifest.get("grid"), "grid")
        rows = _positive_exact_int(grid.get("rows"), "grid.rows")
        columns = _positive_exact_int(grid.get("columns"), "grid.columns")
        if (
            rows != actual_rows
            or columns != actual_columns
            or grid.get("coordinate_system") != "odd-r"
            or grid.get("position_encoding") != "row_times_100_plus_column"
        ):
            raise MapRuntimeDataError(
                "map_runtime_grid_mismatch",
                f"manifest {rows}x{columns} != map {actual_rows}x{actual_columns}",
            )
        if manifest.get("native_rules_equivalence") is not False:
            raise MapRuntimeDataError(
                "invalid_runtime_manifest",
                "native_rules_equivalence must be false",
            )

        movement_record = _mapping(manifest.get("movement"), "movement")
        movement_payload = _mapping(
            movement_record.get("canonical_payload"),
            "movement.canonical_payload",
        )
        movement_path = _fixed_member_path(
            directory,
            movement_payload.get("name"),
            MOVEMENT_COST_FILENAME,
        )
        movement_sha = _sha256_text(
            movement_payload.get("sha256"),
            "movement.canonical_payload.sha256",
        )
        if verify_payload_hashes and _file_sha256(movement_path) != movement_sha:
            raise MapRuntimeDataError(
                "movement_payload_hash_mismatch",
                movement_path.name,
            )
        movement_document = _read_json_mapping(movement_path, "movement costs")
        factors = _load_canonical_movement_costs(
            movement_document,
            map_id=map_id,
            rows=rows,
            columns=columns,
            neighbors=neighbors,
        )
        if (
            movement_record.get("factor_semantics")
            != "maximum_speed_divided_by_actual_speed"
            or movement_record.get("mode_order") != list(MOVEMENT_MODE_NAMES)
            or movement_record.get("edge_count") != len(factors)
        ):
            raise MapRuntimeDataError(
                "movement_manifest_mismatch",
                map_id,
            )
        movement_source = _mapping(
            movement_record.get("source"), "movement.source"
        )
        movement = MovementCostLookup(
            map_id=map_id,
            rows=rows,
            columns=columns,
            source_sha256=_sha256_text(
                movement_source.get("sha256"), "movement.source.sha256"
            ),
            payload_sha256=movement_sha,
            factors=factors,
        )

        visibility_record = _mapping(manifest.get("visibility"), "visibility")
        visibility_payload = _mapping(
            visibility_record.get("canonical_payload"),
            "visibility.canonical_payload",
        )
        visibility_path = _fixed_member_path(
            directory,
            visibility_payload.get("name"),
            VISIBILITY_FILENAME,
        )
        visibility_sha = _sha256_text(
            visibility_payload.get("sha256"),
            "visibility.canonical_payload.sha256",
        )
        if verify_payload_hashes and _file_sha256(visibility_path) != visibility_sha:
            raise MapRuntimeDataError(
                "visibility_payload_hash_mismatch",
                visibility_path.name,
            )
        header = _read_npy_header(visibility_path.open("rb"))
        expected_shape = (10, rows, columns, rows, columns)
        if header.shape != expected_shape:
            raise MapRuntimeDataError(
                "visibility_shape_mismatch",
                f"{header.shape} != {expected_shape}",
            )
        expected_size = header.data_offset + math.prod(header.shape)
        if visibility_path.stat().st_size != expected_size:
            raise MapRuntimeDataError(
                "visibility_payload_size_mismatch",
                f"{visibility_path.stat().st_size} != {expected_size}",
            )
        expected_modes = [
            {
                "mode": mode,
                "first_height_m": first,
                "second_height_m": second,
                "name": name,
            }
            for mode, first, second, name in VISIBILITY_MODE_RECORDS
        ]
        if (
            visibility_record.get("semantics") != "static_line_of_sight_only"
            or visibility_record.get("mode_order") != expected_modes
            or visibility_payload.get("dtype") != "|b1"
            or visibility_payload.get("shape") != list(expected_shape)
            or visibility_payload.get("data_offset") != header.data_offset
            or visibility_payload.get("size_bytes") != expected_size
            or visibility_payload.get("storage") != "read_only_memory_mapped_npy"
        ):
            raise MapRuntimeDataError(
                "visibility_manifest_mismatch",
                map_id,
            )
        visibility_source = _mapping(
            visibility_record.get("source"), "visibility.source"
        )
        visibility = StaticVisibilityLookup(
            map_id=map_id,
            rows=rows,
            columns=columns,
            source_sha256=_sha256_text(
                visibility_source.get("sha256"), "visibility.source.sha256"
            ),
            payload_sha256=visibility_sha,
            path=visibility_path,
            shape=header.shape,
            data_offset=header.data_offset,
        )
        return cls(
            map_id=map_id,
            map_sha256=map_sha,
            manifest_sha256=_file_sha256(manifest_path),
            movement=movement,
            visibility=visibility,
        )

    def identity_record(self) -> dict[str, object]:
        return {
            "schema": MAP_RUNTIME_SCHEMA,
            "map_id": self.map_id,
            "map_sha256": self.map_sha256,
            "manifest_sha256": self.manifest_sha256,
            "movement_source_sha256": self.movement.source_sha256,
            "movement_payload_sha256": self.movement.payload_sha256,
            "visibility_source_sha256": self.visibility.source_sha256,
            "visibility_payload_sha256": self.visibility.payload_sha256,
        }


def publish_map_runtime_data(
    native_map: NativeMap,
    *,
    source_map_path: str | Path,
    movement_cost_path: str | Path,
    visibility_npz_path: str | Path,
    output_dir: str | Path,
) -> PublishedMapRuntimeData:
    """Validate legacy data and publish a new canonical runtime directory.

    The input files are never modified.  Publication is atomic and refuses to
    replace an existing directory.
    """

    source_map = Path(source_map_path)
    movement_source = Path(movement_cost_path)
    visibility_source = Path(visibility_npz_path)
    for path, label in (
        (source_map, "source map"),
        (movement_source, "movement cost"),
        (visibility_source, "visibility npz"),
    ):
        if not path.is_file():
            raise MapRuntimeDataError("source_not_found", f"{label}: {path}")

    rows, columns, neighbor_map = _native_map_grid(native_map)
    source_map_record = _read_json_mapping(source_map, "source map")
    source_map_id = source_map_record.get("map_id")
    if (
        isinstance(source_map_id, bool)
        or not isinstance(source_map_id, (str, int))
        or str(source_map_id) != native_map.map_id
    ):
        raise MapRuntimeDataError(
            "source_map_identity_mismatch",
            f"{source_map_id!r} != {native_map.map_id!r}",
        )
    movement_source_sha = _file_sha256(movement_source)
    movement_legacy = _load_restricted_pickle(movement_source)
    factors = _validate_legacy_movement_costs(
        movement_legacy,
        rows=rows,
        columns=columns,
        neighbors=neighbor_map,
    )
    movement_document = {
        "schema": MOVEMENT_COST_SCHEMA,
        "map_id": native_map.map_id,
        "rows": rows,
        "columns": columns,
        "factor_semantics": "maximum_speed_divided_by_actual_speed",
        "mode_order": list(MOVEMENT_MODE_NAMES),
        "edges": [
            [mode, from_hex, to_hex, factor.numerator, factor.denominator]
            for (mode, from_hex, to_hex), factor in sorted(factors.items())
        ],
    }
    movement_payload = _canonical_payload(movement_document)

    target = Path(output_dir)
    if target.exists():
        raise FileExistsError(f"map runtime output already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(
        prefix=f".{target.name}.staging-",
        dir=target.parent,
    ))
    try:
        movement_output = staging / MOVEMENT_COST_FILENAME
        movement_output.write_bytes(movement_payload)
        visibility_output = staging / VISIBILITY_FILENAME
        npy_header, visibility_source_sha, visibility_payload_sha = (
            _extract_visibility_npy(
                visibility_source,
                visibility_output,
                expected_shape=(10, rows, columns, rows, columns),
            )
        )
        source_map_sha = _file_sha256(source_map)
        manifest = {
            "schema": MAP_RUNTIME_SCHEMA,
            "converter_version": MAP_RUNTIME_CONVERTER_VERSION,
            "map_identity": {
                "map_id": native_map.map_id,
                "sha256": native_map_identity_hash(native_map),
            },
            "grid": {
                "coordinate_system": "odd-r",
                "position_encoding": "row_times_100_plus_column",
                "rows": rows,
                "columns": columns,
            },
            "source_map": {
                "name": source_map.name,
                "sha256": source_map_sha,
                "size_bytes": source_map.stat().st_size,
            },
            "movement": {
                "source": {
                    "name": movement_source.name,
                    "sha256": movement_source_sha,
                    "size_bytes": movement_source.stat().st_size,
                    "legacy_format": "pickle_protocol_3_builtins_only",
                },
                "canonical_payload": {
                    "name": MOVEMENT_COST_FILENAME,
                    "sha256": hashlib.sha256(movement_payload).hexdigest(),
                    "size_bytes": len(movement_payload),
                },
                "factor_semantics": "maximum_speed_divided_by_actual_speed",
                "mode_order": list(MOVEMENT_MODE_NAMES),
                "edge_count": len(factors),
            },
            "visibility": {
                "source": {
                    "name": visibility_source.name,
                    "sha256": visibility_source_sha,
                    "size_bytes": visibility_source.stat().st_size,
                    "legacy_format": "npz_single_bool_array",
                },
                "canonical_payload": {
                    "name": VISIBILITY_FILENAME,
                    "sha256": visibility_payload_sha,
                    "size_bytes": visibility_output.stat().st_size,
                    "dtype": "|b1",
                    "shape": list(npy_header.shape),
                    "data_offset": npy_header.data_offset,
                    "storage": "read_only_memory_mapped_npy",
                },
                "mode_order": [
                    {
                        "mode": mode,
                        "first_height_m": first,
                        "second_height_m": second,
                        "name": name,
                    }
                    for mode, first, second, name in VISIBILITY_MODE_RECORDS
                ],
                "semantics": "static_line_of_sight_only",
            },
            "native_rules_equivalence": False,
            "warnings": [
                "lookup data supplies Native simplified movement/LOS inputs; "
                "it does not recover or claim old SDK adjudication equivalence"
            ],
        }
        manifest_payload = _canonical_payload(manifest)
        manifest_path = staging / MANIFEST_FILENAME
        manifest_path.write_bytes(manifest_payload)

        # Load through the runtime reader before publication.  This validates
        # all file hashes and the mmap shape using the exact emitted bytes.
        probe = MapRuntimeData.load(staging, native_map)
        probe.visibility.close()
        os.replace(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return PublishedMapRuntimeData(
        output_dir=target,
        manifest_path=target / MANIFEST_FILENAME,
        movement_cost_path=target / MOVEMENT_COST_FILENAME,
        visibility_path=target / VISIBILITY_FILENAME,
        manifest_sha256=hashlib.sha256(manifest_payload).hexdigest(),
    )


def prepare_map_runtime_data(
    native_map: NativeMap,
    *,
    release_dir: str | Path,
    cache_root: str | Path,
) -> PreparedMapRuntimeData:
    """Prepare or reuse one manifest-addressed canonical runtime cache.

    ``release_dir`` contains a canonical manifest and movement JSON alongside
    the original visibility NPZ named by that manifest.  Source and canonical
    hashes are checked before an existing cache can be used.  A cold cache is
    extracted in a same-filesystem staging directory, validated through
    :meth:`MapRuntimeData.load`, and renamed into place.  Existing cache
    directories are never replaced, including when they are invalid.
    """

    source_root = Path(release_dir)
    manifest_path = source_root / MANIFEST_FILENAME
    try:
        manifest_payload = manifest_path.read_bytes()
    except OSError as exc:
        raise MapRuntimeDataError(
            "release_manifest_read_failed", str(manifest_path)
        ) from exc
    manifest_sha = hashlib.sha256(manifest_payload).hexdigest()
    manifest = _decode_json_mapping(manifest_payload, "runtime manifest")
    if manifest.get("schema") != MAP_RUNTIME_SCHEMA:
        raise MapRuntimeDataError(
            "unsupported_map_runtime_schema", str(manifest.get("schema"))
        )

    rows, columns, _ = _native_map_grid(native_map)
    map_record = _mapping(manifest.get("map_identity"), "map_identity")
    expected_map_sha = native_map_identity_hash(native_map)
    if (
        map_record.get("map_id") != native_map.map_id
        or map_record.get("sha256") != expected_map_sha
    ):
        raise MapRuntimeDataError(
            "map_runtime_identity_mismatch",
            f"release does not match {native_map.map_id}/{expected_map_sha}",
        )

    movement_record = _mapping(manifest.get("movement"), "movement")
    movement_payload_record = _mapping(
        movement_record.get("canonical_payload"),
        "movement.canonical_payload",
    )
    movement_path = _fixed_member_path(
        source_root,
        movement_payload_record.get("name"),
        MOVEMENT_COST_FILENAME,
    )
    _validate_release_file(
        movement_path,
        movement_payload_record,
        label="movement canonical payload",
    )

    visibility_record = _mapping(manifest.get("visibility"), "visibility")
    visibility_source_record = _mapping(
        visibility_record.get("source"), "visibility.source"
    )
    visibility_source = _release_member_path(
        source_root,
        visibility_source_record.get("name"),
        label="visibility source",
    )
    _validate_release_file(
        visibility_source,
        visibility_source_record,
        label="visibility source",
    )

    root = Path(cache_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MapRuntimeDataError("cache_root_create_failed", str(root)) from exc
    target = root / f"map-runtime-{manifest_sha}"
    prepared = _load_prepared_cache(
        target,
        native_map=native_map,
        manifest_sha=manifest_sha,
    )
    if prepared is not None:
        return prepared

    lock_path = root / f".{target.name}.lock"
    try:
        lock_stream = lock_path.open("a+b")
    except OSError as exc:
        raise MapRuntimeDataError("cache_lock_open_failed", str(lock_path)) from exc
    with lock_stream:
        try:
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise MapRuntimeDataError("cache_lock_failed", str(lock_path)) from exc
        prepared = _load_prepared_cache(
            target,
            native_map=native_map,
            manifest_sha=manifest_sha,
        )
        if prepared is not None:
            return prepared

        staging = Path(tempfile.mkdtemp(
            prefix=f".{target.name}.staging-",
            dir=root,
        ))
        try:
            _write_cache_payload(
                manifest_payload,
                staging / MANIFEST_FILENAME,
            )
            _copy_release_file(movement_path, staging / MOVEMENT_COST_FILENAME)
            visibility_output = staging / VISIBILITY_FILENAME
            header, source_sha, payload_sha = _extract_visibility_npy(
                visibility_source,
                visibility_output,
                expected_shape=(10, rows, columns, rows, columns),
            )
            expected_source_sha = _sha256_text(
                visibility_source_record.get("sha256"),
                "visibility.source.sha256",
            )
            visibility_payload_record = _mapping(
                visibility_record.get("canonical_payload"),
                "visibility.canonical_payload",
            )
            expected_payload_sha = _sha256_text(
                visibility_payload_record.get("sha256"),
                "visibility.canonical_payload.sha256",
            )
            if source_sha != expected_source_sha:
                raise MapRuntimeDataError(
                    "visibility_source_hash_mismatch", visibility_source.name
                )
            if payload_sha != expected_payload_sha:
                raise MapRuntimeDataError(
                    "visibility_payload_hash_mismatch", visibility_output.name
                )
            if visibility_payload_record.get("data_offset") != header.data_offset:
                raise MapRuntimeDataError(
                    "visibility_manifest_mismatch", "data offset"
                )

            runtime = MapRuntimeData.load(staging, native_map)
            identity = runtime.identity_record()
            runtime.visibility.close()
            if identity["manifest_sha256"] != manifest_sha:
                raise MapRuntimeDataError(
                    "cache_manifest_identity_mismatch", str(staging)
                )
            for member in (
                staging / MANIFEST_FILENAME,
                staging / MOVEMENT_COST_FILENAME,
                visibility_output,
            ):
                member.chmod(0o444)
            try:
                os.rename(staging, target)
            except FileExistsError:
                # A publisher not using this advisory lock may have won the
                # race.  Its directory must validate; it is never replaced.
                prepared = _load_prepared_cache(
                    target,
                    native_map=native_map,
                    manifest_sha=manifest_sha,
                )
                if prepared is None:
                    raise MapRuntimeDataError(
                        "cache_publish_race_failed", str(target)
                    )
                return prepared
            return PreparedMapRuntimeData(
                cache_dir=target,
                cache_hit=False,
                manifest_sha256=manifest_sha,
                identity=identity,
            )
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)


def _load_prepared_cache(
    target: Path,
    *,
    native_map: NativeMap,
    manifest_sha: str,
) -> PreparedMapRuntimeData | None:
    if not target.exists():
        return None
    if not target.is_dir():
        raise MapRuntimeDataError("cache_target_not_directory", str(target))
    runtime = MapRuntimeData.load(target, native_map)
    try:
        if runtime.manifest_sha256 != manifest_sha:
            raise MapRuntimeDataError(
                "cache_manifest_identity_mismatch", str(target)
            )
        return PreparedMapRuntimeData(
            cache_dir=target,
            cache_hit=True,
            manifest_sha256=manifest_sha,
            identity=runtime.identity_record(),
        )
    finally:
        runtime.visibility.close()


def _validate_release_file(
    path: Path,
    record: Mapping[str, Any],
    *,
    label: str,
) -> None:
    if not path.is_file():
        raise MapRuntimeDataError("release_member_not_found", f"{label}: {path}")
    expected_size = _positive_exact_int(record.get("size_bytes"), f"{label}.size_bytes")
    try:
        actual_size = path.stat().st_size
    except OSError as exc:
        raise MapRuntimeDataError("release_member_read_failed", str(path)) from exc
    if actual_size != expected_size:
        raise MapRuntimeDataError(
            "release_member_size_mismatch",
            f"{path.name}: {actual_size} != {expected_size}",
        )
    expected_sha = _sha256_text(record.get("sha256"), f"{label}.sha256")
    if _file_sha256(path) != expected_sha:
        raise MapRuntimeDataError("release_member_hash_mismatch", path.name)


def _release_member_path(root: Path, value: object, *, label: str) -> Path:
    name = _nonempty_text(value, f"{label}.name")
    if Path(name).name != name or name in {".", ".."}:
        raise MapRuntimeDataError("invalid_release_member_name", name)
    return root / name


def _copy_release_file(source: Path, target: Path) -> None:
    try:
        with source.open("rb") as input_stream, target.open("xb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
    except OSError as exc:
        raise MapRuntimeDataError("cache_member_write_failed", str(target)) from exc


def _write_cache_payload(payload: bytes, target: Path) -> None:
    try:
        with target.open("xb") as output_stream:
            output_stream.write(payload)
            output_stream.flush()
            os.fsync(output_stream.fileno())
    except OSError as exc:
        raise MapRuntimeDataError("cache_member_write_failed", str(target)) from exc


@dataclass(frozen=True, slots=True)
class _NpyHeader:
    shape: tuple[int, ...]
    data_offset: int


class _BuiltinsOnlyUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str) -> Any:
        raise MapRuntimeDataError(
            "unsafe_movement_pickle",
            f"global {module}.{name} is not allowed",
        )

    def persistent_load(self, pid: object) -> Any:
        raise MapRuntimeDataError(
            "unsafe_movement_pickle",
            "persistent references are not allowed",
        )


def _load_restricted_pickle(path: Path) -> object:
    try:
        with path.open("rb") as stream:
            value = _BuiltinsOnlyUnpickler(stream).load()
            if stream.read(1):
                raise MapRuntimeDataError(
                    "invalid_movement_pickle",
                    "trailing bytes after the pickle value",
                )
            return value
    except MapRuntimeDataError:
        raise
    except (OSError, pickle.PickleError, EOFError, ValueError) as exc:
        raise MapRuntimeDataError(
            "invalid_movement_pickle",
            str(path),
        ) from exc


def _validate_legacy_movement_costs(
    value: object,
    *,
    rows: int,
    columns: int,
    neighbors: Mapping[int, frozenset[int]],
) -> dict[tuple[int, int, int], Fraction]:
    if not isinstance(value, list) or len(value) != len(MOVEMENT_MODE_NAMES):
        raise MapRuntimeDataError(
            "invalid_movement_cost_shape",
            "root must contain exactly four movement modes",
        )
    factors: dict[tuple[int, int, int], Fraction] = {}
    for mode, mode_rows in enumerate(value):
        if not isinstance(mode_rows, list) or len(mode_rows) != rows:
            raise MapRuntimeDataError(
                "invalid_movement_cost_shape",
                f"mode {mode} must contain {rows} rows",
            )
        for row, row_cells in enumerate(mode_rows):
            if not isinstance(row_cells, list) or len(row_cells) != columns:
                raise MapRuntimeDataError(
                    "invalid_movement_cost_shape",
                    f"mode {mode} row {row} must contain {columns} cells",
                )
            for column, raw_edges in enumerate(row_cells):
                if not isinstance(raw_edges, dict):
                    raise MapRuntimeDataError(
                        "invalid_movement_cost_cell",
                        f"mode {mode} cell {row},{column} must be a dict",
                    )
                from_hex = row * 100 + column
                allowed = neighbors[from_hex]
                for raw_target, raw_factor in raw_edges.items():
                    if type(raw_target) is not int:
                        raise MapRuntimeDataError(
                            "invalid_movement_cost_target",
                            f"mode {mode} cell {from_hex}",
                        )
                    target = int(raw_target)
                    if target not in allowed:
                        raise MapRuntimeDataError(
                            "movement_cost_non_neighbor",
                            f"mode {mode} edge {from_hex}->{target}",
                        )
                    factor = _positive_fraction(raw_factor, "movement cost factor")
                    factors[(mode, from_hex, target)] = factor
    return factors


def _load_canonical_movement_costs(
    value: Mapping[str, Any],
    *,
    map_id: str,
    rows: int,
    columns: int,
    neighbors: Mapping[int, frozenset[int]],
) -> dict[tuple[int, int, int], Fraction]:
    if value.get("schema") != MOVEMENT_COST_SCHEMA:
        raise MapRuntimeDataError(
            "unsupported_movement_cost_schema",
            str(value.get("schema")),
        )
    if str(value.get("map_id")) != map_id:
        raise MapRuntimeDataError("movement_map_id_mismatch", map_id)
    if value.get("rows") != rows or value.get("columns") != columns:
        raise MapRuntimeDataError("movement_grid_mismatch", map_id)
    if value.get("mode_order") != list(MOVEMENT_MODE_NAMES):
        raise MapRuntimeDataError("movement_mode_order_mismatch", map_id)
    if value.get("factor_semantics") != "maximum_speed_divided_by_actual_speed":
        raise MapRuntimeDataError("movement_factor_semantics_mismatch", map_id)
    raw_edges = value.get("edges")
    if not isinstance(raw_edges, list):
        raise MapRuntimeDataError("invalid_movement_edges", "edges must be a list")
    factors: dict[tuple[int, int, int], Fraction] = {}
    previous: tuple[int, int, int] | None = None
    for raw in raw_edges:
        if not isinstance(raw, list) or len(raw) != 5:
            raise MapRuntimeDataError("invalid_movement_edge", repr(raw))
        if any(type(item) is not int for item in raw):
            raise MapRuntimeDataError("invalid_movement_edge", repr(raw))
        mode, from_hex, to_hex, numerator, denominator = raw
        key = (mode, from_hex, to_hex)
        if not 0 <= mode < len(MOVEMENT_MODE_NAMES):
            raise MapRuntimeDataError("invalid_movement_mode", str(mode))
        if from_hex not in neighbors or to_hex not in neighbors[from_hex]:
            raise MapRuntimeDataError(
                "movement_cost_non_neighbor",
                f"mode {mode} edge {from_hex}->{to_hex}",
            )
        if previous is not None and key <= previous:
            raise MapRuntimeDataError(
                "movement_edges_not_strictly_sorted",
                repr(key),
            )
        if numerator <= 0 or denominator <= 0:
            raise MapRuntimeDataError("invalid_movement_factor", repr(raw))
        factors[key] = Fraction(numerator, denominator)
        previous = key
    return factors


def _native_map_grid(
    native_map: NativeMap,
) -> tuple[int, int, dict[int, frozenset[int]]]:
    if not native_map.hexes:
        raise MapRuntimeDataError("hex_map_required", native_map.map_id)
    rows = max(divmod(item.hex_id, 100)[0] for item in native_map.hexes) + 1
    columns = max(divmod(item.hex_id, 100)[1] for item in native_map.hexes) + 1
    if columns > 100:
        raise MapRuntimeDataError("unsupported_column_count", str(columns))
    expected = {row * 100 + column for row in range(rows) for column in range(columns)}
    actual = {item.hex_id for item in native_map.hexes}
    if actual != expected:
        raise MapRuntimeDataError(
            "runtime_map_requires_zero_based_rectangular_grid",
            native_map.map_id,
        )
    neighbors = {
        item.hex_id: frozenset(value for value in item.neighbors if value >= 0)
        for item in native_map.hexes
    }
    return rows, columns, neighbors


def _extract_visibility_npy(
    source: Path,
    output: Path,
    *,
    expected_shape: tuple[int, ...],
) -> tuple[_NpyHeader, str, str]:
    source_hash = hashlib.sha256()
    with source.open("rb") as raw:
        for chunk in iter(lambda: raw.read(1024 * 1024), b""):
            source_hash.update(chunk)
    try:
        with ZipFile(source) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if names != ["data.npy"]:
                raise MapRuntimeDataError(
                    "invalid_visibility_npz_members",
                    repr(names),
                )
            info = infos[0]
            if info.is_dir() or info.filename.startswith(("/", "\\")):
                raise MapRuntimeDataError(
                    "invalid_visibility_npz_member",
                    info.filename,
                )
            with archive.open(info) as probe:
                header = _read_npy_header(probe)
            if header.shape != expected_shape:
                raise MapRuntimeDataError(
                    "visibility_shape_mismatch",
                    f"{header.shape} != {expected_shape}",
                )
            expected_size = header.data_offset + math.prod(expected_shape)
            if info.file_size != expected_size:
                raise MapRuntimeDataError(
                    "visibility_uncompressed_size_mismatch",
                    f"{info.file_size} != {expected_size}",
                )
            payload_hash = hashlib.sha256()
            written = 0
            with archive.open(info) as stream, output.open("wb") as target:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
                    payload_hash.update(chunk)
                    written += len(chunk)
                target.flush()
                os.fsync(target.fileno())
            if written != expected_size:
                raise MapRuntimeDataError(
                    "visibility_extraction_size_mismatch",
                    f"{written} != {expected_size}",
                )
            return header, source_hash.hexdigest(), payload_hash.hexdigest()
    except MapRuntimeDataError:
        raise
    except (BadZipFile, OSError, EOFError, ValueError) as exc:
        raise MapRuntimeDataError("invalid_visibility_npz", str(source)) from exc


def _read_npy_header(stream: BinaryIO) -> _NpyHeader:
    if stream.read(6) != b"\x93NUMPY":
        raise MapRuntimeDataError("invalid_visibility_npy", "missing NPY magic")
    version = stream.read(2)
    if version == b"\x01\x00":
        raw_length = stream.read(2)
        if len(raw_length) != 2:
            raise MapRuntimeDataError("invalid_visibility_npy", "truncated header")
        header_length = struct.unpack("<H", raw_length)[0]
    elif version in {b"\x02\x00", b"\x03\x00"}:
        raw_length = stream.read(4)
        if len(raw_length) != 4:
            raise MapRuntimeDataError("invalid_visibility_npy", "truncated header")
        header_length = struct.unpack("<I", raw_length)[0]
    else:
        raise MapRuntimeDataError(
            "unsupported_visibility_npy_version",
            repr(version),
        )
    if not 1 <= header_length <= 65536:
        raise MapRuntimeDataError(
            "invalid_visibility_npy_header_length",
            str(header_length),
        )
    raw_header = stream.read(header_length)
    if len(raw_header) != header_length:
        raise MapRuntimeDataError("invalid_visibility_npy", "truncated header")
    encoding = "utf-8" if version == b"\x03\x00" else "latin1"
    try:
        header = ast.literal_eval(raw_header.decode(encoding).strip())
    except (UnicodeDecodeError, SyntaxError, ValueError) as exc:
        raise MapRuntimeDataError("invalid_visibility_npy_header", str(exc)) from exc
    if not isinstance(header, dict):
        raise MapRuntimeDataError("invalid_visibility_npy_header", "not a dict")
    if set(header) != {"descr", "fortran_order", "shape"}:
        raise MapRuntimeDataError(
            "invalid_visibility_npy_header",
            "unexpected header fields",
        )
    if header.get("descr") != "|b1":
        raise MapRuntimeDataError(
            "unsupported_visibility_dtype",
            str(header.get("descr")),
        )
    if header.get("fortran_order") is not False:
        raise MapRuntimeDataError(
            "unsupported_visibility_order",
            str(header.get("fortran_order")),
        )
    raw_shape = header.get("shape")
    if not isinstance(raw_shape, tuple) or not raw_shape:
        raise MapRuntimeDataError("invalid_visibility_shape", repr(raw_shape))
    if any(type(item) is not int or item <= 0 for item in raw_shape):
        raise MapRuntimeDataError("invalid_visibility_shape", repr(raw_shape))
    return _NpyHeader(
        tuple(raw_shape),
        6 + 2 + len(raw_length) + header_length,
    )


def _positive_fraction(value: object, field: str) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MapRuntimeDataError("invalid_movement_factor", field)
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise MapRuntimeDataError("invalid_movement_factor", field)
    fraction = Fraction(str(value))
    if fraction.denominator > 1_000_000:
        raise MapRuntimeDataError(
            "movement_factor_precision_exceeded",
            str(value),
        )
    return fraction


def _exact_height(value: object, field: str) -> int:
    if type(value) is not int or value not in {0, 20, 200, 500}:
        raise MapRuntimeDataError(
            "unsupported_visibility_height",
            f"{field}={value!r}",
        )
    return value


def _canonical_payload(value: Mapping[str, Any]) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _read_json_mapping(path: Path, name: str) -> Mapping[str, Any]:
    if not path.is_file():
        raise MapRuntimeDataError("runtime_member_not_found", str(path))
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise MapRuntimeDataError("invalid_runtime_json", name) from exc
    return _decode_json_mapping(payload, name)


def _decode_json_mapping(payload: bytes, name: str) -> Mapping[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MapRuntimeDataError("invalid_runtime_json", name) from exc
    if not isinstance(value, Mapping):
        raise MapRuntimeDataError("invalid_runtime_json_root", name)
    return value


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MapRuntimeDataError("invalid_runtime_manifest", f"{name} must be an object")
    return value


def _nonempty_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise MapRuntimeDataError("invalid_runtime_manifest", field)
    return value


def _sha256_text(value: object, field: str) -> str:
    result = _nonempty_text(value, field)
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise MapRuntimeDataError("invalid_sha256", field)
    return result


def _positive_exact_int(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise MapRuntimeDataError("invalid_runtime_manifest", field)
    return value


def _fixed_member_path(directory: Path, value: object, expected: str) -> Path:
    if value != expected:
        raise MapRuntimeDataError(
            "runtime_member_name_mismatch",
            f"{value!r} != {expected!r}",
        )
    return directory / expected


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise MapRuntimeDataError("runtime_member_read_failed", str(path)) from exc
    return digest.hexdigest()
