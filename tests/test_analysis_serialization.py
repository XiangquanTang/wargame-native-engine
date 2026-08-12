from __future__ import annotations

import json
from copy import deepcopy

import pytest

from wargame_native_engine.analysis import (
    ANALYSIS_SCHEMA,
    AnalysisEvent,
    frame_analysis_to_record,
)
from wargame_native_engine.analysis.eight_factor_contract import (
    FACTOR_ORDER,
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
    FactorValue,
    FrameAnalysis,
    SideAnalysis,
    UnitFactorAnalysis,
)


# ── helpers ───────────────────────────────────────────────────────────


def _unit(obj_id: int, color: int, **overrides) -> dict:
    base = {
        "obj_id": obj_id,
        "color": color,
        "type": 2,
        "sub_type": 7,
        "cur_hex": 100 + obj_id,
        "blood": 100,
        "max_blood": 100,
        "value": 8500,
        "carry_weapon_ids": [42],
        "remain_bullet_nums": 30,
        "weapon_cool_time": 0,
        "weapon_unfold_state": 2,
        "move_state": 0,
        "stop": 1,
        "can_to_move": True,
        "on_board": False,
        "see_enemy_bop_ids": [201, 202],
        "keep": 0,
        "tire": 0,
        "lose_control": False,
        "close_combat": False,
    }
    base.update(overrides)
    return base


def _green_frame(**overrides) -> dict:
    return {"operators": [_unit(1, 0), _unit(2, 1)], "passengers": [], **overrides}


# ── 1. real build_green_frame_analysis output ────────────────────────


def test_real_frame_analysis_to_record():
    """build_green_frame_analysis → frame_analysis_to_record round-trip."""
    frame = _green_frame()
    analysis = build_green_frame_analysis(frame, frame_index=0)
    record = frame_analysis_to_record(analysis)

    assert isinstance(record, dict)
    assert record["analysis_schema"] == ANALYSIS_SCHEMA

    # top-level keys
    for key in (
        "analysis_schema", "provenance", "unit_analyses",
        "side_analyses", "advantage", "events", "warnings",
    ):
        assert key in record, f"missing top-level key {key!r}"


# ── 2. complete nested field verification ────────────────────────────


def test_provenance_fields_in_record():
    analysis = build_green_frame_analysis(_green_frame(), frame_index=7)
    record = frame_analysis_to_record(analysis)
    p = record["provenance"]
    assert p["perspective"] == "GREEN"
    assert p["frame_index"] == 7
    assert "frame_hash" in p
    assert "scenario_hash" in p
    assert p["model_id"] == "native-eight-factor"
    assert p["model_version"] == "0.1.0-contract"
    assert "model_hash" in p


def test_unit_analysis_fields_complete():
    analysis = build_green_frame_analysis(_green_frame(), frame_index=0)
    record = frame_analysis_to_record(analysis)
    ua = record["unit_analyses"][0]

    for key in (
        "operator_id", "side", "factor_evidence", "factor_values",
        "effective_power", "valid", "observed_factor_count", "warnings",
    ):
        assert key in ua, f"missing unit_analysis key {key!r}"

    assert isinstance(ua["factor_evidence"], list)
    assert len(ua["factor_evidence"]) == 8
    assert isinstance(ua["factor_values"], list)
    assert len(ua["factor_values"]) == 8


def test_factor_evidence_fields_complete():
    analysis = build_green_frame_analysis(_green_frame(), frame_index=0)
    record = frame_analysis_to_record(analysis)
    ev = record["unit_analyses"][0]["factor_evidence"][0]

    for key in ("name", "raw_value", "source_paths", "status", "missing_fields"):
        assert key in ev, f"missing factor_evidence key {key!r}"

    # FactorStatus is serialized as string
    assert isinstance(ev["status"], str)
    assert ev["status"] in ("OBSERVED", "UNKNOWN")
    # source_paths and missing_fields are JSON arrays
    assert isinstance(ev["source_paths"], list)
    assert isinstance(ev["missing_fields"], list)


def test_factor_value_fields_complete():
    analysis = build_green_frame_analysis(_green_frame(), frame_index=0)
    record = frame_analysis_to_record(analysis)
    fv = record["unit_analyses"][0]["factor_values"][0]

    for key in (
        "factor_name", "value", "status", "source_paths",
        "formula_id", "warnings", "missing_fields",
    ):
        assert key in fv, f"missing factor_value key {key!r}"

    assert isinstance(fv["status"], str)
    assert isinstance(fv["source_paths"], list)
    assert isinstance(fv["warnings"], list)
    assert isinstance(fv["missing_fields"], list)


def test_side_analysis_fields_complete():
    analysis = build_green_frame_analysis(_green_frame(), frame_index=0)
    record = frame_analysis_to_record(analysis)
    sa = record["side_analyses"][0]

    for key in (
        "side", "unit_ids", "unit_count", "observed_unit_count",
        "effective_power", "coverage", "confidence", "warnings",
    ):
        assert key in sa, f"missing side_analysis key {key!r}"

    assert isinstance(sa["unit_ids"], list)


def test_advantage_fields_complete():
    analysis = build_green_frame_analysis(_green_frame(), frame_index=0)
    record = frame_analysis_to_record(analysis)
    adv = record["advantage"]

    for key in (
        "raw_advantage", "normalized_advantage", "label",
        "confidence", "warnings",
    ):
        assert key in adv, f"missing advantage key {key!r}"

    assert adv["label"] in ("RED", "BLUE", "EVEN", "UNKNOWN")


def test_factor_evidence_order_matches_factor_order():
    analysis = build_green_frame_analysis(_green_frame(), frame_index=0)
    record = frame_analysis_to_record(analysis)

    for ua in record["unit_analyses"]:
        names = [ev["name"] for ev in ua["factor_evidence"]]
        assert names == list(FACTOR_ORDER)


# ── 3. UNKNOWN / None handling ───────────────────────────────────────


def test_unknown_unit_produces_null_effective_power():
    # Unit missing most fields → factors are UNKNOWN → effective_power is None
    frame = _green_frame(
        operators=[{"obj_id": 99, "color": 0, "value": 100}],
        passengers=[],
    )
    analysis = build_green_frame_analysis(frame, frame_index=0)
    record = frame_analysis_to_record(analysis)

    ua = record["unit_analyses"][0]
    assert ua["effective_power"] is None
    assert ua["valid"] is False

    # UNKNOWN factor values are null
    for fv in ua["factor_values"]:
        if fv["status"] == "UNKNOWN":
            assert fv["value"] is None, fv["factor_name"]


def test_empty_frame_produces_valid_record():
    frame: dict = {}
    analysis = build_green_frame_analysis(frame, frame_index=0)
    record = frame_analysis_to_record(analysis)

    assert record["unit_analyses"] == []
    assert record["side_analyses"][0]["unit_count"] == 0
    assert record["advantage"]["label"] == "EVEN"


# ── 4. events inclusion ──────────────────────────────────────────────


def test_events_appear_in_record():
    analysis = build_green_frame_analysis(_green_frame(), frame_index=5)
    events: tuple[AnalysisEvent, ...] = (
        AnalysisEvent(
            event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
            previous_frame_index=3,
            frame_index=5,
            from_label="RED",
            to_label="BLUE",
            delta_advantage=-0.25,
        ),
    )
    record = frame_analysis_to_record(analysis, events=events)

    assert len(record["events"]) == 1
    e = record["events"][0]
    assert e["event_type"] == EVENT_TYPE_ADVANTAGE_LABEL_CHANGE
    assert e["previous_frame_index"] == 3
    assert e["frame_index"] == 5
    assert e["from_label"] == "RED"
    assert e["to_label"] == "BLUE"
    assert e["delta_advantage"] == pytest.approx(-0.25)


def test_empty_events_default():
    analysis = build_green_frame_analysis(_green_frame(), frame_index=0)
    record = frame_analysis_to_record(analysis)
    assert record["events"] == []


# ── 5. event 归属错误 ────────────────────────────────────────────────


def test_event_wrong_frame_index_raises():
    analysis = build_green_frame_analysis(_green_frame(), frame_index=0)
    bad_event = AnalysisEvent(
        event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
        previous_frame_index=0,
        frame_index=99,  # does not match analysis.provenance.frame_index (0)
        from_label="RED",
        to_label="BLUE",
        delta_advantage=0.1,
    )
    with pytest.raises(ValueError, match="does not match"):
        frame_analysis_to_record(analysis, events=(bad_event,))


# ── 6. raw_value deep isolation ──────────────────────────────────────


def test_raw_value_is_deepcopy_isolated():
    analysis = build_green_frame_analysis(
        _green_frame(operators=[_unit(1, 0, carry_weapon_ids=[42, 43])]),
        frame_index=0,
    )
    # Get the original raw_value from the analysis for comparison
    orig_weapon_ev = analysis.unit_analyses[0].factor_evidence[2]  # weapon
    orig_raw = orig_weapon_ev.raw_value

    record = frame_analysis_to_record(analysis)
    record_raw = record["unit_analyses"][0]["factor_evidence"][2]["raw_value"]

    # Same content
    assert record_raw == list(orig_raw) if isinstance(orig_raw, tuple) else orig_raw

    # Mutating the record's raw_value must not affect the original
    if isinstance(record_raw, list):
        record_raw.append(999)
        assert list(orig_raw) != record_raw  # orig is unchanged


# ── 7. non-JSON-safe values ──────────────────────────────────────────


def test_non_json_safe_raw_value_raises():
    """A raw_value that is not JSON-serializable must fail explicitly."""
    bad_evidence = FactorEvidence(
        name="base_value",
        raw_value={1, 2, 3},  # set — not JSON-serializable
        source_paths=("unit.value",),
        status=FactorStatus.OBSERVED,
        missing_fields=(),
    )
    bad_unit = UnitFactorAnalysis(
        operator_id=1,
        side="red",
        factor_evidence=(bad_evidence,),
        factor_values=(),
        effective_power=None,
        valid=False,
        observed_factor_count=0,
        warnings=(),
    )
    frame = FrameAnalysis(
        provenance=AnalysisProvenance(
            perspective="GREEN", frame_index=0, frame_hash=None,
            scenario_hash=None, model_id="test", model_version="1",
            model_hash=None,
        ),
        unit_analyses=(bad_unit,),
        side_analyses=(),
        advantage=AdvantageAnalysis(
            raw_advantage=0.0, normalized_advantage=0.0,
            label="EVEN", confidence=None, warnings=(),
        ),
        warnings=(),
    )

    with pytest.raises((TypeError, ValueError), match="JSON"):
        frame_analysis_to_record(frame)


def test_dict_with_object_key_raises():
    """raw_value dict with an object key must fail explicitly."""
    bad_evidence = FactorEvidence(
        name="base_value",
        raw_value={object(): "value"},  # object key — not a string
        source_paths=("unit.value",),
        status=FactorStatus.OBSERVED,
        missing_fields=(),
    )
    frame = FrameAnalysis(
        provenance=AnalysisProvenance(
            perspective="GREEN", frame_index=0, frame_hash=None,
            scenario_hash=None, model_id="test", model_version="1",
            model_hash=None,
        ),
        unit_analyses=(
            UnitFactorAnalysis(
                operator_id=1, side="red",
                factor_evidence=(bad_evidence,),
                factor_values=(),
                effective_power=None, valid=False,
                observed_factor_count=0, warnings=(),
            ),
        ),
        side_analyses=(),
        advantage=AdvantageAnalysis(
            raw_advantage=0.0, normalized_advantage=0.0,
            label="EVEN", confidence=None, warnings=(),
        ),
        warnings=(),
    )

    with pytest.raises(TypeError, match="Non-string dict key"):
        frame_analysis_to_record(frame)


def test_dict_with_int_key_raises():
    """raw_value dict with an int key must fail explicitly."""
    bad_evidence = FactorEvidence(
        name="base_value",
        raw_value={1: "value"},  # int key
        source_paths=("unit.value",),
        status=FactorStatus.OBSERVED,
        missing_fields=(),
    )
    frame = FrameAnalysis(
        provenance=AnalysisProvenance(
            perspective="GREEN", frame_index=0, frame_hash=None,
            scenario_hash=None, model_id="test", model_version="1",
            model_hash=None,
        ),
        unit_analyses=(
            UnitFactorAnalysis(
                operator_id=1, side="red",
                factor_evidence=(bad_evidence,),
                factor_values=(),
                effective_power=None, valid=False,
                observed_factor_count=0, warnings=(),
            ),
        ),
        side_analyses=(),
        advantage=AdvantageAnalysis(
            raw_advantage=0.0, normalized_advantage=0.0,
            label="EVEN", confidence=None, warnings=(),
        ),
        warnings=(),
    )

    with pytest.raises(TypeError, match="Non-string dict key"):
        frame_analysis_to_record(frame)


def test_nested_dict_with_non_string_key_raises():
    """Nested dict with a non-string key must fail with path info."""
    bad_evidence = FactorEvidence(
        name="base_value",
        raw_value={"outer": {"inner": "ok", 99: "bad"}},  # nested int key
        source_paths=("unit.value",),
        status=FactorStatus.OBSERVED,
        missing_fields=(),
    )
    frame = FrameAnalysis(
        provenance=AnalysisProvenance(
            perspective="GREEN", frame_index=0, frame_hash=None,
            scenario_hash=None, model_id="test", model_version="1",
            model_hash=None,
        ),
        unit_analyses=(
            UnitFactorAnalysis(
                operator_id=1, side="red",
                factor_evidence=(bad_evidence,),
                factor_values=(),
                effective_power=None, valid=False,
                observed_factor_count=0, warnings=(),
            ),
        ),
        side_analyses=(),
        advantage=AdvantageAnalysis(
            raw_advantage=0.0, normalized_advantage=0.0,
            label="EVEN", confidence=None, warnings=(),
        ),
        warnings=(),
    )

    with pytest.raises(TypeError, match="Non-string dict key"):
        frame_analysis_to_record(frame)


def test_dict_with_string_keys_preserved():
    """raw_value dict with string keys passes through unchanged."""
    original = {
        "key1": "val1",
        "key2": [1, 2, 3],
        "key3": (4, 5),         # tuple → list
        "key4": {"nested": 6},  # nested dict with string key
    }
    evidence = FactorEvidence(
        name="base_value",
        raw_value=original,
        source_paths=("unit.value",),
        status=FactorStatus.OBSERVED,
        missing_fields=(),
    )
    frame = FrameAnalysis(
        provenance=AnalysisProvenance(
            perspective="GREEN", frame_index=0, frame_hash=None,
            scenario_hash=None, model_id="test", model_version="1",
            model_hash=None,
        ),
        unit_analyses=(
            UnitFactorAnalysis(
                operator_id=1, side="red",
                factor_evidence=(evidence,),
                factor_values=(),
                effective_power=None, valid=False,
                observed_factor_count=0, warnings=(),
            ),
        ),
        side_analyses=(),
        advantage=AdvantageAnalysis(
            raw_advantage=0.0, normalized_advantage=0.0,
            label="EVEN", confidence=None, warnings=(),
        ),
        warnings=(),
    )

    record = frame_analysis_to_record(frame)
    rv = record["unit_analyses"][0]["factor_evidence"][0]["raw_value"]

    # String keys preserved, values correct
    assert rv["key1"] == "val1"
    assert rv["key2"] == [1, 2, 3]
    assert rv["key3"] == [4, 5]          # tuple → list
    assert rv["key4"] == {"nested": 6}   # nested dict preserved


def test_dict_raw_value_mutation_isolation():
    """Mutating returned raw_value dict must not affect original evidence."""
    original = {"items": [1, 2, 3], "label": "test"}
    evidence = FactorEvidence(
        name="base_value",
        raw_value=original,
        source_paths=("unit.value",),
        status=FactorStatus.OBSERVED,
        missing_fields=(),
    )
    frame = FrameAnalysis(
        provenance=AnalysisProvenance(
            perspective="GREEN", frame_index=0, frame_hash=None,
            scenario_hash=None, model_id="test", model_version="1",
            model_hash=None,
        ),
        unit_analyses=(
            UnitFactorAnalysis(
                operator_id=1, side="red",
                factor_evidence=(evidence,),
                factor_values=(),
                effective_power=None, valid=False,
                observed_factor_count=0, warnings=(),
            ),
        ),
        side_analyses=(),
        advantage=AdvantageAnalysis(
            raw_advantage=0.0, normalized_advantage=0.0,
            label="EVEN", confidence=None, warnings=(),
        ),
        warnings=(),
    )

    record = frame_analysis_to_record(frame)
    rv = record["unit_analyses"][0]["factor_evidence"][0]["raw_value"]

    # Mutate the returned dict
    rv["items"].append(999)
    rv["new_key"] = "should not appear in original"

    # Original evidence is untouched
    assert evidence.raw_value == {"items": [1, 2, 3], "label": "test"}


def test_non_string_key_error_includes_path():
    """Error message must include the path and the illegal key repr."""
    bad_evidence = FactorEvidence(
        name="base_value",
        raw_value={42: "bad"},
        source_paths=("unit.value",),
        status=FactorStatus.OBSERVED,
        missing_fields=(),
    )
    frame = FrameAnalysis(
        provenance=AnalysisProvenance(
            perspective="GREEN", frame_index=0, frame_hash=None,
            scenario_hash=None, model_id="test", model_version="1",
            model_hash=None,
        ),
        unit_analyses=(
            UnitFactorAnalysis(
                operator_id=1, side="red",
                factor_evidence=(bad_evidence,),
                factor_values=(),
                effective_power=None, valid=False,
                observed_factor_count=0, warnings=(),
            ),
        ),
        side_analyses=(),
        advantage=AdvantageAnalysis(
            raw_advantage=0.0, normalized_advantage=0.0,
            label="EVEN", confidence=None, warnings=(),
        ),
        warnings=(),
    )

    with pytest.raises(TypeError) as exc_info:
        frame_analysis_to_record(frame)

    msg = str(exc_info.value)
    assert "raw_value" in msg
    assert "42" in msg


def test_nan_float_raises():
    frame = FrameAnalysis(
        provenance=AnalysisProvenance(
            perspective="GREEN", frame_index=0, frame_hash=None,
            scenario_hash=None, model_id="test", model_version="1",
            model_hash=None,
        ),
        unit_analyses=(),
        side_analyses=(),
        advantage=AdvantageAnalysis(
            raw_advantage=float("nan"),
            normalized_advantage=float("nan"),
            label="UNKNOWN",
            confidence=None,
            warnings=(),
        ),
        warnings=(),
    )

    with pytest.raises(ValueError, match="Non-finite float"):
        frame_analysis_to_record(frame)


def test_infinity_float_raises():
    frame = FrameAnalysis(
        provenance=AnalysisProvenance(
            perspective="GREEN", frame_index=0, frame_hash=None,
            scenario_hash=None, model_id="test", model_version="1",
            model_hash=None,
        ),
        unit_analyses=(),
        side_analyses=(),
        advantage=AdvantageAnalysis(
            raw_advantage=float("inf"),
            normalized_advantage=float("inf"),
            label="UNKNOWN",
            confidence=None,
            warnings=(),
        ),
        warnings=(),
    )

    with pytest.raises(ValueError, match="Non-finite float"):
        frame_analysis_to_record(frame)


def test_negative_infinity_float_raises():
    frame = FrameAnalysis(
        provenance=AnalysisProvenance(
            perspective="GREEN", frame_index=0, frame_hash=None,
            scenario_hash=None, model_id="test", model_version="1",
            model_hash=None,
        ),
        unit_analyses=(),
        side_analyses=(),
        advantage=AdvantageAnalysis(
            raw_advantage=float("-inf"),
            normalized_advantage=float("-inf"),
            label="UNKNOWN",
            confidence=None,
            warnings=(),
        ),
        warnings=(),
    )

    with pytest.raises(ValueError, match="Non-finite float"):
        frame_analysis_to_record(frame)


# ── 9. event ordering ────────────────────────────────────────────────


def test_events_sorted_by_fixed_fields():
    analysis = build_green_frame_analysis(_green_frame(), frame_index=5)
    events: tuple[AnalysisEvent, ...] = (
        AnalysisEvent(
            event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
            previous_frame_index=4, frame_index=5,
            from_label="BLUE", to_label="EVEN",
            delta_advantage=0.05,
        ),
        AnalysisEvent(
            event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
            previous_frame_index=3, frame_index=5,
            from_label="RED", to_label="BLUE",
            delta_advantage=-0.15,
        ),
    )
    record = frame_analysis_to_record(analysis, events=events)
    assert len(record["events"]) == 2
    # Sorted by (previous_frame_index, ...) → prev=3 first, prev=4 second
    assert record["events"][0]["previous_frame_index"] == 3
    assert record["events"][1]["previous_frame_index"] == 4


# ── 10. input unchanged & determinism ─────────────────────────────────


def test_frame_input_not_modified():
    frame = _green_frame()
    analysis = build_green_frame_analysis(frame, frame_index=0)
    before = deepcopy(analysis)

    frame_analysis_to_record(analysis)

    assert analysis == before


def test_repeated_calls_produce_same_result():
    analysis = build_green_frame_analysis(_green_frame(), frame_index=0)
    r1 = frame_analysis_to_record(analysis)
    r2 = frame_analysis_to_record(analysis)
    assert r1 == r2


# ── 11. json.dumps round-trip ────────────────────────────────────────


def test_output_passes_json_dumps():
    analysis = build_green_frame_analysis(_green_frame(), frame_index=0)
    record = frame_analysis_to_record(analysis)

    # Must not raise
    text = json.dumps(record, sort_keys=True, allow_nan=False)
    # Round-trip back
    reloaded = json.loads(text)
    assert reloaded == record


def test_json_dumps_with_events_passes():
    analysis = build_green_frame_analysis(_green_frame(), frame_index=0)
    events: tuple[AnalysisEvent, ...] = (
        AnalysisEvent(
            event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
            previous_frame_index=0, frame_index=0,
            from_label="EVEN", to_label="RED",
            delta_advantage=0.3,
        ),
    )
    record = frame_analysis_to_record(analysis, events=events)
    text = json.dumps(record, sort_keys=True, allow_nan=False)
    reloaded = json.loads(text)
    assert reloaded == record


def test_warnings_are_json_arrays():
    analysis = build_green_frame_analysis(_green_frame(), frame_index=0)
    record = frame_analysis_to_record(analysis)
    assert isinstance(record["warnings"], list)
    for sa in record["side_analyses"]:
        assert isinstance(sa["warnings"], list)


# ── 12. package-level exports ────────────────────────────────────────


def test_analysis_schema_constant_exported():
    assert ANALYSIS_SCHEMA == "native-eight-factor-frame-analysis/1.0"


def test_frame_analysis_to_record_exported():
    from wargame_native_engine.analysis.serialization import (
        frame_analysis_to_record as direct_import,
    )
    # The package-level export must be the same function
    assert frame_analysis_to_record is direct_import


# ── edge: non-contiguous frame_index & multi-event ───────────────────


def test_multi_events_for_same_frame():
    """Multiple events arriving at the same target frame are all recorded."""
    analysis = build_green_frame_analysis(_green_frame(), frame_index=2)
    events: tuple[AnalysisEvent, ...] = (
        AnalysisEvent(
            event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
            previous_frame_index=0, frame_index=2,
            from_label="RED", to_label="EVEN",
            delta_advantage=-0.1,
        ),
        AnalysisEvent(
            event_type=EVENT_TYPE_ADVANTAGE_LABEL_CHANGE,
            previous_frame_index=1, frame_index=2,
            from_label="EVEN", to_label="BLUE",
            delta_advantage=-0.2,
        ),
    )
    record = frame_analysis_to_record(analysis, events=events)
    assert len(record["events"]) == 2


def test_unit_analyses_are_json_arrays():
    analysis = build_green_frame_analysis(_green_frame(), frame_index=0)
    record = frame_analysis_to_record(analysis)
    assert isinstance(record["unit_analyses"], list)
    assert isinstance(record["side_analyses"], list)


def test_side_analyses_are_always_two():
    analysis = build_green_frame_analysis(_green_frame(), frame_index=0)
    record = frame_analysis_to_record(analysis)
    assert len(record["side_analyses"]) == 2
    assert record["side_analyses"][0]["side"] == "red"
    assert record["side_analyses"][1]["side"] == "blue"
