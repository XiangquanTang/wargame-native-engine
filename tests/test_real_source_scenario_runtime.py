from __future__ import annotations

from contextlib import contextmanager
import hashlib
from http.client import HTTPConnection
import json
from pathlib import Path
import threading
from typing import Iterator

import pytest

from scripts.run_source_native_match import run_source_native_match
from wargame_native_engine.data import (
    EXPLICIT_NATIVE_SUPPLEMENT,
    ScenarioBundle,
    ScenarioCatalog,
    convert_source_scenario,
    publish_source_scenario_conversion,
)
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)
from wargame_native_engine.operations import WargameFacade
from wargame_native_engine.viewer.dual_control_server import (
    DualSideControlHTTPServer,
    create_dual_side_control_server,
)


SNAPSHOT_ROOT = Path(
    "/home/txq/wargame/wargame_benchmark_lab/local_data/"
    "sdk_data_snapshot/phase9a_multireplay_pilot"
)
SOURCE_SCENARIO = SNAPSHOT_ROOT / "scenarios/2201010105.json"
SOURCE_MAP = SNAPSHOT_ROOT / "maps/123/basic.json"
SUPPLEMENT = Path(
    "config/source_scenario_conversion/scenarios/"
    "2201010105.native-acceptance-supplement.json"
)


@pytest.fixture(scope="module")
def real_bundle(tmp_path_factory: pytest.TempPathFactory):
    if not SOURCE_SCENARIO.is_file() or not SOURCE_MAP.is_file():
        pytest.skip("read-only Map 123 Source acceptance inputs are unavailable")
    hashes_before = (_sha256(SOURCE_SCENARIO), _sha256(SOURCE_MAP))
    conversion = convert_source_scenario(
        SOURCE_SCENARIO,
        SOURCE_MAP,
        conversion_mode=EXPLICIT_NATIVE_SUPPLEMENT,
        source_scenario_identity="scenarios/2201010105.json",
        source_map_identity="maps/123/basic.json",
        expected_scenario_id="2201010105",
        expected_map_id="123",
        supplement_path=SUPPLEMENT,
    )
    published = publish_source_scenario_conversion(
        conversion, tmp_path_factory.mktemp("real-source") / "canonical-bundle"
    )
    yield published, conversion, hashes_before
    assert hashes_before == (_sha256(SOURCE_SCENARIO), _sha256(SOURCE_MAP))


def test_real_source_scenario_dual_http_and_map_identity(real_bundle, tmp_path: Path) -> None:
    published, conversion, _ = real_bundle
    bundle = ScenarioBundle.from_path(published.scenario_path)
    app = WargameFacade(
        ScenarioCatalog.from_bundles({bundle.scenario_identity.scenario_id: bundle}),
        storage_root=tmp_path / "http-managed",
    )
    match = app.create_match(
        scenario_id=bundle.scenario_identity.scenario_id,
        red_controller_id="http-real-red",
        blue_controller_id="http-real-blue",
        seed=42,
        decision_interval_ms=60_000,
        frame_interval_ms=60_000,
        record=False,
    )
    server = create_dual_side_control_server(
        match.gateway,
        session_id=match.session_id,
        red_controller_id="http-real-red",
        blue_controller_id="http-real-blue",
        red_token="real-red-token",
        blue_token="real-blue-token",
        map_data=match.map_record,
        output_dir=tmp_path / "http-output",
        port=0,
    )
    try:
        with _running_server(server) as address:
            red = _request_json(address, "GET", "/api/product/bootstrap?token=real-red-token")
            blue = _request_json(address, "GET", "/api/product/bootstrap?token=real-blue-token")
            red_control = _request_json(address, "GET", "/api/bootstrap?token=real-red-token")
            blue_control = _request_json(address, "GET", "/api/bootstrap?token=real-blue-token")
            assert "view" not in red
            assert "view" not in blue
            assert red["scene"]["map"]["map_identity"]["sha256"] == (
                conversion.manifest["native_map"]["sha256"]
            )
            assert len(red["scene"]["map"]["cells"]) == 10_000
            assert len(red_control["view"]["observation"]["friendly_operators"]) == 26
            assert len(blue_control["view"]["observation"]["friendly_operators"]) == 23
            assert red_control["view"]["valid_actions"]
            assert blue_control["view"]["valid_actions"]
            blue_ids = [
                item["operator_id"]
                for item in blue["scene"]["frame"]["viewer_units"]
                if item["side"] == "blue" and item["category"] == "operator"
            ]
            blue_capabilities = _request_json(
                address,
                "POST",
                "/api/product/capabilities",
                {"token": "real-blue-token", "actor_ids": blue_ids},
            )
            assert blue_capabilities["selected_count"] == 23
            assert blue_capabilities["operations"]["MOVE"]["applicable_count"] > 0
            assert blue_capabilities["operations"]["WAIT"]["applicable_count"] == 23
            assert all(
                item["operator_id"] != 75
                for item in red_control["view"]["observation"]["visible_enemies"]
            )
            preview = _request_json(
                address,
                "POST",
                "/api/product/preview",
                {
                    "token": "real-red-token",
                    "request_id": "real-http-move-preview",
                    "actor_ids": [27, 28, 29],
                    "operation_type": "MOVE",
                    "destination_hex": 2940,
                },
            )
            assert preview["executable_count"] == 3
            assert [len(item["path"]) for item in preview["per_unit_results"]] == [3, 4, 5]
    finally:
        server.server_close()
        match.close()


def test_real_source_scenario_move_shoot_replay_product_loop(
    real_bundle, tmp_path: Path,
) -> None:
    published, conversion, hashes_before = real_bundle
    report = run_source_native_match(
        published.scenario_path,
        tmp_path / "match",
        seed=42,
        decision_interval_ms=60_000,
        frame_interval_ms=60_000,
        max_decision_windows=20,
    )

    assert report["status"] == "passed"
    assert report["scenario"]["operator_count"] == 49
    assert report["scenario"]["red_count"] == 26
    assert report["scenario"]["blue_count"] == 23
    assert report["scenario"]["map_sha256"] == (
        conversion.manifest["native_map"]["sha256"]
    )
    paths = report["move"]["recorded_paths"]
    assert {key: len(value["path"]) for key, value in paths.items()} == {
        "27": 3,
        "28": 4,
        "29": 5,
    }
    for unit_id in ("27", "28", "29"):
        positions = report["move"]["frame_positions"][unit_id]
        assert positions[0]["hex"] == report["move"]["initial_positions"][unit_id]
        assert positions[-1]["hex"] == 2940
        assert len({item["hex"] for item in positions}) >= 2
    assert report["shoot"]["recorded_action"]["action"] == {
        "action_type": "SHOOT",
        "actor_id": 71,
        "parameters": {"weapon_id": 73},
        "side": "red",
        "target_id": 75,
        "target_position": None,
    }
    judge_events = report["shoot"]["judge_info"]["judge_info"][0]["result"][
        "events"
    ]
    validated = next(
        item for item in judge_events if item["event_type"] == "direct_fire_validated"
    )
    assert validated["data"]["legal"] is True
    assert validated["data"]["evidence"]["checks"]["target_observed"] is True
    assert report["runtime"]["terminal"] is True
    assert report["runtime"]["termination_reason"] == "max_ticks"
    assert report["runtime"]["frame_count"] == 16
    assert report["runtime"]["step_count"] == 15
    assert report["runtime"]["final_sim_time_ms"] == 900_000
    assert report["evidence"]["strict_replay_match"] is True
    assert report["evidence"]["online_offline_analysis_match"] is True

    archive = SourceReplayArchiveImporter().import_path(
        tmp_path / "match/replay.zip"
    )
    assert len(archive.frames) == 16
    assert len(archive.steps) == 15
    assert any(
        action["action_type"] == "SHOOT"
        for step in archive.steps
        for action in step["ordered_actions"]
    )
    assert hashes_before == (_sha256(SOURCE_SCENARIO), _sha256(SOURCE_MAP))


@contextmanager
def _running_server(
    server: DualSideControlHTTPServer,
) -> Iterator[tuple[str, int]]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield str(host), int(port)
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _request_json(address, method: str, path: str, value=None):
    body = None if value is None else json.dumps(value).encode("utf-8")
    headers = {} if body is None else {"Content-Type": "application/json"}
    connection = HTTPConnection(*address, timeout=30)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        assert response.status == 200, payload.decode("utf-8", errors="replace")
        return json.loads(payload.decode("utf-8"))
    finally:
        connection.close()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
