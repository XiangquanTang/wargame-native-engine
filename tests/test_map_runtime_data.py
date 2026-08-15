from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import pickle
import struct
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.template import (
    source_frame_template_for_scenario,
)
from wargame_native_engine.core.action import ActionType
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.data import (
    MapRuntimeData,
    MapRuntimeDataError,
    publish_map_runtime_data,
)
from wargame_native_engine.gateway.session_manager import SessionManager
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.scenario import (
    NativeHex,
    NativeMap,
    scenario_from_dict,
)
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import (
    SourceCompatibleEnvironment,
)


def _native_map() -> NativeMap:
    return NativeMap(
        map_id="runtime-test-map",
        width=400.0,
        height=400.0,
        hexes=(
            NativeHex(0, 0, 0, (1, -1, -1, -1, -1, 100)),
            NativeHex(1, 0, 0, (-1, -1, -1, 0, 100, 101)),
            NativeHex(100, 0, 0, (101, 1, 0, -1, -1, -1)),
            NativeHex(101, 0, 0, (-1, -1, 1, 100, -1, -1)),
        ),
        hex_size_m=200,
        elevation_unit_m=20,
        elevation_kind="METERS",
    )


def _movement_costs() -> list[list[list[dict[int, float | int]]]]:
    native_map = _native_map()
    by_hex = {item.hex_id: item for item in native_map.hexes}
    modes: list[list[list[dict[int, float | int]]]] = []
    for mode in range(4):
        rows: list[list[dict[int, float | int]]] = []
        for row in range(2):
            cells = []
            for column in range(2):
                cell = by_hex[row * 100 + column]
                factor: float | int = (2 if mode == 0 else 1.5 if mode == 1 else 1)
                cells.append({target: factor for target in cell.neighbors if target >= 0})
            rows.append(cells)
        modes.append(rows)
    return modes


def _npy_payload(
    *,
    shape: tuple[int, ...] = (10, 2, 2, 2, 2),
    true_values: tuple[tuple[int, int, int], ...] = (),
) -> bytes:
    document = {
        "descr": "|b1",
        "fortran_order": False,
        "shape": shape,
    }
    body = repr(document).encode("latin1")
    padding = (-(10 + len(body) + 1)) % 64
    header = body + b" " * padding + b"\n"
    prefix = b"\x93NUMPY\x01\x00" + struct.pack("<H", len(header)) + header
    values = bytearray(_product(shape))
    rows, columns = shape[1], shape[2]
    for mode, first_hex, second_hex in true_values:
        first_row, first_col = divmod(first_hex, 100)
        second_row, second_col = divmod(second_hex, 100)
        flat = (
            ((((mode * rows) + first_row) * columns + first_col)
             * rows + second_row)
            * columns
            + second_col
        )
        values[flat] = 1
    return prefix + bytes(values)


def _write_sources(
    root: Path,
    *,
    movement: object | None = None,
    visibility_payload: bytes | None = None,
) -> tuple[Path, Path, Path]:
    root.mkdir(parents=True)
    map_path = root / "basic.json"
    map_path.write_text(
        '{"map_id":"runtime-test-map","fixture":"runtime-map"}\n',
        encoding="utf-8",
    )
    cost_path = root / "cost.pickle"
    cost_path.write_bytes(pickle.dumps(
        _movement_costs() if movement is None else movement,
        protocol=3,
    ))
    see_path = root / "see.npz"
    with ZipFile(see_path, "w", compression=ZIP_DEFLATED) as archive:
        info = ZipInfo("data.npy", date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = ZIP_DEFLATED
        archive.writestr(
            info,
            _npy_payload() if visibility_payload is None else visibility_payload,
        )
    return map_path, cost_path, see_path


def _publish(root: Path, *, true_values=()) -> tuple[MapRuntimeData, Path]:
    sources = root / "sources"
    map_path, cost_path, see_path = _write_sources(
        sources,
        visibility_payload=_npy_payload(true_values=true_values),
    )
    output = root / "runtime"
    publish_map_runtime_data(
        _native_map(),
        source_map_path=map_path,
        movement_cost_path=cost_path,
        visibility_npz_path=see_path,
        output_dir=output,
    )
    return MapRuntimeData.load(output, _native_map()), output


def _scenario():
    native_map = _native_map()
    operators = []
    for unit_id, side, current_hex, domain in (
        (1, "red", 0, "VEHICLE"),
        (2, "blue", 101, "VEHICLE"),
    ):
        row, column = divmod(current_hex, 100)
        operators.append({
            "operator_id": unit_id,
            "side": side,
            "operator_type": domain,
            "subtype": None,
            "x": column,
            "y": row,
            "altitude": 0,
            "heading": 0,
            "health": 100,
            "max_health": 100,
            "blood": 3,
            "max_blood": 3,
            "speed": 0,
            "max_speed": 36,
            "basic_speed": 36,
            "move_range": 0,
            "sensor_range": 25,
            "state": "STOPPED",
            "alive": True,
            "deployed": True,
            "weapon_ids": [],
            "attack_range": 0,
            "attack_damage": 0,
            "current_hex": current_hex,
            "cur_pos_millionths": 0,
            "move_path": [],
            "movement_mode": "NORMAL",
            "stopped": True,
            "raw_attributes": {
                "canonical_type": "HEAVY_TANK",
                "armor_class": "COMPOSITE",
            },
        })
    return scenario_from_dict({
        "schema_version": "native-scenario/1.0",
        "scenario_id": "runtime-data-test",
        "initial_stage": "combat",
        "map": native_map.to_dict(),
        "sides": ["red", "blue"],
        "operators": operators,
        "objectives": [],
        "deployment": {"positions": {
            "red": {"1": [0, 0]},
            "blue": {"2": [1, 1]},
        }},
        "terminal_rules": {
            "max_ticks": 100,
            "elimination": True,
            "objective_score_to_win": None,
        },
        "metadata": {"source_compatible": {"weapons": {}}},
    })


def test_conversion_is_deterministic_disk_backed_and_preserves_sources(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "sources"
    map_path, cost_path, see_path = _write_sources(source_root)
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (map_path, cost_path, see_path)
    }
    first = publish_map_runtime_data(
        _native_map(),
        source_map_path=map_path,
        movement_cost_path=cost_path,
        visibility_npz_path=see_path,
        output_dir=tmp_path / "first",
    )
    second = publish_map_runtime_data(
        _native_map(),
        source_map_path=map_path,
        movement_cost_path=cost_path,
        visibility_npz_path=see_path,
        output_dir=tmp_path / "second",
    )

    assert first.manifest_path.read_bytes() == second.manifest_path.read_bytes()
    assert first.movement_cost_path.read_bytes() == second.movement_cost_path.read_bytes()
    assert first.visibility_path.read_bytes() == second.visibility_path.read_bytes()
    assert first.visibility_path.stat().st_size == len(_npy_payload())
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["visibility"]["canonical_payload"]["storage"] == (
        "read_only_memory_mapped_npy"
    )
    assert manifest["visibility"]["semantics"] == "static_line_of_sight_only"
    assert manifest["native_rules_equivalence"] is False
    assert {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (map_path, cost_path, see_path)
    } == before


def test_movement_factor_and_visibility_modes_are_queried_without_numpy(
    tmp_path: Path,
) -> None:
    runtime, _ = _publish(tmp_path, true_values=((0, 0, 101), (2, 1, 100)))

    assert runtime.movement.factor(0, 0, 1).as_integer_ratio() == (2, 1)
    assert runtime.movement.factor(1, 0, 1).as_integer_ratio() == (3, 2)
    assert runtime.visibility.visible(
        0, 101, observer_height_m=0, target_height_m=0
    )
    # Mixed-height mode 2 is stored low-to-ground.  Querying from the ground
    # endpoint swaps coordinates deterministically.
    assert runtime.visibility.visible(
        100, 1, observer_height_m=0, target_height_m=200
    )
    assert runtime.visibility._mapping is not None
    runtime.visibility.close()
    assert runtime.visibility._mapping is None


def test_restricted_pickle_rejects_global_reduce_without_execution(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "must-not-exist"

    class Payload:
        def __reduce__(self):
            return os.system, (f"touch {marker}",)

    map_path, cost_path, see_path = _write_sources(
        tmp_path / "sources",
        movement=Payload(),
    )
    with pytest.raises(MapRuntimeDataError, match="unsafe_movement_pickle"):
        publish_map_runtime_data(
            _native_map(),
            source_map_path=map_path,
            movement_cost_path=cost_path,
            visibility_npz_path=see_path,
            output_dir=tmp_path / "runtime",
        )
    assert not marker.exists()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value[0][0][0].__setitem__(101, 1), "non_neighbor"),
        (lambda value: value[0][0][0].__setitem__(1, True), "factor"),
        (lambda value: value.pop(), "shape"),
    ],
)
def test_invalid_movement_costs_are_rejected(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    movement = _movement_costs()
    mutation(movement)
    map_path, cost_path, see_path = _write_sources(
        tmp_path / "sources",
        movement=movement,
    )
    with pytest.raises(MapRuntimeDataError, match=message):
        publish_map_runtime_data(
            _native_map(),
            source_map_path=map_path,
            movement_cost_path=cost_path,
            visibility_npz_path=see_path,
            output_dir=tmp_path / "runtime",
        )


def test_visibility_shape_and_runtime_payload_hash_are_enforced(
    tmp_path: Path,
) -> None:
    map_path, cost_path, see_path = _write_sources(
        tmp_path / "sources",
        visibility_payload=_npy_payload(shape=(10, 1, 1, 1, 1)),
    )
    with pytest.raises(MapRuntimeDataError, match="shape_mismatch"):
        publish_map_runtime_data(
            _native_map(),
            source_map_path=map_path,
            movement_cost_path=cost_path,
            visibility_npz_path=see_path,
            output_dir=tmp_path / "bad-runtime",
        )

    runtime, output = _publish(tmp_path / "valid")
    runtime.visibility.close()
    payload = output / "movement_costs.native.json"
    payload.write_bytes(payload.read_bytes() + b" ")
    with pytest.raises(MapRuntimeDataError, match="movement_payload_hash_mismatch"):
        MapRuntimeData.load(output, _native_map())


def test_runtime_revalidates_canonical_edges_after_manifest_rehash(
    tmp_path: Path,
) -> None:
    runtime, output = _publish(tmp_path)
    runtime.visibility.close()
    payload_path = output / "movement_costs.native.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload["edges"][0][2] = 101  # not adjacent to source hex 0
    payload_bytes = (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    payload_path.write_bytes(payload_bytes)
    manifest_path = output / "map_runtime_manifest.native.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["movement"]["canonical_payload"]["sha256"] = hashlib.sha256(
        payload_bytes
    ).hexdigest()
    manifest_path.write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(MapRuntimeDataError, match="movement_cost_non_neighbor"):
        MapRuntimeData.load(output, _native_map())


def test_engine_uses_cost_for_state_progress_and_static_visibility_lookup(
    tmp_path: Path,
) -> None:
    # The Native algorithm would see these nearby endpoints.  A false static
    # lookup proves that the configured map data is the LOS authority.
    runtime, _ = _publish(tmp_path)
    scenario = _scenario()
    engine = NativeEngine(scenario, map_runtime_data=runtime)
    engine.reset(42)

    assert engine.observe("red").visible_enemies == ()
    move = next(
        item.action
        for item in engine.valid_actions("red")
        if item.action.action_type is ActionType.MOVE
        and item.action.parameters["path"] == [1]
    )
    result = engine.step([move], delta_time_ms=1_000)
    state = engine.snapshot()["world"]["operators"]["1"]
    evidence = engine.snapshot()["world"]["movement_evidence"][0]

    assert result.decisions[0].accepted
    assert state["current_hex"] == 0
    assert state["cur_pos_millionths"] == 25_000
    assert state["segment_traversal_ms"] == 40_000
    assert evidence["effective_speed"] == 18
    assert evidence["map_cost_mode"] == 0
    assert evidence["map_cost_factor"] == [2, 1]
    assert evidence["map_cost_source_sha256"] == runtime.movement.source_sha256
    assert engine.snapshot()["map_runtime_data"] == runtime.identity_record()


def test_explicit_air_cost_mode_enables_adjacent_native_air_progress(
    tmp_path: Path,
) -> None:
    runtime, _ = _publish(tmp_path)
    record = _scenario().to_dict()
    red = next(item for item in record["operators"] if item["side"] == "red")
    red["operator_type"] = "AIR"
    red["basic_speed"] = 100
    red["max_speed"] = 100
    red["raw_attributes"]["canonical_type"] = "DRONE"
    engine = NativeEngine(
        scenario_from_dict(record),
        map_runtime_data=runtime,
    )
    engine.reset(42)

    action = next(
        item.action
        for item in engine.valid_actions("red")
        if item.action.action_type is ActionType.MOVE
        and item.action.parameters["path"] == [1]
    )
    result = engine.step([action], delta_time_ms=1_000)
    evidence = engine.snapshot()["world"]["movement_evidence"][0]

    assert result.decisions[0].accepted
    assert evidence["map_cost_mode"] == 3
    assert evidence["effective_speed"] == 100
    assert evidence["map_cost_factor"] == [1, 1]


def test_runtime_identity_is_bound_to_replay_and_strict_replay(
    tmp_path: Path,
) -> None:
    runtime, _ = _publish(tmp_path / "data", true_values=((0, 0, 101),))
    scenario = _scenario()

    def factory() -> SourceCompatibleEnvironment:
        return SourceCompatibleEnvironment(
            GameSession(NativeEngineBackend(
                scenario,
                map_runtime_data=runtime,
            )),
            template=source_frame_template_for_scenario(scenario),
        )

    manager = SessionManager(factory, storage_root=tmp_path / "sessions")
    descriptor = manager.create_session(seed=42, record=True)
    action = next(
        item.action
        for item in manager.valid_actions(descriptor.session_id, "red")
        if item.action.action_type is ActionType.MOVE
        and item.action.parameters["path"] == [1]
    )
    manager.advance_ms(descriptor.session_id, (action,), delta_time_ms=1_000)
    publication = manager.publish_session(
        descriptor.session_id,
        tmp_path / "published",
        truncated=True,
    )

    verification = replay_source_archive(
        scenario,
        publication.replay_path,
        strict=True,
        map_runtime_data=runtime,
    )
    assert verification.matches
    with pytest.raises(ValueError, match="map_runtime_data"):
        replay_source_archive(
            scenario,
            publication.replay_path,
            strict=True,
        )


def test_runtime_map_identity_must_match(tmp_path: Path) -> None:
    runtime, output = _publish(tmp_path)
    runtime.visibility.close()
    other = NativeMap(
        map_id="other",
        width=400.0,
        height=400.0,
        hexes=_native_map().hexes,
        hex_size_m=200,
        elevation_unit_m=20,
        elevation_kind="METERS",
    )
    with pytest.raises(MapRuntimeDataError, match="identity_mismatch"):
        MapRuntimeData.load(output, other)


def _product(values: tuple[int, ...]) -> int:
    result = 1
    for value in values:
        result *= value
    return result
