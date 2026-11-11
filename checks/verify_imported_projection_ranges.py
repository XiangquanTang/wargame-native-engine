"""Import the real external Source inputs, then verify tools and live HTTP.

Usage: python checks/verify_imported_projection_ranges.py SOURCE_ROOT INPUTS OUTPUT
INPUTS contains map_221.zip, 2201010101.json, map_9601.zip, 201033029601.json.
Original map/scenario bytes are external inputs and are never modified.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread
from time import perf_counter
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def main():
    root, inputs, output = (Path(value).resolve() for value in sys.argv[1:4])
    output.mkdir(parents=True, exist_ok=True)
    sys.path[:0] = [str(root / "src"), str(root)]
    from wargame_native_engine.agents.realtime import ManualRealtimeClock, RealtimeAgentSession
    from wargame_native_engine.agents.tool_calling import GameToolAdapter
    from wargame_native_engine.agents.tool_transport import GameToolTransport
    from wargame_native_engine.data import ScenarioBundle
    from wargame_native_engine.operations import ScenarioMatchFactory
    from wargame_native_engine.viewer.human_capabilities import source_weapon_profiles
    from wargame_native_engine.viewer.realtime_control_server import create_realtime_dual_control_server

    report = {"source_root": str(root), "maps": [], "input_sha256": {}}
    for map_id, scenario_name in (("221", "2201010101.json"), ("9601", "201033029601.json")):
        started = perf_counter()
        map_input, scenario_input = inputs / f"map_{map_id}.zip", inputs / scenario_name
        for path in (map_input, scenario_input):
            report["input_sha256"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        workspace = output / map_id
        env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root)))}
        result = subprocess.run([sys.executable, "-m", "scripts.run_maps", "start",
            "--input", str(map_input), "--scenario-input", str(scenario_input),
            "--workspace", str(workspace), "--prepare-only", "--json"],
            cwd=root, env=env, capture_output=True, text=True, check=True)
        prepared = json.loads(result.stdout)
        (output / f"prepare-{map_id}.json").write_text(result.stdout)
        bundle = ScenarioBundle.from_path(prepared["scenario_path"]).with_map_runtime_data(prepared["runtime"]["path"])
        session = RealtimeAgentSession(ScenarioMatchFactory(bundle=bundle,
            storage_root=workspace / "session", red_controller_id="real-red", blue_controller_id="real-blue",
            decision_interval_ms=1000, frame_interval_ms=1000),
            run_namespace=f"real-range-{map_id}", tick_interval_ms=1000, clock=ManualRealtimeClock())
        server = None
        try:
            reset = session.reset(seed=42)
            server = create_realtime_dual_control_server(session,
                red_binding=reset.red_binding, blue_binding=reset.blue_binding,
                red_token="red-token", blue_token="blue-token", map_data=bundle.render_map_record(),
                weapon_profiles=source_weapon_profiles(bundle.scenario.metadata), port=0)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"

            def request(path, body=None):
                payload = None if body is None else json.dumps(body).encode()
                req = Request(base + path, data=payload, headers={"Content-Type": "application/json"})
                try:
                    with urlopen(req, timeout=30) as response:
                        return response.status, json.load(response)
                except HTTPError as error:
                    return error.code, json.load(error)

            match = session._require_match()
            engine = match.gateway.manager._managed(match.session_id).environment.session.backend.engine
            map_result = {"map_id": map_id, "queries": [], "frame_85": None}
            selected_units = (("red", reset.red_binding, 11), ("blue", reset.blue_binding, 2)) if map_id == "221" else (
                ("red", reset.red_binding, 0), ("blue", reset.blue_binding, 1),
                ("red", reset.red_binding, 100), ("blue", reset.blue_binding, 101))
            for side, binding, actor in selected_units:
                seat, controller = session.controller_snapshot(binding)
                source = next(unit for unit in controller.source_frame["operators"] if unit["obj_id"] == actor)
                assert "owner" not in source or source["owner"] is None, source
                assert type(source["color"]) is int and source["color"] == (0 if side == "red" else 1)
                version = seat.observation_version
                adapter = GameToolAdapter(session, binding)
                transport = GameToolTransport(adapter)
                args = {"observation_version": version, "actor_id": actor}
                authoritative = engine.query_unit_tactical_overlay(side, actor).to_record()
                before = deepcopy(engine.snapshot())
                commands_before = session.command_events
                status_before = session.status_snapshot(binding)
                # A successful read must not recover state from live WorldState.
                with patch.object(engine, "_require_world", side_effect=AssertionError("live world read")):
                    overlay = session.tactical_overlay(binding, observation_version=version, unit_id=actor).to_record()
                    visibility = adapter.dispatch("game_visibility_range", args)
                    attack = adapter.dispatch("game_attack_range", {**args, "weapon_id": source["carry_weapon_ids"][0]})
                    region = adapter.dispatch("game_region_visibility", {**args,
                        "region": {"x_min": source["cur_hex"] % 100, "y_min": source["cur_hex"] // 100,
                                   "width": 1, "height": 1},
                        "target_profile": {"target_class": "VEHICLE", "concealed": False, "height_m": 0}})
                    overlay_status, http = request("/api/product/tactical-overlay", {
                        "token": side + "-token", "observation_version": version, "unit_id": actor})
                    bootstrap_status, bootstrap = request(
                        f"/api/product/bootstrap?token={side}-token&overlay_unit_id={actor}&include_map=0")
                assert overlay_status == 200 and bootstrap_status == 200, (overlay_status, http, bootstrap_status, bootstrap)
                assert visibility["visibility_field"] == overlay["visibility_field"] == http["visibility_field"]
                assert authoritative["visibility_field"] == overlay["visibility_field"]
                assert authoritative["weapon_envelope"] == overlay["weapon_envelope"]
                assert bootstrap["tactical_overlay"]["visibility_field"] == overlay["visibility_field"]
                expected_weapon = next(w for w in overlay["weapon_envelope"]["weapons"] if w["weapon_id"] == source["carry_weapon_ids"][0])
                assert attack["weapon_envelope"]["hex_ids"] == expected_weapon["hex_ids"]
                assert visibility["actor"]["current_hex"] == attack["actor"]["current_hex"] == source["cur_hex"]
                assert before == engine.snapshot() and commands_before == session.command_events
                assert status_before == session.status_snapshot(binding)
                visible_ids = {unit["operator_id"] for unit in controller.observation["visible_enemies"]}
                projected_enemy_ids = {unit["obj_id"] for unit in controller.source_frame["operators"] if unit["color"] != source["color"]}
                assert projected_enemy_ids == visible_ids
                assert all(target["target_id"] in visible_ids for target in http["fire_targets"]["targets"])
                assert "target_id" not in json.dumps(visibility) and "target_id" not in json.dumps(attack)
                # Unknown IDs and foreign IDs cannot be distinguished through range authorization.
                foreign = 1 if side == "red" else (11 if map_id == "221" else 100)
                rejected = []
                for selected in (foreign, 999999):
                    reply = transport.invoke("game_visibility_range", json.dumps({**args, "actor_id": selected}))
                    assert not reply["ok"] and reply["error"]["code"] == "actor_not_owned", reply
                    rejected.append(reply["error"])
                assert rejected[0] == rejected[1]
                status, denied = request("/api/product/tactical-overlay", {
                    "token": side + "-token", "observation_version": version, "unit_id": foreign})
                assert status == 409 and denied["code"] == "actor_not_owned", denied
                map_result["queries"].append({"side": side, "actor_id": actor,
                    "color": source["color"], "origin": source["cur_hex"], "owner_present": "owner" in source,
                    "visibility_cells": len(visibility["visibility_field"]["hex_ids"]),
                    "attack_cells": len(attack["weapon_envelope"]["hex_ids"]),
                    "http_status": overlay_status, "bootstrap_status": bootstrap_status,
                    "hidden_enemy_ids_absent": True, "foreign_unknown_rejected": True,
                    "read_only_no_live_world": True, "region_visibility": region["visibility"]["cell_count"]})

            if map_id == "221":
                adapter = GameToolAdapter(session, reset.red_binding)
                before_version = session.latest_view(reset.red_binding).observation_version
                rejected = GameToolTransport(adapter).invoke("game_visibility_range", json.dumps({
                    "observation_version": before_version, "actor_id": 13}))
                assert not rejected["ok"], rejected  # Mounted infantry is not a deployed observer.
                for _ in range(85):
                    session.advance_tick()
                current = session.latest_view(reset.red_binding)
                visibility = adapter.dispatch("game_visibility_range", {
                    "observation_version": current.observation_version, "actor_id": 11})
                attack = adapter.dispatch("game_attack_range", {
                    "observation_version": current.observation_version, "actor_id": 11, "weapon_id": 43})
                status, bootstrap = request("/api/product/bootstrap?token=red-token&overlay_unit_id=11&include_map=0")
                assert status == 200 and current.frame_index == 85, (status, current.frame_index)
                assert bootstrap["tactical_overlay"]["visibility_field"] == visibility["visibility_field"]
                stale = GameToolTransport(adapter).invoke("game_visibility_range", json.dumps({
                    "observation_version": before_version, "actor_id": 11}))
                assert not stale["ok"] and stale["error"]["code"] == "observation_version_stale", stale
                map_result["frame_85"] = {"actor_id": 11, "frame_index": current.frame_index,
                    "visibility_cells": len(visibility["visibility_field"]["hex_ids"]),
                    "attack_cells": len(attack["weapon_envelope"]["hex_ids"]),
                    "bootstrap_status": status, "mounted_infantry_13_rejected": rejected["error"]["code"],
                    "stale_version_rejected": True}
            map_result["duration_seconds"] = round(perf_counter() - started, 3)
            report["maps"].append(map_result)
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
                thread.join(timeout=30)
            session.close()
            bundle.map_runtime_data.visibility.close()
    for name, expected in report["input_sha256"].items():
        assert hashlib.sha256((inputs / name).read_bytes()).hexdigest() == expected
    report["passed"] = True
    (output / "validation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
