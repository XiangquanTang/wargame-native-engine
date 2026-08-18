from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from http import HTTPStatus
from http.client import HTTPConnection
import json
from pathlib import Path
import threading
from typing import Iterator

from scripts.run_realtime_dual_control import build_parser
from wargame_native_engine.agents import (
    CommandStatus,
    ManualRealtimeClock,
    RealtimeAgentSession,
)
from wargame_native_engine.data import (
    DataProvenance,
    ScenarioBundle,
    ScenarioCatalog,
)
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.operations import WargameFacade
from wargame_native_engine.viewer.realtime_control_server import (
    RealtimeDualControlRequestHandler,
    RealtimeDualControlHTTPServer,
    create_realtime_dual_control_server,
)


SCENARIO = Path("examples/source_movement_scenario.json")


class MatchFactory:
    def __init__(
        self,
        root: Path,
        *,
        tick_interval_ms: int = 1_000,
        bundle: ScenarioBundle | None = None,
    ) -> None:
        self.root = root
        self.tick_interval_ms = tick_interval_ms
        self.bundle = bundle or ScenarioBundle.from_path(SCENARIO)

    def __call__(self, seed: int, episode_sequence: int):
        app = WargameFacade(
            ScenarioCatalog.from_bundles({
                self.bundle.scenario_identity.scenario_id: self.bundle,
            }),
            storage_root=self.root / f"managed-{episode_sequence}",
        )
        return app.create_match(
            scenario_id=self.bundle.scenario_identity.scenario_id,
            red_controller_id="realtime-web-red",
            blue_controller_id="realtime-web-blue",
            seed=seed,
            decision_interval_ms=self.tick_interval_ms,
            frame_interval_ms=self.tick_interval_ms,
        )


class Fixture:
    def __init__(
        self, root: Path, *, bundle: ScenarioBundle | None = None,
    ) -> None:
        self.factory = MatchFactory(root, bundle=bundle)
        self.session = RealtimeAgentSession(
            self.factory,
            run_namespace="realtime-web-test",
            tick_interval_ms=1_000,
            clock=ManualRealtimeClock(),
        )
        self.reset = self.session.reset(seed=42)
        self.server = create_realtime_dual_control_server(
            self.session,
            red_binding=self.reset.red_binding,
            blue_binding=self.reset.blue_binding,
            red_token="red-token-123",
            blue_token="blue-token-456",
            map_data=self.factory.bundle.render_map_record(),
            port=0,
        )

    def close(self) -> None:
        self.session.close()


@contextmanager
def running_server(
    value: RealtimeDualControlHTTPServer,
) -> Iterator[tuple[str, int]]:
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
    connection = HTTPConnection(*address, timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read(), response.getheader("Content-Type")
    finally:
        connection.close()


def decoded(payload: bytes):
    return json.loads(payload.decode("utf-8"))


class _DisconnectedWriter:
    def write(self, _payload: bytes) -> None:
        raise BrokenPipeError(32, "client disconnected")


def preview_move(address, bootstrap, *, request_id: str = "preview-red-move"):
    status, payload, _ = request(address, "POST", "/api/product/preview", {
        "token": "red-token-123",
        "request_id": request_id,
        "observation_version": bootstrap["observation_version"],
        "actor_ids": [1],
        "operation_type": "MOVE",
        "destination_hex": 106,
        "movement_mode": "NORMAL",
    })
    assert status == 200, payload
    return decoded(payload)


def _multi_actor_bundle() -> ScenarioBundle:
    record = json.loads(SCENARIO.read_text(encoding="utf-8"))
    second = deepcopy(record["operators"][0])
    second.update({"operator_id": 3, "current_hex": 102, "x": 2})
    record["operators"].append(second)
    record["deployment"]["positions"]["red"]["3"] = [2, 1]
    record["scenario_id"] = "realtime-web-multi-actor"
    return ScenarioBundle.from_record(
        record,
        provenance=DataProvenance(
            source_sha256=canonical_hash(record),
            source_kind="deterministic_test_derivative",
        ),
    )


def test_realtime_web_execute_queues_without_waiting_for_blue_and_remains_operable(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    try:
        with running_server(fixture.server) as address:
            bootstrap = decoded(request(
                address,
                "GET",
                "/api/product/bootstrap?token=red-token-123",
            )[1])
            assert bootstrap["control_mode"] == "realtime"
            assert bootstrap["product_status"]["can_operate"] is True
            preview = preview_move(address, bootstrap)
            assert preview["can_execute"] is True
            status, payload, _ = request(
                address,
                "POST",
                "/api/product/execute",
                {
                    "token": "red-token-123",
                    "preview_token": preview["preview_token"],
                    "request_id": "execute-red-move",
                    "confirm_partial": False,
                },
            )
            assert status == 202
            receipt = decoded(payload)
            assert receipt == {
                "accepted": True,
                "can_operate": True,
                "command_sequence": 0,
                "control_mode": "realtime",
                "error_code": None,
                "product_state": "COMMAND_PENDING",
                "side": "red",
                "status": "PENDING",
                "target_tick": 0,
            }

            pending = decoded(request(
                address,
                "GET",
                "/api/product/status?token=red-token-123",
            )[1])
            assert pending["product_state"] == "COMMAND_PENDING"
            assert pending["can_operate"] is True
            assert pending["pending_command_count"] == 1

            tick = fixture.session.advance_tick()
            assert tick.red_command_sequences == (0,)
            assert tick.blue_command_sequences == ()
            assert fixture.session.current_tick == 1
            assert fixture.session.command_events[0].status is CommandStatus.APPLIED

            ready = decoded(request(
                address,
                "GET",
                "/api/product/status?token=red-token-123",
            )[1])
            assert ready["product_state"] == "WAITING_SELECTION"
            assert ready["can_operate"] is True
            assert ready["frame_index"] == 1
    finally:
        fixture.close()


def test_realtime_web_reads_the_controller_view_cached_with_observation(
    tmp_path: Path, monkeypatch,
) -> None:
    fixture = Fixture(tmp_path)
    try:
        match = fixture.session._match
        assert match is not None

        def unexpected_projection(*_args, **_kwargs):
            raise AssertionError("HTTP reads must not rebuild a side projection")

        monkeypatch.setattr(match.gateway, "controller_state", unexpected_projection)
        with running_server(fixture.server) as address:
            status, payload, _ = request(
                address,
                "GET",
                "/api/product/bootstrap?token=red-token-123",
            )
            assert status == 200
            value = decoded(payload)
            assert value["observation_version"] == fixture.reset.red.observation_version
            assert value["product_status"]["frame_index"] == fixture.reset.red.frame_index
    finally:
        fixture.close()


def test_realtime_web_ignores_a_client_disconnect_while_writing_response() -> None:
    handler = object.__new__(RealtimeDualControlRequestHandler)
    handler.wfile = _DisconnectedWriter()
    handler.send_response = lambda _status: None
    handler.send_header = lambda _name, _value: None
    handler.end_headers = lambda: None

    handler._send_bytes(HTTPStatus.OK, b"payload", "application/json")


def test_realtime_web_receipt_is_idempotent_and_events_are_seat_isolated(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    try:
        with running_server(fixture.server) as address:
            bootstrap = decoded(request(
                address,
                "GET",
                "/api/product/bootstrap?token=red-token-123",
            )[1])
            preview = preview_move(address, bootstrap)
            body = {
                "token": "red-token-123",
                "preview_token": preview["preview_token"],
                "request_id": "idempotent-execute",
                "confirm_partial": False,
            }
            first = request(address, "POST", "/api/product/execute", body)
            second = request(address, "POST", "/api/product/execute", body)
            assert first[0] == second[0] == 202
            assert first[1] == second[1]
            assert len(fixture.session.command_events) == 1
            different_request = request(address, "POST", "/api/product/execute", {
                **body,
                "request_id": "different-request-same-preview",
            })
            assert different_request[0] == 409
            assert len(fixture.session.command_events) == 1

            red_events = decoded(request(
                address,
                "GET",
                "/api/realtime/events?token=red-token-123",
            )[1])
            blue_events = decoded(request(
                address,
                "GET",
                "/api/realtime/events?token=blue-token-456",
            )[1])
            assert [value["seat"] for value in red_events["events"]] == ["red"]
            assert blue_events == {"events": [], "side": "blue"}
            assert request(
                address,
                "GET",
                "/red?token=blue-token-456",
            )[0] == 403
    finally:
        fixture.close()


def test_realtime_web_accumulates_disjoint_actors_in_one_target_tick(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path, bundle=_multi_actor_bundle())
    try:
        with running_server(fixture.server) as address:
            bootstrap = decoded(request(
                address,
                "GET",
                "/api/product/bootstrap?token=red-token-123",
            )[1])
            move = preview_move(address, bootstrap, request_id="multi-move")
            assert request(address, "POST", "/api/product/execute", {
                "token": "red-token-123",
                "preview_token": move["preview_token"],
                "request_id": "multi-move-execute",
            })[0] == 202
            status, payload, _ = request(address, "POST", "/api/product/preview", {
                "token": "red-token-123",
                "request_id": "multi-wait",
                "observation_version": bootstrap["observation_version"],
                "actor_ids": [3],
                "operation_type": "WAIT",
                "movement_mode": "NORMAL",
            })
            assert status == 200
            wait = decoded(payload)
            assert request(address, "POST", "/api/product/execute", {
                "token": "red-token-123",
                "preview_token": wait["preview_token"],
                "request_id": "multi-wait-execute",
            })[0] == 202

            events = fixture.session.command_events
            assert [value.status for value in events] == [
                CommandStatus.SUPERSEDED,
                CommandStatus.PENDING,
            ]
            assert events[0].target_tick == events[1].target_tick == 0
            assert [
                (value.operation_type, value.actor_ids)
                for value in events[1].decision.operations
            ] == [("MOVE", (1,)), ("WAIT", (3,))]

            tick = fixture.session.advance_tick()
            assert tick.red_command_sequences == (1,)
            assert tick.blue_command_sequences == ()
            assert fixture.session.command_events[1].status is CommandStatus.APPLIED
            match = fixture.session._require_match()  # noqa: SLF001
            step = match.gateway.manager._managed(  # noqa: SLF001
                match.session_id
            ).environment.last_step_record
            assert [value["actor_id"] for value in step["ordered_actions"]] == [1, 3]
    finally:
        fixture.close()


def test_realtime_web_rejects_client_paths_and_cross_seat_preview_use(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    try:
        with running_server(fixture.server) as address:
            bootstrap = decoded(request(
                address,
                "GET",
                "/api/product/bootstrap?token=red-token-123",
            )[1])
            invalid = {
                "token": "red-token-123",
                "request_id": "client-path",
                "observation_version": bootstrap["observation_version"],
                "actor_ids": [1],
                "operation_type": "MOVE",
                "destination_hex": 106,
                "movement_mode": "NORMAL",
                "path": [101, 102, 106],
            }
            assert request(
                address, "POST", "/api/product/preview", invalid
            )[0] == 400
            preview = preview_move(address, bootstrap, request_id="owned-preview")
            cross = request(address, "POST", "/api/product/execute", {
                "token": "blue-token-456",
                "preview_token": preview["preview_token"],
                "request_id": "cross-seat-execute",
            })
            assert cross[0] == 403
            assert fixture.session.command_events == ()
    finally:
        fixture.close()


def test_realtime_web_assets_express_continuous_command_contract() -> None:
    script = Path(
        "src/wargame_native_engine/viewer/static/dual_control.js"
    ).read_text(encoding="utf-8")
    assert "COMMAND_PENDING" in script
    assert "observation_version: current.observation_version" in script
    assert 'current.control_mode === "realtime"' in script
    assert "无需等待对方" in script
    args = build_parser().parse_args([
        "--output-dir", "/tmp/realtime-web",
        "--run-namespace", "realtime-web-worker-000",
    ])
    assert args.host == "127.0.0.1"
    assert args.port == 8003
    assert args.tick_interval_ms == 1_000


def test_realtime_web_command_publishes_with_replay_analysis_and_tick_evidence(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path / "session")
    try:
        with running_server(fixture.server) as address:
            bootstrap = decoded(request(
                address,
                "GET",
                "/api/product/bootstrap?token=red-token-123",
            )[1])
            preview = preview_move(address, bootstrap, request_id="publish-move")
            assert request(address, "POST", "/api/product/execute", {
                "token": "red-token-123",
                "preview_token": preview["preview_token"],
                "request_id": "publish-move-execute",
            })[0] == 202
            fixture.session.advance_tick()

        publication = fixture.session.publish(
            tmp_path / "published", truncated=True,
        )
        assert publication.tick_count == 1
        assert publication.accepted_command_count == 1
        assert publication.applied_command_count == 1
        assert publication.truncated is True
        for path in (
            publication.replay_path,
            publication.analysis_path,
            publication.command_path,
            publication.tick_path,
            publication.summary_path,
        ):
            assert path.is_file() and path.stat().st_size > 0
        command = json.loads(
            publication.command_path.read_text(encoding="utf-8").splitlines()[0]
        )
        assert command["seat"] == "red"
        assert command["status"] == "APPLIED"
        assert "green" not in json.dumps(command).lower()
    finally:
        fixture.close()
