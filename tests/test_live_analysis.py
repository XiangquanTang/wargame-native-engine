from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from zipfile import ZipFile

import pytest

import wargame_native_engine.analysis.live as live_module

from scripts.run_minimal_source_replay import (
    _blue_controller,
    _red_controller,
    _template,
)
from wargame_native_engine.analysis import (
    EightFactorAnalyzer,
    GreenFrameAnalysisInput,
    LiveAnalysisDesynchronizedError,
    LiveAnalyzedSession,
    analysis_frames_to_jsonl,
    analyze_replay_to_jsonl,
    build_analysis_timeline,
)
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.serialization import (
    canonical_green_frame_payload,
)
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchiveImporter,
)
from wargame_native_engine.io.serialization import canonical_hash, json_clone
from wargame_native_engine.replay import SourceFrameRecorder
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import (
    SourceCompatibleEnvironment,
)
from wargame_native_engine.version import ENGINE_VERSION


SCENARIO_PATH = Path("examples/source_movement_scenario.json")
STARTED_AT = "2026-01-01T00:00:00+00:00"


@pytest.fixture(scope="module")
def completed_live_episode(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("live-analysis")
    return _run_episode(root, live_enabled=True)


def test_setup_and_every_successful_step_analyze_continuous_frames(
    completed_live_episode: dict[str, Any],
) -> None:
    live = completed_live_episode["live"]
    updates = completed_live_episode["updates"]
    assert updates[0].frame_index == 0
    assert updates[0].done is None
    assert updates[0].timeline.frame_indices == (0,)
    assert updates[0].timeline.delta_advantages == (None,)
    assert [update.frame_index for update in updates] == list(range(len(updates)))
    assert live.current_timeline.frame_indices == tuple(range(len(updates)))
    assert live.frame_analyses == live.current_timeline.frame_analyses
    assert live.latest_analysis == live.frame_analyses[-1]


def test_terminal_frame_is_analyzed(
    completed_live_episode: dict[str, Any],
) -> None:
    updates = completed_live_episode["updates"]
    assert updates[-1].done is True
    assert updates[-1].frame_index == len(updates) - 1
    assert completed_live_episode["result"]["terminal"] is True
    assert len(updates) == completed_live_episode["manifest"]["frame_count"]


def test_live_frame_hashes_are_exact_recorder_payload_hashes(
    completed_live_episode: dict[str, Any],
) -> None:
    replay = completed_live_episode["replay"]
    live = completed_live_episode["live"]
    with ZipFile(replay) as archive:
        payloads = tuple(
            archive.read(f"{index}.json")
            for index in range(len(live.frame_analyses))
        )
    expected = tuple(hashlib.sha256(payload).hexdigest() for payload in payloads)
    actual = tuple(
        frame.provenance.frame_hash for frame in live.frame_analyses
    )
    assert actual == expected
    assert all(payload.endswith(b"\n") for payload in payloads)


def test_live_provenance_matches_episode_and_shared_analyzer(
    completed_live_episode: dict[str, Any],
) -> None:
    live = completed_live_episode["live"]
    analyzer = completed_live_episode["analyzer"]
    scenario_hash = completed_live_episode["scenario_hash"]
    provenances = tuple(frame.provenance for frame in live.frame_analyses)
    assert {value.scenario_hash for value in provenances} == {scenario_hash}
    assert {value.model_id for value in provenances} == {analyzer.model_id}
    assert {value.model_version for value in provenances} == {
        analyzer.model_version
    }
    assert {value.model_hash for value in provenances} == {analyzer.model_hash}
    assert {value.perspective for value in provenances} == {"GREEN"}


def test_live_timeline_is_exact_existing_builder_result(
    completed_live_episode: dict[str, Any],
) -> None:
    live = completed_live_episode["live"]
    assert live.current_timeline == build_analysis_timeline(live.frame_analyses)


def test_live_and_offline_records_timeline_and_jsonl_are_identical(
    completed_live_episode: dict[str, Any],
) -> None:
    live = completed_live_episode["live"]
    offline = completed_live_episode["offline"]
    offline_payload = offline.read_bytes()
    assert live.frame_analyses == (
        completed_live_episode["offline_timeline"].frame_analyses
    )
    assert live.current_timeline == completed_live_episode["offline_timeline"]
    assert live.analysis_jsonl().encode("utf-8") == offline_payload
    assert analysis_frames_to_jsonl(live.frame_analyses).encode("utf-8") == (
        offline_payload
    )


def test_one_wrapper_step_calls_underlying_step_exactly_once() -> None:
    live, _ = _new_live()
    initial = live.setup(42)
    action = _red_controller(initial.source_result.red)
    actions = [] if action is None else [action]
    original = live.environment.session.step
    live.environment.session.step = Mock(wraps=original)  # type: ignore[method-assign]

    update = live.step(actions, delta_time_seconds=20)

    assert update.frame_index == 1
    assert live.environment.session.step.call_count == 1  # type: ignore[attr-defined]


def test_environment_step_failure_does_not_append_analysis() -> None:
    live, _ = _new_live()
    live.setup(42)
    before = live.current_timeline
    before_jsonl = live.analysis_jsonl()
    live.environment.step_views = Mock(  # type: ignore[method-assign]
        side_effect=RuntimeError("step failed before GREEN frame")
    )

    with pytest.raises(RuntimeError, match="step failed"):
        live.step([])

    assert live.current_timeline == before
    assert live.latest_analysis == before.frame_analyses[-1]
    assert live.frame_analyses == before.frame_analyses
    assert live.analysis_jsonl() == before_jsonl
    assert live.environment.step_views.call_count == 1  # type: ignore[attr-defined]


def test_setup_analyzer_failure_rejects_current_analysis_until_reset(
) -> None:
    delegate = EightFactorAnalyzer()

    class FailFirstAnalysis:
        def __init__(self) -> None:
            self.calls = 0
            self.enabled = True

        def analyze_green_frame(self, item):
            self.calls += 1
            if self.enabled:
                raise ValueError("injected setup analysis failure")
            return delegate.analyze_green_frame(item)

    analyzer = FailFirstAnalysis()
    live, _ = _new_live(analyzer=analyzer)  # type: ignore[arg-type]
    original_setup = live.environment.setup_views
    live.environment.setup_views = Mock(  # type: ignore[method-assign]
        wraps=original_setup
    )
    live.environment.step_views = Mock(  # type: ignore[method-assign]
        wraps=live.environment.step_views
    )

    with pytest.raises(ValueError, match="injected setup analysis failure"):
        live.setup(42)

    assert live.environment.setup_views.call_count == 1  # type: ignore[attr-defined]
    _assert_current_analysis_desynchronized(live)
    with pytest.raises(LiveAnalysisDesynchronizedError) as error:
        live.step([])
    assert str(error.value) == "live_analysis_desynchronized_reset_required"
    assert live.environment.step_views.call_count == 0  # type: ignore[attr-defined]

    analyzer.enabled = False
    recovered = live.reset(43)
    assert recovered.frame_index == 0
    assert live.current_timeline.frame_indices == (0,)
    assert live.environment.setup_views.call_count == 2  # type: ignore[attr-defined]


def test_mid_step_analyzer_failure_rejects_stale_prefix_until_reset() -> None:
    delegate = EightFactorAnalyzer()

    class FailSecondAnalysis:
        def __init__(self) -> None:
            self.calls = 0
            self.enabled = True

        def analyze_green_frame(self, item):
            self.calls += 1
            if self.enabled and self.calls == 2:
                raise ValueError("injected live analysis failure")
            return delegate.analyze_green_frame(item)

    analyzer = FailSecondAnalysis()
    live, _ = _new_live(analyzer=analyzer)  # type: ignore[arg-type]
    initial = live.setup(42)
    action = _red_controller(initial.source_result.red)
    original = live.environment.session.step
    live.environment.session.step = Mock(wraps=original)  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="injected live analysis failure"):
        live.step([] if action is None else [action], delta_time_seconds=20)

    assert live.environment.session.step.call_count == 1  # type: ignore[attr-defined]
    assert live.environment.last_bundle.green["time"]["cur_step"] == 20
    assert len(live._frame_analyses) == 1
    _assert_current_analysis_desynchronized(live)
    with pytest.raises(LiveAnalysisDesynchronizedError) as error:
        live.step([])
    assert str(error.value) == "live_analysis_desynchronized_reset_required"
    assert live.environment.session.step.call_count == 1  # type: ignore[attr-defined]

    analyzer.enabled = False
    recovered = live.reset(43)
    assert recovered.frame_index == 0
    assert live.current_timeline.frame_indices == (0,)


def test_terminal_step_analyzer_failure_rejects_stale_analysis_and_recovers(
) -> None:
    delegate = EightFactorAnalyzer()

    class FailTerminalAnalysis:
        def __init__(self) -> None:
            self.enabled = True

        def analyze_green_frame(self, item):
            if self.enabled and item.frame["time"]["stage"] == 3:
                raise ValueError("injected terminal analysis failure")
            return delegate.analyze_green_frame(item)

    analyzer = FailTerminalAnalysis()
    live, scenario = _new_live(analyzer=analyzer)  # type: ignore[arg-type]
    update = live.setup(42)
    original = live.environment.session.step
    live.environment.session.step = Mock(wraps=original)  # type: ignore[method-assign]
    max_steps = scenario.terminal_rules.max_ticks // 20 + 2

    for _ in range(max_steps):
        actions = [
            value
            for value in (
                _red_controller(update.source_result.red),
                _blue_controller(update.source_result.blue),
            )
            if value is not None
        ]
        try:
            update = live.step(actions, delta_time_seconds=20)
        except ValueError as error:
            assert str(error) == "injected terminal analysis failure"
            break
    else:  # pragma: no cover - protects the terminal fixture contract
        pytest.fail("terminal GREEN frame did not reach injected analyzer")

    assert live.environment.session.result().terminal is True
    assert live.environment.last_bundle.green["time"]["stage"] == 3
    committed_steps = live.environment.session.step.call_count  # type: ignore[attr-defined]
    assert len(live._frame_analyses) == committed_steps
    _assert_current_analysis_desynchronized(live)
    with pytest.raises(LiveAnalysisDesynchronizedError):
        live.step([])
    assert live.environment.session.step.call_count == committed_steps  # type: ignore[attr-defined]

    analyzer.enabled = False
    recovered = live.setup(44)
    assert recovered.frame_index == 0
    assert live.current_timeline.frame_indices == (0,)


def test_canonical_payload_failure_after_step_marks_desynchronized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live, _ = _new_live()
    live.setup(42)
    original_payload = live_module.canonical_green_frame_payload
    original_step = live.environment.session.step
    live.environment.session.step = Mock(  # type: ignore[method-assign]
        wraps=original_step
    )

    def fail_payload(_frame):
        raise TypeError("injected canonical payload failure")

    monkeypatch.setattr(live_module, "canonical_green_frame_payload", fail_payload)
    with pytest.raises(TypeError, match="injected canonical payload failure"):
        live.step([], delta_time_seconds=20)

    assert live.environment.session.step.call_count == 1  # type: ignore[attr-defined]
    assert live.environment.last_bundle.green["time"]["cur_step"] == 20
    assert len(live._frame_analyses) == 1
    _assert_current_analysis_desynchronized(live)

    monkeypatch.setattr(
        live_module,
        "canonical_green_frame_payload",
        original_payload,
    )
    recovered = live.reset(43)
    assert recovered.frame_index == 0


def test_timeline_build_failure_after_step_marks_desynchronized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live, _ = _new_live()
    live.setup(42)
    original_builder = live_module.build_analysis_timeline

    def fail_timeline(_frames):
        raise ValueError("injected timeline failure")

    monkeypatch.setattr(live_module, "build_analysis_timeline", fail_timeline)
    with pytest.raises(ValueError, match="injected timeline failure"):
        live.step([], delta_time_seconds=20)

    assert len(live._frame_analyses) == 1
    _assert_current_analysis_desynchronized(live)
    monkeypatch.setattr(live_module, "build_analysis_timeline", original_builder)
    assert live.reset(43).frame_index == 0


def test_update_copy_failure_after_analysis_commit_does_not_desynchronize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live, _ = _new_live()
    live.setup(42)
    original_clone = live_module.json_clone
    clone_calls = 0

    def fail_first_result_copy(value):
        nonlocal clone_calls
        clone_calls += 1
        if clone_calls == 2:
            raise TypeError("injected update copy failure")
        return original_clone(value)

    monkeypatch.setattr(live_module, "json_clone", fail_first_result_copy)
    with pytest.raises(TypeError, match="injected update copy failure"):
        live.step([], delta_time_seconds=20)

    assert live.current_timeline.frame_indices == (0, 1)
    assert tuple(
        frame.provenance.frame_index for frame in live.frame_analyses
    ) == (0, 1)

    monkeypatch.setattr(live_module, "json_clone", original_clone)
    assert live.step([], delta_time_seconds=20).frame_index == 2


def test_before_setup_current_analysis_accessors_remain_empty() -> None:
    live, _ = _new_live()
    assert live.latest_analysis is None
    assert live.current_timeline == build_analysis_timeline(())
    assert live.frame_analyses == ()
    assert live.analysis_jsonl() == ""


def test_repeated_setup_and_reset_clear_previous_episode() -> None:
    live, _ = _new_live()
    first = live.setup(42)
    live.step([], delta_time_seconds=1)
    assert live.current_timeline.frame_indices == (0, 1)

    second = live.setup(43)
    assert second.frame_index == 0
    assert live.current_timeline.frame_indices == (0,)
    assert first.frame_analysis.provenance.scenario_hash == (
        second.frame_analysis.provenance.scenario_hash
    )

    third = live.reset(44)
    assert third.frame_index == 0
    assert live.current_timeline.frame_indices == (0,)


def test_returned_mutable_values_cannot_pollute_internal_history() -> None:
    live, _ = _new_live()
    update = live.setup(42)
    expected = live.current_timeline

    update.green_frame["operators"][0]["blood"] = -1  # type: ignore[index]
    update.source_result.green["operators"][0]["blood"] = -2  # type: ignore[index]
    evidence = update.frame_analysis.unit_analyses[0].factor_evidence[2]
    assert isinstance(evidence.raw_value, list)
    evidence.raw_value.append("external-mutation")

    assert live.current_timeline == expected
    assert live.latest_analysis == expected.frame_analyses[-1]


def test_green_input_is_not_modified_by_live_analysis() -> None:
    live, _ = _new_live()
    captured: list[dict[str, Any]] = []
    original = live.analyzer.analyze_green_frame

    def inspect(item):
        before = deepcopy(item.frame)
        value = original(item)
        assert item.frame == before
        captured.append(before)
        return value

    live.analyzer = Mock(wraps=live.analyzer)
    live.analyzer.analyze_green_frame.side_effect = inspect
    update = live.setup(42)
    assert captured == [update.green_frame]


def test_live_analysis_does_not_change_trace_observations_result_or_replay(
    tmp_path: Path,
) -> None:
    enabled = _run_episode(tmp_path / "enabled", live_enabled=True)
    repeated = _run_episode(tmp_path / "repeated", live_enabled=True)
    disabled = _run_episode(tmp_path / "disabled", live_enabled=False)

    assert enabled["live"].frame_analyses == repeated["live"].frame_analyses
    assert enabled["live"].analysis_jsonl() == repeated["live"].analysis_jsonl()
    assert enabled["trace"] == disabled["trace"]
    assert enabled["trace"] == repeated["trace"]
    assert enabled["red_blue_views"] == disabled["red_blue_views"]
    assert enabled["red_blue_views"] == repeated["red_blue_views"]
    assert enabled["result"] == disabled["result"]
    assert enabled["result"] == repeated["result"]
    assert enabled["result"]["winner"] == disabled["result"]["winner"]
    assert enabled["replay"].read_bytes() == disabled["replay"].read_bytes()
    assert enabled["replay"].read_bytes() == repeated["replay"].read_bytes()


def test_canonical_payload_helper_preserves_recorder_bytes() -> None:
    live, _ = _new_live()
    update = live.setup(42)
    expected = (
        json.dumps(
            json_clone(update.green_frame),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    assert canonical_green_frame_payload(update.green_frame) == expected


def _new_live(*, analyzer: EightFactorAnalyzer | None = None):
    scenario = load_scenario(SCENARIO_PATH)
    environment = SourceCompatibleEnvironment(
        GameSession(NativeEngineBackend(scenario)),
        template=_template(scenario),
    )
    return LiveAnalyzedSession(environment, analyzer=analyzer), scenario


def _assert_current_analysis_desynchronized(live: LiveAnalyzedSession) -> None:
    readers = (
        lambda: live.latest_analysis,
        lambda: live.current_timeline,
        lambda: live.frame_analyses,
        live.analysis_jsonl,
    )
    for read in readers:
        with pytest.raises(LiveAnalysisDesynchronizedError) as error:
            read()
        assert str(error.value) == "live_analysis_desynchronized_reset_required"


def _run_episode(root: Path, *, live_enabled: bool) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    analyzer = EightFactorAnalyzer(
        model_id="live-offline-model",
        model_version="1.0-test",
        model_hash="model-hash-test",
    )
    live, scenario = _new_live(analyzer=analyzer)
    environment = live.environment
    if live_enabled:
        initial_update = live.setup(42)
        bundle = initial_update.source_result
        updates = [initial_update]
    else:
        bundle = environment.setup_views(42)
        updates = []

    recorder = SourceFrameRecorder(root / "facts")
    recorder.start(
        _recorder_metadata(environment, bundle.green),
        started_at=STARTED_AT,
    )
    recorder.record_initial(
        bundle.green,
        initial_state_hash=environment.initial_state_hash,
    )
    trace: list[dict[str, Any]] = []
    red_blue_views = [
        (canonical_hash(bundle.red), canonical_hash(bundle.blue))
    ]
    max_steps = scenario.terminal_rules.max_ticks // 20 + 2
    for _ in range(max_steps):
        actions = [
            value
            for value in (
                _red_controller(bundle.red),
                _blue_controller(bundle.blue),
            )
            if value is not None
        ]
        if live_enabled:
            update = live.step(actions, delta_time_seconds=20)
            updates.append(update)
            bundle, done = update.source_result, update.done
        else:
            bundle, done = environment.step_views(
                actions,
                delta_time_seconds=20,
            )
        step_record = environment.last_step_record
        assert step_record is not None
        recorder.record_step(bundle.green, step_record)
        red_blue_views.append(
            (canonical_hash(bundle.red), canonical_hash(bundle.blue))
        )
        trace.append({
            "green": canonical_hash(bundle.green),
            "red": red_blue_views[-1][0],
            "blue": red_blue_views[-1][1],
            "step": json_clone(step_record),
        })
        if done:
            break

    result = environment.session.result().to_dict()
    manifest = recorder.finalize(
        result,
        final_state_hash=result["world_hash"],
    )
    replay = recorder.package_zip(root / "replay.zip")
    output: dict[str, Any] = {
        "analyzer": analyzer,
        "live": live if live_enabled else None,
        "updates": tuple(updates),
        "result": result,
        "manifest": manifest,
        "replay": replay,
        "trace": trace,
        "red_blue_views": red_blue_views,
        "scenario_hash": scenario.scenario_hash,
    }
    if live_enabled:
        offline = root / "frame_analysis.native.jsonl"
        analyze_replay_to_jsonl(replay, offline, analyzer=analyzer)
        output["offline"] = offline
        archive = SourceReplayArchiveImporter().import_path(replay)
        output["offline_timeline"] = analyzer.analyze_green_frames(tuple(
            GreenFrameAnalysisInput(
                frame_index=index,
                frame={
                    "operators": [json_clone(item.raw)
                                  for item in frame.operators],
                    "passengers": [json_clone(item.raw)
                                   for item in frame.passengers],
                },
                frame_hash=frame.source_hash,
                scenario_hash=archive.manifest["scenario_hash"],
            )
            for index, frame in enumerate(archive.frames)
        ))
    return output


def _recorder_metadata(
    environment: SourceCompatibleEnvironment,
    green: dict[str, Any],
) -> dict[str, Any]:
    native = green["extra"]["native"]
    scenario = environment.session.scenario
    return {
        "scenario_id": scenario.scenario_id,
        "scenario_hash": scenario.scenario_hash,
        "terrain_id": green["terrain_id"],
        "episode_id": environment.session.session_id,
        "engine_version": ENGINE_VERSION,
        "ruleset_id": native["ruleset_id"],
        "ruleset_version": native["ruleset_version"],
        "ruleset_hash": native["ruleset_hash"],
        "episode_seed": environment.session.seed,
        "max_ticks": environment.session.max_ticks,
    }
