from __future__ import annotations

from contextlib import contextmanager
from http.client import HTTPConnection
import json
from pathlib import Path
import threading
from typing import Iterator

from scripts.run_dual_side_control import build_parser
from scripts.run_minimal_source_replay import _template
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.gateway import CommandGateway, SessionManager
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment
from wargame_native_engine.viewer.dual_control_server import (
    DualSideControlHTTPServer,
    create_dual_side_control_server,
)
from wargame_native_engine.viewer.live_control import native_scenario_map_record


SCENARIO_PATH = Path("examples/source_movement_scenario.json")


class Fixture:
    def __init__(self, root: Path) -> None:
        self.scenario = load_scenario(SCENARIO_PATH)
        self.environments: list[SourceCompatibleEnvironment] = []

        def factory() -> SourceCompatibleEnvironment:
            environment = SourceCompatibleEnvironment(
                GameSession(NativeEngineBackend(self.scenario)),
                template=_template(self.scenario),
            )
            self.environments.append(environment)
            return environment

        self.manager = SessionManager(factory, storage_root=root / "facts")
        self.gateway = CommandGateway(self.manager, delta_time_seconds=20)
        self.red_id, self.blue_id = "dual-red", "dual-blue"
        descriptor = self.gateway.create_session(
            red_controller_id=self.red_id,
            blue_controller_id=self.blue_id,
            seed=42,
        )
        self.session_id = descriptor.session_id
        self.server = create_dual_side_control_server(
            self.gateway,
            session_id=self.session_id,
            red_controller_id=self.red_id,
            blue_controller_id=self.blue_id,
            red_token="red-token-123",
            blue_token="blue-token-456",
            map_data=native_scenario_map_record(self.scenario),
            output_dir=root / "published",
            port=0,
        )

    def close(self) -> None:
        self.gateway.close_session(self.session_id)


@contextmanager
def running_server(value: DualSideControlHTTPServer) -> Iterator[tuple[str, int]]:
    thread = threading.Thread(target=value.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = value.server_address[:2]
        yield str(host), int(port)
    finally:
        value.shutdown()
        value.server_close()
        thread.join(timeout=5)


def request(address, method: str, path: str, value=None):
    body = None
    headers = {}
    if value is not None:
        body = json.dumps(value).encode("utf-8")
        headers["Content-Type"] = "application/json"
    connection = HTTPConnection(*address, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read(), response.getheader("Content-Type")
    finally:
        connection.close()


def decoded(payload: bytes):
    return json.loads(payload.decode("utf-8"))


def test_dual_pages_static_assets_and_default_cli_are_loopback_only(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    try:
        with running_server(fixture.server) as address:
            for path in (
                "/red?token=red-token-123",
                "/blue?token=blue-token-456",
                "/static/dual_control.js",
                "/static/styles.css",
                "/static/live_styles.css",
            ):
                status, payload, _ = request(address, "GET", path)
                assert status == 200
                assert payload
            assert request(
                address, "GET", "/red?token=blue-token-456"
            )[0] == 403
    finally:
        fixture.close()
    args = build_parser().parse_args(["--output-dir", "/tmp/dual"])
    assert args.host == "127.0.0.1"
    assert args.decision_interval_ms == 10_000
    assert args.frame_interval_ms == 1_000


def test_each_token_receives_only_its_bound_side_observation(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    try:
        with running_server(fixture.server) as address:
            red = decoded(request(
                address, "GET", "/api/bootstrap?token=red-token-123"
            )[1])
            blue = decoded(request(
                address, "GET", "/api/bootstrap?token=blue-token-456"
            )[1])

            assert red["side"] == "red"
            assert blue["side"] == "blue"
            assert red["view"]["observation"]["side"] == "red"
            assert blue["view"]["observation"]["side"] == "blue"
            assert red["view"]["source_frame"]["extra"]["native"]["perspective"] == "RED"
            assert blue["view"]["source_frame"]["extra"]["native"]["perspective"] == "BLUE"
            assert red["referee_data_exposed"] is False
            assert "frame_analysis" not in json.dumps(red).lower()
            red_ids = {item["action_id"] for item in red["view"]["valid_actions"]}
            blue_ids = {item["action_id"] for item in blue["view"]["valid_actions"]}
            assert red_ids.isdisjoint(blue_ids)
            assert request(
                address, "GET", "/api/bootstrap?token=invalid-token"
            )[0] == 403
    finally:
        fixture.close()


def test_two_side_batches_advance_once_and_retry_cannot_double_advance(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    try:
        with running_server(fixture.server) as address:
            red = decoded(request(
                address, "GET", "/api/bootstrap?token=red-token-123"
            )[1])
            red_action = red["view"]["valid_actions"][0]["action_id"]
            first_body = {
                "token": "red-token-123",
                "request_id": "red-0",
                "expected_window": 0,
                "ordered_action_ids": [red_action],
            }
            first = decoded(request(address, "POST", "/api/batch", first_body)[1])
            assert first["result"]["advanced"] is False
            assert first["result"]["pending_perspectives"] == ["BLUE"]
            assert fixture.manager.descriptor(fixture.session_id).frame_index == 0

            blue_body = {
                "token": "blue-token-456",
                "request_id": "blue-0",
                "expected_window": 0,
                "ordered_action_ids": [],
            }
            blue = decoded(request(address, "POST", "/api/batch", blue_body)[1])
            assert blue["result"]["advanced"] is True
            assert blue["result"]["view"]["step_index"] == 1
            assert fixture.manager.descriptor(fixture.session_id).frame_index == 1

            repeated = decoded(request(
                address, "POST", "/api/batch", first_body,
            )[1])
            assert repeated == first
            assert fixture.manager.descriptor(fixture.session_id).frame_index == 1
            owned = decoded(request(
                address,
                "GET",
                "/api/submission?token=red-token-123&request_id=red-0",
            )[1])
            assert owned["found"] is True
            assert request(
                address,
                "GET",
                "/api/submission?token=blue-token-456&request_id=red-0",
            )[0] == 403
    finally:
        fixture.close()


def test_http_rejects_arbitrary_action_payload_cross_side_id_and_mutation_methods(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    try:
        with running_server(fixture.server) as address:
            blue = decoded(request(
                address, "GET", "/api/bootstrap?token=blue-token-456"
            )[1])
            blue_action = blue["view"]["valid_actions"][0]["action_id"]
            arbitrary = {
                "token": "red-token-123",
                "request_id": "raw-action",
                "expected_window": 0,
                "ordered_action_ids": [],
                "engine_action": {"side": "red", "action_type": "MOVE"},
            }
            assert request(address, "POST", "/api/batch", arbitrary)[0] == 400
            cross_side = {
                "token": "red-token-123",
                "request_id": "cross-side",
                "expected_window": 0,
                "ordered_action_ids": [blue_action],
            }
            assert request(address, "POST", "/api/batch", cross_side)[0] == 403
            assert fixture.manager.descriptor(fixture.session_id).frame_index == 0
            for method in ("PUT", "PATCH", "DELETE"):
                assert request(address, method, "/api/batch", {})[0] == 405
    finally:
        fixture.close()
