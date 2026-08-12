from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from wargame_native_engine.analysis import (
    ANALYSIS_SCHEMA,
    AnalysisEvent,
    AnalysisSidecarFormatError,
    AnalysisSidecarLoadResult,
    analysis_frames_to_jsonl,
    analysis_sidecar_from_jsonl,
    frame_analysis_from_record,
    frame_analysis_to_record,
)
from wargame_native_engine.analysis.eight_factor_contract import (
    FactorEvidence,
    FactorStatus,
)
from wargame_native_engine.analysis.events import (
    EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
)
from wargame_native_engine.analysis.green_frame_contract import (
    build_green_frame_analysis,
)
from wargame_native_engine.analysis.models import (
    AdvantageAnalysis,
    AnalysisProvenance,
    AnalysisTimeline,
    FactorValue,
    FrameAnalysis,
    SideAnalysis,
    UnitFactorAnalysis,
)
from wargame_native_engine.analysis.timeline import build_analysis_timeline


def _unit(operator_id: int, color: int, *, value: float = 100.0) -> dict:
    return {
        "obj_id": operator_id,
        "color": color,
        "value": value,
        "blood": 100,
        "max_blood": 100,
        "carry_weapon_ids": [7],
        "remain_bullet_nums": 10,
        "weapon_cool_time": 0,
        "weapon_unfold_state": 2,
        "move_state": 0,
        "stop": True,
        "can_to_move": True,
        "on_board": True,
        "see_enemy_bop_ids": [operator_id + 100],
        "keep": 0,
        "tire": 0,
        "lose_control": False,
        "close_combat": False,
    }


def _analysis(
    frame_index: int,
    *,
    red_value: float = 100.0,
    blue_value: float = 100.0,
) -> FrameAnalysis:
    return build_green_frame_analysis(
        {
            "operators": [
                _unit(1, 0, value=red_value),
                _unit(2, 1, value=blue_value),
            ],
            "passengers": [],
        },
        frame_index=frame_index,
        frame_hash=f"frame-{frame_index}",
        scenario_hash="scenario",
        model_id="test-model",
        model_version="1.0",
        model_hash=None,
    )


def _unknown_analysis(frame_index: int) -> FrameAnalysis:
    return build_green_frame_analysis(
        {
            "operators": [
                {"obj_id": 1, "color": 0},
                {"obj_id": 2, "color": 1},
            ],
            "passengers": [],
        },
        frame_index=frame_index,
        frame_hash=f"frame-{frame_index}",
        scenario_hash="scenario",
        model_id="test-model",
        model_version="1.0",
        model_hash=None,
    )


def _event(
    previous_frame_index: int,
    frame_index: int,
    *,
    from_label: str = "RED",
    to_label: str = "BLUE",
    delta: float | None = -0.25,
) -> AnalysisEvent:
    return AnalysisEvent(
        event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
        previous_frame_index=previous_frame_index,
        frame_index=frame_index,
        from_label=from_label,
        to_label=to_label,
        delta_advantage=delta,
    )


def _record(frame_index: int = 0) -> dict[str, object]:
    return frame_analysis_to_record(_analysis(frame_index))


def _jsonl_from_records(records: list[dict[str, object]]) -> str:
    return "\n".join(
        json.dumps(
            record,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        )
        for record in records
    ) + "\n"


def _nested_raw_analysis() -> FrameAnalysis:
    frame = _analysis(0)
    first_unit = frame.unit_analyses[0]
    first_evidence = first_unit.factor_evidence[0]
    nested_evidence = replace(
        first_evidence,
        raw_value={
            "object": {"array": [1, 0.0, True, None, "value"]},
            "empty": [],
        },
    )
    nested_unit = replace(
        first_unit,
        factor_evidence=(nested_evidence, *first_unit.factor_evidence[1:]),
        warnings=("unit warning",),
    )
    return replace(
        frame,
        unit_analyses=(nested_unit, *frame.unit_analyses[1:]),
        warnings=("frame warning",),
    )


def _at(record: object, path: tuple[str | int, ...]) -> object:
    current = record
    for component in path:
        current = current[component]  # type: ignore[index]
    return current


# ── successful model restoration and round trips ────────────────────


def test_record_restores_every_nested_model_and_tuple_field():
    frame = _nested_raw_analysis()
    event = _event(0, 0, delta=0.0)

    restored, events = frame_analysis_from_record(
        frame_analysis_to_record(frame, events=(event,))
    )

    assert isinstance(restored, FrameAnalysis)
    assert isinstance(restored.provenance, AnalysisProvenance)
    assert isinstance(restored.unit_analyses[0], UnitFactorAnalysis)
    assert isinstance(restored.unit_analyses[0].factor_evidence[0], FactorEvidence)
    assert isinstance(restored.unit_analyses[0].factor_values[0], FactorValue)
    assert isinstance(restored.side_analyses[0], SideAnalysis)
    assert isinstance(restored.advantage, AdvantageAnalysis)
    assert restored.unit_analyses[0].factor_values[0].status is FactorStatus.OBSERVED
    assert isinstance(restored.unit_analyses, tuple)
    assert isinstance(restored.unit_analyses[0].factor_evidence, tuple)
    assert isinstance(restored.unit_analyses[0].factor_values, tuple)
    assert isinstance(restored.unit_analyses[0].factor_values[0].source_paths, tuple)
    assert isinstance(restored.unit_analyses[0].factor_values[0].warnings, tuple)
    assert isinstance(restored.side_analyses, tuple)
    assert isinstance(restored.side_analyses[0].unit_ids, tuple)
    assert restored.warnings == ("frame warning",)
    assert restored.provenance.frame_hash == "frame-0"
    assert restored.provenance.model_hash is None
    assert events == (event,)
    assert events[0].delta_advantage == 0.0


def test_record_round_trip_is_exact_for_canonical_json_record():
    frame = _nested_raw_analysis()
    events = (_event(0, 0, delta=None),)
    record = frame_analysis_to_record(frame, events=events)

    restored_frame, restored_events = frame_analysis_from_record(record)

    assert frame_analysis_to_record(
        restored_frame, events=restored_events
    ) == record


def test_raw_value_keeps_json_containers_and_is_deeply_detached():
    record = frame_analysis_to_record(_nested_raw_analysis())
    original = deepcopy(record)
    raw_input = record["unit_analyses"][0]["factor_evidence"][0]["raw_value"]

    restored, _ = frame_analysis_from_record(record)
    raw_output = restored.unit_analyses[0].factor_evidence[0].raw_value

    assert record == original
    assert raw_output == raw_input
    assert isinstance(raw_output, dict)
    assert isinstance(raw_output["object"]["array"], list)
    raw_input["object"]["array"].append("changed")
    assert raw_output["object"]["array"][-1] == "value"
    raw_output["object"]["array"].append("output-only")
    assert "output-only" not in raw_input["object"]["array"]


def test_canonical_jsonl_round_trip_is_byte_for_byte_equal():
    frames = (_analysis(0, red_value=150), _analysis(3, blue_value=150))
    events = (_event(0, 3),)
    payload = analysis_frames_to_jsonl(frames, events=events)

    result = analysis_sidecar_from_jsonl(payload.encode("utf-8"))

    assert analysis_frames_to_jsonl(
        result.timeline.frame_analyses,
        events=result.events,
    ) == payload


def test_jsonl_result_type_and_persisted_event_order():
    frames = (_analysis(0), _analysis(2), _analysis(5))
    events = (
        _event(0, 2, to_label="EVEN", delta=-0.1),
        _event(2, 5, from_label="EVEN", delta=-0.2),
    )
    payload = analysis_frames_to_jsonl(frames, events=events)

    result = analysis_sidecar_from_jsonl(payload)

    assert isinstance(result, AnalysisSidecarLoadResult)
    assert isinstance(result.timeline, AnalysisTimeline)
    assert tuple(
        frame_analysis_to_record(frame)
        for frame in result.timeline.frame_analyses
    ) == tuple(frame_analysis_to_record(frame) for frame in frames)
    assert result.events == events


def test_reader_preserves_duplicate_events_without_generating_or_deduplicating():
    record = frame_analysis_to_record(_analysis(1), events=(_event(0, 1),))
    record["events"].append(deepcopy(record["events"][0]))

    result = analysis_sidecar_from_jsonl(_jsonl_from_records([record]))

    assert result.events == (_event(0, 1), _event(0, 1))


def test_timeline_rebuilds_normalized_delta_unknown_edges_and_recovery():
    frames = (
        _analysis(0, red_value=150),
        _unknown_analysis(2),
        _analysis(5, red_value=200),
        _analysis(9, red_value=250),
    )
    expected = build_analysis_timeline(frames)

    result = analysis_sidecar_from_jsonl(analysis_frames_to_jsonl(frames))

    assert result.timeline.frame_indices == expected.frame_indices
    assert result.timeline.raw_advantages == expected.raw_advantages
    assert result.timeline.normalized_advantages == expected.normalized_advantages
    assert result.timeline.delta_advantages == expected.delta_advantages
    assert tuple(
        frame_analysis_to_record(frame)
        for frame in result.timeline.frame_analyses
    ) == tuple(frame_analysis_to_record(frame) for frame in frames)
    assert result.timeline.frame_indices == (0, 2, 5, 9)
    assert result.timeline.delta_advantages[0] is None
    assert result.timeline.delta_advantages[1] is None
    assert result.timeline.delta_advantages[2] is None
    assert result.timeline.delta_advantages[3] is not None
    assert result.timeline.delta_advantages[3] == (
        result.timeline.normalized_advantages[3]
        - result.timeline.normalized_advantages[2]
    )


def test_reader_does_not_auto_generate_events_for_label_changes():
    frames = (
        _analysis(0, red_value=200, blue_value=100),
        _analysis(1, red_value=100, blue_value=200),
    )

    result = analysis_sidecar_from_jsonl(analysis_frames_to_jsonl(frames))

    assert frames[0].advantage.label != frames[1].advantage.label
    assert result.events == ()


def test_parser_is_deterministic_stateless_and_recovers_after_failure():
    payload = analysis_frames_to_jsonl((_analysis(0),))

    first = analysis_sidecar_from_jsonl(payload)
    second = analysis_sidecar_from_jsonl(payload)
    with pytest.raises(AnalysisSidecarFormatError):
        analysis_sidecar_from_jsonl("not-json\n")
    third = analysis_sidecar_from_jsonl(payload)

    assert first == second == third


# ── exact fields, enum values, identities and model constraints ─────


@pytest.mark.parametrize(
    ("path", "field"),
    [
        ((), "warnings"),
        (("provenance",), "frame_hash"),
        (("unit_analyses", 0), "valid"),
        (("unit_analyses", 0, "factor_evidence", 0), "raw_value"),
        (("unit_analyses", 0, "factor_values", 0), "formula_id"),
        (("side_analyses", 0), "coverage"),
        (("advantage",), "label"),
    ],
)
@pytest.mark.parametrize("operation", ["missing", "extra"])
def test_every_record_object_level_requires_exact_fields(path, field, operation):
    record = _record()
    obj = _at(record, path)
    if operation == "missing":
        del obj[field]
    else:
        obj["unexpected"] = None

    with pytest.raises(AnalysisSidecarFormatError, match=r"at \$"):
        frame_analysis_from_record(record)


def test_event_object_requires_exact_fields():
    record = frame_analysis_to_record(_analysis(1), events=(_event(0, 1),))
    del record["events"][0]["to_label"]

    with pytest.raises(
        AnalysisSidecarFormatError,
        match=r"\$\.events\[0\].*missing fields",
    ):
        frame_analysis_from_record(record)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("analysis_schema",), "unknown/9", "analysis_schema"),
        (("provenance", "perspective"), "BLUE", "perspective"),
        (("unit_analyses", 0, "side"), "RED", "side"),
        (
            ("unit_analyses", 0, "factor_evidence", 0, "status"),
            "observed",
            "FactorStatus",
        ),
        (("unit_analyses", 0, "factor_values", 0, "status"), "MISSING", "FactorStatus"),
        (("advantage", "label"), "TIED", "label"),
    ],
)
def test_unknown_schema_fixed_values_and_enums_are_rejected(path, value, message):
    record = _record()
    obj = _at(record, path[:-1])
    obj[path[-1]] = value

    with pytest.raises(AnalysisSidecarFormatError, match=message):
        frame_analysis_from_record(record)


@pytest.mark.parametrize("field", ["from_label", "to_label"])
def test_invalid_event_labels_are_rejected(field):
    record = frame_analysis_to_record(_analysis(1), events=(_event(0, 1),))
    record["events"][0][field] = "UNKNOWN"

    with pytest.raises(AnalysisSidecarFormatError, match=field):
        frame_analysis_from_record(record)


def test_event_frame_index_must_match_owning_record():
    record = frame_analysis_to_record(_analysis(1), events=(_event(0, 1),))
    record["events"][0]["frame_index"] = 2

    with pytest.raises(
        AnalysisSidecarFormatError,
        match=r"\$\.events\[0\]\.frame_index",
    ):
        frame_analysis_from_record(record)


def test_duplicate_structural_ids_are_rejected():
    record = _record()
    record["unit_analyses"].append(deepcopy(record["unit_analyses"][0]))

    with pytest.raises(AnalysisSidecarFormatError, match="duplicate operator_id"):
        frame_analysis_from_record(record)


@pytest.mark.parametrize("collection", ["factor_evidence", "factor_values"])
def test_duplicate_factor_names_are_rejected(collection):
    record = _record()
    name_field = "name" if collection == "factor_evidence" else "factor_name"
    factors = record["unit_analyses"][0][collection]
    factors[1][name_field] = factors[0][name_field]

    with pytest.raises(AnalysisSidecarFormatError, match="duplicate factor name"):
        frame_analysis_from_record(record)


def test_factor_names_must_keep_canonical_order():
    record = _record()
    factors = record["unit_analyses"][0]["factor_values"]
    factors[0], factors[1] = factors[1], factors[0]

    with pytest.raises(AnalysisSidecarFormatError, match="canonical order"):
        frame_analysis_from_record(record)


def test_duplicate_side_names_and_side_unit_ids_are_rejected():
    duplicate_side = _record()
    duplicate_side["side_analyses"][1]["side"] = "red"
    with pytest.raises(AnalysisSidecarFormatError, match="duplicate side"):
        frame_analysis_from_record(duplicate_side)

    duplicate_unit_id = _record()
    unit_ids = duplicate_unit_id["side_analyses"][0]["unit_ids"]
    unit_ids.append(unit_ids[0])
    with pytest.raises(AnalysisSidecarFormatError, match="duplicate unit id"):
        frame_analysis_from_record(duplicate_unit_id)


# ── exact scalar/container types and finite numbers ─────────────────


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("provenance", "frame_index"), True),
        (("provenance", "frame_index"), 1.0),
        (("provenance", "frame_index"), "1"),
        (("provenance", "frame_index"), -1),
        (("unit_analyses", 0, "operator_id"), False),
        (("unit_analyses", 0, "observed_factor_count"), True),
        (("unit_analyses", 0, "observed_factor_count"), "8"),
        (("unit_analyses", 0, "valid"), 1),
        (("unit_analyses", 0, "effective_power"), True),
        (("unit_analyses", 0, "effective_power"), "1.0"),
        (("side_analyses", 0, "unit_count"), False),
        (("side_analyses", 0, "unit_count"), -1),
        (("side_analyses", 0, "coverage"), "1"),
        (("side_analyses", 0, "unit_ids", 0), True),
        (("advantage", "normalized_advantage"), False),
        (("unit_analyses", 0, "factor_values", 0, "value"), True),
        (("unit_analyses", 0, "factor_values", 0, "value"), "0.5"),
        (("unit_analyses", 0, "factor_values", 0, "source_paths", 0), 1),
        (("warnings",), "warning"),
        (("unit_analyses",), {}),
        (("unit_analyses", 0, "factor_values"), ()),
        (("advantage",), []),
    ],
)
def test_wrong_scalar_and_container_types_are_rejected_without_coercion(path, value):
    record = _record()
    obj = _at(record, path[:-1])
    obj[path[-1]] = value

    with pytest.raises(AnalysisSidecarFormatError):
        frame_analysis_from_record(record)


@pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize(
    "path",
    [
        ("unit_analyses", 0, "factor_values", 0, "value"),
        ("unit_analyses", 0, "effective_power"),
        ("side_analyses", 0, "coverage"),
        ("advantage", "raw_advantage"),
    ],
)
def test_non_finite_analysis_numbers_are_rejected(path, non_finite):
    record = _record()
    obj = _at(record, path[:-1])
    obj[path[-1]] = non_finite

    with pytest.raises(AnalysisSidecarFormatError, match="finite"):
        frame_analysis_from_record(record)


@pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
def test_raw_value_recursively_rejects_non_finite_numbers(non_finite):
    record = _record()
    record["unit_analyses"][0]["factor_evidence"][0]["raw_value"] = {
        "nested": [non_finite]
    }

    with pytest.raises(
        AnalysisSidecarFormatError,
        match=r"raw_value\.nested\[0\].*finite",
    ):
        frame_analysis_from_record(record)


@pytest.mark.parametrize("raw_value", [{1: "bad"}, (1, 2), {"set"}])
def test_raw_value_rejects_non_json_objects_and_non_string_keys(raw_value):
    record = _record()
    record["unit_analyses"][0]["factor_evidence"][0]["raw_value"] = raw_value

    with pytest.raises(AnalysisSidecarFormatError, match="raw_value"):
        frame_analysis_from_record(record)


def test_record_api_rejects_non_string_object_keys():
    record = _record()
    record[1] = None

    with pytest.raises(AnalysisSidecarFormatError, match="object key"):
        frame_analysis_from_record(record)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_type", 1),
        ("previous_frame_index", True),
        ("frame_index", "1"),
        ("delta_advantage", False),
        ("delta_advantage", float("inf")),
    ],
)
def test_event_fields_use_strict_types_and_finite_numbers(field, value):
    record = frame_analysis_to_record(_analysis(1), events=(_event(0, 1),))
    record["events"][0][field] = value

    with pytest.raises(AnalysisSidecarFormatError, match=field):
        frame_analysis_from_record(record)


# ── JSONL lexical boundaries and JSON parser strictness ─────────────


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        "",
        "   ",
        "\n",
        "{}",
        "\n{}\n",
        "{}\n\n{}\n",
        "{}\n\n",
        "{}\r\n",
    ],
)
def test_jsonl_rejects_empty_partial_blank_and_non_lf_shapes(payload):
    with pytest.raises(AnalysisSidecarFormatError):
        analysis_sidecar_from_jsonl(payload)


def test_jsonl_rejects_invalid_utf8():
    with pytest.raises(AnalysisSidecarFormatError, match="UTF-8"):
        analysis_sidecar_from_jsonl(b"\xff\n")


@pytest.mark.parametrize(
    "line",
    [
        "{",
        "{} trailing",
        "[]",
        "null",
        '"record"',
        '{"duplicate":1,"duplicate":2}',
        '{"analysis_schema":NaN}',
        '{"analysis_schema":Infinity}',
        '{"analysis_schema":-Infinity}',
    ],
)
def test_jsonl_rejects_invalid_json_non_objects_duplicates_and_constants(line):
    with pytest.raises(AnalysisSidecarFormatError, match="line 1"):
        analysis_sidecar_from_jsonl(line + "\n")


def test_json_numeric_overflow_to_infinity_is_rejected():
    payload = analysis_frames_to_jsonl((_analysis(0),))
    payload = payload.replace('"raw_advantage":0.0', '"raw_advantage":1e999')

    with pytest.raises(AnalysisSidecarFormatError, match=r"line 1.*finite"):
        analysis_sidecar_from_jsonl(payload)


@pytest.mark.parametrize("payload", [None, 1, [], bytearray(b"{}\n")])
def test_jsonl_api_misuse_uses_type_error(payload):
    with pytest.raises(TypeError, match="str or bytes"):
        analysis_sidecar_from_jsonl(payload)


def test_jsonl_errors_include_one_based_line_and_field_path():
    records = [_record(0), _record(2)]
    records[1]["provenance"]["frame_index"] = True

    with pytest.raises(
        AnalysisSidecarFormatError,
        match=r"line 2, \$\.provenance\.frame_index",
    ):
        analysis_sidecar_from_jsonl(_jsonl_from_records(records))


# ── timeline order and pure-memory batch rules ──────────────────────


@pytest.mark.parametrize("indices", [(1, 1), (2, 1)])
def test_jsonl_rejects_duplicate_and_out_of_order_frame_indices(indices):
    payload = _jsonl_from_records([_record(index) for index in indices])

    with pytest.raises(
        AnalysisSidecarFormatError,
        match=r"line 2, \$\.provenance\.frame_index",
    ):
        analysis_sidecar_from_jsonl(payload)


def test_jsonl_allows_strictly_increasing_gap_without_requiring_zero():
    frames = (_analysis(3), _analysis(10))

    result = analysis_sidecar_from_jsonl(analysis_frames_to_jsonl(frames))

    assert result.timeline.frame_indices == (3, 10)


def test_jsonl_rejects_inconsistent_run_provenance_with_line_context():
    records = [_record(0), _record(1)]
    records[1]["provenance"]["model_version"] = "2.0"

    with pytest.raises(
        AnalysisSidecarFormatError,
        match=r"line 2, \$\.provenance",
    ):
        analysis_sidecar_from_jsonl(_jsonl_from_records(records))


def test_jsonl_does_not_require_contiguous_indices_or_initial_zero():
    frame = _analysis(7)

    result = analysis_sidecar_from_jsonl(analysis_frames_to_jsonl((frame,)))

    assert result.timeline.frame_indices == (7,)
    assert result.timeline.delta_advantages == (None,)
    assert result.events == ()


def test_analysis_schema_constant_is_the_single_serialization_source():
    assert _record()["analysis_schema"] == ANALYSIS_SCHEMA
