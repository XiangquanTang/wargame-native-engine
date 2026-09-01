"""Derived reward-sequence serialization and replay verification."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
from typing import Mapping, Sequence

from wargame_native_engine.analysis.models import FrameAnalysis

from wargame_native_engine.compat.source_frame.serialization import (
    canonical_source_json,
)
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.training.models import DecisionTransition
from wargame_native_engine.training.reward import (
    BaseRewardPolicy,
    JointTrainingRewardRecord,
    RewardContractError,
    build_joint_training_reward_record,
    load_reward_policy,
)
from wargame_native_engine.training.discount import SharedTrainingDiscountConfig
from wargame_native_engine.training.eight_factor_shaping_contract import (
    EightFactorShapingPolicy,
)
from wargame_native_engine.training.eight_factor_shaping import (
    CompositeRewardPolicy,
    compose_joint_training_reward,
    derive_joint_eight_factor_shaping,
)


REWARD_RECORDS_NAME = "joint_reward_records.native.jsonl"


@dataclass(frozen=True, slots=True)
class RewardSequenceVerification:
    record_count: int
    expected_sequence_hash: str
    rederived_sequence_hash: str
    matches: bool
    records: tuple[JointTrainingRewardRecord, ...]


def joint_records_from_transitions(
    transitions: Sequence[DecisionTransition],
) -> tuple[JointTrainingRewardRecord, ...]:
    """Reconstruct stored joint records without inventing reward semantics."""

    pairs = _transition_pairs(transitions)
    result = []
    for red, blue in pairs:
        winner = None
        if red.reward_record.outcome.value == "WIN":
            winner = red.reward_record.side
        elif blue.reward_record.outcome.value == "WIN":
            winner = blue.reward_record.side
        result.append(JointTrainingRewardRecord(
            schema_version="native-training-joint-reward/1.0",
            reward_policy=red.reward_record.reward_policy,
            relation=load_reward_policy().relation,
            winner=winner,
            red=red.reward_record,
            blue=blue.reward_record,
            composite_policy=red.reward_record.composite_policy,
        ))
    return tuple(result)


def verify_reward_sequence(
    transitions: Sequence[DecisionTransition],
    *,
    terminal: bool,
    truncated: bool,
    winner: str | None,
    terminal_reason_code: str | None,
    policy: BaseRewardPolicy | None = None,
) -> RewardSequenceVerification:
    """Re-derive rewards from committed transition identities and result authority."""

    values = _transition_pairs(transitions)
    active = load_reward_policy() if policy is None else policy
    expected = joint_records_from_transitions(transitions)
    rederived = []
    for index, (red, blue) in enumerate(values):
        is_final = index == len(values) - 1
        pair_terminated = terminal and is_final
        pair_truncated = truncated and is_final
        if red.terminated != pair_terminated or blue.terminated != pair_terminated:
            raise RewardContractError("stored reward terminal alignment mismatch")
        if red.truncated != pair_truncated or blue.truncated != pair_truncated:
            raise RewardContractError("stored reward truncation alignment mismatch")
        provenance = red.reward_record.truncation
        if provenance != blue.reward_record.truncation:
            raise RewardContractError("stored side truncation provenance differs")
        record = build_joint_training_reward_record(
            red_transition_identity=red.reward_record.transition_identity,
            blue_transition_identity=blue.reward_record.transition_identity,
            terminated=pair_terminated,
            truncated=pair_truncated,
            winner=winner if pair_terminated else None,
            terminal_reason_code=(
                terminal_reason_code if pair_terminated or pair_truncated else None
            ),
            truncation=provenance,
            policy=active,
        )
        rederived.append(record)
    expected_tuple = tuple(expected)
    rederived_tuple = tuple(rederived)
    expected_hash = reward_sequence_hash(expected_tuple)
    actual_hash = reward_sequence_hash(rederived_tuple)
    return RewardSequenceVerification(
        record_count=len(rederived_tuple),
        expected_sequence_hash=expected_hash,
        rederived_sequence_hash=actual_hash,
        matches=expected_tuple == rederived_tuple and expected_hash == actual_hash,
        records=rederived_tuple,
    )


def verify_composite_reward_sequence(
    transitions: Sequence[DecisionTransition],
    *,
    frame_analyses: Mapping[int, FrameAnalysis],
    terminal: bool,
    truncated: bool,
    winner: str | None,
    terminal_reason_code: str | None,
    base_policy: BaseRewardPolicy,
    shaping_policy: EightFactorShapingPolicy,
    discount: SharedTrainingDiscountConfig,
    composite_policy: CompositeRewardPolicy,
) -> RewardSequenceVerification:
    """Strictly rederive shaping from replayed endpoint analyses, never scalars."""

    values = _transition_pairs(transitions)
    expected = joint_records_from_transitions(transitions)
    rederived = []
    for index, (red, blue) in enumerate(values):
        is_final = index == len(values) - 1
        pair_terminated = terminal and is_final
        pair_truncated = truncated and is_final
        if red.terminated != pair_terminated or blue.terminated != pair_terminated:
            raise RewardContractError("stored composite terminal alignment mismatch")
        if red.truncated != pair_truncated or blue.truncated != pair_truncated:
            raise RewardContractError("stored composite truncation alignment mismatch")
        provenance = red.reward_record.truncation
        if provenance != blue.reward_record.truncation:
            raise RewardContractError("stored side truncation provenance differs")
        base = build_joint_training_reward_record(
            red_transition_identity=red.reward_record.transition_identity,
            blue_transition_identity=blue.reward_record.transition_identity,
            terminated=pair_terminated,
            truncated=pair_truncated,
            winner=winner if pair_terminated else None,
            terminal_reason_code=(
                terminal_reason_code if pair_terminated or pair_truncated else None
            ),
            truncation=provenance,
            policy=base_policy,
        )
        identity = base.red.transition_identity
        try:
            before = frame_analyses[identity.frame_before]
            after = None if pair_terminated else frame_analyses[identity.frame_after]
        except KeyError as exc:
            raise RewardContractError(
                "replayed shaping endpoint analysis is missing"
            ) from exc
        shaping = derive_joint_eight_factor_shaping(
            base_record=base,
            analysis_before=before,
            analysis_after=after,
            policy=shaping_policy,
            discount=discount,
        )
        rederived.append(compose_joint_training_reward(
            base_record=base,
            shaping=shaping,
            composite_policy=composite_policy,
        ))
    expected_tuple = tuple(expected)
    rederived_tuple = tuple(rederived)
    expected_hash = reward_sequence_hash(expected_tuple)
    actual_hash = reward_sequence_hash(rederived_tuple)
    return RewardSequenceVerification(
        record_count=len(rederived_tuple),
        expected_sequence_hash=expected_hash,
        rederived_sequence_hash=actual_hash,
        matches=expected_tuple == rederived_tuple and expected_hash == actual_hash,
        records=rederived_tuple,
    )


def reward_sequence_hash(
    values: Sequence[JointTrainingRewardRecord],
) -> str:
    records = tuple(values)
    if not all(isinstance(value, JointTrainingRewardRecord) for value in records):
        raise TypeError("reward sequence must contain joint reward records")
    return canonical_hash([value.to_dict() for value in records])


def reward_records_to_jsonl(
    values: Sequence[JointTrainingRewardRecord],
) -> str:
    records = tuple(values)
    if not all(isinstance(value, JointTrainingRewardRecord) for value in records):
        raise TypeError("values must contain joint reward records")
    return "".join(
        canonical_source_json(value.to_dict()) + "\n" for value in records
    )


def write_reward_records_jsonl(
    path: str | Path,
    values: Sequence[JointTrainingRewardRecord],
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = reward_records_to_jsonl(values).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def _transition_pairs(
    transitions: Sequence[DecisionTransition],
) -> tuple[tuple[DecisionTransition, DecisionTransition], ...]:
    values = tuple(transitions)
    if len(values) % 2:
        raise RewardContractError("reward transitions must contain side pairs")
    result = []
    for offset in range(0, len(values), 2):
        red, blue = values[offset:offset + 2]
        if red.side != "red" or blue.side != "blue":
            raise RewardContractError("reward transition order must be RED then BLUE")
        if red.decision_index != blue.decision_index:
            raise RewardContractError("reward transition decision indices differ")
        result.append((red, blue))
    return tuple(result)
