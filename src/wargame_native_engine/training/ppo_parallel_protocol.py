"""Typed detached IPC contract for synchronous Native PPO env workers."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping

from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.training.decision_encoder import DecisionObservationEncoder
from wargame_native_engine.training.training_seed import TrainingSeedConfig


PPO_PARALLEL_WORKER_PROTOCOL_SCHEMA_VERSION = (
    "native-ppo-parallel-worker-protocol/1.0"
)
PPO_PARALLEL_WORKER_PROTOCOL_ID = "native-ppo-parallel-worker-protocol"
PPO_PARALLEL_WORKER_PROTOCOL_VERSION = "1.0.0"

_OBSERVATION_ENCODER = DecisionObservationEncoder()
_OBSERVATION_WIDTH = _OBSERVATION_ENCODER.observation_space.flat_shape[0]

_PROTOCOL_SPEC = {
    "observation_encoder_hash": _OBSERVATION_ENCODER.contract_hash,
    "observation_width": _OBSERVATION_WIDTH,
    "schema_version": PPO_PARALLEL_WORKER_PROTOCOL_SCHEMA_VERSION,
    "protocol_id": PPO_PARALLEL_WORKER_PROTOCOL_ID,
    "protocol_version": PPO_PARALLEL_WORKER_PROTOCOL_VERSION,
    "transport": "DEDICATED_DUPLEX_PIPE_PER_WORKER",
    "ordering": "REQUEST_ID_AND_ENV_INDEX_VALIDATED_STABLE_GATHER",
    "messages": {
        "WorkerInitRequest": ["run_id", "worker_index", "env_index"],
        "WorkerReady": ["authority", "initial_decision"],
        "WorkerDecisionEnvelope": ["binding_token", "observation", "action_mask"],
        "CoordinatorActionCommand": ["binding_token", "action_index"],
        "WorkerTransitionResult": [
            "action_id", "reward", "next_observation", "next_decision"
        ],
        "WorkerResetRequest": ["episode_index", "evaluation"],
        "WorkerResetResult": ["decision"],
        "WorkerShutdown": [],
        "WorkerShutdownAck": [],
        "WorkerError": ["code", "detail"],
    },
    "post_truncation_bootstrap": {
        "observation_required": True,
        "action_mask_may_be_all_false": True,
        "action_command_forbidden": True,
        "reset_observation_substitution": "FORBIDDEN",
    },
    "forbidden_payloads": [
        "WorldState", "ControllerView", "ActionCatalogSnapshot", "GREEN",
        "FrameAnalysis", "Engine", "SessionManager",
    ],
}
PPO_PARALLEL_WORKER_PROTOCOL_HASH = canonical_hash(_PROTOCOL_SPEC)


class PPOParallelProtocolError(RuntimeError):
    """An IPC message violated the versioned parallel worker contract."""


@dataclass(frozen=True, slots=True)
class WorkerDecisionEnvelope:
    protocol_version: str
    run_id: str
    worker_index: int
    env_index: int
    request_id: int
    episode_index: int
    side: str
    decision_window_index: int
    frame_index: int
    sim_time_ms: int
    decision_hash: str
    catalog_hash: str
    action_encoder_hash: str
    preprocessor_hash: str
    observation: tuple[float, ...]
    action_mask: tuple[bool, ...]
    learner_tensor_hash: str
    binding_token: str
    scenario_id: str
    post_truncation_observation: bool = False

    def __post_init__(self) -> None:
        _protocol(self.protocol_version)
        _text(self.run_id, "run_id")
        for name in (
            "worker_index", "env_index", "request_id", "episode_index",
            "decision_window_index", "frame_index", "sim_time_ms",
        ):
            _index(getattr(self, name), name)
        if self.side not in {"red", "blue"}:
            raise PPOParallelProtocolError("decision envelope side is invalid")
        for name in (
            "decision_hash", "catalog_hash", "action_encoder_hash",
            "preprocessor_hash", "learner_tensor_hash", "binding_token",
        ):
            _sha256(getattr(self, name), name)
        if len(self.observation) != _OBSERVATION_WIDTH or any(
            not math.isfinite(item) for item in self.observation
        ):
            raise PPOParallelProtocolError(f"worker observation must be finite [{_OBSERVATION_WIDTH}]")
        if len(self.action_mask) != 256 or any(
            type(item) is not bool for item in self.action_mask
        ):
            raise PPOParallelProtocolError("worker mask must be bool[256]")
        _text(self.scenario_id, "scenario_id")
        if type(self.post_truncation_observation) is not bool:
            raise TypeError("post_truncation_observation must be bool")
        if not self.post_truncation_observation and not any(self.action_mask):
            raise PPOParallelProtocolError("active worker mask must contain an action")
        if self.binding_token != parallel_binding_token(self):
            raise PPOParallelProtocolError("worker binding token mismatch")


@dataclass(frozen=True, slots=True)
class WorkerInitRequest:
    protocol_version: str
    run_id: str
    worker_index: int
    env_index: int
    base_worker_index: int
    base_env_index: int
    trained_side: str
    reward_mode: str
    seed_config: TrainingSeedConfig
    expected_preprocessor_hash: str
    expected_action_encoder_hash: str
    expected_discount_hash: str
    expected_analyzer_hash: str | None
    expected_shaping_hash: str | None
    episode_index: int
    evaluation: bool
    artifact_root: str | None


@dataclass(frozen=True, slots=True)
class WorkerReady:
    protocol_version: str
    run_id: str
    worker_index: int
    env_index: int
    pid: int
    runtime_authority: Mapping[str, object]
    reward_authority: Mapping[str, object]
    seed_hash: str
    preprocessor_hash: str
    scenario_id: str
    torch_imported: bool
    session_identity: str
    initial_decision: WorkerDecisionEnvelope


@dataclass(frozen=True, slots=True)
class CoordinatorActionCommand:
    protocol_version: str
    run_id: str
    worker_index: int
    env_index: int
    request_id: int
    episode_index: int
    binding_token: str
    action_index: int
    operational_delay_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class WorkerEpisodeResult:
    episode_id: str
    episode_index: int
    environment_seed: int
    outcome: str
    winner: str | None
    terminal_reason: str | None
    terminated: bool
    truncated: bool
    truncation_source: str
    decision_count: int
    sim_time_ms: int
    base_return: float
    shaping_return: float
    composite_return: float
    action_ids: tuple[str, ...]
    reward_record_hashes: tuple[str, ...]
    reward_rederivation_verified: bool
    replay_path: str | None


@dataclass(frozen=True, slots=True)
class WorkerTransitionResult:
    protocol_version: str
    run_id: str
    worker_index: int
    env_index: int
    request_id: int
    episode_index: int
    episode_id: str
    binding_token: str
    action_index: int
    action_id: str
    reward: float
    reward_record_hash: str
    base_reward_amount: float
    shaping_reward_amount: float
    terminated: bool
    truncated: bool
    truncation_source: str
    effective_gamma: float
    next_decision_identity: str
    next_observation: tuple[float, ...]
    next_decision: WorkerDecisionEnvelope | None
    bootstrap_required: bool
    reset_required: bool
    episode_result: WorkerEpisodeResult | None
    engine_seconds: float
    preprocessing_seconds: float
    reward_replay_seconds: float
    worker_total_seconds: float
    engine_started_monotonic: float
    engine_finished_monotonic: float


@dataclass(frozen=True, slots=True)
class WorkerResetRequest:
    protocol_version: str
    run_id: str
    worker_index: int
    env_index: int
    request_id: int
    episode_index: int
    evaluation: bool


@dataclass(frozen=True, slots=True)
class WorkerResetResult:
    protocol_version: str
    run_id: str
    worker_index: int
    env_index: int
    request_id: int
    episode_index: int
    environment_seed: int
    decision: WorkerDecisionEnvelope


@dataclass(frozen=True, slots=True)
class WorkerShutdown:
    protocol_version: str
    run_id: str


@dataclass(frozen=True, slots=True)
class WorkerShutdownAck:
    protocol_version: str
    run_id: str
    worker_index: int
    env_index: int


@dataclass(frozen=True, slots=True)
class WorkerError:
    protocol_version: str
    run_id: str
    worker_index: int
    env_index: int
    request_id: int | None
    code: str
    detail: str


def build_worker_decision_envelope(**fields: object) -> WorkerDecisionEnvelope:
    payload = dict(fields)
    payload["binding_token"] = canonical_hash(_binding_payload(payload))
    return WorkerDecisionEnvelope(**payload)


def parallel_binding_token(value: WorkerDecisionEnvelope) -> str:
    return canonical_hash(_binding_payload(value))


def _binding_payload(value: object) -> dict[str, object]:
    def get(name: str) -> object:
        return value[name] if isinstance(value, Mapping) else getattr(value, name)
    return {
        "protocol_version": get("protocol_version"),
        "run_id": get("run_id"),
        "worker_index": get("worker_index"),
        "env_index": get("env_index"),
        "request_id": get("request_id"),
        "episode_index": get("episode_index"),
        "side": get("side"),
        "decision_window_index": get("decision_window_index"),
        "frame_index": get("frame_index"),
        "sim_time_ms": get("sim_time_ms"),
        "decision_hash": get("decision_hash"),
        "catalog_hash": get("catalog_hash"),
        "action_encoder_hash": get("action_encoder_hash"),
    }


def _protocol(value: object) -> None:
    if value != PPO_PARALLEL_WORKER_PROTOCOL_SCHEMA_VERSION:
        raise PPOParallelProtocolError("parallel worker protocol mismatch")


def _index(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise PPOParallelProtocolError(f"{name} must be non-negative")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PPOParallelProtocolError(f"{name} must be non-empty text")
    return value


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        item not in "0123456789abcdef" for item in value
    ):
        raise PPOParallelProtocolError(f"{name} must be lowercase SHA-256")
    return value


__all__ = [
    name for name in globals()
    if name.startswith("Worker") or name.startswith("PPO_PARALLEL")
] + ["CoordinatorActionCommand", "build_worker_decision_envelope",
     "parallel_binding_token"]
