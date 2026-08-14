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
                "/static/map_renderer.js",
                "/static/live_actions.js",
                "/static/styles.css",
                "/static/live_styles.css",
            ):
                status, payload, _ = request(address, "GET", path)
                assert status == 200
                assert payload
                if path.startswith("/red"):
                    page = payload.decode("utf-8")
                    assert "算子命令" not in page
                    assert "本窗口批次" not in page
                    assert "action_id" not in page.lower()
                    assert "decisionwindow" not in page.lower()
                    assert '<details id="advanced-debug"' in page
                    assert (
                        '<details id="advanced-debug" class="advanced-debug" open'
                        not in page
                    )
            assert request(
                address, "GET", "/red?token=blue-token-456"
            )[0] == 403
    finally:
        fixture.close()


def test_map_scene_commands_and_multicell_proposal_are_side_safe(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    try:
        with running_server(fixture.server) as address:
            red = decoded(request(
                address, "GET", "/api/bootstrap?token=red-token-123"
            )[1])
            assert red["scene"]["perspective"] == "RED"
            assert red["scene"]["map"]["map_id"] == fixture.scenario.map.map_id
            assert red["scene"]["frame"]["viewer_units"]
            assert request(
                address,
                "GET",
                "/api/scene?token=red-token-123",
            )[0] == 200

            menu = decoded(request(
                address,
                "GET",
                "/api/commands?token=red-token-123&unit_id=1",
            )[1])
            assert menu["unit_id"] == 1
            assert menu["move_planning_available"] is True
            assert "MOVE" in menu["available"]
            assert request(
                address,
                "GET",
                "/api/commands?token=red-token-123&unit_id=2",
            )[0] == 403

            plan_body = {
                "token": "red-token-123",
                "request_id": "plan-route",
                "expected_window": 0,
                "unit_id": 1,
                "destination_hex": 106,
                "movement_mode": "NORMAL",
            }
            status, payload, _ = request(
                address, "POST", "/api/plan-move", plan_body
            )
            assert status == 200
            proposal = decoded(payload)
            assert proposal["reachable"] is True
            assert len(proposal["ordered_path"]) >= 5
            assert proposal["action_id"]
            fetched = decoded(request(
                address,
                "GET",
                "/api/proposal?token=red-token-123&proposal_id="
                + proposal["proposal_id"],
            )[1])
            assert fetched == proposal
            assert request(
                address,
                "GET",
                "/api/proposal?token=blue-token-456&proposal_id="
                + proposal["proposal_id"],
            )[0] == 403
    finally:
        fixture.close()


def test_server_staging_replaces_actor_and_submits_authorized_route_once(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    try:
        with running_server(fixture.server) as address:
            def plan(destination: int, suffix: str):
                return decoded(request(address, "POST", "/api/plan-move", {
                    "token": "red-token-123",
                    "request_id": f"plan-{suffix}",
                    "expected_window": 0,
                    "unit_id": 1,
                    "destination_hex": destination,
                    "movement_mode": "NORMAL",
                })[1])

            first = plan(104, "first")
            second = plan(106, "second")
            stage_first = {
                "token": "red-token-123",
                "request_id": "stage-first",
                "expected_window": 0,
                "action_id": first["action_id"],
            }
            assert request(
                address, "POST", "/api/batch/stage", stage_first
            )[0] == 200
            stage_second = {
                **stage_first,
                "request_id": "stage-second",
                "action_id": second["action_id"],
            }
            staged = decoded(request(
                address, "POST", "/api/batch/stage", stage_second
            )[1])["staged_actions"]
            assert len(staged) == 1
            assert staged[0]["action_id"] == second["action_id"]
            repeated = decoded(request(
                address, "POST", "/api/batch/stage", stage_second
            )[1])["staged_actions"]
            assert repeated == staged
            conflict = {**stage_second, "action_id": first["action_id"]}
            assert request(
                address, "POST", "/api/batch/stage", conflict
            )[0] == 409

            red = decoded(request(address, "POST", "/api/batch/submit", {
                "token": "red-token-123",
                "request_id": "submit-staged",
                "expected_window": 0,
            })[1])
            assert red["result"]["advanced"] is False
            blue = decoded(request(address, "POST", "/api/batch", {
                "token": "blue-token-456",
                "request_id": "blue-empty",
                "expected_window": 0,
                "ordered_action_ids": [],
            })[1])
            assert blue["result"]["advanced"] is True
            assert fixture.manager.descriptor(fixture.session_id).frame_index == 1
            assert fixture.environments[0].last_step_record[
                "ordered_actions"
            ][0]["parameters"]["path"] == second["ordered_path"]
    finally:
        fixture.close()


def test_plan_endpoint_rejects_client_path_and_stale_window_without_advancing(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    try:
        with running_server(fixture.server) as address:
            raw = {
                "token": "red-token-123",
                "request_id": "client-path",
                "expected_window": 0,
                "unit_id": 1,
                "destination_hex": 105,
                "movement_mode": "NORMAL",
                "path": [999],
            }
            assert request(address, "POST", "/api/plan-move", raw)[0] == 400
            stale = dict(raw)
            stale.pop("path")
            stale["expected_window"] = 1
            assert request(address, "POST", "/api/plan-move", stale)[0] == 409
            assert fixture.manager.descriptor(fixture.session_id).frame_index == 0
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


def test_product_http_contract_hides_control_internals_and_executes_once(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    try:
        with running_server(fixture.server) as address:
            status, payload, _ = request(
                address,
                "GET",
                "/api/product/bootstrap?token=red-token-123",
            )
            assert status == 200
            bootstrap = decoded(payload)
            assert set(bootstrap) == {
                "session_id", "side", "referee_data_exposed", "scene",
                "product_status",
            }
            assert bootstrap["side"] == "red"
            assert bootstrap["referee_data_exposed"] is False
            assert bootstrap["scene"]["perspective"] == "RED"
            assert "window" not in bootstrap
            assert "view" not in bootstrap
            assert "valid_actions" not in json.dumps(bootstrap).lower()
            assert set(bootstrap["scene"]["frame"]) == {
                "frame_index", "time", "cities", "viewer_units",
            }
            assert "frame_analysis" not in json.dumps(bootstrap).lower()

            capabilities = decoded(request(
                address,
                "POST",
                "/api/product/capabilities",
                {"token": "red-token-123", "actor_ids": [1]},
            )[1])
            assert capabilities["selected_count"] == 1
            assert capabilities["operations"]["MOVE"]["applicable_count"] == 1
            assert "action_id" not in json.dumps(capabilities).lower()

            preview_body = {
                "token": "red-token-123",
                "request_id": "product-preview-move",
                "actor_ids": [1],
                "operation_type": "MOVE",
                "destination_hex": 105,
            }
            status, payload, _ = request(
                address, "POST", "/api/product/preview", preview_body
            )
            assert status == 200
            preview = decoded(payload)
            assert preview["executable_count"] == 1
            assert preview["per_unit_results"][0]["path"] == [102, 103, 104, 105]
            assert "action_id" not in json.dumps(preview).lower()
            assert "proposal_id" not in json.dumps(preview).lower()
            token = preview["preview_token"]

            execute_body = {
                "token": "red-token-123",
                "preview_token": token,
                "request_id": "product-execute-move",
            }
            first = decoded(request(
                address, "POST", "/api/product/execute", execute_body
            )[1])
            repeated = decoded(request(
                address, "POST", "/api/product/execute", execute_body
            )[1])
            assert repeated == first
            assert first["product_state"] == "WAITING_OPPONENT"
            assert fixture.manager.descriptor(fixture.session_id).frame_index == 0

            blue_preview = decoded(request(
                address,
                "POST",
                "/api/product/preview",
                {
                    "token": "blue-token-456",
                    "request_id": "product-blue-wait-preview",
                    "actor_ids": [],
                    "operation_type": "WAIT",
                    "whole_side_wait": True,
                },
            )[1])
            advanced = decoded(request(
                address,
                "POST",
                "/api/product/execute",
                {
                    "token": "blue-token-456",
                    "preview_token": blue_preview["preview_token"],
                    "request_id": "product-blue-wait-execute",
                },
            )[1])
            assert advanced["advanced"] is True
            assert fixture.manager.descriptor(fixture.session_id).frame_index == 1
    finally:
        fixture.close()


def test_product_http_partial_confirmation_and_input_boundary(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    try:
        with running_server(fixture.server) as address:
            body = {
                "token": "red-token-123",
                "request_id": "partial-http-preview",
                "actor_ids": [1, 2],
                "operation_type": "MOVE",
                "destination_hex": 105,
            }
            preview = decoded(request(
                address, "POST", "/api/product/preview", body
            )[1])
            assert preview["executable_count"] == 1
            assert preview["rejected_count"] == 1
            assert preview["partial_confirmation_required"] is True
            execute = {
                "token": "red-token-123",
                "preview_token": preview["preview_token"],
                "request_id": "partial-http-execute",
            }
            assert request(
                address, "POST", "/api/product/execute", execute
            )[0] == 409
            execute["confirm_partial"] = True
            assert request(
                address, "POST", "/api/product/execute", execute
            )[0] == 200
            assert fixture.manager.descriptor(fixture.session_id).frame_index == 0

            injected = dict(body)
            injected["request_id"] = "injected-client-path"
            injected["path"] = [999]
            assert request(
                address, "POST", "/api/product/preview", injected
            )[0] == 400
            unavailable = decoded(request(
                address,
                "POST",
                "/api/product/capabilities",
                {"token": "red-token-123", "actor_ids": [2]},
            )[1])
            assert unavailable["operations"]["MOVE"]["applicable_count"] == 0
            assert unavailable["operations"]["MOVE"]["unavailable"][0][
                "code"
            ] == "unit_not_controllable"
    finally:
        fixture.close()
