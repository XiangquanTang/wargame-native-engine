from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from scripts.run_minimal_source_replay import DEFAULT_SCENARIO, _run_episode
from wargame_native_engine.analysis import (
    EightFactorAnalyzer,
    GreenFrameAnalysisInput,
)
from wargame_native_engine.analysis.jsonl import analysis_frames_to_jsonl
from wargame_native_engine.io.importers.historical_replay import (
    HistoricalReplayImporter,
)
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.scenario import load_scenario
import wargame_native_engine.replay.source_frame_recorder as recorder_module
from wargame_native_engine.replay.source_frame_recorder import (
    ANALYSIS_SIDECAR_NAME,
    SourceFrameRecorder,
)


FACT_SIDECARS = ("replay_manifest.native.jsonl", "steps.jsonl")


@pytest.fixture(scope="module")
def packaged_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("analysis-sidecar")
    scenario = load_scenario(DEFAULT_SCENARIO)
    recorded = _run_episode(
        scenario,
        42,
        20,
        recorder_root=root / "green_frames",
    )
    unrecorded = _run_episode(
        scenario,
        42,
        20,
        recorder_root=None,
    )
    recorder = recorded["recorder"]
    analyzer = EightFactorAnalyzer(
        model_id="sidecar-test-model",
        model_version="2.0-test",
        model_hash=None,
    )
    disabled = recorder.package_zip(root / "disabled.zip")

    # A same-named work-directory file is not an input member.  Enabled
    # packaging must generate its one sidecar from the numbered fact bytes.
    (recorder.root / ANALYSIS_SIDECAR_NAME).write_bytes(b"not-authoritative")
    enabled = recorder.package_zip(
        root / "enabled.zip",
        analysis_analyzer=analyzer,
    )
    repeated = recorder.package_zip(
        root / "enabled-repeated.zip",
        analysis_analyzer=analyzer,
    )
    return {
        "root": root,
        "scenario": scenario,
        "recorded": recorded,
        "unrecorded": unrecorded,
        "recorder": recorder,
        "analyzer": analyzer,
        "disabled": disabled,
        "enabled": enabled,
        "repeated": repeated,
    }


def test_analysis_sidecar_is_default_off_and_preserves_fact_member_order(
    packaged_run,
) -> None:
    with ZipFile(packaged_run["disabled"]) as archive:
        names = archive.namelist()
        numbered = _numbered_names(names)

    assert ANALYSIS_SIDECAR_NAME not in names
    assert names == numbered + list(FACT_SIDECARS)


def test_enabled_sidecar_member_contract_and_deterministic_zip_metadata(
    packaged_run,
) -> None:
    with ZipFile(packaged_run["enabled"]) as archive:
        names = archive.namelist()
        numbered = _numbered_names(names)
        payload = archive.read(ANALYSIS_SIDECAR_NAME)
        info = archive.getinfo(ANALYSIS_SIDECAR_NAME)
        manifest = _manifest(archive)

    assert names == numbered + [ANALYSIS_SIDECAR_NAME, *FACT_SIDECARS]
    assert names.count(ANALYSIS_SIDECAR_NAME) == 1
    assert payload != b"not-authoritative"
    assert payload and payload.endswith(b"\n")
    text = payload.decode("utf-8")
    assert all(line.strip() for line in text.splitlines())
    assert len(text.splitlines()) == manifest["frame_count"]
    assert numbered == [
        f"{index}.json" for index in range(manifest["frame_count"])
    ]
    assert info.date_time == (1980, 1, 1, 0, 0, 0)
    assert info.compress_type == ZIP_DEFLATED
    assert info.external_attr >> 16 == 0o100644


def test_sidecar_provenance_uses_exact_fact_payloads_and_analyzer_config(
    packaged_run,
) -> None:
    analyzer = packaged_run["analyzer"]
    with ZipFile(packaged_run["enabled"]) as archive:
        manifest = _manifest(archive)
        records = _jsonl(archive.read(ANALYSIS_SIDECAR_NAME))
        numbered = _numbered_names(archive.namelist())
        frame_payloads = [archive.read(name) for name in numbered]

    assert len(records) == manifest["frame_count"]
    for index, (record, frame_payload) in enumerate(
        zip(records, frame_payloads)
    ):
        provenance = record["provenance"]
        assert record["analysis_schema"] == (
            "native-eight-factor-frame-analysis/1.0"
        )
        assert provenance["frame_index"] == index
        assert provenance["frame_hash"] == hashlib.sha256(
            frame_payload
        ).hexdigest()
        assert provenance["scenario_hash"] == manifest["scenario_hash"]
        assert provenance["model_id"] == analyzer.model_id
        assert provenance["model_version"] == analyzer.model_version
        assert provenance["model_hash"] is None


def test_sidecar_bytes_equal_manual_analyzer_and_jsonl_composition(
    packaged_run,
) -> None:
    analyzer = packaged_run["analyzer"]
    with ZipFile(packaged_run["enabled"]) as archive:
        manifest = _manifest(archive)
        frame_payloads = [
            (index, archive.read(f"{index}.json"))
            for index in range(manifest["frame_count"])
        ]
        actual = archive.read(ANALYSIS_SIDECAR_NAME)

    inputs = tuple(
        GreenFrameAnalysisInput(
            frame_index=index,
            frame=json.loads(payload.decode("utf-8")),
            frame_hash=hashlib.sha256(payload).hexdigest(),
            scenario_hash=manifest.get("scenario_hash"),
        )
        for index, payload in frame_payloads
    )
    timeline = analyzer.analyze_green_frames(inputs)
    expected = analysis_frames_to_jsonl(timeline.frame_analyses).encode("utf-8")

    assert actual == expected
    assert timeline.frame_indices == tuple(range(manifest["frame_count"]))


def test_repeated_enabled_packaging_is_byte_identical(packaged_run) -> None:
    first = packaged_run["enabled"].read_bytes()
    second = packaged_run["repeated"].read_bytes()

    assert first == second
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()
    with ZipFile(packaged_run["enabled"]) as first_zip, ZipFile(
        packaged_run["repeated"]
    ) as second_zip:
        assert first_zip.namelist() == second_zip.namelist()
        assert first_zip.read(ANALYSIS_SIDECAR_NAME) == second_zip.read(
            ANALYSIS_SIDECAR_NAME
        )


def test_analysis_toggle_changes_only_derived_member(packaged_run) -> None:
    with ZipFile(packaged_run["disabled"]) as disabled, ZipFile(
        packaged_run["enabled"]
    ) as enabled:
        disabled_names = disabled.namelist()
        enabled_names = enabled.namelist()
        fact_names = _numbered_names(disabled_names) + list(FACT_SIDECARS)
        for name in fact_names:
            assert disabled.read(name) == enabled.read(name)
        disabled_manifest = _manifest(disabled)
        enabled_manifest = _manifest(enabled)

    assert set(enabled_names) - set(disabled_names) == {ANALYSIS_SIDECAR_NAME}
    assert disabled_manifest == enabled_manifest
    for field in (
        "frame_count",
        "frames_merkle_root",
        "deterministic_content_hash",
        "final_state_hash",
        "winner",
        "termination",
        "truncated",
    ):
        assert disabled_manifest[field] == enabled_manifest[field]


def test_packaging_analysis_remains_observer_only(packaged_run) -> None:
    recorded = packaged_run["recorded"]
    unrecorded = packaged_run["unrecorded"]

    assert recorded["result"] == unrecorded["result"]
    assert recorded["trace"] == unrecorded["trace"]
    with ZipFile(packaged_run["disabled"]) as disabled, ZipFile(
        packaged_run["enabled"]
    ) as enabled:
        for name in _numbered_names(disabled.namelist()) + list(FACT_SIDECARS):
            assert disabled.read(name) == enabled.read(name)


def test_source_historical_and_strict_replay_ignore_missing_valid_or_bad_sidecar(
    packaged_run,
) -> None:
    damaged = packaged_run["root"] / "damaged-sidecar.zip"
    _copy_zip_with_sidecar(
        packaged_run["disabled"],
        damaged,
        b"{this is not valid JSONL\n",
    )
    paths = (
        packaged_run["disabled"],
        packaged_run["enabled"],
        damaged,
    )

    imported = [SourceReplayArchiveImporter().import_path(path) for path in paths]
    verified = [
        replay_source_archive(packaged_run["scenario"], path, strict=True)
        for path in paths
    ]
    historical = [HistoricalReplayImporter().import_path(path) for path in paths]

    assert all(value.frame_names == imported[0].frame_names for value in imported)
    assert all(value.manifest == imported[0].manifest for value in imported)
    assert all(value.matches for value in verified)
    assert len({value.actual_world_hash for value in verified}) == 1
    assert len({value.expected_world_hash for value in verified}) == 1
    assert all(
        value.replay.summary.to_dict() == historical[0].replay.summary.to_dict()
        for value in historical
    )
    assert all(
        len(value.replay.frames) == len(historical[0].replay.frames)
        for value in historical
    )
    for value in historical:
        ignored = value.replay.metadata["native_sidecars_ignored_as_frames"]
        assert ANALYSIS_SIDECAR_NAME in ignored


def test_packaged_unknown_timeline_behavior_matches_existing_analyzer(
    tmp_path: Path,
) -> None:
    recorder = _finalized_recorder(tmp_path / "unknown-recovery", frame_count=4)
    incomplete = _green_frame(red_value=100.0, blue_value=100.0)
    del incomplete["operators"][0]["blood"]
    _replace_frame_and_manifest(recorder.root, 1, _json_bytes(incomplete))
    analyzer = EightFactorAnalyzer()

    output = recorder.package_zip(
        tmp_path / "unknown-recovery.zip",
        analysis_analyzer=analyzer,
    )
    with ZipFile(output) as archive:
        manifest = _manifest(archive)
        inputs = tuple(
            _analysis_input(archive, index, manifest.get("scenario_hash"))
            for index in range(manifest["frame_count"])
        )
        actual = archive.read(ANALYSIS_SIDECAR_NAME)

    timeline = analyzer.analyze_green_frames(inputs)
    assert timeline.delta_advantages[:3] == (None, None, None)
    assert timeline.delta_advantages[3] is not None
    assert actual == analysis_frames_to_jsonl(
        timeline.frame_analyses
    ).encode("utf-8")


@pytest.mark.parametrize("target_exists", [False, True])
def test_analysis_failure_is_atomic_for_absent_or_existing_target(
    tmp_path: Path,
    target_exists: bool,
) -> None:
    case = "existing" if target_exists else "absent"
    recorder = _finalized_recorder(tmp_path / f"atomic-{case}")
    bad_frame = _green_frame()
    bad_frame["operators"][0]["value"] = "NaN"
    _replace_frame_and_manifest(recorder.root, 0, _json_bytes(bad_frame))
    target = tmp_path / f"atomic-{case}.zip"
    old_payload = b"pre-existing-target"
    if target_exists:
        target.write_bytes(old_payload)

    with pytest.raises(ValueError, match="base_value_factor"):
        recorder.package_zip(target, analysis_analyzer=EightFactorAnalyzer())

    if target_exists:
        assert target.read_bytes() == old_payload
        assert hashlib.sha256(target.read_bytes()).hexdigest() == hashlib.sha256(
            old_payload
        ).hexdigest()
    else:
        assert not target.exists()
    assert list(target.parent.glob(f".{target.name}.*")) == []


def test_analysis_sidecar_rejects_numbered_frame_gap_before_zip_creation(
    tmp_path: Path,
) -> None:
    recorder = _finalized_recorder(tmp_path / "gap", frame_count=2)
    (recorder.root / "1.json").rename(recorder.root / "2.json")
    target = tmp_path / "gap.zip"

    with pytest.raises(ValueError, match="continuous from 0"):
        recorder.package_zip(target, analysis_analyzer=EightFactorAnalyzer())

    assert not target.exists()
    assert list(target.parent.glob(f".{target.name}.*")) == []


def test_analysis_sidecar_rejects_manifest_frame_count_mismatch(
    tmp_path: Path,
) -> None:
    recorder = _finalized_recorder(tmp_path / "count")
    manifest = _read_manifest_path(recorder.root)
    manifest["frame_count"] += 1
    _write_manifest_path(recorder.root, manifest)
    target = tmp_path / "count.zip"

    with pytest.raises(ValueError, match="frame_count mismatch"):
        recorder.package_zip(target, analysis_analyzer=EightFactorAnalyzer())

    assert not target.exists()


def test_analysis_sidecar_rejects_recorder_without_numbered_frames(
    tmp_path: Path,
) -> None:
    recorder = _finalized_recorder(tmp_path / "no-frames")
    (recorder.root / "0.json").unlink()
    target = tmp_path / "no-frames.zip"

    with pytest.raises(ValueError, match="requires numbered frames"):
        recorder.package_zip(target, analysis_analyzer=EightFactorAnalyzer())

    assert not target.exists()


def test_analysis_sidecar_rejects_invalid_utf8_frame_before_zip_creation(
    tmp_path: Path,
) -> None:
    recorder = _finalized_recorder(tmp_path / "invalid-utf8")
    _replace_frame_and_manifest(recorder.root, 0, b"\xff\xfe")
    target = tmp_path / "invalid-utf8.zip"

    with pytest.raises(UnicodeDecodeError):
        recorder.package_zip(target, analysis_analyzer=EightFactorAnalyzer())

    assert not target.exists()
    assert list(target.parent.glob(f".{target.name}.*")) == []


@pytest.mark.parametrize(
    "member_name",
    ["0.json", "1.json", "steps.jsonl", "replay_manifest.native.jsonl"],
)
@pytest.mark.parametrize("analysis_enabled", [False, True])
def test_archive_output_cannot_replace_any_authoritative_member(
    tmp_path: Path,
    member_name: str,
    analysis_enabled: bool,
) -> None:
    case = f"{member_name}-{analysis_enabled}"
    recorder = _finalized_recorder(tmp_path / case, frame_count=2)
    facts_before = _fact_snapshot(recorder)
    entries_before = _workdir_entries(recorder)
    analyzer = Mock(spec=EightFactorAnalyzer) if analysis_enabled else None
    target = recorder.root / member_name

    with pytest.raises(
        ValueError,
        match=rf"archive output .* authoritative replay member {member_name}",
    ):
        recorder.package_zip(target, analysis_analyzer=analyzer)

    assert _fact_snapshot(recorder) == facts_before
    assert _workdir_entries(recorder) == entries_before
    assert list(target.parent.glob(f".{target.name}.*")) == []
    if analyzer is not None:
        analyzer.analyze_green_frames.assert_not_called()


def test_archive_output_rejects_normalized_parent_path_alias(
    tmp_path: Path,
) -> None:
    recorder = _finalized_recorder(tmp_path / "normalized-alias", frame_count=2)
    facts_before = _fact_snapshot(recorder)
    entries_before = _workdir_entries(recorder)
    analyzer = Mock(spec=EightFactorAnalyzer)
    target = recorder.root / "not-created" / ".." / "steps.jsonl"

    with pytest.raises(
        ValueError,
        match=r"archive output .* authoritative replay member steps.jsonl",
    ):
        recorder.package_zip(target, analysis_analyzer=analyzer)

    assert _fact_snapshot(recorder) == facts_before
    assert _workdir_entries(recorder) == entries_before
    assert list(recorder.root.glob(".steps.jsonl.*")) == []
    analyzer.analyze_green_frames.assert_not_called()


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_archive_output_rejects_existing_samefile_alias(
    tmp_path: Path,
    alias_kind: str,
) -> None:
    recorder = _finalized_recorder(
        tmp_path / f"samefile-{alias_kind}",
        frame_count=2,
    )
    facts_before = _fact_snapshot(recorder)
    entries_before = _workdir_entries(recorder)
    fact = recorder.root / "steps.jsonl"
    target = tmp_path / f"{alias_kind}-steps-alias.zip"
    try:
        if alias_kind == "symlink":
            target.symlink_to(fact)
        else:
            os.link(fact, target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"{alias_kind} is not supported: {exc}")
    alias_before = target.read_bytes()
    analyzer = Mock(spec=EightFactorAnalyzer)

    with pytest.raises(
        ValueError,
        match=r"archive output .* authoritative replay member steps.jsonl",
    ):
        recorder.package_zip(target, analysis_analyzer=analyzer)

    assert _fact_snapshot(recorder) == facts_before
    assert _workdir_entries(recorder) == entries_before
    assert target.read_bytes() == alias_before
    assert list(target.parent.glob(f".{target.name}.*")) == []
    analyzer.analyze_green_frames.assert_not_called()


@pytest.mark.parametrize("analysis_enabled", [False, True])
@pytest.mark.parametrize(
    "output_case",
    ["default", "inside", "external-new", "external-existing"],
)
def test_legal_archive_outputs_remain_supported_and_repeatable(
    tmp_path: Path,
    output_case: str,
    analysis_enabled: bool,
) -> None:
    recorder = _finalized_recorder(
        tmp_path / f"legal-{output_case}-{analysis_enabled}",
        frame_count=2,
    )
    facts_before = _fact_snapshot(recorder)
    analyzer = EightFactorAnalyzer() if analysis_enabled else None
    if output_case == "default":
        output = None
        expected_target = recorder.root / "replay.zip"
    elif output_case == "inside":
        output = recorder.root / "replay.zip"
        expected_target = output
    else:
        output = tmp_path / f"{output_case}-{analysis_enabled}.zip"
        expected_target = output
        if output_case == "external-existing":
            output.write_bytes(b"old non-fact archive target")

    first = recorder.package_zip(output, analysis_analyzer=analyzer)
    first_payload = first.read_bytes()
    second = recorder.package_zip(output, analysis_analyzer=analyzer)

    assert first == expected_target
    assert second == expected_target
    assert second.read_bytes() == first_payload
    assert _fact_snapshot(recorder) == facts_before
    with ZipFile(second) as archive:
        names = archive.namelist()
    assert (ANALYSIS_SIDECAR_NAME in names) is analysis_enabled


@pytest.mark.parametrize("target_exists", [False, True])
def test_invalid_json_analysis_failure_preserves_target_and_facts(
    tmp_path: Path,
    target_exists: bool,
) -> None:
    recorder = _finalized_recorder(
        tmp_path / f"invalid-json-{target_exists}",
        frame_count=2,
    )
    _replace_frame_and_manifest(recorder.root, 0, b"{not valid JSON\n")
    facts_before = _fact_snapshot(recorder)
    entries_before = _workdir_entries(recorder)
    target = tmp_path / f"invalid-json-{target_exists}.zip"
    old_target = _prepare_target(target, target_exists)

    with pytest.raises(json.JSONDecodeError):
        recorder.package_zip(target, analysis_analyzer=EightFactorAnalyzer())

    _assert_failed_package_state(
        recorder,
        target,
        old_target,
        facts_before,
        entries_before,
    )


@pytest.mark.parametrize("target_exists", [False, True])
def test_zip_member_write_failure_is_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_exists: bool,
) -> None:
    recorder = _finalized_recorder(
        tmp_path / f"writestr-failure-{target_exists}",
        frame_count=2,
    )
    facts_before = _fact_snapshot(recorder)
    entries_before = _workdir_entries(recorder)
    target = tmp_path / f"writestr-failure-{target_exists}.zip"
    old_target = _prepare_target(target, target_exists)

    def fail_writestr(*args, **kwargs) -> None:
        raise OSError("injected ZipFile.writestr failure")

    monkeypatch.setattr(recorder_module.ZipFile, "writestr", fail_writestr)
    with pytest.raises(OSError, match="injected ZipFile.writestr failure"):
        recorder.package_zip(target, analysis_analyzer=EightFactorAnalyzer())

    _assert_failed_package_state(
        recorder,
        target,
        old_target,
        facts_before,
        entries_before,
    )


@pytest.mark.parametrize("target_exists", [False, True])
def test_zip_close_failure_is_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_exists: bool,
) -> None:
    recorder = _finalized_recorder(
        tmp_path / f"close-failure-{target_exists}",
        frame_count=2,
    )
    facts_before = _fact_snapshot(recorder)
    entries_before = _workdir_entries(recorder)
    target = tmp_path / f"close-failure-{target_exists}.zip"
    old_target = _prepare_target(target, target_exists)

    class CloseFailZipFile(ZipFile):
        _raise_on_close = True

        def close(self) -> None:
            super().close()
            if self._raise_on_close:
                self._raise_on_close = False
                raise OSError("injected ZipFile.close failure")

    monkeypatch.setattr(recorder_module, "ZipFile", CloseFailZipFile)
    with pytest.raises(OSError, match="injected ZipFile.close failure"):
        recorder.package_zip(target, analysis_analyzer=EightFactorAnalyzer())

    _assert_failed_package_state(
        recorder,
        target,
        old_target,
        facts_before,
        entries_before,
    )


def test_os_replace_failure_preserves_existing_target_and_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _finalized_recorder(
        tmp_path / "replace-failure",
        frame_count=2,
    )
    facts_before = _fact_snapshot(recorder)
    entries_before = _workdir_entries(recorder)
    target = tmp_path / "replace-failure.zip"
    old_target = _prepare_target(target, True)

    def fail_replace(*args, **kwargs) -> None:
        raise OSError("injected os.replace failure")

    monkeypatch.setattr(recorder_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected os.replace failure"):
        recorder.package_zip(target, analysis_analyzer=EightFactorAnalyzer())

    _assert_failed_package_state(
        recorder,
        target,
        old_target,
        facts_before,
        entries_before,
    )


def _fact_snapshot(recorder: SourceFrameRecorder) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in sorted(recorder.root.iterdir())
        if path.is_file()
        and (
            path.name in FACT_SIDECARS
            or (path.name.endswith(".json") and path.name[:-5].isdigit())
        )
    }


def _workdir_entries(recorder: SourceFrameRecorder) -> tuple[str, ...]:
    return tuple(sorted(path.name for path in recorder.root.iterdir()))


def _prepare_target(target: Path, target_exists: bool) -> bytes | None:
    if not target_exists:
        return None
    payload = b"pre-existing non-fact archive target"
    target.write_bytes(payload)
    return payload


def _assert_failed_package_state(
    recorder: SourceFrameRecorder,
    target: Path,
    old_target: bytes | None,
    facts_before: dict[str, bytes],
    entries_before: tuple[str, ...],
) -> None:
    assert _fact_snapshot(recorder) == facts_before
    assert _workdir_entries(recorder) == entries_before
    if old_target is None:
        assert not target.exists()
    else:
        assert target.read_bytes() == old_target
        assert hashlib.sha256(target.read_bytes()).hexdigest() == hashlib.sha256(
            old_target
        ).hexdigest()
    assert list(target.parent.glob(f".{target.name}.*")) == []


def _finalized_recorder(root: Path, *, frame_count: int = 1) -> SourceFrameRecorder:
    recorder = SourceFrameRecorder(root)
    recorder.start(
        {"scenario_hash": "scenario-hash", "episode_id": root.name},
        started_at="2026-01-01T00:00:00Z",
    )
    recorder.record_initial(
        _green_frame(),
        initial_state_hash="a" * 64,
    )
    for index in range(1, frame_count):
        red_value = 300.0 if index == frame_count - 1 else 100.0
        recorder.record_step(
            _green_frame(red_value=red_value),
            {
                "delta_time_ms": 1,
                "ordered_actions": [],
                "expected_state_hash": f"state-{index}",
            },
        )
    recorder.finalize(
        {
            "terminal": False,
            "truncated": True,
            "winner": None,
            "reason": "test",
        },
        final_state_hash="b" * 64,
    )
    return recorder


def _green_frame(
    *,
    red_value: float = 100.0,
    blue_value: float = 100.0,
) -> dict[str, Any]:
    return {
        "operators": [
            _unit(1, 0, red_value),
            _unit(2, 1, blue_value),
        ],
        "passengers": [],
        "actions": [],
        "judge_info": [],
        "extra": {"native": {"perspective": "GREEN"}},
    }


def _unit(obj_id: int, color: int, value: float) -> dict[str, Any]:
    return {
        "obj_id": obj_id,
        "color": color,
        "value": value,
        "blood": 100,
        "max_blood": 100,
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
    }


def _analysis_input(
    archive: ZipFile,
    index: int,
    scenario_hash: str | None,
) -> GreenFrameAnalysisInput:
    payload = archive.read(f"{index}.json")
    return GreenFrameAnalysisInput(
        frame_index=index,
        frame=json.loads(payload.decode("utf-8")),
        frame_hash=hashlib.sha256(payload).hexdigest(),
        scenario_hash=scenario_hash,
    )


def _numbered_names(names: list[str]) -> list[str]:
    return [
        name for name in names
        if name.endswith(".json") and name[:-5].isdigit()
    ]


def _manifest(archive: ZipFile) -> dict[str, Any]:
    records = _jsonl(archive.read("replay_manifest.native.jsonl"))
    assert len(records) == 1
    return records[0]


def _jsonl(payload: bytes) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in payload.decode("utf-8").splitlines()
        if line.strip()
    ]


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"
    ).encode("utf-8")


def _read_manifest_path(root: Path) -> dict[str, Any]:
    return _jsonl((root / "replay_manifest.native.jsonl").read_bytes())[0]


def _write_manifest_path(root: Path, manifest: dict[str, Any]) -> None:
    (root / "replay_manifest.native.jsonl").write_bytes(_json_bytes(manifest))


def _replace_frame_and_manifest(root: Path, index: int, payload: bytes) -> None:
    (root / f"{index}.json").write_bytes(payload)
    manifest = _read_manifest_path(root)
    manifest["frame_hashes"][index] = hashlib.sha256(payload).hexdigest()
    _write_manifest_path(root, manifest)


def _copy_zip_with_sidecar(source: Path, target: Path, payload: bytes) -> None:
    with ZipFile(source) as source_zip, ZipFile(
        target,
        "w",
        compression=ZIP_DEFLATED,
        compresslevel=9,
    ) as target_zip:
        for name in source_zip.namelist():
            if name == "replay_manifest.native.jsonl":
                _write_test_member(target_zip, ANALYSIS_SIDECAR_NAME, payload)
            _write_test_member(target_zip, name, source_zip.read(name))


def _write_test_member(archive: ZipFile, name: str, payload: bytes) -> None:
    info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, payload, compresslevel=9)
