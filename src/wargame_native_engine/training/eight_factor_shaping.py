"""Pure EightFactor potential shaping over detached committed frame analyses."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import math

from wargame_native_engine.analysis.analyzer import (
    EightFactorAnalyzer,
    GreenFrameAnalysisInput,
)
from wargame_native_engine.analysis.models import FrameAnalysis, SideAnalysis
from wargame_native_engine.analysis.semantic_identity import (
    AnalyzerAuthorityMetadata,
    DEFAULT_ANALYZER_AUTHORITY,
    require_analyzer_authority,
)
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.training.discount import SharedTrainingDiscountConfig
from wargame_native_engine.training.eight_factor_shaping_contract import (
    EightFactorShapingPolicy,
    EightFactorShapingPolicyIdentity,
    require_shaping_prerequisites,
)
from wargame_native_engine.training.reward import (
    BaseRewardPolicy,
    JointTrainingRewardRecord,
    RewardComponent,
    RewardContractError,
    RewardPolicyIdentity,
    RewardSupervisionSource,
    TrainingRewardRecord,
)


EIGHT_FACTOR_POTENTIAL_SCHEMA_VERSION = "native-eight-factor-potential/1.0"
EIGHT_FACTOR_SHAPING_TRANSITION_SCHEMA_VERSION = (
    "native-eight-factor-shaping-transition/1.0"
)
COMPOSITE_REWARD_POLICY_SCHEMA_VERSION = (
    "native-training-composite-reward-policy/1.0"
)
COMPOSITE_REWARD_POLICY_ID = "native-base-plus-eight-factor-potential"
COMPOSITE_REWARD_POLICY_VERSION = "1.0.0"


class EightFactorShapingError(RewardContractError):
    """EightFactor shaping evidence violates its fail-closed contract."""


@dataclass(frozen=True, slots=True)
class CompositeRewardPolicy:
    schema_version: str
    policy_id: str
    policy_version: str
    base: RewardPolicyIdentity
    shaping: EightFactorShapingPolicyIdentity | None
    shaping_enabled: bool
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != COMPOSITE_REWARD_POLICY_SCHEMA_VERSION:
            raise EightFactorShapingError("unsupported composite reward schema")
        if self.policy_id != COMPOSITE_REWARD_POLICY_ID:
            raise EightFactorShapingError("unsupported composite reward policy id")
        if self.policy_version != COMPOSITE_REWARD_POLICY_VERSION:
            raise EightFactorShapingError(
                "unsupported composite reward policy version"
            )
        if not isinstance(self.base, RewardPolicyIdentity):
            raise TypeError("base must be RewardPolicyIdentity")
        if type(self.shaping_enabled) is not bool:
            raise TypeError("shaping_enabled must be exact bool")
        if self.shaping_enabled != (self.shaping is not None):
            raise EightFactorShapingError("composite shaping identity mismatch")
        object.__setattr__(self, "content_hash", canonical_hash(
            self.semantic_payload()
        ))

    @property
    def identity(self) -> RewardPolicyIdentity:
        return RewardPolicyIdentity(
            self.policy_id, self.policy_version, self.content_hash
        )

    def semantic_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "base": self.base.to_dict(),
            "shaping": None if self.shaping is None else self.shaping.to_dict(),
            "shaping_enabled": self.shaping_enabled,
        }

    def to_dict(self) -> dict[str, object]:
        result = self.semantic_payload()
        result["composite_hash"] = self.content_hash
        return result


def build_composite_reward_policy(
    base: BaseRewardPolicy,
    shaping: EightFactorShapingPolicy | None,
) -> CompositeRewardPolicy:
    if not isinstance(base, BaseRewardPolicy):
        raise TypeError("base must be BaseRewardPolicy")
    if shaping is not None and not isinstance(shaping, EightFactorShapingPolicy):
        raise TypeError("shaping must be EightFactorShapingPolicy or None")
    return CompositeRewardPolicy(
        schema_version=COMPOSITE_REWARD_POLICY_SCHEMA_VERSION,
        policy_id=COMPOSITE_REWARD_POLICY_ID,
        policy_version=COMPOSITE_REWARD_POLICY_VERSION,
        base=base.identity,
        shaping=None if shaping is None else shaping.identity,
        shaping_enabled=shaping is not None,
    )


@dataclass(frozen=True, slots=True)
class EightFactorPotentialRecord:
    schema_version: str
    analyzer: AnalyzerAuthorityMetadata
    frame_index: int
    frame_hash: str
    scenario_hash: str
    phi_red: float
    coverage_red: float
    coverage_blue: float
    privileged_supervision_used: bool
    semantic_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != EIGHT_FACTOR_POTENTIAL_SCHEMA_VERSION:
            raise EightFactorShapingError("unsupported EightFactor potential schema")
        require_analyzer_authority(self.analyzer)
        if type(self.frame_index) is not int or self.frame_index < 0:
            raise EightFactorShapingError("potential frame index must be nonnegative")
        _sha256(self.frame_hash, "potential frame hash")
        _sha256(self.scenario_hash, "potential scenario hash")
        phi = _finite(self.phi_red, "phi_red")
        if phi < -1.0 or phi > 1.0:
            raise EightFactorShapingError("potential is outside [-1,1]")
        object.__setattr__(self, "phi_red", phi)
        for name in ("coverage_red", "coverage_blue"):
            if _finite(getattr(self, name), name) != 1.0:
                raise EightFactorShapingError("potential requires complete coverage")
        if self.privileged_supervision_used is not True:
            raise EightFactorShapingError("EightFactor potential must be privileged")
        object.__setattr__(self, "semantic_hash", canonical_hash(
            self.semantic_payload()
        ))

    @property
    def phi_blue(self) -> float:
        return -self.phi_red

    def semantic_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "analyzer": self.analyzer.to_dict(),
            "frame_identity": {
                "frame_index": self.frame_index,
                "frame_hash": self.frame_hash,
                "scenario_hash": self.scenario_hash,
            },
            "phi_red": self.phi_red,
            "phi_blue": self.phi_blue,
            "coverage_red": self.coverage_red,
            "coverage_blue": self.coverage_blue,
            "privileged_supervision_used": self.privileged_supervision_used,
        }

    def to_dict(self) -> dict[str, object]:
        result = self.semantic_payload()
        result["semantic_hash"] = self.semantic_hash
        return result


def derive_eight_factor_potential(
    frame: GreenFrameAnalysisInput,
    *,
    analyzer: EightFactorAnalyzer | None = None,
    authority: AnalyzerAuthorityMetadata = DEFAULT_ANALYZER_AUTHORITY,
) -> EightFactorPotentialRecord:
    """Analyze one detached GREEN frame and derive canonical RED potential."""

    active = EightFactorAnalyzer() if analyzer is None else analyzer
    actual = AnalyzerAuthorityMetadata(
        analysis_schema_version=authority.analysis_schema_version,
        model_id=active.model_id,
        model_version=active.model_version,
        model_hash=active.model_hash,
    )
    require_analyzer_authority(actual, authority)
    return potential_from_frame_analysis(
        active.analyze_green_frame(frame), authority=authority
    )


def potential_from_frame_analysis(
    analysis: FrameAnalysis,
    *,
    authority: AnalyzerAuthorityMetadata = DEFAULT_ANALYZER_AUTHORITY,
) -> EightFactorPotentialRecord:
    """Validate a cached committed analysis and derive the exact same potential."""

    if not isinstance(analysis, FrameAnalysis):
        raise TypeError("analysis must be FrameAnalysis")
    provenance = analysis.provenance
    actual = AnalyzerAuthorityMetadata(
        analysis_schema_version=authority.analysis_schema_version,
        model_id=provenance.model_id,
        model_version=provenance.model_version,
        model_hash=provenance.model_hash,
    )
    require_analyzer_authority(actual, authority)
    sides = {item.side.lower(): item for item in analysis.side_analyses}
    if set(sides) != {"red", "blue"}:
        raise EightFactorShapingError("analysis must contain RED and BLUE exactly")
    red_coverage = _complete_side(sides["red"])
    blue_coverage = _complete_side(sides["blue"])
    if analysis.advantage.confidence is not None or any(
        item.confidence is not None for item in sides.values()
    ):
        raise EightFactorShapingError(
            "confidence semantics require a shaping policy version update"
        )
    phi = analysis.advantage.normalized_advantage
    if phi is None:
        raise EightFactorShapingError("normalized advantage is missing")
    if provenance.frame_hash is None or provenance.scenario_hash is None:
        raise EightFactorShapingError("potential frame provenance is incomplete")
    return EightFactorPotentialRecord(
        schema_version=EIGHT_FACTOR_POTENTIAL_SCHEMA_VERSION,
        analyzer=authority,
        frame_index=provenance.frame_index,
        frame_hash=provenance.frame_hash,
        scenario_hash=provenance.scenario_hash,
        phi_red=phi,
        coverage_red=red_coverage,
        coverage_blue=blue_coverage,
        privileged_supervision_used=True,
    )


@dataclass(frozen=True, slots=True)
class JointEightFactorShapingRecord:
    schema_version: str
    shaping_policy: EightFactorShapingPolicyIdentity
    discount_config_hash: str
    analyzer: AnalyzerAuthorityMetadata
    joint_transition_id: str
    potential_before: EightFactorPotentialRecord
    potential_after: EightFactorPotentialRecord | None
    phi_after_effective: float
    terminal_override: bool
    gamma_effective: float
    lambda_value: float
    red_amount: float
    blue_amount: float
    semantic_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != EIGHT_FACTOR_SHAPING_TRANSITION_SCHEMA_VERSION:
            raise EightFactorShapingError("unsupported shaping transition schema")
        if not isinstance(self.shaping_policy, EightFactorShapingPolicyIdentity):
            raise TypeError("shaping_policy identity is required")
        _sha256(self.discount_config_hash, "discount config hash")
        _sha256(self.joint_transition_id, "joint transition id")
        require_analyzer_authority(self.analyzer)
        for name in (
            "phi_after_effective", "gamma_effective", "lambda_value",
            "red_amount", "blue_amount",
        ):
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        if type(self.terminal_override) is not bool:
            raise TypeError("terminal_override must be exact bool")
        if self.terminal_override != (self.potential_after is None):
            raise EightFactorShapingError("terminal potential override mismatch")
        expected = self.lambda_value * (
            self.gamma_effective * self.phi_after_effective
            - self.potential_before.phi_red
        )
        if self.red_amount != expected or self.blue_amount != -expected:
            raise EightFactorShapingError("potential shaping formula mismatch")
        bound = self.lambda_value * (1.0 + self.gamma_effective)
        if abs(self.red_amount) > bound + 1e-15:
            raise EightFactorShapingError("potential shaping bound exceeded")
        object.__setattr__(self, "semantic_hash", canonical_hash(
            self.semantic_payload()
        ))

    def semantic_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "shaping_policy": self.shaping_policy.to_dict(),
            "discount_config_hash": self.discount_config_hash,
            "analyzer": self.analyzer.to_dict(),
            "joint_transition_id": self.joint_transition_id,
            "potential_before_hash": self.potential_before.semantic_hash,
            "potential_after_hash": (
                None if self.potential_after is None
                else self.potential_after.semantic_hash
            ),
            "phi_before": self.potential_before.phi_red,
            "phi_after_analyzed": (
                None if self.potential_after is None
                else self.potential_after.phi_red
            ),
            "phi_after_effective": self.phi_after_effective,
            "terminal_override": self.terminal_override,
            "gamma_effective": self.gamma_effective,
            "lambda": self.lambda_value,
            "red_amount": self.red_amount,
            "blue_amount": self.blue_amount,
        }

    def to_dict(self) -> dict[str, object]:
        result = self.semantic_payload()
        result["semantic_hash"] = self.semantic_hash
        return result


def derive_joint_eight_factor_shaping(
    *,
    base_record: JointTrainingRewardRecord,
    analysis_before: FrameAnalysis,
    analysis_after: FrameAnalysis | None,
    policy: EightFactorShapingPolicy,
    discount: SharedTrainingDiscountConfig,
) -> JointEightFactorShapingRecord:
    """Derive one zero-sum shaping component from one committed transition."""

    require_shaping_prerequisites(
        policy, discount=discount, analyzer=DEFAULT_ANALYZER_AUTHORITY
    )
    identity = base_record.red.transition_identity
    before = potential_from_frame_analysis(
        analysis_before, authority=policy.analyzer
    )
    if before.frame_index != identity.frame_before:
        raise EightFactorShapingError("pre-potential frame is misaligned")
    terminal = base_record.red.terminated
    if terminal:
        after = None
        effective_after = 0.0
    else:
        if analysis_after is None:
            raise EightFactorShapingError("committed post analysis is required")
        after = potential_from_frame_analysis(
            analysis_after, authority=policy.analyzer
        )
        if after.frame_index != identity.frame_after:
            raise EightFactorShapingError("post-potential frame is misaligned")
        if after.scenario_hash != before.scenario_hash:
            raise EightFactorShapingError("potential scenario identity changed")
        effective_after = after.phi_red
    gamma = discount.effective_gamma(
        delta_time_ms=(
            identity.sim_time_after_ms - identity.sim_time_before_ms
        )
    )
    amount = policy.lambda_value * (
        gamma * effective_after - before.phi_red
    )
    return JointEightFactorShapingRecord(
        schema_version=EIGHT_FACTOR_SHAPING_TRANSITION_SCHEMA_VERSION,
        shaping_policy=policy.identity,
        discount_config_hash=discount.content_hash,
        analyzer=policy.analyzer,
        joint_transition_id=identity.joint_transition_id,
        potential_before=before,
        potential_after=after,
        phi_after_effective=effective_after,
        terminal_override=terminal,
        gamma_effective=gamma,
        lambda_value=policy.lambda_value,
        red_amount=amount,
        blue_amount=-amount,
    )


def compose_joint_training_reward(
    *,
    base_record: JointTrainingRewardRecord,
    shaping: JointEightFactorShapingRecord,
    composite_policy: CompositeRewardPolicy,
) -> JointTrainingRewardRecord:
    """Add a separately identified component without changing base authority."""

    if composite_policy.base != base_record.reward_policy:
        raise EightFactorShapingError("composite/base policy mismatch")
    if composite_policy.shaping != shaping.shaping_policy:
        raise EightFactorShapingError("composite/shaping policy mismatch")
    if shaping.joint_transition_id != base_record.joint_transition_id:
        raise EightFactorShapingError("shaping transition identity mismatch")
    policy_identity = RewardPolicyIdentity(
        shaping.shaping_policy.policy_id,
        shaping.shaping_policy.policy_version,
        shaping.shaping_policy.policy_hash,
    )

    def side_record(
        record: TrainingRewardRecord, amount: float
    ) -> TrainingRewardRecord:
        component = RewardComponent(
            namespace="shaping",
            code="EIGHT_FACTOR_POTENTIAL",
            amount=amount,
            supervision_source=(
                RewardSupervisionSource.GREEN_EIGHT_FACTOR_ANALYZER
            ),
            policy=policy_identity,
            evidence_hash=shaping.semantic_hash,
            privileged_supervision_used=True,
            provenance=tuple(sorted({
                "analyzer_model_hash": shaping.analyzer.model_hash,
                "discount_config_hash": shaping.discount_config_hash,
                "gamma_effective": shaping.gamma_effective,
                "joint_transition_id": shaping.joint_transition_id,
                "lambda": shaping.lambda_value,
                "phi_after_analyzed": (
                    None if shaping.potential_after is None
                    else shaping.potential_after.phi_red
                ),
                "phi_after_effective": shaping.phi_after_effective,
                "phi_before": shaping.potential_before.phi_red,
                "potential_after_hash": (
                    None if shaping.potential_after is None
                    else shaping.potential_after.semantic_hash
                ),
                "potential_before_hash": shaping.potential_before.semantic_hash,
                "shaping_policy_hash": shaping.shaping_policy.policy_hash,
                "terminal_override": shaping.terminal_override,
            }.items())),
        )
        return replace(
            record,
            total_reward=record.total_reward + amount,
            components=(*record.components, component),
            privileged_supervision_used=True,
            composite_policy=composite_policy.identity,
        )

    red = side_record(base_record.red, shaping.red_amount)
    blue = side_record(base_record.blue, shaping.blue_amount)
    return replace(
        base_record,
        red=red,
        blue=blue,
        composite_policy=composite_policy.identity,
    )


def _complete_side(value: SideAnalysis) -> float:
    if value.unit_count <= 0:
        raise EightFactorShapingError(
            "potential requires a nonempty analyzed side"
        )
    if value.coverage != 1.0 or value.observed_unit_count != value.unit_count:
        raise EightFactorShapingError("potential analysis coverage is incomplete")
    if value.effective_power is None:
        raise EightFactorShapingError("side effective force is missing")
    _finite(value.effective_power, "side effective force")
    return 1.0


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite number (not bool)")
    result = float(value)
    if not math.isfinite(result):
        raise EightFactorShapingError(f"{name} must be finite")
    return result


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise EightFactorShapingError(f"{name} must be lowercase SHA-256")
    return value


__all__ = [
    "COMPOSITE_REWARD_POLICY_SCHEMA_VERSION",
    "CompositeRewardPolicy",
    "EIGHT_FACTOR_POTENTIAL_SCHEMA_VERSION",
    "EIGHT_FACTOR_SHAPING_TRANSITION_SCHEMA_VERSION",
    "EightFactorPotentialRecord",
    "EightFactorShapingError",
    "JointEightFactorShapingRecord",
    "build_composite_reward_policy",
    "compose_joint_training_reward",
    "derive_eight_factor_potential",
    "derive_joint_eight_factor_shaping",
    "potential_from_frame_analysis",
]
