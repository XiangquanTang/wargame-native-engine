"""Typed prerequisites for optional EightFactor potential shaping.

This module freezes policy identity and validation only.  It intentionally
contains no shaping calculator and is not integrated with reward records.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import json
import math
from pathlib import Path
from typing import Mapping

from wargame_native_engine.analysis.semantic_identity import (
    AnalyzerAuthorityMetadata,
    DEFAULT_ANALYZER_AUTHORITY,
)
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.training.discount import (
    SharedTrainingDiscountConfig,
    TrainingDiscountIdentity,
    load_shared_training_discount_config,
    require_discount_identity,
)


EIGHT_FACTOR_SHAPING_POLICY_SCHEMA_VERSION = (
    "native-eight-factor-reward-shaping-policy/1.0"
)
EIGHT_FACTOR_SHAPING_POLICY_ID = "native-eight-factor-reward-shaping"
EIGHT_FACTOR_SHAPING_POLICY_VERSION = "1.0.0"


class EightFactorShapingContractError(ValueError):
    """A shaping prerequisite does not satisfy the frozen v1 contract."""


@dataclass(frozen=True, slots=True)
class EightFactorShapingPolicyIdentity:
    policy_id: str
    policy_version: str
    policy_hash: str

    def __post_init__(self) -> None:
        if self.policy_id != EIGHT_FACTOR_SHAPING_POLICY_ID:
            raise EightFactorShapingContractError(
                "unsupported EightFactor shaping policy id"
            )
        if self.policy_version != EIGHT_FACTOR_SHAPING_POLICY_VERSION:
            raise EightFactorShapingContractError(
                "unsupported EightFactor shaping policy version"
            )
        _sha256(self.policy_hash, "shaping policy hash")

    def to_dict(self) -> dict[str, str]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy_hash": self.policy_hash,
        }


@dataclass(frozen=True, slots=True)
class EightFactorShapingPolicy:
    schema_version: str
    policy_id: str
    policy_version: str
    enabled: bool
    component_namespace: str
    component_code: str
    units: str
    potential_source: str
    canonical_side: str
    blue_projection: str
    potential_transform: str
    potential_min: float
    potential_max: float
    lambda_value: float
    discount: TrainingDiscountIdentity
    formula: str
    terminal_handling: str
    truncation_handling: str
    zero_force_policy: str
    missing_analysis_policy: str
    coverage_policy: str
    confidence_policy: str
    analyzer: AnalyzerAuthorityMetadata
    privileged_supervision_used: bool
    supervision_source: str
    future_information: str
    reward_clipping: str
    reward_normalization: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        expected = {
            "schema_version": EIGHT_FACTOR_SHAPING_POLICY_SCHEMA_VERSION,
            "policy_id": EIGHT_FACTOR_SHAPING_POLICY_ID,
            "policy_version": EIGHT_FACTOR_SHAPING_POLICY_VERSION,
            "enabled": True,
            "component_namespace": "shaping",
            "component_code": "EIGHT_FACTOR_POTENTIAL",
            "units": "dimensionless",
            "potential_source": "ANALYZER_NORMALIZED_RELATIVE_ADVANTAGE",
            "canonical_side": "RED",
            "blue_projection": "NEGATE_CANONICAL_RED",
            "potential_transform": "IDENTITY",
            "potential_min": -1.0,
            "potential_max": 1.0,
            "formula": "LAMBDA_TIMES_GAMMA_PHI_NEXT_MINUS_PHI_BEFORE",
            "terminal_handling": "ABSORBING_PHI_NEXT_ZERO",
            "truncation_handling": "ANALYZE_COMMITTED_POST_STATE",
            "zero_force_policy": "NORMALIZED_EXPLICIT_ZERO_TOTAL",
            "missing_analysis_policy": "FAIL_CLOSED",
            "coverage_policy": "REQUIRE_COMPLETE_NONEMPTY_SIDES",
            "confidence_policy": "IGNORE_UNAVAILABLE_CONFIDENCE_V1",
            "privileged_supervision_used": True,
            "supervision_source": "GREEN_EIGHT_FACTOR_ANALYZER",
            "future_information": "FORBIDDEN",
            "reward_clipping": "NONE",
            "reward_normalization": "NONE",
        }
        for name, value in expected.items():
            if getattr(self, name) != value:
                raise EightFactorShapingContractError(
                    f"EightFactor shaping v1 field mismatch: {name}"
                )
        lambda_value = _lambda(self.lambda_value)
        object.__setattr__(self, "lambda_value", lambda_value)
        if not isinstance(self.discount, TrainingDiscountIdentity):
            raise TypeError("discount must be TrainingDiscountIdentity")
        if not isinstance(self.analyzer, AnalyzerAuthorityMetadata):
            raise TypeError("analyzer must be AnalyzerAuthorityMetadata")
        _sha256(self.analyzer.model_hash, "analyzer model hash")
        object.__setattr__(
            self, "content_hash", canonical_hash(self.semantic_payload())
        )

    @property
    def identity(self) -> EightFactorShapingPolicyIdentity:
        return EightFactorShapingPolicyIdentity(
            self.policy_id, self.policy_version, self.content_hash
        )

    def semantic_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "enabled": self.enabled,
            "component": {
                "namespace": self.component_namespace,
                "code": self.component_code,
                "units": self.units,
            },
            "potential": {
                "source": self.potential_source,
                "canonical_side": self.canonical_side,
                "blue_projection": self.blue_projection,
                "transform": self.potential_transform,
                "range": [self.potential_min, self.potential_max],
            },
            "lambda": self.lambda_value,
            "discount_reference": self.discount.to_dict(),
            "formula": self.formula,
            "terminal_handling": self.terminal_handling,
            "truncation_handling": self.truncation_handling,
            "zero_force_policy": self.zero_force_policy,
            "missing_analysis_policy": self.missing_analysis_policy,
            "coverage_policy": self.coverage_policy,
            "confidence_policy": self.confidence_policy,
            "analyzer": self.analyzer.to_dict(),
            "privileged_supervision_used": self.privileged_supervision_used,
            "supervision_source": self.supervision_source,
            "future_information": self.future_information,
            "reward_clipping": self.reward_clipping,
            "reward_normalization": self.reward_normalization,
        }

    def to_dict(self) -> dict[str, object]:
        result = self.semantic_payload()
        result["policy_hash"] = self.content_hash
        return result


def build_eight_factor_shaping_policy(
    *,
    lambda_value: int | float,
    discount: SharedTrainingDiscountConfig,
    analyzer: AnalyzerAuthorityMetadata = DEFAULT_ANALYZER_AUTHORITY,
) -> EightFactorShapingPolicy:
    """Build a complete policy fixture/reference without calculating reward."""

    if not isinstance(discount, SharedTrainingDiscountConfig):
        raise TypeError("discount must be SharedTrainingDiscountConfig")
    if not isinstance(analyzer, AnalyzerAuthorityMetadata):
        raise TypeError("analyzer must be AnalyzerAuthorityMetadata")
    normalized_lambda = _lambda(lambda_value)
    values = {
        "schema_version": EIGHT_FACTOR_SHAPING_POLICY_SCHEMA_VERSION,
        "policy_id": EIGHT_FACTOR_SHAPING_POLICY_ID,
        "policy_version": EIGHT_FACTOR_SHAPING_POLICY_VERSION,
        "enabled": True,
        "component_namespace": "shaping",
        "component_code": "EIGHT_FACTOR_POTENTIAL",
        "units": "dimensionless",
        "potential_source": "ANALYZER_NORMALIZED_RELATIVE_ADVANTAGE",
        "canonical_side": "RED",
        "blue_projection": "NEGATE_CANONICAL_RED",
        "potential_transform": "IDENTITY",
        "potential_min": -1.0,
        "potential_max": 1.0,
        "lambda_value": normalized_lambda,
        "discount": discount.identity,
        "formula": "LAMBDA_TIMES_GAMMA_PHI_NEXT_MINUS_PHI_BEFORE",
        "terminal_handling": "ABSORBING_PHI_NEXT_ZERO",
        "truncation_handling": "ANALYZE_COMMITTED_POST_STATE",
        "zero_force_policy": "NORMALIZED_EXPLICIT_ZERO_TOTAL",
        "missing_analysis_policy": "FAIL_CLOSED",
        "coverage_policy": "REQUIRE_COMPLETE_NONEMPTY_SIDES",
        "confidence_policy": "IGNORE_UNAVAILABLE_CONFIDENCE_V1",
        "analyzer": analyzer,
        "privileged_supervision_used": True,
        "supervision_source": "GREEN_EIGHT_FACTOR_ANALYZER",
        "future_information": "FORBIDDEN",
        "reward_clipping": "NONE",
        "reward_normalization": "NONE",
    }
    return EightFactorShapingPolicy(**values)


def require_shaping_prerequisites(
    policy: EightFactorShapingPolicy,
    *,
    discount: SharedTrainingDiscountConfig,
    analyzer: AnalyzerAuthorityMetadata,
) -> None:
    """Verify exact prerequisite identities without calculating shaping."""

    if not isinstance(policy, EightFactorShapingPolicy):
        raise TypeError("policy must be EightFactorShapingPolicy")
    require_discount_identity(discount, policy.discount)
    if analyzer != policy.analyzer:
        raise EightFactorShapingContractError(
            "EightFactor analyzer authority identity mismatch"
        )


@lru_cache(maxsize=8)
def load_eight_factor_shaping_policy(
    path: str | Path | None = None,
    *,
    discount: SharedTrainingDiscountConfig | None = None,
) -> EightFactorShapingPolicy:
    """Load the strict production/experiment policy without duplicating gamma."""

    source = (
        Path(path)
        if path is not None
        else Path(__file__).resolve().parents[3]
        / "config/training/native_eight_factor_reward_shaping_v1.json"
    )
    root = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(root, Mapping):
        raise EightFactorShapingContractError("shaping policy root must be an object")
    expected = set(build_eight_factor_shaping_policy(
        lambda_value=0.0,
        discount=(
            load_shared_training_discount_config()
            if discount is None else discount
        ),
    ).semantic_payload())
    if set(root) != expected:
        raise EightFactorShapingContractError(
            "shaping policy fields are missing or unknown"
        )
    active_discount = (
        load_shared_training_discount_config()
        if discount is None else discount
    )
    reference = root.get("discount_reference")
    if reference != active_discount.identity.to_dict():
        raise EightFactorShapingContractError(
            "shaping policy discount reference mismatch"
        )
    analyzer = root.get("analyzer")
    if analyzer != DEFAULT_ANALYZER_AUTHORITY.to_dict():
        raise EightFactorShapingContractError(
            "shaping policy analyzer reference mismatch"
        )
    policy = build_eight_factor_shaping_policy(
        lambda_value=root.get("lambda"),
        discount=active_discount,
        analyzer=DEFAULT_ANALYZER_AUTHORITY,
    )
    if root != policy.semantic_payload():
        raise EightFactorShapingContractError(
            "shaping policy semantic fields do not match v1"
        )
    return policy


def _lambda(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("lambda must be a finite number (not bool)")
    result = float(value)
    if not math.isfinite(result):
        raise EightFactorShapingContractError("lambda must be finite")
    if result < 0.0:
        raise EightFactorShapingContractError("lambda must be >= 0")
    return result


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise EightFactorShapingContractError(
            f"{name} must be lowercase SHA-256"
        )
    return value


__all__ = [
    "EIGHT_FACTOR_SHAPING_POLICY_ID",
    "EIGHT_FACTOR_SHAPING_POLICY_SCHEMA_VERSION",
    "EIGHT_FACTOR_SHAPING_POLICY_VERSION",
    "EightFactorShapingContractError",
    "EightFactorShapingPolicy",
    "EightFactorShapingPolicyIdentity",
    "build_eight_factor_shaping_policy",
    "load_eight_factor_shaping_policy",
    "require_shaping_prerequisites",
]
