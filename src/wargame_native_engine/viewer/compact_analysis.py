"""Read-only, bounded eight-factor projections of compact recorded facts.

No simulation, historical imputation, sidecar, or per-frame hash is involved.
The model is fixed per request; the two established arithmetic contracts are
never mixed within a frame or curve.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
import json
from threading import RLock

from wargame_native_engine.analysis.analyzer import EightFactorAnalyzer, GreenFrameAnalysisInput
from wargame_native_engine.analysis.native_analyzer import NativeEightFactorAnalyzer
from wargame_native_engine.analysis.serialization import frame_analysis_to_record
from wargame_native_engine.analysis.timeline import build_analysis_timeline
from wargame_native_engine.compat.source_frame.contract import (
    NATIVE_ANALYSIS_FACTS_SCHEMA_VERSION, has_analysis_facts,
    source_contract_from_manifest, validate_frame_contract,
)
from wargame_native_engine.io.serialization import json_clone

ANALYSIS_CACHE_ENTRIES = 16
ANALYSIS_CACHE_BYTES = 8 * 1024 * 1024


class CompactReplayAnalysis:
    """Small frame LRU plus one numeric-only timeline for each selected model."""

    def __init__(self, reader, frame_indices, first_frame):
        self.reader = reader
        self.frame_indices = frame_indices
        self._analyzers = {"source": EightFactorAnalyzer()}
        self._records = OrderedDict()
        self._record_bytes = 0
        self._record_lock = RLock()
        self._timelines = {}
        self._timeline_locks = {"source": RLock(), "native": RLock()}
        self._contract = None
        self._selection_error = None
        header = reader.manifest
        declared = header.get("analysis_model")
        self.original_model = declared if declared in ("source", "native") else None
        self.default_model = "source"
        self.selection = "default_source"
        self.note = "Source 口径 · 从已记录事实按需重算；原录制分析口径未声明，缺项保持 UNKNOWN。"
        try:
            # A projection version alone is not an original analyzer choice.
            # It only establishes which current Native facts can be interpreted.
            contract = source_contract_from_manifest(header)
            validate_frame_contract(first_frame, contract)
            if has_analysis_facts(contract):
                self._contract = contract
                self._analyzers["native"] = NativeEightFactorAnalyzer()
        except (TypeError, ValueError):
            pass  # Historical raw Source inputs may have no Native declarations.
        if "analysis_model" in header:
            if type(declared) is not str or declared not in self._analyzers:
                self._selection_error = "录制声明的分析口径无效或缺少匹配的 Native 投影证据。"
            else:
                self.default_model = declared
                self.selection = "recorded_model"
                self.note = f"{declared.capitalize()} 口径 · 按录制声明从记录事实重算；缺项保持 UNKNOWN。"
        elif "native" in self._analyzers:
            units = [unit for collection in ("operators", "passengers")
                     for unit in first_frame.get(collection, []) or []]
            complete_facts = bool(units) and all(
                isinstance(unit, Mapping) and isinstance(unit.get("extra"), Mapping)
                and isinstance(unit["extra"].get("native"), Mapping)
                and isinstance(unit["extra"]["native"].get("analysis_facts"), Mapping)
                and unit["extra"]["native"]["analysis_facts"].get("schema_version") == NATIVE_ANALYSIS_FACTS_SCHEMA_VERSION
                for unit in units)
            if complete_facts:
                candidate = self._analyze(self.frame_indices[0], first_frame, "native")
                if candidate.unit_analyses and all(unit.valid for unit in candidate.unit_analyses):
                    self.default_model = "native"
                    self.selection = "recorded_native_facts"
                    self.note = "Native 派生口径 · 依据已记录 Native 事实；原录制分析口径未知。可切换 Source 查看其真实缺项。"

    def model(self, model=None):
        if self._selection_error:
            raise ValueError(self._selection_error)
        model = self.default_model if model is None else model
        if type(model) is not str or model not in self._analyzers:
            raise ValueError("analysis_model must select an available source or native model")
        return model

    def metadata(self):
        analyzer = self._analyzers[self.default_model]
        return {
            "analysis_status": "unavailable" if self._selection_error else "on_demand",
            "analysis_note": self._selection_error or self.note,
            "analysis_model": self.default_model,
            "analysis_selection": self.selection,
            "original_analysis_model": self.original_model,
            "model_provenance": {key: getattr(analyzer, key) for key in ("model_id", "model_version", "model_hash")},
            "analysis_models": [] if self._selection_error else list(self._analyzers),
            "analysis_model_provenances": {name: {key: getattr(value, key) for key in ("model_id", "model_version", "model_hash")}
                                           for name, value in self._analyzers.items()},
        }

    def _analyze(self, index, raw, model):
        # No normalize/export round trip: unknown/null source fields stay unknown.
        if model == "native":
            validate_frame_contract(raw, self._contract)
        native = raw.get("extra", {}).get("native", {})
        if native.get("perspective", "GREEN") != "GREEN":
            raise ValueError("compact analysis requires recorded GREEN facts")
        return self._analyzers[model].analyze_green_frame(GreenFrameAnalysisInput(
            frame_index=index, frame=raw, frame_hash=None,
            scenario_hash=self.reader.manifest.get("scenario_hash"),
        ))

    def record(self, index, ordinal, model=None, raw=None):
        if self._selection_error:
            return {"frame_index": index, "status": "unavailable", "reason": self._selection_error}
        model = self.model(model)
        analyzer = self._analyzers[model]
        key = (model, analyzer.model_version, analyzer.model_hash, index)
        with self._record_lock:
            payload = self._records.get(key)
            if payload is not None:
                self._records.move_to_end(key)
                return json.loads(payload)
            if raw is None:
                raw = self.reader.raw_frame(ordinal)
            record = frame_analysis_to_record(self._analyze(index, raw, model))
            payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(payload) <= ANALYSIS_CACHE_BYTES:
                while self._records and (len(self._records) >= ANALYSIS_CACHE_ENTRIES
                        or self._record_bytes + len(payload) > ANALYSIS_CACHE_BYTES):
                    _, removed = self._records.popitem(last=False)
                    self._record_bytes -= len(removed)
                self._records[key] = payload
                self._record_bytes += len(payload)
            return record

    def timeline(self, model=None):
        if self._selection_error:
            return {"frame_indices": list(self.frame_indices), "analysis_status": "unavailable",
                    "analysis_note": self._selection_error}
        model = self.model(model)
        # This lock covers only this model's curve. Individual-frame requests
        # and the other model remain responsive while the curve is calculated.
        with self._timeline_locks[model]:
            if model in self._timelines:
                return json_clone(self._timelines[model])
            result = {name: [] for name in (
                "frame_indices", "red_effective_power", "blue_effective_power",
                "raw_advantages", "normalized_advantages", "delta_advantages", "advantage_labels")}
            previous = None
            # One linear decode pass, not repeated seek/reconstruction from each
            # checkpoint. Retain at most two full analyses, never all unit trees.
            for index, raw in zip(self.frame_indices, self.reader.iter_raw_frames()):
                frame = self._analyze(index, raw, model)
                pair = build_analysis_timeline((frame,) if previous is None else (previous, frame))
                sides = {side.side: side for side in frame.side_analyses}
                result["frame_indices"].append(index)
                result["red_effective_power"].append(sides["red"].effective_power)
                result["blue_effective_power"].append(sides["blue"].effective_power)
                result["raw_advantages"].append(frame.advantage.raw_advantage)
                result["normalized_advantages"].append(frame.advantage.normalized_advantage)
                result["delta_advantages"].append(pair.delta_advantages[-1])
                result["advantage_labels"].append(frame.advantage.label)
                previous = frame
            result.update(analysis_status="available", analysis_model=model,
                          analysis_note="从记录事实派生；未写分析侧文件或逐帧 hash。")
            self._timelines[model] = result
            return json_clone(result)
