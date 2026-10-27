"""Native-state generation path sharing the unchanged Source numeric core."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from .analyzer import GreenFrameAnalysisInput
from .eight_factor_contract import FactorStatus
from .green_frame_contract import build_green_frame_analysis
from .models import AnalysisTimeline, FrameAnalysis
from .native_adapter import (
    NATIVE_MODEL_HASH, NATIVE_MODEL_ID, NATIVE_MODEL_VERSION,
    NATIVE_POLICY_HASH, adapt_native_unit,
)
from .timeline import build_analysis_timeline


@dataclass(frozen=True, slots=True)
class NativeEightFactorAnalyzer:
    """Stateless, explicit Native interpretation; never imputes unknown facts."""

    model_id: str = field(default=NATIVE_MODEL_ID, init=False)
    model_version: str = field(default=NATIVE_MODEL_VERSION, init=False)
    model_hash: str = field(default=NATIVE_MODEL_HASH, init=False)

    def analyze_green_frame(self, item: GreenFrameAnalysisInput) -> FrameAnalysis:
        frame = item.frame
        converted: dict = {}
        evidence = {}
        for collection in ("operators", "passengers"):
            units = frame.get(collection, [])
            if units is None:
                units = []
            if not isinstance(units, (list, tuple)):
                raise TypeError(f"frame[{collection!r}] must be a sequence")
            converted[collection] = []
            for unit in units:
                if not isinstance(unit, Mapping):
                    raise TypeError("Native unit must be a mapping")
                obj_id = unit.get("obj_id")
                if type(obj_id) is not int:
                    raise TypeError("Native obj_id must be an exact int")
                if obj_id in evidence:
                    raise ValueError(f"duplicate obj_id {obj_id} across operators and passengers")
                adapted = adapt_native_unit(unit)
                converted[collection].append(adapted.unit)
                evidence[obj_id] = adapted.evidence
        result = build_green_frame_analysis(
            converted, frame_index=item.frame_index, frame_hash=item.frame_hash,
            scenario_hash=item.scenario_hash, model_id=self.model_id,
            model_version=self.model_version, model_hash=self.model_hash,
        )
        units = []
        for unit in result.unit_analyses:
            evs = evidence[unit.operator_id]
            factors = tuple(replace(
                factor, status=ev.status, source_paths=ev.source_paths,
                missing_fields=ev.missing_fields,
                formula_id=("native-status:suppression-as-loss-control-once/1.0"
                            if ev.name == "status" else factor.formula_id),
                warnings=(*factor.warnings, "DERIVED_NATIVE")
                         if ev.name in {"ammo", "mobility", "status"} else factor.warnings,
            ) for factor, ev in zip(unit.factor_values, evs))
            units.append(replace(
                unit, factor_evidence=evs, factor_values=factors,
                observed_factor_count=sum(ev.status is FactorStatus.OBSERVED for ev in evs),
            ))
        return replace(result, unit_analyses=tuple(units), warnings=(
            *result.warnings, "DERIVED_NATIVE", f"native_input_policy:{NATIVE_POLICY_HASH}",
            "native_status_keep_excluded_by_policy",
        ))

    def analyze_green_frames(self, inputs: Sequence[GreenFrameAnalysisInput]) -> AnalysisTimeline:
        return build_analysis_timeline(tuple(self.analyze_green_frame(item) for item in inputs))
