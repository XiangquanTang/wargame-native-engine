"""Versioned sparse terminal reward records over committed transitions.

This module is a pure training-supervision boundary.  It accepts detached
transition/result evidence and never reads or mutates WorldState or Engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
import json
import math
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from wargame_native_engine.io.serialization import canonical_hash, json_clone


REWARD_POLICY_SCHEMA_VERSION = "native-training-reward-policy/1.0"
TRAINING_REWARD_SCHEMA_VERSION = "native-training-reward/1.0"
JOINT_TRAINING_REWARD_SCHEMA_VERSION = "native-training-joint-reward/1.0"
TRAINING_TRUNCATION_CONFIG_SCHEMA_VERSION = (
    "native-training-truncation-config/1.0"
)


class RewardContractError(ValueError):
    """Detached reward evidence violates the frozen machine contract."""


class RewardSide(str, Enum):
    RED = "red"
    BLUE = "blue"


class RewardRelation(str, Enum):
    ZERO_SUM = "ZERO_SUM"


class RewardOutcome(str, Enum):
    NONTERMINAL = "NONTERMINAL"
    WIN = "WIN"
    LOSS = "LOSS"
    DRAW = "DRAW"
    TRUNCATED = "TRUNCATED"


class RewardSupervisionSource(str, Enum):
    ENGINE_COMMITTED_TRANSITION = "ENGINE_COMMITTED_TRANSITION"
    ENGINE_TERMINAL_AUTHORITY = "ENGINE_TERMINAL_AUTHORITY"
    TRAINING_TRUNCATION_AUTHORITY = "TRAINING_TRUNCATION_AUTHORITY"
    GREEN_EIGHT_FACTOR_ANALYZER = "GREEN_EIGHT_FACTOR_ANALYZER"


class RewardTruncationSource(str, Enum):
    NONE = "NONE"
    NATIVE_MAX_TICKS = "NATIVE_MAX_TICKS"
    TRAINING_MAX_STEPS = "TRAINING_MAX_STEPS"
    TRAINING_MAX_DECISION_WINDOWS = "TRAINING_MAX_DECISION_WINDOWS"


@dataclass(frozen=True, slots=True)
class RewardPolicyIdentity:
    policy_id: str
    policy_version: str
    policy_hash: str

    def __post_init__(self) -> None:
        _nonempty(self.policy_id, "reward policy id")
        _nonempty(self.policy_version, "reward policy version")
        _sha256(self.policy_hash, "reward policy hash")

    def to_dict(self) -> dict[str, str]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy_hash": self.policy_hash,
        }


@dataclass(frozen=True, slots=True)
class BaseRewardPolicy:
    schema_version: str
    policy_id: str
    policy_version: str
    relation: RewardRelation
    units: str
    component_namespace: str
    component_code: str
    mapping: Mapping[str, tuple[float, float]]
    terminal_reason_codes: tuple[str, ...]
    native_truncation_reason_codes: tuple[str, ...]
    invalid_transition_semantics: str
    wait_semantics: str
    shaping_enabled: bool
    shaping_components: tuple[str, ...]
    content_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != REWARD_POLICY_SCHEMA_VERSION:
            raise RewardContractError("unsupported reward policy schema")
        if self.policy_id != "native-sparse-terminal-zero-sum":
            raise RewardContractError("unsupported reward policy id")
        if self.policy_version != "1.0.0":
            raise RewardContractError("unsupported reward policy version")
        if self.relation is not RewardRelation.ZERO_SUM:
            raise RewardContractError("base reward policy must be ZERO_SUM")
        if self.units != "dimensionless":
            raise RewardContractError("base reward units must be dimensionless")
        if (self.component_namespace, self.component_code) != (
            "base", "TERMINAL_OUTCOME"
        ):
            raise RewardContractError("base reward component mismatch")
        expected = {
            "nonterminal": (0.0, 0.0),
            "red_win": (1.0, -1.0),
            "blue_win": (-1.0, 1.0),
            "draw": (0.0, 0.0),
            "native_truncation": (0.0, 0.0),
            "training_truncation": (0.0, 0.0),
        }
        if dict(self.mapping) != expected:
            raise RewardContractError("base reward mapping mismatch")
        object.__setattr__(
            self,
            "mapping",
            MappingProxyType({key: tuple(value) for key, value in self.mapping.items()}),
        )
        if self.invalid_transition_semantics != (
            "NO_COMMITTED_TRANSITION_NO_REWARD_RECORD"
        ):
            raise RewardContractError("invalid transition semantics mismatch")
        if self.wait_semantics != "NO_ACTION_COST":
            raise RewardContractError("WAIT reward semantics mismatch")
        if self.shaping_enabled or self.shaping_components:
            raise RewardContractError("reward shaping is unsupported in v1")
        if tuple(sorted(set(self.terminal_reason_codes))) != (
            self.terminal_reason_codes
        ):
            raise RewardContractError("terminal reason codes must be unique/sorted")
        if tuple(sorted(set(self.native_truncation_reason_codes))) != (
            self.native_truncation_reason_codes
        ):
            raise RewardContractError(
                "native truncation reason codes must be unique/sorted"
            )
        _sha256(self.content_hash, "reward policy content hash")

    @property
    def identity(self) -> RewardPolicyIdentity:
        return RewardPolicyIdentity(
            self.policy_id, self.policy_version, self.content_hash
        )

    def rewards_for(
        self,
        *,
        terminated: bool,
        truncated: bool,
        winner: str | None,
        truncation_source: RewardTruncationSource,
    ) -> tuple[float, float]:
        _terminal_flags(terminated, truncated)
        if winner is not None and winner not in {"red", "blue"}:
            raise RewardContractError("winner must be RED, BLUE or null")
        if terminated:
            if truncation_source is not RewardTruncationSource.NONE:
                raise RewardContractError("terminal transition cannot be truncated")
            key = "draw" if winner is None else f"{winner}_win"
        elif truncated:
            if truncation_source is RewardTruncationSource.NATIVE_MAX_TICKS:
                key = "native_truncation"
            elif truncation_source in {
                RewardTruncationSource.TRAINING_MAX_STEPS,
                RewardTruncationSource.TRAINING_MAX_DECISION_WINDOWS,
            }:
                key = "training_truncation"
            else:
                raise RewardContractError("truncation source is missing")
            if winner is not None:
                raise RewardContractError("truncated transition cannot have winner")
        else:
            if winner is not None:
                raise RewardContractError("nonterminal transition cannot have winner")
            if truncation_source is not RewardTruncationSource.NONE:
                raise RewardContractError("nonterminal transition has truncation source")
            key = "nonterminal"
        return self.mapping[key]


@dataclass(frozen=True, slots=True)
class TrainingTruncationProvenance:
    source: RewardTruncationSource
    config_schema_version: str | None = None
    limit_kind: str | None = None
    configured_limit: int | None = None
    config_hash: str | None = None

    def __post_init__(self) -> None:
        training_sources = {
            RewardTruncationSource.TRAINING_MAX_STEPS: "max_steps",
            RewardTruncationSource.TRAINING_MAX_DECISION_WINDOWS: (
                "max_decision_windows"
            ),
        }
        if self.source in training_sources:
            if self.config_schema_version != (
                TRAINING_TRUNCATION_CONFIG_SCHEMA_VERSION
            ):
                raise RewardContractError("training truncation schema mismatch")
            if self.limit_kind != training_sources[self.source]:
                raise RewardContractError("training truncation limit kind mismatch")
            _positive_int(self.configured_limit, "training truncation limit")
            expected = canonical_hash({
                "schema_version": self.config_schema_version,
                "limit_kind": self.limit_kind,
                "configured_limit": self.configured_limit,
            })
            if self.config_hash != expected:
                raise RewardContractError("training truncation config hash mismatch")
            return
        if any(value is not None for value in (
            self.config_schema_version,
            self.limit_kind,
            self.configured_limit,
            self.config_hash,
        )):
            raise RewardContractError(
                "non-training truncation cannot carry training config"
            )

    @classmethod
    def none(cls) -> "TrainingTruncationProvenance":
        return cls(RewardTruncationSource.NONE)

    @classmethod
    def native_max_ticks(cls) -> "TrainingTruncationProvenance":
        return cls(RewardTruncationSource.NATIVE_MAX_TICKS)

    @classmethod
    def training_limit(
        cls, source: RewardTruncationSource, configured_limit: int
    ) -> "TrainingTruncationProvenance":
        kind = {
            RewardTruncationSource.TRAINING_MAX_STEPS: "max_steps",
            RewardTruncationSource.TRAINING_MAX_DECISION_WINDOWS: (
                "max_decision_windows"
            ),
        }.get(source)
        if kind is None:
            raise RewardContractError("source is not a training truncation")
        value = {
            "schema_version": TRAINING_TRUNCATION_CONFIG_SCHEMA_VERSION,
            "limit_kind": kind,
            "configured_limit": _positive_int(
                configured_limit, "training truncation limit"
            ),
        }
        return cls(source, value["schema_version"], kind, configured_limit,
                   canonical_hash(value))

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source.value,
            "config_schema_version": self.config_schema_version,
            "limit_kind": self.limit_kind,
            "configured_limit": self.configured_limit,
            "config_hash": self.config_hash,
        }


@dataclass(frozen=True, slots=True)
class RewardTransitionIdentity:
    episode_id: str
    run_namespace: str
    side: RewardSide
    decision_window_index: int
    frame_before: int
    fact_frame_start: int
    fact_frame_end: int
    frame_after: int
    sim_time_before_ms: int
    sim_time_after_ms: int
    decision_before_hash: str
    action_catalog_hash: str
    submitted_action_ids: tuple[str, ...] | None
    submitted_action_semantic_hash: str
    joint_action_hash: str
    decision_after_hash: str
    joint_transition_id: str

    def __post_init__(self) -> None:
        _nonempty(self.episode_id, "episode_id")
        _nonempty(self.run_namespace, "run_namespace")
        for name, value in (
            ("decision_window_index", self.decision_window_index),
            ("frame_before", self.frame_before),
            ("fact_frame_start", self.fact_frame_start),
            ("fact_frame_end", self.fact_frame_end),
            ("frame_after", self.frame_after),
            ("sim_time_before_ms", self.sim_time_before_ms),
            ("sim_time_after_ms", self.sim_time_after_ms),
        ):
            _nonnegative_int(value, name)
        if self.fact_frame_start != self.frame_before + 1:
            raise RewardContractError("fact frame start must follow frame_before")
        if self.fact_frame_end != self.frame_after:
            raise RewardContractError("fact frame end must equal frame_after")
        if self.fact_frame_end < self.fact_frame_start:
            raise RewardContractError("fact frame range must be non-empty")
        if self.sim_time_after_ms < self.sim_time_before_ms:
            raise RewardContractError("simulation time moved backwards")
        for name, value in (
            ("decision_before_hash", self.decision_before_hash),
            ("action_catalog_hash", self.action_catalog_hash),
            ("submitted_action_semantic_hash", self.submitted_action_semantic_hash),
            ("joint_action_hash", self.joint_action_hash),
            ("decision_after_hash", self.decision_after_hash),
            ("joint_transition_id", self.joint_transition_id),
        ):
            _sha256(value, name)
        if self.submitted_action_ids is not None:
            values = tuple(self.submitted_action_ids)
            if any(not isinstance(value, str) or not value for value in values):
                raise RewardContractError("submitted action IDs must be strings")
            if len(values) != len(set(values)):
                raise RewardContractError("submitted action IDs must be unique")
            object.__setattr__(self, "submitted_action_ids", values)
        expected = canonical_hash(_joint_transition_payload(self))
        if self.joint_transition_id != expected:
            raise RewardContractError("joint transition identity mismatch")

    def same_joint_transition(self, other: "RewardTransitionIdentity") -> bool:
        return (
            isinstance(other, RewardTransitionIdentity)
            and self.joint_transition_id == other.joint_transition_id
            and _joint_transition_payload(self) == _joint_transition_payload(other)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "episode_id": self.episode_id,
            "run_namespace": self.run_namespace,
            "side": self.side.value,
            "decision_window_index": self.decision_window_index,
            "frame_before": self.frame_before,
            "fact_frame_start": self.fact_frame_start,
            "fact_frame_end": self.fact_frame_end,
            "frame_after": self.frame_after,
            "sim_time_before_ms": self.sim_time_before_ms,
            "sim_time_after_ms": self.sim_time_after_ms,
            "decision_before_hash": self.decision_before_hash,
            "action_catalog_hash": self.action_catalog_hash,
            "submitted_action_ids": (
                None
                if self.submitted_action_ids is None
                else list(self.submitted_action_ids)
            ),
            "submitted_action_semantic_hash": (
                self.submitted_action_semantic_hash
            ),
            "joint_action_hash": self.joint_action_hash,
            "decision_after_hash": self.decision_after_hash,
            "joint_transition_id": self.joint_transition_id,
        }


@dataclass(frozen=True, slots=True)
class RewardComponent:
    namespace: str
    code: str
    amount: float
    supervision_source: RewardSupervisionSource
    policy: RewardPolicyIdentity | None = None
    evidence_hash: str | None = None
    privileged_supervision_used: bool = False
    provenance: tuple[tuple[str, str | float | bool | None], ...] = ()

    def __post_init__(self) -> None:
        _machine_code(self.namespace, "reward component namespace")
        _machine_code(self.code, "reward component code")
        if not isinstance(self.supervision_source, RewardSupervisionSource):
            raise TypeError(
                "component supervision_source must be RewardSupervisionSource"
            )
        object.__setattr__(self, "amount", _finite_float(self.amount, "amount"))
        if self.policy is None:
            if (
                self.evidence_hash is not None
                or self.privileged_supervision_used
                or self.provenance
            ):
                raise RewardContractError(
                    "component provenance requires a policy identity"
                )
        else:
            if not isinstance(self.policy, RewardPolicyIdentity):
                raise TypeError("component policy must be RewardPolicyIdentity")
            _sha256(self.evidence_hash, "component evidence hash")
            values = tuple(self.provenance)
            if tuple(sorted(values, key=lambda item: item[0])) != values:
                raise RewardContractError(
                    "component provenance fields must be deterministically sorted"
                )
            if len({name for name, _value in values}) != len(values):
                raise RewardContractError("component provenance fields duplicate")
            for name, value in values:
                _nonempty(name, "component provenance name")
                if value is not None and type(value) not in {str, float, bool}:
                    raise TypeError(
                        "component provenance values must be stable scalars"
                    )
                if type(value) is float:
                    _finite_float(value, f"component provenance {name}")
            object.__setattr__(self, "provenance", values)
        if type(self.privileged_supervision_used) is not bool:
            raise TypeError("component privileged flag must be exact bool")

    def to_dict(self) -> dict[str, object]:
        result = {
            "namespace": self.namespace,
            "code": self.code,
            "amount": self.amount,
            "supervision_source": self.supervision_source.value,
        }
        if self.policy is not None:
            result["policy"] = self.policy.to_dict()
            result["evidence_hash"] = self.evidence_hash
            result["privileged_supervision_used"] = (
                self.privileged_supervision_used
            )
            result["provenance"] = dict(self.provenance)
        return result


@dataclass(frozen=True, slots=True)
class TrainingRewardRecord:
    schema_version: str
    reward_policy: RewardPolicyIdentity
    transition_identity: RewardTransitionIdentity
    side: RewardSide
    total_reward: float
    components: tuple[RewardComponent, ...]
    outcome: RewardOutcome
    terminated: bool
    truncated: bool
    terminal_reason_code: str | None
    truncation: TrainingTruncationProvenance
    supervision_source: RewardSupervisionSource
    privileged_supervision_used: bool
    composite_policy: RewardPolicyIdentity | None = None
    semantic_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != TRAINING_REWARD_SCHEMA_VERSION:
            raise RewardContractError("unsupported training reward schema")
        if not isinstance(self.reward_policy, RewardPolicyIdentity):
            raise TypeError("reward_policy must be RewardPolicyIdentity")
        if not isinstance(self.transition_identity, RewardTransitionIdentity):
            raise TypeError("transition_identity must be RewardTransitionIdentity")
        if not isinstance(self.side, RewardSide):
            raise TypeError("side must be RewardSide")
        if not isinstance(self.outcome, RewardOutcome):
            raise TypeError("outcome must be RewardOutcome")
        if not isinstance(self.truncation, TrainingTruncationProvenance):
            raise TypeError("truncation must be TrainingTruncationProvenance")
        if not isinstance(self.supervision_source, RewardSupervisionSource):
            raise TypeError("supervision_source must be RewardSupervisionSource")
        if self.side is not self.transition_identity.side:
            raise RewardContractError("reward side/transition side mismatch")
        _terminal_flags(self.terminated, self.truncated)
        total = _finite_float(self.total_reward, "total_reward")
        object.__setattr__(self, "total_reward", total)
        components = tuple(self.components)
        if not components or (
            components[0].namespace, components[0].code
        ) != ("base", "TERMINAL_OUTCOME"):
            raise RewardContractError("v1 requires the base terminal component first")
        if self.composite_policy is None:
            if len(components) != 1 or total not in {-1.0, 0.0, 1.0}:
                raise RewardContractError("base reward record shape/value mismatch")
        else:
            if not isinstance(self.composite_policy, RewardPolicyIdentity):
                raise TypeError("composite_policy must be RewardPolicyIdentity")
            if len(components) != 2 or (
                components[1].namespace, components[1].code
            ) != ("shaping", "EIGHT_FACTOR_POTENTIAL"):
                raise RewardContractError(
                    "composite v1 requires one EightFactor shaping component"
                )
            if components[1].policy is None or (
                components[1].supervision_source
                is not RewardSupervisionSource.GREEN_EIGHT_FACTOR_ANALYZER
            ):
                raise RewardContractError("shaping component provenance mismatch")
        if components[0].supervision_source is not self.supervision_source:
            raise RewardContractError("component supervision source mismatch")
        if sum(item.amount for item in components) != total:
            raise RewardContractError("reward components do not sum to total")
        object.__setattr__(self, "components", components)
        if type(self.privileged_supervision_used) is not bool:
            raise TypeError("privileged_supervision_used must be exact bool")
        expected_privileged = (
            self.terminated
            or self.truncation.source is RewardTruncationSource.NATIVE_MAX_TICKS
            or self.composite_policy is not None
        )
        if self.privileged_supervision_used != expected_privileged:
            raise RewardContractError("privileged supervision flag mismatch")
        if self.terminated:
            _nonempty(self.terminal_reason_code, "terminal_reason_code")
        elif self.truncated and (
            self.truncation.source is RewardTruncationSource.NATIVE_MAX_TICKS
        ):
            _nonempty(self.terminal_reason_code, "terminal_reason_code")
        elif self.terminal_reason_code is not None:
            raise RewardContractError("unexpected terminal reason code")
        _validate_record_outcome(self)
        payload = _training_reward_payload(self)
        object.__setattr__(self, "semantic_hash", canonical_hash(payload))

    def to_dict(self) -> dict[str, object]:
        value = _training_reward_payload(self)
        value["semantic_hash"] = self.semantic_hash
        return value


@dataclass(frozen=True, slots=True)
class JointTrainingRewardRecord:
    schema_version: str
    reward_policy: RewardPolicyIdentity
    relation: RewardRelation
    winner: RewardSide | None
    red: TrainingRewardRecord
    blue: TrainingRewardRecord
    composite_policy: RewardPolicyIdentity | None = None
    semantic_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != JOINT_TRAINING_REWARD_SCHEMA_VERSION:
            raise RewardContractError("unsupported joint reward schema")
        if not isinstance(self.reward_policy, RewardPolicyIdentity):
            raise TypeError("reward_policy must be RewardPolicyIdentity")
        if not isinstance(self.relation, RewardRelation):
            raise TypeError("relation must be RewardRelation")
        if self.winner is not None and not isinstance(self.winner, RewardSide):
            raise TypeError("winner must be RewardSide or None")
        if self.relation is not RewardRelation.ZERO_SUM:
            raise RewardContractError("v1 joint reward must be ZERO_SUM")
        if self.red.side is not RewardSide.RED or self.blue.side is not RewardSide.BLUE:
            raise RewardContractError("joint reward side records are misbound")
        if self.red.reward_policy != self.reward_policy or (
            self.blue.reward_policy != self.reward_policy
        ):
            raise RewardContractError("joint reward policy identity mismatch")
        if self.red.composite_policy != self.composite_policy or (
            self.blue.composite_policy != self.composite_policy
        ):
            raise RewardContractError("joint composite policy identity mismatch")
        if not self.red.transition_identity.same_joint_transition(
            self.blue.transition_identity
        ):
            raise RewardContractError("side rewards do not share joint transition")
        if self.red.total_reward + self.blue.total_reward != 0.0:
            raise RewardContractError("joint reward violates ZERO_SUM")
        if self.red.terminated != self.blue.terminated or (
            self.red.truncated != self.blue.truncated
        ):
            raise RewardContractError("side terminal flags differ")
        if self.winner is None:
            if self.red.outcome in {RewardOutcome.WIN, RewardOutcome.LOSS} or (
                self.blue.outcome in {RewardOutcome.WIN, RewardOutcome.LOSS}
            ):
                raise RewardContractError("winner is missing for win/loss record")
        else:
            expected = {
                RewardSide.RED: (RewardOutcome.WIN, RewardOutcome.LOSS),
                RewardSide.BLUE: (RewardOutcome.LOSS, RewardOutcome.WIN),
            }[self.winner]
            if (self.red.outcome, self.blue.outcome) != expected:
                raise RewardContractError("winner/outcome mismatch")
        object.__setattr__(self, "semantic_hash", canonical_hash(
            _joint_reward_payload(self)
        ))

    @property
    def joint_transition_id(self) -> str:
        return self.red.transition_identity.joint_transition_id

    def side_record(self, side: str | RewardSide) -> TrainingRewardRecord:
        normalized = RewardSide(str(side).lower()) if isinstance(side, str) else side
        return self.red if normalized is RewardSide.RED else self.blue

    def to_dict(self) -> dict[str, object]:
        value = _joint_reward_payload(self)
        value["semantic_hash"] = self.semantic_hash
        return value


@lru_cache(maxsize=4)
def load_reward_policy(path: str | Path | None = None) -> BaseRewardPolicy:
    source = (
        Path(path)
        if path is not None
        else Path(__file__).resolve().parents[3]
        / "config/training/native_sparse_terminal_reward_v1.json"
    )
    root = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(root, Mapping):
        raise RewardContractError("reward policy root must be an object")
    expected_fields = {
        "schema_version", "policy_id", "policy_version", "relation", "units",
        "base_component", "mapping", "terminal_reason_codes",
        "native_truncation_reason_codes", "invalid_transition_semantics",
        "wait_semantics", "shaping",
    }
    if set(root) != expected_fields:
        raise RewardContractError("reward policy fields are missing or unknown")
    base = _exact_mapping(root["base_component"], "base_component")
    if set(base) != {"namespace", "code"}:
        raise RewardContractError("base component fields are invalid")
    mappings = _exact_mapping(root["mapping"], "mapping")
    mapping_names = (
        "blue_win", "draw", "native_truncation", "nonterminal", "red_win",
        "training_truncation",
    )
    if set(mappings) != set(mapping_names):
        raise RewardContractError("reward mapping fields are invalid")
    normalized: dict[str, tuple[float, float]] = {}
    for name in mapping_names:
        pair = _exact_mapping(mappings[name], f"mapping.{name}")
        if set(pair) != {"red", "blue"}:
            raise RewardContractError(f"mapping.{name} side fields are invalid")
        normalized[name] = (
            _finite_float(pair["red"], f"mapping.{name}.red"),
            _finite_float(pair["blue"], f"mapping.{name}.blue"),
        )
    shaping = _exact_mapping(root["shaping"], "shaping")
    if set(shaping) != {"enabled", "components"}:
        raise RewardContractError("reward shaping fields are invalid")
    if type(shaping["enabled"]) is not bool:
        raise TypeError("reward shaping enabled must be exact bool")
    components = _string_sequence(shaping["components"], "shaping components")
    document = json_clone(root)
    return BaseRewardPolicy(
        schema_version=str(root["schema_version"]),
        policy_id=str(root["policy_id"]),
        policy_version=str(root["policy_version"]),
        relation=RewardRelation(str(root["relation"])),
        units=str(root["units"]),
        component_namespace=str(base["namespace"]),
        component_code=str(base["code"]),
        mapping=normalized,
        terminal_reason_codes=tuple(sorted(_string_sequence(
            root["terminal_reason_codes"], "terminal reason codes"
        ))),
        native_truncation_reason_codes=tuple(sorted(_string_sequence(
            root["native_truncation_reason_codes"],
            "native truncation reason codes",
        ))),
        invalid_transition_semantics=str(root["invalid_transition_semantics"]),
        wait_semantics=str(root["wait_semantics"]),
        shaping_enabled=shaping["enabled"],
        shaping_components=components,
        content_hash=canonical_hash(document),
    )


def build_reward_transition_identity(
    *,
    episode_id: str,
    run_namespace: str,
    side: str | RewardSide,
    decision_window_index: int,
    frame_before: int,
    fact_frame_start: int,
    fact_frame_end: int,
    frame_after: int,
    sim_time_before_ms: int,
    sim_time_after_ms: int,
    decision_before_hash: str,
    action_catalog_hash: str,
    submitted_action_ids: Sequence[str] | None,
    submitted_action_semantic_hash: str,
    joint_action_hash: str,
    decision_after_hash: str,
) -> RewardTransitionIdentity:
    normalized_side = (
        side if isinstance(side, RewardSide) else RewardSide(str(side).lower())
    )
    shared = {
        "episode_id": episode_id,
        "run_namespace": run_namespace,
        "decision_window_index": decision_window_index,
        "frame_before": frame_before,
        "fact_frame_start": fact_frame_start,
        "fact_frame_end": fact_frame_end,
        "frame_after": frame_after,
        "sim_time_before_ms": sim_time_before_ms,
        "sim_time_after_ms": sim_time_after_ms,
        "joint_action_hash": joint_action_hash,
    }
    return RewardTransitionIdentity(
        episode_id=episode_id,
        run_namespace=run_namespace,
        side=normalized_side,
        decision_window_index=decision_window_index,
        frame_before=frame_before,
        fact_frame_start=fact_frame_start,
        fact_frame_end=fact_frame_end,
        frame_after=frame_after,
        sim_time_before_ms=sim_time_before_ms,
        sim_time_after_ms=sim_time_after_ms,
        decision_before_hash=decision_before_hash,
        action_catalog_hash=action_catalog_hash,
        submitted_action_ids=(
            None if submitted_action_ids is None else tuple(submitted_action_ids)
        ),
        submitted_action_semantic_hash=submitted_action_semantic_hash,
        joint_action_hash=joint_action_hash,
        decision_after_hash=decision_after_hash,
        joint_transition_id=canonical_hash(shared),
    )


def build_training_reward_record(
    *,
    transition_identity: RewardTransitionIdentity,
    terminated: bool,
    truncated: bool,
    winner: str | None,
    terminal_reason_code: str | None,
    truncation: TrainingTruncationProvenance | None = None,
    policy: BaseRewardPolicy | None = None,
) -> TrainingRewardRecord:
    active = load_reward_policy() if policy is None else policy
    provenance = TrainingTruncationProvenance.none() if truncation is None else truncation
    _validate_terminal_evidence(
        active, terminated=terminated, truncated=truncated, winner=winner,
        terminal_reason_code=terminal_reason_code, truncation=provenance,
    )
    red, blue = active.rewards_for(
        terminated=terminated, truncated=truncated, winner=winner,
        truncation_source=provenance.source,
    )
    total = red if transition_identity.side is RewardSide.RED else blue
    outcome = _reward_outcome(
        transition_identity.side, terminated=terminated, truncated=truncated,
        winner=winner,
    )
    source = _supervision_source(terminated, provenance.source)
    component = RewardComponent(
        active.component_namespace, active.component_code, total, source
    )
    return TrainingRewardRecord(
        schema_version=TRAINING_REWARD_SCHEMA_VERSION,
        reward_policy=active.identity,
        transition_identity=transition_identity,
        side=transition_identity.side,
        total_reward=total,
        components=(component,),
        outcome=outcome,
        terminated=terminated,
        truncated=truncated,
        terminal_reason_code=terminal_reason_code,
        truncation=provenance,
        supervision_source=source,
        privileged_supervision_used=(
            terminated
            or provenance.source is RewardTruncationSource.NATIVE_MAX_TICKS
        ),
    )


def build_joint_training_reward_record(
    *,
    red_transition_identity: RewardTransitionIdentity,
    blue_transition_identity: RewardTransitionIdentity,
    terminated: bool,
    truncated: bool,
    winner: str | None,
    terminal_reason_code: str | None,
    truncation: TrainingTruncationProvenance | None = None,
    policy: BaseRewardPolicy | None = None,
) -> JointTrainingRewardRecord:
    active = load_reward_policy() if policy is None else policy
    if red_transition_identity.side is not RewardSide.RED or (
        blue_transition_identity.side is not RewardSide.BLUE
    ):
        raise RewardContractError("joint reward identities must be RED then BLUE")
    if not red_transition_identity.same_joint_transition(blue_transition_identity):
        raise RewardContractError("reward identities do not share a transition")
    provenance = TrainingTruncationProvenance.none() if truncation is None else truncation
    red = build_training_reward_record(
        transition_identity=red_transition_identity,
        terminated=terminated,
        truncated=truncated,
        winner=winner,
        terminal_reason_code=terminal_reason_code,
        truncation=provenance,
        policy=active,
    )
    blue = build_training_reward_record(
        transition_identity=blue_transition_identity,
        terminated=terminated,
        truncated=truncated,
        winner=winner,
        terminal_reason_code=terminal_reason_code,
        truncation=provenance,
        policy=active,
    )
    return JointTrainingRewardRecord(
        schema_version=JOINT_TRAINING_REWARD_SCHEMA_VERSION,
        reward_policy=active.identity,
        relation=active.relation,
        winner=None if winner is None else RewardSide(winner),
        red=red,
        blue=blue,
    )


def joint_action_hash(
    red_action_ids: Sequence[str] | None,
    blue_action_ids: Sequence[str] | None,
    *,
    red_semantic_hash: str,
    blue_semantic_hash: str,
) -> str:
    _sha256(red_semantic_hash, "RED action semantic hash")
    _sha256(blue_semantic_hash, "BLUE action semantic hash")
    return canonical_hash({
        "red": {
            "action_ids": None if red_action_ids is None else list(red_action_ids),
            "semantic_hash": red_semantic_hash,
        },
        "blue": {
            "action_ids": None if blue_action_ids is None else list(blue_action_ids),
            "semantic_hash": blue_semantic_hash,
        },
    })


def _validate_terminal_evidence(
    policy: BaseRewardPolicy,
    *,
    terminated: bool,
    truncated: bool,
    winner: str | None,
    terminal_reason_code: str | None,
    truncation: TrainingTruncationProvenance,
) -> None:
    _terminal_flags(terminated, truncated)
    if terminated:
        if terminal_reason_code not in policy.terminal_reason_codes:
            raise RewardContractError("terminal reason is not policy-authorized")
        if truncation.source is not RewardTruncationSource.NONE:
            raise RewardContractError("terminated transition cannot be truncated")
        return
    if truncated:
        if truncation.source is RewardTruncationSource.NATIVE_MAX_TICKS:
            if terminal_reason_code not in policy.native_truncation_reason_codes:
                raise RewardContractError("native truncation reason mismatch")
        elif truncation.source in {
            RewardTruncationSource.TRAINING_MAX_STEPS,
            RewardTruncationSource.TRAINING_MAX_DECISION_WINDOWS,
        }:
            if terminal_reason_code is not None:
                raise RewardContractError("training truncation has Engine reason")
        else:
            raise RewardContractError("truncated transition lacks provenance")
        return
    if terminal_reason_code is not None:
        raise RewardContractError("nonterminal transition has terminal reason")
    if truncation.source is not RewardTruncationSource.NONE:
        raise RewardContractError("nonterminal transition has truncation provenance")


def _validate_record_outcome(record: TrainingRewardRecord) -> None:
    base_reward = record.components[0].amount
    if record.terminated:
        if record.outcome is RewardOutcome.WIN and base_reward != 1.0:
            raise RewardContractError("winning reward must be +1")
        if record.outcome is RewardOutcome.LOSS and base_reward != -1.0:
            raise RewardContractError("losing reward must be -1")
        if record.outcome is RewardOutcome.DRAW and base_reward != 0.0:
            raise RewardContractError("draw reward must be zero")
        if record.outcome not in {
            RewardOutcome.WIN, RewardOutcome.LOSS, RewardOutcome.DRAW
        }:
            raise RewardContractError("terminal reward outcome mismatch")
    elif record.truncated:
        if record.outcome is not RewardOutcome.TRUNCATED or base_reward != 0.0:
            raise RewardContractError("truncated reward outcome mismatch")
    elif record.outcome is not RewardOutcome.NONTERMINAL or base_reward != 0.0:
        raise RewardContractError("nonterminal reward outcome mismatch")


def _reward_outcome(
    side: RewardSide, *, terminated: bool, truncated: bool, winner: str | None
) -> RewardOutcome:
    if truncated:
        return RewardOutcome.TRUNCATED
    if not terminated:
        return RewardOutcome.NONTERMINAL
    if winner is None:
        return RewardOutcome.DRAW
    return RewardOutcome.WIN if winner == side.value else RewardOutcome.LOSS


def _supervision_source(
    terminated: bool, truncation_source: RewardTruncationSource
) -> RewardSupervisionSource:
    if terminated or truncation_source is RewardTruncationSource.NATIVE_MAX_TICKS:
        return RewardSupervisionSource.ENGINE_TERMINAL_AUTHORITY
    if truncation_source in {
        RewardTruncationSource.TRAINING_MAX_STEPS,
        RewardTruncationSource.TRAINING_MAX_DECISION_WINDOWS,
    }:
        return RewardSupervisionSource.TRAINING_TRUNCATION_AUTHORITY
    return RewardSupervisionSource.ENGINE_COMMITTED_TRANSITION


def _training_reward_payload(value: TrainingRewardRecord) -> dict[str, object]:
    result = {
        "schema_version": value.schema_version,
        "reward_policy": value.reward_policy.to_dict(),
        "transition_identity": value.transition_identity.to_dict(),
        "side": value.side.value,
        "total_reward": value.total_reward,
        "components": [item.to_dict() for item in value.components],
        "outcome": value.outcome.value,
        "terminated": value.terminated,
        "truncated": value.truncated,
        "terminal_reason_code": value.terminal_reason_code,
        "truncation": value.truncation.to_dict(),
        "supervision_source": value.supervision_source.value,
        "privileged_supervision_used": value.privileged_supervision_used,
    }
    if value.composite_policy is not None:
        result["composite_policy"] = value.composite_policy.to_dict()
    return result


def _joint_reward_payload(value: JointTrainingRewardRecord) -> dict[str, object]:
    result = {
        "schema_version": value.schema_version,
        "reward_policy": value.reward_policy.to_dict(),
        "relation": value.relation.value,
        "winner": None if value.winner is None else value.winner.value,
        "joint_transition_id": value.joint_transition_id,
        "red": value.red.to_dict(),
        "blue": value.blue.to_dict(),
    }
    if value.composite_policy is not None:
        result["composite_policy"] = value.composite_policy.to_dict()
    return result


def _joint_transition_payload(value: RewardTransitionIdentity) -> dict[str, object]:
    return {
        "episode_id": value.episode_id,
        "run_namespace": value.run_namespace,
        "decision_window_index": value.decision_window_index,
        "frame_before": value.frame_before,
        "fact_frame_start": value.fact_frame_start,
        "fact_frame_end": value.fact_frame_end,
        "frame_after": value.frame_after,
        "sim_time_before_ms": value.sim_time_before_ms,
        "sim_time_after_ms": value.sim_time_after_ms,
        "joint_action_hash": value.joint_action_hash,
    }


def _terminal_flags(terminated: bool, truncated: bool) -> None:
    if type(terminated) is not bool or type(truncated) is not bool:
        raise TypeError("terminated and truncated must be exact bool")
    if terminated and truncated:
        raise RewardContractError("transition cannot be terminated and truncated")


def _finite_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise RewardContractError(f"{name} must be finite")
    return result


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RewardContractError(f"{name} must be a non-empty trimmed string")
    return value


def _machine_code(value: object, name: str) -> str:
    result = _nonempty(value, name)
    if any(not (character.isupper() or character.isdigit() or character == "_")
           for character in result):
        if name.endswith("namespace") and result.islower() and result.isalpha():
            return result
        raise RewardContractError(f"{name} must be a stable machine code")
    return result


def _nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise TypeError(f"{name} must be a non-negative exact int")
    return value


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise TypeError(f"{name} must be a positive exact int")
    return value


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise RewardContractError(f"{name} must be lowercase SHA-256")
    if any(character not in "0123456789abcdef" for character in value):
        raise RewardContractError(f"{name} must be lowercase SHA-256")
    return value


def _exact_mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    return value


def _string_sequence(value: object, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{name} must be a sequence")
    result = tuple(_nonempty(item, name) for item in value)
    if len(result) != len(set(result)):
        raise RewardContractError(f"{name} must be unique")
    return result


__all__ = [
    "BaseRewardPolicy",
    "JOINT_TRAINING_REWARD_SCHEMA_VERSION",
    "JointTrainingRewardRecord",
    "REWARD_POLICY_SCHEMA_VERSION",
    "RewardComponent",
    "RewardContractError",
    "RewardOutcome",
    "RewardPolicyIdentity",
    "RewardRelation",
    "RewardSide",
    "RewardSupervisionSource",
    "RewardTransitionIdentity",
    "RewardTruncationSource",
    "TRAINING_REWARD_SCHEMA_VERSION",
    "TRAINING_TRUNCATION_CONFIG_SCHEMA_VERSION",
    "TrainingRewardRecord",
    "TrainingTruncationProvenance",
    "build_joint_training_reward_record",
    "build_reward_transition_identity",
    "build_training_reward_record",
    "joint_action_hash",
    "load_reward_policy",
]
