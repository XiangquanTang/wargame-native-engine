from __future__ import annotations

from contextlib import contextmanager
import hashlib
from http.client import HTTPConnection
import json
from pathlib import Path
import threading
from typing import Iterator

import pytest

from scripts.run_local_control import build_parser
from scripts.run_minimal_source_replay import _template
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.gateway import CommandGateway, SessionManager
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import (
    SourceCompatibleEnvironment,
)
from wargame_native_engine.viewer.live_control import (
    controller_view_record,
    native_scenario_map_record,
)
from wargame_native_engine.viewer.live_server import (
    LiveControlHTTPServer,
    create_live_control_server,
)


SCENARIO_PATH = Path("examples/source_movement_scenario.json")


class Fixture:
    def __init__(self, root: Path, *, perspective: str = "red") -> None:
        self.scenario = load_scenario(SCENARIO_PATH)
        self.environments: list[SourceCompatibleEnvironment] = []

        def factory() -> SourceCompatibleEnvironment:
            environment = SourceCompatibleEnvironment(
                GameSession(NativeEngineBackend(self.scenario)),
                template=_template(self.scenario),
            )
            self.environments.append(environment)
            return environment

        self.manager = SessionManager(factory, storage_root=root / "managed")
        self.gateway = CommandGateway(self.manager, delta_time_seconds=20)
        self.red_id = "web-red"
        self.blue_id = "web-blue"
        descriptor = self.gateway.create_session(
            red_controller_id=self.red_id,
            blue_controller_id=self.blue_id,
            seed=42,
        )
        self.session_id = descriptor.session_id
        selected = self.red_id if perspective == "red" else self.blue_id
        opponent = self.blue_id if perspective == "red" else self.red_id
        self.output = root / "published"
        self.server = create_live_control_server(
            self.gateway,
            session_id=self.session_id,
            controller_id=selected,
            opponent_controller_id=opponent,
            perspective=perspective,
            map_data=native_scenario_map_record(self.scenario),
            output_dir=self.output,
            port=0,
        )

    def close(self) -> None:
        self.gateway.close_session(self.session_id)


@contextmanager
def running_server(value: LiveControlHTTPServer) -> Iterator[tuple[str, int]]:
    thread = threading.Thread(target=value.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = value.server_address[:2]
        yield str(host), int(port)
    finally:
        value.shutdown()
        value.server_close()
        thread.join(timeout=5)


def request(
    address: tuple[str, int],
    method: str,
    path: str,
    value: object | None = None,
) -> tuple[int, bytes, str]:
    headers: dict[str, str] = {}
    body: bytes | None = None
    if value is not None:
        body = json.dumps(value).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    connection = HTTPConnection(*address, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read(), response.getheader("Content-Type")
    finally:
        connection.close()


def as_json(payload: bytes) -> dict:
    value = json.loads(payload)
    assert isinstance(value, dict)
    return value


def test_native_map_projection_preserves_real_hex_facts() -> None:
    scenario = load_scenario(SCENARIO_PATH)

    record = native_scenario_map_record(scenario)

    assert record["map_id"] == "source-movement-fixture-map"
    assert record["coordinate_system"] == "odd-r"
    assert record["bounds"] == {
        "min_row": 1,
        "max_row": 1,
        "min_column": 1,
        "max_column": 6,
    }
    cells = record["cells"]
    assert [cell["position_id"] for cell in cells] == [
        "101", "102", "103", "104", "105", "106",
    ]
    assert cells[1]["terrain_semantic"] == "forest"
    assert cells[2]["elevation"] == 2
    assert cells[0]["roads"] == [1, 0, 0, 0, 0, 0]
    assert record["scenario"]["dynamic_units_source"] == "side_observation"


def test_controller_record_is_detached_and_uses_only_bound_side_frame(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    try:
        original = fixture.gateway.view(fixture.session_id, fixture.red_id)
        record = controller_view_record(original)

        assert record["perspective"] == "RED"
        assert record["source_frame"]["extra"]["native"]["perspective"] == "RED"
        assert "green" not in record
        assert "frame_analysis" not in record
        assert record["frame"]["frame_index"] == 0
        assert record["frame"]["viewer_units"]
        record["source_frame"]["operators"].clear()
        refreshed = controller_view_record(
            fixture.gateway.view(fixture.session_id, fixture.red_id)
        )
        assert refreshed["source_frame"]["operators"]
    finally:
        fixture.close()


@pytest.mark.parametrize("perspective", ["red", "blue"])
def test_http_command_uses_current_valid_action_and_advances_once_without_leak(
    tmp_path: Path,
    perspective: str,
) -> None:
    fixture = Fixture(tmp_path, perspective=perspective)
    environment = fixture.environments[0]
    original_step = environment.session.step
    calls = 0

    def counted_step(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_step(*args, **kwargs)

    environment.session.step = counted_step  # type: ignore[method-assign]
    try:
        with running_server(fixture.server) as address:
            status, payload, _ = request(address, "GET", "/api/view")
            initial = as_json(payload)
            assert status == 200
            assert initial["perspective"] == perspective.upper()
            action_id = (
                initial["valid_actions"][0]["action_id"]
                if initial["valid_actions"]
                else None
            )
            status, payload, _ = request(address, "POST", "/api/command", {
                "request_id": "browser-step-0",
                "expected_step": 0,
                "valid_action_id": action_id,
            })
            response = as_json(payload)

            assert status == 200
            assert calls == 1
            assert response["command"] == {
                "accepted": True,
                "advanced": True,
                "request_id": "browser-step-0",
                "step_index": 1,
            }
            assert "view" not in response["command"]
            assert response["current"]["perspective"] == perspective.upper()
            serialized = json.dumps(response, sort_keys=True)
            opposite = "BLUE" if perspective == "red" else "RED"
            assert f'"perspective": "{opposite}"' not in serialized
    finally:
        fixture.close()


def test_http_surface_is_fixed_and_publication_aligns_replay_analysis(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    scenario_before = hashlib.sha256(SCENARIO_PATH.read_bytes()).hexdigest()
    try:
        with running_server(fixture.server) as address:
            for path, content_type in (
                ("/", "text/html"),
                ("/static/live_app.js", "text/javascript"),
                ("/static/map_renderer.js", "text/javascript"),
                ("/static/styles.css", "text/css"),
                ("/static/live_styles.css", "text/css"),
            ):
                status, payload, actual_type = request(address, "GET", path)
                assert status == 200
                assert payload
                assert content_type in actual_type
            for path in ("/api/session", "/api/map", "/api/view"):
                status, payload, _ = request(address, "GET", path)
                assert status == 200
                assert as_json(payload)
            session = as_json(request(address, "GET", "/api/session")[1])
            assert session["perspective"] == "RED"
            assert session["referee_analysis_exposed"] is False
            assert session["opponent_policy"] == "NoOp"

            for method in ("PUT", "PATCH", "DELETE"):
                assert request(address, method, "/api/view")[0] == 405
            assert request(address, "GET", "/etc/passwd")[0] == 404
            assert request(address, "GET", "/static/../live_server.py")[0] == 404

            request(address, "POST", "/api/command", {
                "request_id": "advance",
                "expected_step": 0,
                "valid_action_id": None,
            })
            status, payload, _ = request(address, "POST", "/api/publish", {
                "truncated": True,
            })
            publication = as_json(payload)
            assert status == 200
            assert publication["frame_count"] == 2
            assert publication["step_count"] == 1
            assert publication["truncated"] is True

        archive = SourceReplayArchiveImporter().import_path(
            fixture.output / "replay.zip"
        )
        lines = (fixture.output / "frame_analysis.native.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        assert archive.frame_names == ("0.json", "1.json")
        assert len(lines) == 2
        assert hashlib.sha256(SCENARIO_PATH.read_bytes()).hexdigest() == scenario_before
    finally:
        fixture.close()


def test_stale_command_and_invalid_request_id_fail_before_advance(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    try:
        with running_server(fixture.server) as address:
            status, payload, _ = request(address, "POST", "/api/command", {
                "request_id": 7,
                "expected_step": 0,
                "valid_action_id": None,
            })
            assert status == 400
            assert "request_id" in as_json(payload)["error"]
            assert fixture.manager.descriptor(fixture.session_id).step_index == 0

            request(address, "POST", "/api/command", {
                "request_id": "first",
                "expected_step": 0,
                "valid_action_id": None,
            })
            status, payload, _ = request(address, "POST", "/api/command", {
                "request_id": "stale",
                "expected_step": 0,
                "valid_action_id": None,
            })
            assert status == 409
            assert as_json(payload)["code"] == "stale_expected_step"
            assert fixture.manager.descriptor(fixture.session_id).step_index == 1
    finally:
        fixture.close()


def test_static_client_has_one_step_index_and_no_referee_analysis_endpoint() -> None:
    source = Path(
        "src/wargame_native_engine/viewer/static/live_app.js"
    ).read_text(encoding="utf-8")
    html = Path(
        "src/wargame_native_engine/viewer/static/live_index.html"
    ).read_text(encoding="utf-8")

    assert source.count("let currentStepIndex") == 1
    assert "currentView.step_index" in source
    assert 'requestJson("/api/command"' in source
    assert "/api/referee" not in source
    assert "GREEN / 八因子不暴露给控制端" in html


def test_local_control_cli_defaults_to_loopback_and_requires_output() -> None:
    parser = build_parser()
    args = parser.parse_args(["--output-dir", "/tmp/native-live-control"])

    assert args.host == "127.0.0.1"
    assert args.port == 8001
    assert args.perspective == "red"
