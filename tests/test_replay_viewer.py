from __future__ import annotations

from contextlib import contextmanager
import hashlib
from http.client import HTTPConnection
import json
from pathlib import Path
import threading
from typing import Iterator
from urllib.error import HTTPError
from urllib.request import urlopen
from zipfile import ZipFile

import pytest

from scripts.run_replay_viewer import build_parser as viewer_argument_parser
from wargame_native_engine.analysis import (
    EightFactorAnalyzer,
    GreenFrameAnalysisInput,
    analysis_frames_to_jsonl,
    analyze_replay_to_jsonl,
)
from wargame_native_engine.replay import SourceFrameRecorder, convert_legacy_replay
from wargame_native_engine.viewer import (
    ReplayViewerLoadError,
    create_replay_viewer_server,
    load_replay_view,
)


@pytest.fixture
def native_bundle(tmp_path: Path) -> Path:
    return _make_native_bundle(tmp_path, {
        "scenario_id": "viewer-fixture",
        "scenario_hash": "s" * 64,
        "ruleset_id": "native-simplified",
        "ruleset_version": "1.0",
        "ruleset_hash": "r" * 64,
        "episode_seed": 7,
    })


def _make_native_bundle(
    tmp_path: Path,
    metadata: dict,
    *,
    include_step: bool = True,
    step_overrides: dict | None = None,
) -> Path:
    recorder = SourceFrameRecorder(tmp_path / "recording")
    recorder.start(metadata, started_at="2026-01-01T00:00:00Z")
    initial = _frame(0, perspective=True)
    recorder.record_initial(initial, initial_state_hash="a" * 64)
    following = _frame(1, perspective=True)
    following["actions"] = [{
        "side": "red",
        "actor_id": 1,
        "action_type": "MOVE",
        "target_id": None,
        "target_position": None,
        "parameters": {},
    }]
    following["judge_info"] = [{"event": "arrived"}]
    if include_step:
        step_record = {
            "delta_time_ms": 100,
            "ordered_actions": following["actions"],
            "expected_state_hash": "b" * 64,
        }
        step_record.update(step_overrides or {})
        recorder.record_step(following, step_record)
    recorder.finalize(
        {"terminal": False, "truncated": True, "winner": None, "reason": "test"},
        final_state_hash="b" * 64,
    )
    output = tmp_path / "native-view"
    output.mkdir()
    recorder.package_zip(output / "replay.zip")
    analyze_replay_to_jsonl(
        output / "replay.zip",
        output / "frame_analysis.native.jsonl",
        source_format="native",
    )
    return output


@pytest.fixture
def legacy_bundle(tmp_path: Path) -> Path:
    source = tmp_path / "legacy.zip"
    first = _frame(0, optional_time=False)
    first["operators"][0] = _unit(1, 0, complete_factors=False)
    final_initial = _frame(0, optional_time=False)
    final_initial["operators"][0] = _unit(1, 0, complete_factors=False)
    following = _frame(2, optional_time=False)
    with ZipFile(source, "w") as archive:
        for name, value in (
            ("session/1.json", first),
            ("session/2.json", final_initial),
            ("session/3.json", following),
        ):
            archive.writestr(name, _json_bytes(value))
    output = tmp_path / "legacy-view"
    convert_legacy_replay(source, output)
    return output


def test_loads_native_replay_with_available_strict_replay_evidence(
    native_bundle: Path,
) -> None:
    before = _tree_hashes(native_bundle)

    view = load_replay_view(native_bundle)

    assert view.frame_count == 2
    assert view.archive.frame_names == ("0.json", "1.json")
    replay = view.replay_record()
    assert replay["strict_replay_evidence_status"] == "Available"
    assert "strict_replay_supported" not in replay
    assert replay["strict_replay_evidence_note"] == (
        "Evidence availability does not mean replay verified."
    )
    assert view.replay_record()["perspective"] == "GREEN"
    assert view.frame_record(0)["time"]["cur_step"] == 0
    assert view.frame_record(1)["actions"][0]["action_type"] == "MOVE"
    assert _tree_hashes(native_bundle) == before


def test_loads_legacy_conversion_with_unknown_and_missing_time_fields(
    legacy_bundle: Path,
) -> None:
    view = load_replay_view(legacy_bundle)

    assert view.frame_count == 2
    assert view.replay_record()["strict_replay_evidence_status"] == "Unavailable"
    assert view.replay_record()["playback_compatibility"] == (
        "source-numbered-frame-playback/1.0"
    )
    assert view.frame_record(0)["time"] == {"cur_step": 0, "tick": 0}
    assert view.archive.frames[0].time.max_time is None
    assert view.archive.frames[0].time.max_step is None
    assert view.archive.frames[0].time.stage is None
    unknown_values = [
        factor["value"]
        for factor in view.analysis_record(0)["unit_analyses"][0]["factor_values"]
        if factor["status"] == "UNKNOWN"
    ]
    assert unknown_values and all(value is None for value in unknown_values)


def test_rejects_replay_analysis_frame_count_mismatch(
    native_bundle: Path,
) -> None:
    path = native_bundle / "frame_analysis.native.jsonl"
    path.write_text(path.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")

    with pytest.raises(ReplayViewerLoadError, match="frame count mismatch"):
        load_replay_view(native_bundle)


def test_rejects_analysis_frame_index_mismatch(native_bundle: Path) -> None:
    records = _analysis_records(native_bundle)
    records[1]["provenance"]["frame_index"] = 2
    _write_records(native_bundle, records)

    with pytest.raises(ReplayViewerLoadError, match="index mismatch at frame 1"):
        load_replay_view(native_bundle)


def test_rejects_analysis_frame_hash_mismatch(native_bundle: Path) -> None:
    records = _analysis_records(native_bundle)
    records[1]["provenance"]["frame_hash"] = "0" * 64
    _write_records(native_bundle, records)

    with pytest.raises(ReplayViewerLoadError, match="hash mismatch at frame 1"):
        load_replay_view(native_bundle)


def test_rejects_non_continuous_numbered_playback(legacy_bundle: Path) -> None:
    replay = legacy_bundle / "replay.zip"
    with ZipFile(replay) as source:
        payloads = [source.read(name) for name in source.namelist()]
    with ZipFile(replay, "w") as target:
        for index, payload in enumerate(payloads, start=1):
            target.writestr(f"{index}.json", payload)

    with pytest.raises(ReplayViewerLoadError, match="continuous from 0.json"):
        load_replay_view(legacy_bundle)


def test_rejects_explicit_non_green_frame_perspective(
    legacy_bundle: Path,
) -> None:
    replay = legacy_bundle / "replay.zip"
    with ZipFile(replay) as source:
        entries = [(name, source.read(name)) for name in source.namelist()]
    first = json.loads(entries[0][1])
    first["extra"] = {"native": {"perspective": "RED"}}
    entries[0] = (entries[0][0], _json_bytes(first))
    with ZipFile(replay, "w") as target:
        for name, payload in entries:
            target.writestr(name, payload)
    records = _analysis_records(legacy_bundle)
    records[0]["provenance"]["frame_hash"] = hashlib.sha256(entries[0][1]).hexdigest()
    _write_records(legacy_bundle, records)

    with pytest.raises(ReplayViewerLoadError, match="perspective must be GREEN"):
        load_replay_view(legacy_bundle)


@pytest.mark.parametrize("missing_name", ["replay.zip", "frame_analysis.native.jsonl"])
def test_missing_required_input_fails_clearly(
    native_bundle: Path,
    missing_name: str,
) -> None:
    (native_bundle / missing_name).unlink()

    with pytest.raises(ReplayViewerLoadError, match="missing"):
        load_replay_view(native_bundle)


def test_first_middle_last_records_and_timeline_align(tmp_path: Path) -> None:
    source = tmp_path / "legacy.zip"
    with ZipFile(source, "w") as archive:
        for sequence, step in enumerate((0, 1, 2), start=1):
            archive.writestr(f"session/{sequence}.json", _json_bytes(_frame(step)))
    output = tmp_path / "view"
    convert_legacy_replay(source, output)
    view = load_replay_view(output)

    assert [view.frame_record(index)["frame_index"] for index in (0, 1, 2)] == [0, 1, 2]
    assert [view.analysis_record(index)["provenance"]["frame_index"] for index in (0, 1, 2)] == [0, 1, 2]
    timeline = view.timeline_record()
    assert timeline["frame_indices"] == [0, 1, 2]
    assert all(len(values) == 3 for key, values in timeline.items() if key != "advantage_labels")
    assert len(timeline["advantage_labels"]) == 3


def test_frame_hashes_are_exact_numbered_payload_hashes(legacy_bundle: Path) -> None:
    view = load_replay_view(legacy_bundle)
    with ZipFile(legacy_bundle / "replay.zip") as archive:
        payloads = [archive.read(name) for name in archive.namelist()]

    for index, payload in enumerate(payloads):
        expected = hashlib.sha256(payload).hexdigest()
        assert view.frame_record(index)["source_hash"] == expected
        assert view.analysis_record(index)["provenance"]["frame_hash"] == expected


def test_out_of_range_frame_index_is_explicit(legacy_bundle: Path) -> None:
    view = load_replay_view(legacy_bundle)

    with pytest.raises(IndexError, match="outside"):
        view.frame_record(-1)
    with pytest.raises(IndexError, match="outside"):
        view.analysis_record(view.frame_count)


def test_missing_actions_and_judge_info_are_displayed_as_empty(
    tmp_path: Path,
) -> None:
    output = tmp_path / "optional-display-evidence"
    output.mkdir()
    frame = _frame(0)
    del frame["actions"]
    del frame["judge_info"]
    payload = _json_bytes(frame)
    with ZipFile(output / "replay.zip", "w") as archive:
        archive.writestr("0.json", payload)
    timeline = EightFactorAnalyzer().analyze_green_frames((
        GreenFrameAnalysisInput(
            frame_index=0,
            frame=frame,
            frame_hash=hashlib.sha256(payload).hexdigest(),
        ),
    ))
    (output / "frame_analysis.native.jsonl").write_text(
        analysis_frames_to_jsonl(timeline.frame_analyses),
        encoding="utf-8",
    )

    view = load_replay_view(output)

    assert view.frame_record(0)["actions"] == []
    assert view.frame_record(0)["judge_info"] == []


def test_http_api_and_static_assets_are_read_only(legacy_bundle: Path) -> None:
    view = load_replay_view(legacy_bundle)
    with _running_server(view) as base:
        replay = _get_json(base + "/api/replay")
        frame = _get_json(base + "/api/frame/0")
        analysis = _get_json(base + "/api/analysis/0")
        timeline = _get_json(base + "/api/timeline")
        map_record = _get_json(base + "/api/map")
        index = urlopen(base + "/", timeout=5).read().decode("utf-8")
        script = urlopen(base + "/static/app.js", timeout=5).read().decode("utf-8")
        map_script = urlopen(base + "/static/map_renderer.js", timeout=5).read().decode("utf-8")
        styles = urlopen(base + "/static/styles.css", timeout=5).read().decode("utf-8")

        assert replay["frame_count"] == 2
        assert frame["frame_index"] == analysis["provenance"]["frame_index"] == 0
        assert len(timeline["frame_indices"]) == 2
        assert map_record["mode"] == "coordinate-fallback"
        assert "六角地图态势视图" in index
        assert "currentFrameIndex" in script
        assert "hexToPixel" in map_script
        assert "--red" in styles
        with pytest.raises(HTTPError) as out_of_range:
            urlopen(base + "/api/frame/99", timeout=5)
        assert out_of_range.value.code == 400
        host, port = base.removeprefix("http://").split(":")
        connection = HTTPConnection(host, int(port), timeout=5)
        try:
            connection.request("POST", "/api/step", body=b"{}")
            assert connection.getresponse().status == 405
        finally:
            connection.close()


def test_frontend_uses_one_frame_index_for_all_synchronized_views() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "src/wargame_native_engine/viewer/static/app.js"
    ).read_text(encoding="utf-8")

    assert script.count("let currentFrameIndex = 0;") == 1
    assert "requestJson(`/api/frame/${currentFrameIndex}`)" in script
    assert "requestJson(`/api/analysis/${currentFrameIndex}`)" in script
    assert "x(currentFrameIndex)" in script
    assert '$("frame-slider").value = String(currentFrameIndex)' in script
    assert "if (currentFrameIndex >= frameCount - 1) { pausePlayback(); return; }" in script
    assert 'factor.status === "UNKNOWN" ? "UNKNOWN"' in script
    assert "currentFrame.actions ?? []" in script
    assert "currentFrame.judge_info ?? []" in script
    assert 'requestJson("/api/map")' in script
    assert "strict_replay_supported" not in script
    assert "Strict replay evidence:" in script


def test_incomplete_native_evidence_is_not_reported_as_supported(
    tmp_path: Path,
) -> None:
    output = _make_native_bundle(tmp_path, {"scenario_hash": "s" * 64})

    replay = load_replay_view(output).replay_record()

    assert replay["strict_replay_evidence_status"] == "Incomplete"
    assert "strict_replay_supported" not in replay


def test_native_manifest_without_steps_is_incomplete(tmp_path: Path) -> None:
    output = _make_native_bundle(
        tmp_path,
        _complete_native_metadata(),
        include_step=False,
    )

    assert load_replay_view(output).strict_replay_evidence_status == "Incomplete"


def test_native_steps_without_manifest_are_incomplete(tmp_path: Path) -> None:
    output = _make_native_bundle(tmp_path, _complete_native_metadata())
    _remove_zip_member(output / "replay.zip", "replay_manifest.native.jsonl")

    assert load_replay_view(output).strict_replay_evidence_status == "Incomplete"


@pytest.mark.parametrize(("manifest_field", "invalid_value"), [
    ("scenario_hash", None),
    ("ruleset_hash", None),
    ("episode_seed", None),
    ("episode_seed", True),
    ("scenario_id", ""),
    ("ruleset_id", "   "),
    ("initial_state_hash", ""),
])
def test_native_manifest_unusable_values_are_incomplete(
    tmp_path: Path,
    manifest_field: str,
    invalid_value: object,
) -> None:
    metadata = _complete_native_metadata()
    if manifest_field in {"initial_state_hash"}:
        output = _make_native_bundle(tmp_path, metadata)
        _rewrite_native_manifest(
            output / "replay.zip",
            {manifest_field: invalid_value},
        )
    else:
        metadata[manifest_field] = invalid_value
        output = _make_native_bundle(tmp_path, metadata)

    assert load_replay_view(output).strict_replay_evidence_status == "Incomplete"


@pytest.mark.parametrize(("step_field", "invalid_value"), [
    ("delta_time_ms", None),
    ("delta_time_ms", True),
    ("ordered_actions", None),
    ("expected_state_hash", None),
    ("expected_state_hash", ""),
])
def test_native_step_unusable_values_are_incomplete(
    tmp_path: Path,
    step_field: str,
    invalid_value: object,
) -> None:
    output = _make_native_bundle(
        tmp_path,
        _complete_native_metadata(),
        step_overrides={step_field: invalid_value},
    )

    assert load_replay_view(output).strict_replay_evidence_status == "Incomplete"


def test_explicit_map_config_is_loaded_and_must_match_replay(
    native_bundle: Path,
    tmp_path: Path,
) -> None:
    path = tmp_path / "map.json"
    value = {
        "map_id": "7",
        "coordinate_system": "odd-r",
        "max_row": 2,
        "max_column": 4,
        "map_data": [[
            {"r": 0, "c": 0, "elev": 0, "cond": 0,
             "roads": [0] * 6, "rivers": [0] * 6},
            {"r": 0, "c": 1, "elev": 10, "cond": 1,
             "roads": [1, 0, 0, 0, 0, 0], "rivers": [0] * 6},
            {"r": 0, "c": 2, "elev": 20, "cond": 4,
             "roads": [0] * 6, "rivers": [0, 1, 0, 0, 0, 0]},
        ], [
            {"r": 1, "c": 0, "elev": 30, "cond": 2,
             "roads": [0] * 6, "rivers": [0] * 6},
            {"r": 1, "c": 1, "elev": 40, "cond": 3,
             "roads": [0] * 6, "rivers": [0] * 6},
            {"r": 1, "c": 2, "elev": 50, "cond": 0,
             "roads": [0] * 6, "rivers": [0] * 6},
        ]],
    }
    path.write_text(json.dumps(value), encoding="utf-8")

    view = load_replay_view(native_bundle, map_config=path)

    assert view.map_record()["mode"] == "configured-hex-map"
    assert view.map_record()["bounds"] == {
        "min_row": 0, "max_row": 1,
        "min_column": 0, "max_column": 3,
    }
    value["map_id"] = "wrong"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ReplayViewerLoadError, match="map_id does not match"):
        load_replay_view(native_bundle, map_config=path)


def test_viewer_cli_defaults_to_loopback_only() -> None:
    arguments = viewer_argument_parser().parse_args(["--replay-dir", "/tmp/replay"])

    assert arguments.host == "127.0.0.1"
    assert arguments.port == 8000
    assert arguments.map_config is None


def test_analysis_model_provenance_must_be_consistent(native_bundle: Path) -> None:
    records = _analysis_records(native_bundle)
    records[1]["provenance"]["model_version"] = "different"
    _write_records(native_bundle, records)

    with pytest.raises(ReplayViewerLoadError, match="analysis sidecar"):
        load_replay_view(native_bundle)


def _frame(
    cur_step: int,
    *,
    perspective: bool = False,
    optional_time: bool = True,
) -> dict:
    time = {"cur_step": cur_step, "tick": cur_step}
    if optional_time:
        time.update({"max_time": 10, "max_step": 10, "stage": 2})
    value = {
        "scenario_id": "viewer-fixture",
        "terrain_id": 7,
        "time": time,
        "operators": [_unit(1, 0), _unit(2, 1)],
        "passengers": [_unit(3, 0)],
        "jm_points": [],
        "cities": [],
        "scores": {},
        "landmarks": {},
        "judge_info": [],
        "role_and_grouping_info": {},
        "communication": [],
        "valid_actions": {},
        "actions": [],
    }
    if perspective:
        value["extra"] = {"native": {"perspective": "GREEN"}}
    return value


def _unit(
    obj_id: int,
    color: int,
    *,
    complete_factors: bool = True,
) -> dict:
    value = {
        "obj_id": obj_id,
        "color": color,
        "type": 2,
        "sub_type": 0,
        "name": f"unit-{obj_id}",
        "armor": 0,
        "cur_hex": 100 + obj_id,
        "blood": 100,
        "max_blood": 100,
    }
    if complete_factors:
        value.update({
            "value": 100.0,
            "carry_weapon_ids": [36],
            "remain_bullet_nums": {"36": 10},
            "weapon_cool_time": 0,
            "weapon_unfold_state": 2,
            "move_state": 0,
            "stop": False,
            "can_to_move": True,
            "on_board": False,
            "see_enemy_bop_ids": [],
            "keep": False,
            "tire": 0,
            "lose_control": False,
            "close_combat": False,
        })
    return value


def _json_bytes(value: object) -> bytes:
    return (json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n").encode("utf-8")


def _analysis_records(root: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (root / "frame_analysis.native.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]


def _write_records(root: Path, records: list[dict]) -> None:
    (root / "frame_analysis.native.jsonl").write_bytes(
        b"".join(_json_bytes(record) for record in records)
    )


def _complete_native_metadata() -> dict:
    return {
        "scenario_id": "viewer-fixture",
        "scenario_hash": "s" * 64,
        "ruleset_id": "native-simplified",
        "ruleset_version": "1.0",
        "ruleset_hash": "r" * 64,
        "episode_seed": 7,
    }


def _remove_zip_member(path: Path, removed_name: str) -> None:
    replacement = path.with_suffix(".replacement.zip")
    with ZipFile(path) as source, ZipFile(replacement, "w") as target:
        for info in source.infolist():
            if info.filename != removed_name:
                target.writestr(info, source.read(info))
    replacement.replace(path)


def _rewrite_native_manifest(path: Path, updates: dict) -> None:
    replacement = path.with_suffix(".replacement.zip")
    with ZipFile(path) as source:
        entries = [(info, source.read(info)) for info in source.infolist()]
    manifest_name = "replay_manifest.native.jsonl"
    rewritten = []
    for info, payload in entries:
        if info.filename == manifest_name:
            manifest = json.loads(payload)
            manifest.update(updates)
            deterministic = {
                key: value
                for key, value in manifest.items()
                if key not in {"started_at", "deterministic_content_hash"}
            }
            from wargame_native_engine.io.serialization import canonical_hash
            manifest["deterministic_content_hash"] = canonical_hash(deterministic)
            payload = _json_bytes(manifest)
        rewritten.append((info, payload))
    with ZipFile(replacement, "w") as target:
        for info, payload in rewritten:
            target.writestr(info, payload)
    replacement.replace(path)


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        item.name: hashlib.sha256(item.read_bytes()).hexdigest()
        for item in root.iterdir()
        if item.is_file()
    }


@contextmanager
def _running_server(view) -> Iterator[str]:
    server = create_replay_viewer_server(view, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def _get_json(url: str) -> dict:
    return json.loads(urlopen(url, timeout=5).read())
