from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from http.client import HTTPConnection
import json
from pathlib import Path
import threading
from time import perf_counter
from typing import Iterator

import pytest

from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.agents import ManualRealtimeClock, RealtimeAgentSession
from wargame_native_engine.core.action import ActionType
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.data import (
    DataProvenance,
    ScenarioBundle,
    ScenarioCatalog,
)
from wargame_native_engine.io.scenario import scenario_from_dict
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.session import GameSession
from wargame_native_engine.visibility import hex_distance
from wargame_native_engine.operations import WargameFacade
from wargame_native_engine.viewer.realtime_control_server import (
    RealtimeDualControlHTTPServer,
    create_realtime_dual_control_server,
)


SCENARIO = Path("examples/public_river_valley_scenario.json")


def _overlay_scenario_record() -> dict[str, object]:
    """Return a deterministic combat layout independent of demo edits."""

    record = json.loads(SCENARIO.read_text(encoding="utf-8"))
    record["scenario_id"] = "tactical-overlay-public-river-valley"
    positions = {
        103: (11, 7, 711),
        203: (17, 7, 717),
    }
    for operator in record["operators"]:
        operator_id = int(operator["operator_id"])
        if operator_id not in positions:
            continue
        x, y, hex_id = positions[operator_id]
        operator["x"] = x
        operator["y"] = y
        operator["current_hex"] = hex_id
    record["deployment"]["positions"]["red"]["103"] = [11, 7]
    record["deployment"]["positions"]["blue"]["203"] = [17, 7]
    return record


def _overlay_bundle() -> ScenarioBundle:
    record = _overlay_scenario_record()
    return ScenarioBundle.from_record(
        record,
        provenance=DataProvenance(
            source_sha256=canonical_hash(record),
            source_kind="deterministic_test_derivative",
        ),
    )


class _MatchFactory:
    def __init__(self, root: Path, bundle: ScenarioBundle) -> None:
        self.root = root
        self.bundle = bundle

    def __call__(self, seed: int, episode_sequence: int):
        app = WargameFacade(
            ScenarioCatalog.from_bundles({
                self.bundle.scenario_identity.scenario_id: self.bundle,
            }),
            storage_root=self.root / f"managed-{episode_sequence}",
        )
        return app.create_match(
            scenario_id=self.bundle.scenario_identity.scenario_id,
            red_controller_id="overlay-red",
            blue_controller_id="overlay-blue",
            seed=seed,
            decision_interval_ms=1_000,
            frame_interval_ms=1_000,
        )


@contextmanager
def _running_server(
    server: RealtimeDualControlHTTPServer,
) -> Iterator[tuple[str, int]]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield str(host), int(port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(address, method: str, path: str, value=None):
    body = None if value is None else json.dumps(value).encode("utf-8")
    headers = {} if body is None else {"Content-Type": "application/json"}
    connection = HTTPConnection(*address, timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()


@pytest.fixture
def public_session():
    bundle = _overlay_bundle()
    session = GameSession(NativeEngineBackend(
        bundle.scenario,
        map_runtime_data=bundle.map_runtime_data,
    ))
    session.reset(42)
    try:
        yield session
    finally:
        session.close()


def test_overlay_is_side_bound_detached_and_engine_read_only(public_session) -> None:
    engine = public_session.backend.engine
    before = deepcopy(engine.snapshot())
    events_before = tuple(engine._last_events)  # noqa: SLF001

    overlay = public_session.query_unit_tactical_overlay("red", 103)
    record = overlay.to_record()

    assert record["schema"] == "native-tactical-overlay/1.0"
    assert record["unit_id"] == 103
    assert record["visibility_field"]["semantics"] == (
        "observable_unconcealed_ground_target"
    )
    assert set(record["visibility_field"]["by_target_domain"]) == {
        "PERSONNEL", "VEHICLE",
    }
    assert record["weapon_envelope"]["semantics"] == (
        "theoretical_direct_fire_envelope"
    )
    assert engine.snapshot() == before
    assert engine._last_events == events_before  # noqa: SLF001

    # Returned JSON structures are detached from both the frozen model and the
    # Engine; a browser cannot mutate a later query through nested lists.
    record["visibility_field"]["hex_ids"].clear()
    assert overlay.visibility_field.hex_ids
    assert public_session.query_unit_tactical_overlay(
        "red", 103
    ).visibility_field.hex_ids

    with pytest.raises(ValueError, match="not controlled"):
        public_session.query_unit_tactical_overlay("red", 203)
    with pytest.raises(ValueError, match="not controlled"):
        public_session.query_unit_tactical_overlay("blue", 103)


def test_visibility_field_reuses_los_and_terrain_observation_limits(
    public_session,
) -> None:
    engine = public_session.backend.engine
    overlay = public_session.query_unit_tactical_overlay("red", 103)
    personnel = next(
        value.hex_ids
        for value in overlay.visibility_field.by_target_domain
        if value.target_domain == "PERSONNEL"
    )
    observer = engine._world.operators[103]  # noqa: SLF001
    candidates = [
        cell.hex_id
        for cell in engine.scenario.map.hexes
        if hex_distance(observer.current_hex, cell.hex_id) <= 10
    ]
    blocked = [
        hex_id for hex_id in candidates
        if not engine._line_of_sight_to_ground_hex(  # noqa: SLF001
            observer, hex_id,
        ).visible
    ]

    assert blocked, "public terrain must exercise an actual LOS block"
    assert set(blocked).isdisjoint(personnel)
    assert observer.current_hex in personnel


def test_weapon_envelope_does_not_depend_on_enemy_occupancy() -> None:
    original_record = _overlay_scenario_record()
    no_enemy_record = deepcopy(original_record)
    no_enemy_record["scenario_id"] = "public-river-valley-no-enemy-overlay"
    no_enemy_record["operators"] = [
        value for value in no_enemy_record["operators"]
        if value["side"] == "red"
    ]
    no_enemy_record["deployment"]["positions"]["blue"] = {}

    original = NativeEngine(scenario_from_dict(original_record))
    no_enemy = NativeEngine(scenario_from_dict(no_enemy_record))
    original.reset(42)
    no_enemy.reset(42)
    try:
        with_enemy = original.query_unit_tactical_overlay("red", 103)
        without_enemy = no_enemy.query_unit_tactical_overlay("red", 103)
        assert with_enemy.weapon_envelope == without_enemy.weapon_envelope
        assert with_enemy.fire_targets.targets
        assert without_enemy.fire_targets.targets == ()
    finally:
        original.close()
        no_enemy.close()


def test_fire_targets_are_exactly_the_existing_shoot_action_projection(
    public_session,
) -> None:
    overlay = public_session.query_unit_tactical_overlay("red", 103)
    world = public_session.snapshot()["world"]
    expected = sorted({
        (
            value.action.target_id,
            world["operators"][str(value.action.target_id)]["current_hex"],
        )
        for value in public_session.valid_actions("red")
        if value.action.action_type is ActionType.SHOOT
        and value.action.actor_id == 103
    })

    assert [
        (value.target_id, value.hex_id) for value in overlay.fire_targets.targets
    ] == expected
    assert expected == [(202, 619), (203, 717)]


def test_public_432_hex_overlay_query_is_bounded_and_preserves_move_actions(
    public_session,
) -> None:
    before_moves = tuple(
        value.to_dict() for value in public_session.valid_actions("red")
        if value.action.action_type is ActionType.MOVE
    )
    started = perf_counter()
    overlay = public_session.query_unit_tactical_overlay("red", 103)
    elapsed = perf_counter() - started
    after_moves = tuple(
        value.to_dict() for value in public_session.valid_actions("red")
        if value.action.action_type is ActionType.MOVE
    )

    assert len(public_session.scenario.map.hexes) == 432
    assert overlay.visibility_field.hex_ids
    assert overlay.weapon_envelope.hex_ids
    assert before_moves == after_moves
    assert elapsed < 2.0


def test_realtime_overlay_api_is_current_seat_bound_and_recorder_neutral(
    tmp_path: Path,
) -> None:
    bundle = _overlay_bundle()
    factory = _MatchFactory(tmp_path / "factory", bundle)
    session = RealtimeAgentSession(
        factory,
        run_namespace="tactical-overlay-api-test",
        tick_interval_ms=1_000,
        clock=ManualRealtimeClock(),
    )
    reset = session.reset(seed=42)
    server = create_realtime_dual_control_server(
        session,
        red_binding=reset.red_binding,
        blue_binding=reset.blue_binding,
        red_token="overlay-red-token",
        blue_token="overlay-blue-token",
        map_data=bundle.render_map_record(),
        port=0,
    )
    try:
        match = session._require_match()  # noqa: SLF001
        managed = match.gateway.manager._managed(match.session_id)  # noqa: SLF001
        engine = managed.environment.session.backend.engine
        before = deepcopy(engine.snapshot())
        events_before = tuple(engine._last_events)  # noqa: SLF001
        facts_before = {
            path.relative_to(managed.recorder.root): path.read_bytes()
            for path in managed.recorder.root.rglob("*") if path.is_file()
        }
        with _running_server(server) as address:
            red_status, red = _request(
                address,
                "GET",
                "/api/product/bootstrap?token=overlay-red-token",
            )
            blue_status, blue = _request(
                address,
                "GET",
                "/api/product/bootstrap?token=overlay-blue-token",
            )
            assert red_status == blue_status == 200

            status, overlay = _request(
                address,
                "POST",
                "/api/product/tactical-overlay",
                {
                    "token": "overlay-red-token",
                    "observation_version": red["observation_version"],
                    "unit_id": 103,
                },
            )
            assert status == 200
            assert overlay["schema"] == "native-tactical-overlay/1.0"
            assert overlay["observation_version"] == red["observation_version"]
            assert overlay["fire_targets"]["targets"] == [
                {"target_id": 202, "hex_id": 619},
                {"target_id": 203, "hex_id": 717},
            ]
            assert engine.snapshot() == before
            assert engine._last_events == events_before  # noqa: SLF001
            assert {
                path.relative_to(managed.recorder.root): path.read_bytes()
                for path in managed.recorder.root.rglob("*") if path.is_file()
            } == facts_before

            assert _request(address, "POST", "/api/product/tactical-overlay", {
                "token": "overlay-red-token",
                "observation_version": red["observation_version"],
                "unit_id": 203,
            })[0] == 403
            assert _request(address, "POST", "/api/product/tactical-overlay", {
                "token": "overlay-blue-token",
                "observation_version": blue["observation_version"],
                "unit_id": 203,
            })[0] == 200
            cross_status, cross = _request(
                address, "POST", "/api/product/tactical-overlay", {
                    "token": "overlay-red-token",
                    "observation_version": blue["observation_version"],
                    "unit_id": 103,
                },
            )
            assert cross_status == 409
            assert cross["code"] == "tactical_overlay_observation_side_mismatch"

            # Overlay reads cannot affect the existing product MOVE preview.
            move_status, move = _request(
                address,
                "POST",
                "/api/product/preview",
                {
                    "token": "overlay-red-token",
                    "request_id": "overlay-does-not-affect-move",
                    "observation_version": red["observation_version"],
                    "actor_ids": [101],
                    "operation_type": "MOVE",
                    "destination_hex": 405,
                    "movement_mode": "NORMAL",
                },
            )
            assert move_status == 200
            assert move["can_execute"] is True

            session.advance_tick()
            stale_status, stale = _request(
                address, "POST", "/api/product/tactical-overlay", {
                    "token": "overlay-red-token",
                    "observation_version": red["observation_version"],
                    "unit_id": 103,
                },
            )
            assert stale_status == 409
            assert stale["code"] == "tactical_overlay_observation_stale"
    finally:
        session.close()


def test_tactical_overlay_frontend_is_read_only_and_below_action_targets() -> None:
    static = Path("src/wargame_native_engine/viewer/static")
    source = (static / "dual_control.js").read_text(encoding="utf-8")
    page = (static / "dual_control_index.html").read_text(encoding="utf-8")
    renderer = (static / "map_renderer.js").read_text(encoding="utf-8")

    assert '"/api/product/tactical-overlay"' in source
    assert "observation_version: observationVersion" in source
    assert "unit_id: unitId" in source
    assert "tacticalOverlayCache" in source
    assert "overlayRequestEpoch" in source
    assert 'id="toggle-visibility-overlay"' in page
    assert 'id="toggle-weapon-envelope"' in page
    assert "setTacticalOverlay" in renderer
    assert "_drawTacticalOverlays" in renderer
    assert renderer.index("this._drawMap(context)") < renderer.index(
        "this._drawTacticalOverlays(context)"
    ) < renderer.index("this._drawScenarioFeatures(context")
    assert renderer.index("this._drawTacticalOverlays(context)") < renderer.index(
        "this._drawActionTargets(context)"
    )
    for color in (
        "rgba(255,120,120,0.18)",
        "rgba(255,80,80,0.12)",
        "rgba(110,175,255,0.18)",
        "rgba(70,140,255,0.12)",
    ):
        assert color in renderer
    overlay_draw = renderer[
        renderer.index("_drawTacticalOverlays(context)"):
        renderer.index("_drawSelectedHex(context)")
    ]
    assert "onHexSelected" not in overlay_draw
    assert "ActionType" not in overlay_draw
