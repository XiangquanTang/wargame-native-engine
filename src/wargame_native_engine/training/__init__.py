"""Pure-Python Native training boundary."""

from wargame_native_engine.training.action_encoder import NativeActionEncoder
from wargame_native_engine.training.env import (
    NativeTrainingEnv,
    NativeTrainingError,
)
from wargame_native_engine.training.models import (
    AgentResetResult,
    AgentStepResult,
    DecisionTransition,
    EncodedActionSpace,
    SingleSeatResetResult,
    SingleSeatStepResult,
    TrainingReset,
    TrainingStep,
)
from wargame_native_engine.training.multi_seat import (
    NativeDecisionSession,
    NativeMultiSeatEnv,
    NativeMultiSeatError,
    SingleSeatTrainingAdapter,
    TrainingDecisionSnapshotMismatchError,
    UnsupportedTrainingDecisionModeError,
    training_episode_id,
)
from wargame_native_engine.training.publication import (
    MATCH_SUMMARY_NAME,
    TrainingEpisodePublication,
    TrainingEpisodePublicationError,
    publish_training_episode,
)
from wargame_native_engine.training.observation_encoder import (
    SideObservationEncoder,
)
from wargame_native_engine.training.transitions import (
    DECISION_TRANSITIONS_NAME,
    DECISION_TRANSITION_SCHEMA,
    decision_transitions_to_jsonl,
    write_decision_transitions_jsonl,
)

__all__ = [
    "AgentResetResult",
    "AgentStepResult",
    "DecisionTransition",
    "DECISION_TRANSITIONS_NAME",
    "DECISION_TRANSITION_SCHEMA",
    "EncodedActionSpace",
    "NativeActionEncoder",
    "NativeTrainingEnv",
    "NativeTrainingError",
    "NativeDecisionSession",
    "NativeMultiSeatEnv",
    "NativeMultiSeatError",
    "SingleSeatResetResult",
    "SingleSeatStepResult",
    "SingleSeatTrainingAdapter",
    "TrainingDecisionSnapshotMismatchError",
    "UnsupportedTrainingDecisionModeError",
    "training_episode_id",
    "TrainingEpisodePublication",
    "TrainingEpisodePublicationError",
    "MATCH_SUMMARY_NAME",
    "SideObservationEncoder",
    "TrainingReset",
    "TrainingStep",
    "decision_transitions_to_jsonl",
    "write_decision_transitions_jsonl",
    "publish_training_episode",
]
