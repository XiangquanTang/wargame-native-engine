"""Transactional publication of one complete Native training episode package."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Mapping, Sequence

from wargame_native_engine.analysis import (
    EightFactorAnalyzer,
    analyze_replay_to_jsonl,
)
from wargame_native_engine.analysis.jsonl import analysis_sidecar_from_jsonl
from wargame_native_engine.analysis.native_analyzer import NativeEightFactorAnalyzer
from wargame_native_engine.compat.source_frame.serialization import (
    canonical_source_json,
)
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchive,
    SourceReplayArchiveReader,
)
from wargame_native_engine.io.replay import replay_source_archive
from wargame_native_engine.io.serialization import file_sha256, json_clone
from wargame_native_engine.operations.facade import MatchHandle
from wargame_native_engine.training.models import DecisionTransition
from wargame_native_engine.training.reward import (
    JointTrainingRewardRecord,
    load_reward_policy,
)
from wargame_native_engine.training.reward_replay import (
    joint_records_from_transitions,
    reward_sequence_hash,
    verify_reward_sequence,
    verify_composite_reward_sequence,
)
from wargame_native_engine.training.discount import SharedTrainingDiscountConfig
from wargame_native_engine.training.eight_factor_shaping_contract import (
    EightFactorShapingPolicy,
)
from wargame_native_engine.training.eight_factor_shaping import (
    CompositeRewardPolicy,
)
from wargame_native_engine.training.transitions import (
    DECISION_TRANSITIONS_NAME,
    decision_transitions_to_jsonl,
    write_decision_transitions_jsonl,
)


MATCH_SUMMARY_NAME = "match_summary.native.json"


class TrainingEpisodePublicationError(RuntimeError):
    """A complete sample package could not be verified and published."""

    def __init__(self, phase: str, cause: BaseException) -> None:
        self.phase = phase
        self.cause = cause
        super().__init__(
            f"training_episode_publication_failed:{phase}:"
            f"{type(cause).__name__}:{cause}"
        )


@dataclass(frozen=True, slots=True)
class TrainingEpisodePublication:
    output_dir: Path
    replay_path: Path
    analysis_path: Path
    transition_path: Path
    summary_path: Path
    control_audit_path: Path | None
    episode_id: str
    run_namespace: str
    decision_window_count: int
    transition_count: int
    fact_frame_count: int
    step_count: int
    terminal: bool
    truncated: bool
    winner: str | None
    outcome: str | None
    final_state_hash: str
    strict_replay_match: bool
    online_offline_analysis_match: bool
    reward_record_count: int
    reward_sequence_hash: str
    reward_replay_match: bool
    reward_policy_id: str
    reward_policy_version: str
    reward_policy_hash: str


def publish_training_episode(
    match: MatchHandle,
    output_dir: str | Path,
    *,
    run_namespace: str,
    episode_id: str,
    transitions: Sequence[DecisionTransition],
    joint_reward_records: Sequence[JointTrainingRewardRecord] | None = None,
    decision_window_count: int,
    truncated: bool,
    outcome: str | None,
    summary_schema: str,
    summary_extra: Mapping[str, Any] | None = None,
    overwrite: bool = False,
    shaping_policy: EightFactorShapingPolicy | None = None,
    discount_config: SharedTrainingDiscountConfig | None = None,
    composite_policy: CompositeRewardPolicy | None = None,
) -> TrainingEpisodePublication:
    """Build, verify, then atomically expose one complete training sample."""

    if not isinstance(match, MatchHandle):
        raise TypeError("match must be a MatchHandle")
    namespace = _nonempty(run_namespace, "run_namespace")
    episode = _nonempty(episode_id, "episode_id")
    schema = _nonempty(summary_schema, "summary_schema")
    if type(decision_window_count) is not int or decision_window_count < 0:
        raise TypeError("decision_window_count must be a non-negative exact int")
    if type(truncated) is not bool or type(overwrite) is not bool:
        raise TypeError("truncated and overwrite must be exact bool values")
    values = tuple(transitions)
    if not all(isinstance(value, DecisionTransition) for value in values):
        raise TypeError("transitions must contain DecisionTransition records")
    derived_rewards = joint_records_from_transitions(values)
    reward_values = (
        derived_rewards
        if joint_reward_records is None
        else tuple(joint_reward_records)
    )
    if not all(isinstance(value, JointTrainingRewardRecord)
               for value in reward_values):
        raise TypeError("joint_reward_records must contain joint reward records")
    if reward_values != derived_rewards:
        raise ValueError("joint reward records do not match transition records")
    reward_policy = (
        reward_values[0].reward_policy
        if reward_values else load_reward_policy().identity
    )
    target = Path(output_dir)
    if os.path.lexists(target) and not overwrite:
        raise FileExistsError(f"training episode output already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(
        prefix=f".{target.name}.training.", suffix=".tmp", dir=target.parent,
    ))
    staging_episode = staging_root / "episode"
    phase = "facts"
    try:
        facts = match.publish(
            staging_episode,
            truncated=truncated,
            overwrite=False,
        )
        phase = "transitions"
        transition_path = write_decision_transitions_jsonl(
            staging_episode / DECISION_TRANSITIONS_NAME, values
        )
        phase = "verification"
        archive = SourceReplayArchiveReader(facts.replay_path)
        verification = replay_source_archive(
            match.bundle.scenario,
            facts.replay_path,
            strict=True,
            map_runtime_data=match.bundle.map_runtime_data,
        )
        if not verification.matches:
            raise ValueError("strict replay final state hash mismatch")
        _verify_offline_analysis(facts.replay_path, facts.analysis_path)
        terminal = _manifest_bool(archive.manifest, "termination")
        published_truncated = _manifest_bool(archive.manifest, "truncated")
        winner = archive.manifest.get("winner")
        if winner is not None and winner not in {"red", "blue"}:
            raise ValueError("published winner is invalid")
        final_state_hash = _manifest_text(
            archive.manifest, "final_state_hash"
        )
        if (
            "outcome" in archive.manifest
            and archive.manifest.get("outcome") != outcome
        ):
            raise ValueError("published outcome does not match training result")
        if archive.frame_count != facts.frame_count:
            raise ValueError("published replay fact frame count mismatch")
        _verify_analysis_alignment(archive, facts.analysis_path)
        _verify_transitions(
            values,
            episode_id=episode,
            run_namespace=namespace,
            decision_window_count=decision_window_count,
            terminal=terminal,
            truncated=published_truncated,
        )
        _verify_transition_file(transition_path, values)
        has_shaping = bool(reward_values and reward_values[0].composite_policy)
        if has_shaping:
            if shaping_policy is None or discount_config is None or (
                composite_policy is None
            ):
                raise ValueError("composite reward replay authority is missing")
            analyses = analysis_sidecar_from_jsonl(
                facts.analysis_path.read_bytes()
            ).timeline.frame_analyses
            reward_verification = verify_composite_reward_sequence(
                values,
                frame_analyses={value.provenance.frame_index: value for value in analyses},
                terminal=terminal,
                truncated=published_truncated,
                winner=winner,
                terminal_reason_code=archive.manifest.get("termination_reason"),
                base_policy=load_reward_policy(),
                shaping_policy=shaping_policy,
                discount=discount_config,
                composite_policy=composite_policy,
            )
        else:
            reward_verification = verify_reward_sequence(
                values,
                terminal=terminal,
                truncated=published_truncated,
                winner=winner,
                terminal_reason_code=archive.manifest.get("termination_reason"),
            )
        if not reward_verification.matches:
            raise ValueError("strict replay reward re-derivation mismatch")
        if facts.control_audit_path is not None:
            _verify_jsonl(facts.control_audit_path)
        summary: dict[str, Any] = {
            "summary_schema": schema,
            "status": "completed" if terminal else "truncated",
            "run_namespace": namespace,
            "episode_id": episode,
            "seed": archive.manifest.get("episode_seed"),
            "scenario_id": archive.manifest.get("scenario_id"),
            "scenario_hash": archive.manifest.get("scenario_hash"),
            "map_id": match.bundle.map_identity.map_id,
            "map_hash": match.bundle.map_identity.sha256,
            "decision_window_count": decision_window_count,
            "transition_count": len(values),
            "fact_frame_count": facts.frame_count,
            "step_count": facts.step_count,
            "terminal": terminal,
            "truncated": published_truncated,
            "winner": winner,
            "outcome": outcome,
            "final_state_hash": final_state_hash,
            "replay_sha256": file_sha256(facts.replay_path),
            "analysis_sha256": file_sha256(facts.analysis_path),
            "transitions_sha256": file_sha256(transition_path),
            "reward_record_count": len(reward_values),
            "reward_sequence_hash": reward_sequence_hash(reward_values),
            "reward_replay_match": True,
            "reward_policy": reward_policy.to_dict(),
            "strict_replay_match": True,
            "online_offline_analysis_match": True,
        }
        if has_shaping:
            assert composite_policy is not None
            summary["composite_reward_policy"] = composite_policy.to_dict()
        reserved = frozenset(summary)
        for key, value in (summary_extra or {}).items():
            if key in reserved:
                raise ValueError(f"summary_extra cannot replace {key}")
            summary[str(key)] = json_clone(value)
        phase = "summary"
        summary_path = staging_episode / MATCH_SUMMARY_NAME
        _write_summary(summary_path, summary)
        phase = "integrity"
        _verify_summary(
            summary_path,
            archive=archive,
            transitions=values,
            rewards=reward_values,
            facts_frame_count=facts.frame_count,
            facts_step_count=facts.step_count,
        )
        _fsync_tree(staging_episode)
        phase = "publish"
        _publish_staging(staging_episode, target, overwrite=overwrite)
        return TrainingEpisodePublication(
            output_dir=target,
            replay_path=target / "replay.zip",
            analysis_path=target / "frame_analysis.native.jsonl",
            transition_path=target / DECISION_TRANSITIONS_NAME,
            summary_path=target / MATCH_SUMMARY_NAME,
            control_audit_path=(
                target / "control_audit.native.jsonl"
                if (target / "control_audit.native.jsonl").is_file()
                else None
            ),
            episode_id=episode,
            run_namespace=namespace,
            decision_window_count=decision_window_count,
            transition_count=len(values),
            fact_frame_count=facts.frame_count,
            step_count=facts.step_count,
            terminal=terminal,
            truncated=published_truncated,
            winner=winner,
            outcome=outcome,
            final_state_hash=final_state_hash,
            strict_replay_match=True,
            online_offline_analysis_match=True,
            reward_record_count=len(reward_values),
            reward_sequence_hash=reward_sequence_hash(reward_values),
            reward_replay_match=True,
            reward_policy_id=reward_policy.policy_id,
            reward_policy_version=reward_policy.policy_version,
            reward_policy_hash=reward_policy.policy_hash,
        )
    except TrainingEpisodePublicationError:
        raise
    except BaseException as exc:
        raise TrainingEpisodePublicationError(phase, exc) from exc
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def _verify_offline_analysis(replay_path: Path, analysis_path: Path) -> None:
    # Model selection is explicit in the recorded identity, not inferred from
    # a filename or from the presence of optional Native fields. Full-byte
    # equality below verifies every identity and derived value again.
    with analysis_path.open(encoding="utf-8") as stream:
        first = json.loads(stream.readline())
    identity = first.get("provenance", {})
    native = NativeEightFactorAnalyzer()
    analyzer = (native if identity.get("model_id") == native.model_id
                else EightFactorAnalyzer())
    with tempfile.TemporaryDirectory(
        prefix="wargame-training-analysis."
    ) as temporary:
        offline = Path(temporary) / "frame_analysis.native.jsonl"
        analyze_replay_to_jsonl(
            replay_path, offline, analyzer=analyzer
        )
        if not _files_equal(offline, analysis_path):
            raise ValueError("online and offline analysis JSONL differ")


def _verify_analysis_alignment(
    archive: SourceReplayArchive | SourceReplayArchiveReader,
    analysis_path: Path,
) -> None:
    frame_count = (
        archive.frame_count
        if isinstance(archive, SourceReplayArchiveReader)
        else len(archive.frames)
    )
    expected_hashes = (
        tuple(archive.manifest.get("frame_hashes", ()))
        if isinstance(archive, SourceReplayArchiveReader)
        else tuple(fact.source_hash for fact in archive.frames)
    )
    count = 0
    with analysis_path.open("r", encoding="utf-8", newline="") as stream:
        for index, line in enumerate(stream):
            if not line.endswith("\n") or not line.strip():
                raise ValueError("invalid analysis JSONL framing")
            value = json.loads(line)
            provenance = value.get("provenance") if isinstance(value, Mapping) else None
            if not isinstance(provenance, Mapping):
                raise ValueError("analysis provenance is missing")
            if index >= len(expected_hashes):
                raise ValueError("analysis/replay fact frame count mismatch")
            if provenance.get("frame_index") != index:
                raise ValueError("analysis frame index mismatch")
            if provenance.get("frame_hash") != expected_hashes[index]:
                raise ValueError("analysis fact frame hash mismatch")
            count += 1
    if count != frame_count:
        raise ValueError("analysis/replay fact frame count mismatch")


def _files_equal(first: Path, second: Path) -> bool:
    if first.stat().st_size != second.stat().st_size:
        return False
    with first.open("rb") as left, second.open("rb") as right:
        while True:
            left_chunk = left.read(1024 * 1024)
            right_chunk = right.read(1024 * 1024)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def _verify_transitions(
    values: Sequence[DecisionTransition],
    *,
    episode_id: str,
    run_namespace: str,
    decision_window_count: int,
    terminal: bool,
    truncated: bool,
) -> None:
    if len(values) != decision_window_count * 2:
        raise ValueError("transition count must equal decision windows times two")
    for offset, value in enumerate(values):
        expected_decision = offset // 2
        expected_side = "red" if offset % 2 == 0 else "blue"
        if value.decision_index != expected_decision or value.side != expected_side:
            raise ValueError("transition decision/side order mismatch")
        if value.episode_id != episode_id:
            raise ValueError("transition episode_id mismatch")
        if value.run_namespace != run_namespace:
            raise ValueError("transition run_namespace mismatch")
        is_final = expected_decision == decision_window_count - 1
        if value.terminated != (terminal and is_final):
            raise ValueError("transition terminal status mismatch")
        if value.truncated != (truncated and is_final):
            raise ValueError("transition truncated status mismatch")


def _verify_transition_file(
    path: Path,
    values: Sequence[DecisionTransition],
) -> None:
    records = _verify_jsonl(path)
    if len(records) != len(values):
        raise ValueError("transition JSONL record count mismatch")
    if path.read_text(encoding="utf-8") != decision_transitions_to_jsonl(values):
        raise ValueError("transition JSONL content mismatch")


def _verify_jsonl(path: Path) -> list[Mapping[str, Any]]:
    payload = path.read_bytes()
    if not payload or not payload.endswith(b"\n") or payload.endswith(b"\n\n"):
        raise ValueError(f"invalid canonical JSONL framing: {path.name}")
    result: list[Mapping[str, Any]] = []
    for line in payload[:-1].split(b"\n"):
        if not line.strip():
            raise ValueError(f"blank JSONL line: {path.name}")
        value = json.loads(line)
        if not isinstance(value, Mapping):
            raise ValueError(f"JSONL record must be an object: {path.name}")
        result.append(value)
    return result


def _write_summary(path: Path, value: Mapping[str, Any]) -> None:
    payload = (canonical_source_json(value) + "\n").encode("utf-8")
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _verify_summary(
    path: Path,
    *,
    archive: SourceReplayArchive,
    transitions: Sequence[DecisionTransition],
    rewards: Sequence[JointTrainingRewardRecord],
    facts_frame_count: int,
    facts_step_count: int,
) -> None:
    payload = path.read_bytes()
    if not payload.endswith(b"\n") or payload.endswith(b"\n\n"):
        raise ValueError("summary must have exactly one trailing LF")
    value = json.loads(payload)
    if value["fact_frame_count"] != facts_frame_count:
        raise ValueError("summary fact frame count mismatch")
    if value["step_count"] != facts_step_count:
        raise ValueError("summary step count mismatch")
    if value["transition_count"] != len(transitions):
        raise ValueError("summary transition count mismatch")
    if value["reward_record_count"] != len(rewards):
        raise ValueError("summary reward record count mismatch")
    if value["reward_sequence_hash"] != reward_sequence_hash(rewards):
        raise ValueError("summary reward sequence hash mismatch")
    if value["reward_replay_match"] is not True:
        raise ValueError("summary reward replay verification missing")
    expected_policy = (
        rewards[0].reward_policy if rewards else load_reward_policy().identity
    )
    if value["reward_policy"] != expected_policy.to_dict():
        raise ValueError("summary reward policy mismatch")
    if value["terminal"] is not _manifest_bool(archive.manifest, "termination"):
        raise ValueError("summary terminal mismatch")
    if value["truncated"] is not _manifest_bool(archive.manifest, "truncated"):
        raise ValueError("summary truncated mismatch")
    if value["winner"] != archive.manifest.get("winner"):
        raise ValueError("summary winner mismatch")


def _publish_staging(staging: Path, target: Path, *, overwrite: bool) -> None:
    if not os.path.lexists(target):
        os.replace(staging, target)
        _fsync_directory(target.parent)
        return
    if not overwrite:
        raise FileExistsError(f"training episode output already exists: {target}")
    backup = Path(tempfile.mkdtemp(
        prefix=f".{target.name}.previous.", dir=target.parent
    ))
    backup.rmdir()
    os.replace(target, backup)
    try:
        os.replace(staging, target)
        _fsync_directory(target.parent)
        if backup.is_dir() and not backup.is_symlink():
            shutil.rmtree(backup)
        else:
            backup.unlink()
    except BaseException:
        if os.path.lexists(target):
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        if os.path.lexists(backup):
            os.replace(backup, target)
        raise


def _fsync_tree(root: Path) -> None:
    for path in sorted(value for value in root.rglob("*") if value.is_file()):
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
    for path in sorted(
        (value for value in root.rglob("*") if value.is_dir()), reverse=True
    ):
        _fsync_directory(path)
    _fsync_directory(root)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _manifest_bool(manifest: Mapping[str, Any], field: str) -> bool:
    value = manifest.get(field)
    if type(value) is not bool:
        raise TypeError(f"replay manifest {field} must be an exact bool")
    return value


def _manifest_text(manifest: Mapping[str, Any], field: str) -> str:
    value = manifest.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"replay manifest {field} must be non-empty")
    return value


def _nonempty(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{field} must not contain surrounding whitespace")
    return value
