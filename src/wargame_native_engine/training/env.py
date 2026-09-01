"""Minimum pure-Python training environment over the local CommandGateway."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import tempfile
from typing import Any, Mapping

from wargame_native_engine.agents.protocol import Agent, AgentAction
from wargame_native_engine.gateway import (
    CommandGateway,
    ControllerView,
    SessionManager,
    SessionPublication,
    SimulationAdvance,
)
from wargame_native_engine.operations.controller import SideController
from wargame_native_engine.operations.proposals import ActionProposal
from wargame_native_engine.training.action_encoder import NativeActionEncoder
from wargame_native_engine.training.decision_encoder import (
    DECISION_ACTION_ENCODER_SCHEMA_VERSION,
    DECISION_OBSERVATION_ENCODER_SCHEMA_VERSION,
    DecisionActionEncoder,
    DecisionEncoderConfig,
    DecisionObservationEncoder,
    EncodedDecisionActionSpace,
    EncodedDecisionObservation,
)
from wargame_native_engine.projection.decision_capability import (
    ActionCatalogSnapshot,
)
from wargame_native_engine.training.models import (
    EncodedActionSpace,
    TrainingReset,
    TrainingStep,
)
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.training.reward import (
    BaseRewardPolicy,
    JointTrainingRewardRecord,
    RewardTruncationSource,
    TrainingTruncationProvenance,
    build_joint_training_reward_record,
    build_reward_transition_identity,
    joint_action_hash,
    load_reward_policy,
)
from wargame_native_engine.training.discount import (
    SharedTrainingDiscountConfig,
    load_shared_training_discount_config,
)
from wargame_native_engine.training.eight_factor_shaping_contract import (
    EightFactorShapingPolicy,
    load_eight_factor_shaping_policy,
)
from wargame_native_engine.training.eight_factor_shaping import (
    CompositeRewardPolicy,
    build_composite_reward_policy,
    compose_joint_training_reward,
    derive_joint_eight_factor_shaping,
)
from wargame_native_engine.training.observation_encoder import (
    SideObservationEncoder,
)


class NativeTrainingError(RuntimeError):
    """Stable lifecycle or opponent failure at the training boundary."""


TRAINING_INTERFACE_DECISION_V1 = "decision-v1"
TRAINING_INTERFACE_LEGACY = "legacy-side-observation"


@dataclass(frozen=True, slots=True)
class _FrozenTrainingDecisionSnapshot:
    observation: EncodedDecisionObservation
    action_space: EncodedDecisionActionSpace
    catalog: ActionCatalogSnapshot


@dataclass(frozen=True, slots=True)
class TrainingDecisionBinding:
    """Detached, atomic Decision-v1 observation/catalog learner binding."""

    observation: EncodedDecisionObservation
    action_space: EncodedDecisionActionSpace

    def __post_init__(self) -> None:
        if not isinstance(self.observation, EncodedDecisionObservation):
            raise TypeError("observation must be EncodedDecisionObservation")
        if not isinstance(self.action_space, EncodedDecisionActionSpace):
            raise TypeError("action_space must be EncodedDecisionActionSpace")
        if (
            self.observation.side is not self.action_space.side
            or self.observation.decision_window_index
            != self.action_space.decision_window_index
            or self.observation.frame_index != self.action_space.frame_index
            or self.observation.sim_time_ms != self.action_space.sim_time_ms
        ):
            raise NativeTrainingError("decision_v1_observation_catalog_not_atomic")


class NativeTrainingEnv:
    """One controlled side, one opponent and one Gateway decision window."""

    def __init__(
        self,
        environment_factory,
        *,
        controlled_side: str,
        opponent: Agent,
        storage_root: str | Path | None = None,
        max_actions: int = 256,
        max_steps: int | None = None,
        delta_time_seconds: int | float = 20,
        decision_interval_ms: int | None = None,
        frame_interval_ms: int | None = None,
        training_interface_version: str = TRAINING_INTERFACE_DECISION_V1,
        max_own_operators: int = 64,
        max_observed_enemies: int = 64,
        max_objectives: int = 32,
        enable_eight_factor_shaping: bool = False,
        discount_config: SharedTrainingDiscountConfig | None = None,
        shaping_policy: EightFactorShapingPolicy | None = None,
    ) -> None:
        side = str(controlled_side).lower()
        if side not in {"red", "blue"}:
            raise ValueError("controlled_side must be RED or BLUE")
        if not isinstance(opponent.agent_id, str) or not opponent.agent_id.strip():
            raise ValueError("opponent agent_id must be a non-empty string")
        if max_steps is not None and (
            type(max_steps) is not int or max_steps <= 0
        ):
            raise ValueError("max_steps must be a positive exact int or None")
        if isinstance(delta_time_seconds, bool) or not isinstance(
            delta_time_seconds, (int, float)
        ) or delta_time_seconds <= 0:
            raise ValueError("delta_time_seconds must be a positive number")
        self.controlled_side = side
        self.opponent_side = "blue" if side == "red" else "red"
        self.opponent = opponent
        self.max_steps = max_steps
        if type(enable_eight_factor_shaping) is not bool:
            raise TypeError("enable_eight_factor_shaping must be an exact bool")
        self.base_reward_policy: BaseRewardPolicy = load_reward_policy()
        self.discount_config = (
            load_shared_training_discount_config()
            if discount_config is None else discount_config
        )
        if not isinstance(self.discount_config, SharedTrainingDiscountConfig):
            raise TypeError("discount_config must be SharedTrainingDiscountConfig")
        if enable_eight_factor_shaping:
            self.shaping_policy = (
                load_eight_factor_shaping_policy(
                    discount=self.discount_config
                )
                if shaping_policy is None else shaping_policy
            )
            if not isinstance(self.shaping_policy, EightFactorShapingPolicy):
                raise TypeError("shaping_policy must be EightFactorShapingPolicy")
        else:
            if shaping_policy is not None:
                raise ValueError("disabled shaping cannot carry a shaping policy")
            self.shaping_policy = None
        self.composite_reward_policy: CompositeRewardPolicy = (
            build_composite_reward_policy(
                self.base_reward_policy, self.shaping_policy
            )
        )
        if training_interface_version not in {
            TRAINING_INTERFACE_DECISION_V1,
            TRAINING_INTERFACE_LEGACY,
        }:
            raise ValueError("unsupported training_interface_version")
        self.training_interface_version = training_interface_version
        if training_interface_version == TRAINING_INTERFACE_DECISION_V1:
            config = DecisionEncoderConfig(
                max_own_operators=max_own_operators,
                max_observed_enemies=max_observed_enemies,
                max_objectives=max_objectives,
                max_actions=max_actions,
            )
            self.action_encoder = DecisionActionEncoder(max_actions)
            self.observation_encoder = DecisionObservationEncoder(config)
            self.observation_space = self.observation_encoder.observation_space
            self.action_space = self.action_encoder.action_space
        else:
            self.action_encoder = NativeActionEncoder(max_actions)
            self.observation_encoder = SideObservationEncoder()
            self.observation_space = None
            self.action_space = None
        if storage_root is None:
            self._temporary_storage = tempfile.TemporaryDirectory(
                prefix="wargame-native-training."
            )
            storage = Path(self._temporary_storage.name)
        else:
            self._temporary_storage = None
            storage = Path(storage_root)
        self._manager = SessionManager(environment_factory, storage_root=storage)
        self._gateway = CommandGateway(
            self._manager,
            delta_time_seconds=delta_time_seconds,
            decision_interval_ms=decision_interval_ms,
            frame_interval_ms=frame_interval_ms,
        )
        self._session_id: str | None = None
        self._controller: SideController | None = None
        self._controller_ids = {
            "red": "native-training-red",
            "blue": "native-training-blue",
        }
        self._step_count = 0
        self._ended = False
        self._failed = False
        self._closed = False
        self._episode_sequence = 0
        self._scenario_id: str | None = None
        self._training_snapshot: _FrozenTrainingDecisionSnapshot | None = None
        self._reward_records: list[JointTrainingRewardRecord] = []
        self._episode_return = 0.0

    def reset(self, seed: int = 42) -> TrainingReset:
        self._require_open()
        if type(seed) is not int:
            raise TypeError("seed must be an exact int")
        if self._session_id is not None:
            self._gateway.close_session(self._session_id)
            self._session_id = None
            self._controller = None
            self._training_snapshot = None
        descriptor = self._gateway.create_session(
            red_controller_id=self._controller_ids["red"],
            blue_controller_id=self._controller_ids["blue"],
            seed=seed,
            record=True,
        )
        self._session_id = descriptor.session_id
        self._controller = SideController(
            self._gateway,
            descriptor.session_id,
            self._controller_ids[self.controlled_side],
        )
        self._step_count = 0
        self._ended = descriptor.terminal
        self._failed = False
        self._episode_sequence += 1
        self._reward_records.clear()
        self._episode_return = 0.0
        try:
            context = self._manager.agent_reset_context(
                descriptor.session_id,
                self.opponent_side,
                self.opponent.agent_id,
            )
            self._scenario_id = context.scenario_id
            self.opponent.reset(deepcopy(context))
        except BaseException as exc:
            self._failed = True
            raise NativeTrainingError(
                "training_opponent_reset_failed:"
                f"{self.opponent.agent_id}:{type(exc).__name__}:{exc}"
            ) from exc
        view = self._controlled_view()
        observation, action_space = self._bind_training_view(view)
        return TrainingReset(
            observation,
            action_space,
            self._info(view),
        )

    def step(self, action_id: int | None) -> TrainingStep:
        """Compatibility adapter selecting zero or one action this window."""

        return self.step_batch(() if action_id is None else (action_id,))

    def step_bound_action(
        self,
        action_index: int,
        binding: TrainingDecisionBinding,
    ) -> TrainingStep:
        """Submit an index only if its exact frozen Decision/catalog is current."""

        if self.training_interface_version != TRAINING_INTERFACE_DECISION_V1:
            raise NativeTrainingError("bound_action_requires_decision_v1")
        if not isinstance(binding, TrainingDecisionBinding):
            raise TypeError("binding must be TrainingDecisionBinding")
        if type(action_index) is not int:
            raise TypeError("action_index must be an exact int")
        current = self.current_decision_binding
        expected = binding.action_space
        actual = current.action_space
        if (
            binding.observation.decision_semantic_hash
            != current.observation.decision_semantic_hash
            or expected.catalog_hash != actual.catalog_hash
            or expected.decision_window_index != actual.decision_window_index
            or expected.frame_index != actual.frame_index
            or expected.sim_time_ms != actual.sim_time_ms
            or expected.encoder_contract_hash != actual.encoder_contract_hash
        ):
            raise NativeTrainingError("bound_action_snapshot_stale")
        if not 0 <= action_index < len(expected.mask) or not expected.mask[action_index]:
            raise NativeTrainingError("bound_action_index_not_in_catalog")
        return self.step(action_index)

    def step_batch(self, action_ids: Sequence[int]) -> TrainingStep:
        """Advance one decision window using an ordered controlled-side batch."""

        self._require_active()
        before = self._controlled_view()
        before_views = self._side_views(controlled=before)
        if self.training_interface_version == TRAINING_INTERFACE_DECISION_V1:
            frozen = self._require_training_snapshot(before)
            space = frozen.action_space
            controlled_action_ids = self.action_encoder.resolve_batch(
                space, frozen.catalog, action_ids
            )
        else:
            space = self._action_space(before)
            controlled_action_ids = self.action_encoder.resolve_batch(
                space, action_ids
            )
        opponent_action_ids = self._opponent_action_ids()
        by_side = {
            self.controlled_side: controlled_action_ids,
            self.opponent_side: opponent_action_ids,
        }
        window_index = before.step_index
        try:
            for side in ("red", "blue"):
                request_id = (
                    f"training:{self._episode_sequence}:"
                    f"{window_index}:{side}"
                )
                if side == self.controlled_side:
                    controller = self._require_controller()
                    controller.clear_staged()
                    for action_id in by_side[side]:
                        controller.stage_action(
                            action_id, expected_window=window_index
                        )
                    controller.submit_batch(
                        request_id=request_id,
                        expected_window=window_index,
                    )
                else:
                    self._gateway.submit_batch(
                        self._require_session_id(),
                        self._controller_ids[side],
                        request_id=request_id,
                        expected_window=window_index,
                        ordered_action_ids=by_side[side],
                    )
        except BaseException:
            self._failed = True
            raise
        self._step_count += 1
        view = self._controlled_view()
        after_views = self._side_views(controlled=view)
        advance = self._gateway.last_advance(
            self._require_session_id(),
            self._controller_ids[self.controlled_side],
        )
        if advance is None or advance.decision_window_index != window_index:
            self._failed = True
            raise NativeTrainingError("training_window_advance_evidence_missing")
        terminated = view.terminated
        training_truncated = (
            not terminated
            and not view.truncated
            and self.max_steps is not None
            and self._step_count >= self.max_steps
        )
        truncated = False if terminated else (
            view.truncated or training_truncated
        )
        native_result = self._manager.native_result(self._require_session_id())
        semantic_hashes = {
            side: canonical_hash({"action_ids": list(by_side[side])})
            for side in ("red", "blue")
        }
        actions_hash = joint_action_hash(
            by_side["red"],
            by_side["blue"],
            red_semantic_hash=semantic_hashes["red"],
            blue_semantic_hash=semantic_hashes["blue"],
        )
        identities = {
            side: self._reward_identity(
                side=side,
                before=before_views[side],
                after=after_views[side],
                advance=advance,
                submitted_action_ids=by_side[side],
                submitted_action_semantic_hash=semantic_hashes[side],
                joint_actions_hash=actions_hash,
            )
            for side in ("red", "blue")
        }
        if training_truncated:
            assert self.max_steps is not None
            truncation = TrainingTruncationProvenance.training_limit(
                RewardTruncationSource.TRAINING_MAX_STEPS,
                self.max_steps,
            )
            terminal_reason = None
        elif view.truncated:
            truncation = TrainingTruncationProvenance.native_max_ticks()
            terminal_reason = native_result.reason
        else:
            truncation = TrainingTruncationProvenance.none()
            terminal_reason = native_result.reason if terminated else None
        base_reward = build_joint_training_reward_record(
            red_transition_identity=identities["red"],
            blue_transition_identity=identities["blue"],
            terminated=terminated,
            truncated=truncated,
            winner=native_result.winner,
            terminal_reason_code=terminal_reason,
            truncation=truncation,
            policy=self.base_reward_policy,
        )
        joint_reward = self._composite_reward(base_reward)
        reward_record = joint_reward.side_record(self.controlled_side)
        self._reward_records.append(deepcopy(joint_reward))
        self._episode_return += reward_record.total_reward
        self._ended = terminated or truncated
        observation, action_space = self._bind_training_view(view)
        info = dict(self._info(
            view,
            training_truncated=training_truncated,
            advance=advance,
        ))
        info["training_supervision"] = {
            "classification": "TRAINING_SUPERVISION",
            "reward_record_schema": reward_record.schema_version,
            "reward_record_hash": reward_record.semantic_hash,
            "joint_reward_hash": joint_reward.semantic_hash,
            "joint_transition_id": joint_reward.joint_transition_id,
            "reward_policy": joint_reward.reward_policy.to_dict(),
            "composite_reward_policy": (
                self.composite_reward_policy.to_dict()
            ),
            "episode_return": self._episode_return,
        }
        return TrainingStep(
            observation,
            reward_record.total_reward,
            terminated,
            truncated,
            info,
            action_space,
            reward_record,
        )

    def _composite_reward(
        self, base_reward: JointTrainingRewardRecord
    ) -> JointTrainingRewardRecord:
        policy = self.shaping_policy
        if policy is None:
            return base_reward
        identity = base_reward.red.transition_identity
        before = self._manager.frame_analysis(
            self._require_session_id(), identity.frame_before
        )
        after = None if base_reward.red.terminated else self._manager.frame_analysis(
            self._require_session_id(), identity.frame_after
        )
        shaping = derive_joint_eight_factor_shaping(
            base_record=base_reward,
            analysis_before=before,
            analysis_after=after,
            policy=policy,
            discount=self.discount_config,
        )
        return compose_joint_training_reward(
            base_record=base_reward,
            shaping=shaping,
            composite_policy=self.composite_reward_policy,
        )

    @property
    def valid_action_mask(self) -> tuple[bool, ...]:
        self._require_active()
        view = self._controlled_view()
        if self.training_interface_version == TRAINING_INTERFACE_DECISION_V1:
            return tuple(self._require_training_snapshot(view).action_space.mask)
        return tuple(self._action_space(view).mask)

    @property
    def current_action_space(
        self,
    ) -> EncodedActionSpace | EncodedDecisionActionSpace:
        """Current frozen action IDs, including server route proposals."""

        self._require_active()
        view = self._controlled_view()
        if self.training_interface_version == TRAINING_INTERFACE_DECISION_V1:
            return self._require_training_snapshot(view).action_space
        return self._action_space(view)

    @property
    def current_observation(self) -> Mapping[str, Any]:
        """Return the current Decision-v1 policy record as a detached value."""

        self._require_active()
        view = self._controlled_view()
        if self.training_interface_version == TRAINING_INTERFACE_DECISION_V1:
            return self._require_training_snapshot(view).observation.policy_record()
        return self.observation_encoder.encode(view)

    @property
    def current_decision_binding(self) -> TrainingDecisionBinding:
        """Return the detached encoded observation and catalog from one snapshot."""

        self._require_initialized()
        if self.training_interface_version != TRAINING_INTERFACE_DECISION_V1:
            raise NativeTrainingError("decision_binding_requires_decision_v1")
        snapshot = self._require_training_snapshot(self._controlled_view())
        return TrainingDecisionBinding(
            observation=deepcopy(snapshot.observation),
            action_space=deepcopy(snapshot.action_space),
        )

    @property
    def runtime_authority(self) -> Mapping[str, object]:
        """Checkpoint-safe Engine/Decision authority metadata, never live state."""

        self._require_initialized()
        view = self._controlled_view()
        decision = view.decision_semantic
        if decision is None:
            raise NativeTrainingError("decision_v1_controller_core_missing")
        authority = decision.authority_metadata
        return {
            "engine_version": authority.engine_version,
            "ruleset_id": authority.ruleset_id,
            "ruleset_version": authority.ruleset_version,
            "ruleset_hash": authority.ruleset_hash,
            "perspective_policy_id": authority.perspective_policy_id,
            "perspective_policy_version": authority.perspective_policy_version,
            "perspective_policy_hash": authority.perspective_policy_hash,
            "decision_schema_version": decision.schema_version,
            "observation_encoder_schema": DECISION_OBSERVATION_ENCODER_SCHEMA_VERSION,
            "observation_encoder_hash": self.observation_encoder.contract_hash,
            "action_encoder_schema": DECISION_ACTION_ENCODER_SCHEMA_VERSION,
            "action_encoder_hash": self.action_encoder.contract_hash,
            "action_capacity": self.action_encoder.max_actions,
        }

    @property
    def scenario_id(self) -> str:
        """Return the immutable scenario identifier for the active episode."""

        self._require_initialized()
        if self._scenario_id is None:
            raise NativeTrainingError("training_scenario_identity_missing")
        return self._scenario_id

    def plan_move(
        self,
        unit_id: int,
        destination_hex: int,
        *,
        request_id: str,
        movement_mode: str = "NORMAL",
    ) -> ActionProposal:
        """Optional destination adapter using the same Operations planner."""

        self._require_active()
        proposal = self._require_controller().plan_move(
            unit_id,
            destination_hex,
            request_id=request_id,
            movement_mode=movement_mode,
        )
        if self.training_interface_version == TRAINING_INTERFACE_DECISION_V1:
            self._bind_training_view(self._controlled_view())
        return proposal

    def publish(
        self,
        output_dir: str | Path,
        *,
        overwrite: bool = False,
    ) -> SessionPublication:
        self._require_initialized()
        if self._failed:
            raise NativeTrainingError("training_episode_failed")
        view = self._controlled_view()
        publication = self._gateway.publish_session(
            self._require_session_id(),
            self._controller_ids[self.controlled_side],
            str(output_dir),
            truncated=not view.terminal,
            overwrite=overwrite,
        )
        self._ended = True
        return publication

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self._session_id is not None:
                self._gateway.close_session(self._session_id)
                self._session_id = None
                self._controller = None
                self._training_snapshot = None
        finally:
            try:
                self.opponent.close()
            finally:
                if self._temporary_storage is not None:
                    self._temporary_storage.cleanup()
                self._closed = True

    def _opponent_action_ids(self) -> tuple[str, ...]:
        session_id = self._require_session_id()
        try:
            observation, valid_actions, context = self._manager.agent_step_input(
                session_id,
                self.opponent_side,
                self.opponent.agent_id,
            )
            act_batch = getattr(self.opponent, "act_batch", None)
            if callable(act_batch):
                selected = act_batch(
                    observation=deepcopy(observation),
                    valid_actions=deepcopy(valid_actions),
                    context=deepcopy(context),
                )
                selections = _require_agent_action_sequence(selected)
            else:
                selection = self.opponent.act(
                    observation=deepcopy(observation),
                    valid_actions=deepcopy(valid_actions),
                    context=deepcopy(context),
                )
                selections = () if selection is None else (selection,)
            return _validate_opponent_actions(
                self.opponent_side,
                selections,
                valid_actions,
            )
        except BaseException as exc:
            self._failed = True
            raise NativeTrainingError(
                "training_opponent_act_failed:"
                f"{self.opponent.agent_id}:{self._step_count}:"
                f"{type(exc).__name__}:{exc}"
            ) from exc

    def _controlled_view(self) -> ControllerView:
        return self._require_controller().observe()

    def _action_space(self, view: ControllerView) -> EncodedActionSpace:
        return self.action_encoder.encode(view.valid_actions)

    def _bind_training_view(self, view: ControllerView):
        if self.training_interface_version == TRAINING_INTERFACE_LEGACY:
            return (
                self.observation_encoder.encode(view),
                self._action_space(view),
            )
        decision = view.decision_semantic
        catalog = view.action_catalog
        if decision is None or catalog is None:
            raise NativeTrainingError("decision_v1_controller_core_missing")
        encoded_observation = self.observation_encoder.encode(decision)
        encoded_actions = self.action_encoder.encode(catalog)
        if decision.action_space != catalog.reference():
            raise NativeTrainingError("decision_v1_catalog_reference_mismatch")
        self._training_snapshot = _FrozenTrainingDecisionSnapshot(
            encoded_observation,
            encoded_actions,
            catalog,
        )
        return encoded_observation.policy_record(), encoded_actions

    def _require_training_snapshot(
        self, view: ControllerView
    ) -> _FrozenTrainingDecisionSnapshot:
        snapshot = self._training_snapshot
        decision = view.decision_semantic
        catalog = view.action_catalog
        if snapshot is None or decision is None or catalog is None:
            raise NativeTrainingError("decision_v1_snapshot_missing_reset_required")
        encoded = snapshot.observation
        actions = snapshot.action_space
        if (
            encoded.side.value != view.perspective.lower()
            or encoded.decision_window_index != view.step_index
            or encoded.frame_index != view.frame_index
            or encoded.sim_time_ms != view.sim_time_ms
            or encoded.decision_semantic_hash != decision.semantic_hash
            or actions.catalog_hash != catalog.catalog_hash
            or actions.decision_window_index != view.step_index
        ):
            self._failed = True
            raise NativeTrainingError("decision_v1_snapshot_stale_reset_required")
        return snapshot

    @property
    def reward_records(self) -> tuple[JointTrainingRewardRecord, ...]:
        self._require_initialized()
        return deepcopy(tuple(self._reward_records))

    def verify_reward_rederivation(self):
        """Re-derive this episode's base/composite rewards from committed evidence."""

        from wargame_native_engine.training.reward_replay import (
            RewardSequenceVerification,
            reward_sequence_hash,
        )

        self._require_initialized()
        expected = self.reward_records
        rederived: list[JointTrainingRewardRecord] = []
        for stored in expected:
            base = build_joint_training_reward_record(
                red_transition_identity=stored.red.transition_identity,
                blue_transition_identity=stored.blue.transition_identity,
                terminated=stored.red.terminated,
                truncated=stored.red.truncated,
                winner=None if stored.winner is None else stored.winner.value,
                terminal_reason_code=stored.red.terminal_reason_code,
                truncation=stored.red.truncation,
                policy=self.base_reward_policy,
            )
            if self.shaping_policy is None:
                actual = base
            else:
                identity = base.red.transition_identity
                before = self._manager.frame_analysis(
                    self._require_session_id(), identity.frame_before
                )
                after = None if base.red.terminated else self._manager.frame_analysis(
                    self._require_session_id(), identity.frame_after
                )
                shaping = derive_joint_eight_factor_shaping(
                    base_record=base,
                    analysis_before=before,
                    analysis_after=after,
                    policy=self.shaping_policy,
                    discount=self.discount_config,
                )
                actual = compose_joint_training_reward(
                    base_record=base,
                    shaping=shaping,
                    composite_policy=self.composite_reward_policy,
                )
            rederived.append(actual)
        actual_values = tuple(rederived)
        expected_hash = reward_sequence_hash(expected)
        actual_hash = reward_sequence_hash(actual_values)
        return RewardSequenceVerification(
            record_count=len(actual_values),
            expected_sequence_hash=expected_hash,
            rederived_sequence_hash=actual_hash,
            matches=expected == actual_values and expected_hash == actual_hash,
            records=actual_values,
        )

    @property
    def episode_return(self) -> float:
        self._require_initialized()
        return self._episode_return

    def _side_views(
        self, *, controlled: ControllerView,
    ) -> dict[str, ControllerView]:
        result = {self.controlled_side: controlled}
        result[self.opponent_side] = self._gateway.view(
            self._require_session_id(), self._controller_ids[self.opponent_side]
        )
        return result

    def _reward_identity(
        self,
        *,
        side: str,
        before: ControllerView,
        after: ControllerView,
        advance: SimulationAdvance,
        submitted_action_ids: Sequence[str],
        submitted_action_semantic_hash: str,
        joint_actions_hash: str,
    ):
        before_decision = before.decision_semantic
        before_catalog = before.action_catalog
        after_decision = after.decision_semantic
        if before_decision is None or before_catalog is None or after_decision is None:
            raise NativeTrainingError("reward_decision_core_missing")
        session_id = self._require_session_id()
        return build_reward_transition_identity(
            episode_id=f"native-training:{session_id}",
            run_namespace="native-training-env",
            side=side,
            decision_window_index=before.step_index,
            frame_before=before.frame_index,
            fact_frame_start=advance.first_frame_index,
            fact_frame_end=advance.last_frame_index,
            frame_after=after.frame_index,
            sim_time_before_ms=advance.sim_time_before_ms,
            sim_time_after_ms=advance.sim_time_after_ms,
            decision_before_hash=before_decision.semantic_hash,
            action_catalog_hash=before_catalog.catalog_hash,
            submitted_action_ids=submitted_action_ids,
            submitted_action_semantic_hash=submitted_action_semantic_hash,
            joint_action_hash=joint_actions_hash,
            decision_after_hash=after_decision.semantic_hash,
        )

    def _info(
        self,
        view: ControllerView,
        *,
        training_truncated: bool = False,
        advance: SimulationAdvance | None = None,
    ) -> Mapping[str, Any]:
        value = {
            "session_id": view.session_id,
            "perspective": view.perspective,
            "step_index": view.step_index,
            "decision_window_index": view.step_index,
            "processed_decision_window_index": (
                None if advance is None else advance.decision_window_index
            ),
            "frame_range": (
                None
                if advance is None
                else [advance.first_frame_index, advance.last_frame_index]
            ),
            "generated_frame_count": (
                0 if advance is None else advance.generated_frame_count
            ),
            "sim_time_range_ms": (
                None
                if advance is None
                else [advance.sim_time_before_ms, advance.sim_time_after_ms]
            ),
            "frame_index": view.frame_index,
            "sim_time_ms": view.sim_time_ms,
            "winner": view.winner,
            "outcome": view.outcome,
            "truncation_source": (
                "native"
                if view.truncated
                else "training_max_steps" if training_truncated else None
            ),
            "referee_labels_exposed": False,
            "training_interface_version": self.training_interface_version,
            "observation_encoder_schema": (
                DECISION_OBSERVATION_ENCODER_SCHEMA_VERSION
                if self.training_interface_version
                == TRAINING_INTERFACE_DECISION_V1
                else "legacy-side-observation"
            ),
        }
        if self.training_interface_version == TRAINING_INTERFACE_DECISION_V1:
            decision = view.decision_semantic
            catalog = view.action_catalog
            if decision is None or catalog is None:
                raise NativeTrainingError("decision_v1_controller_core_missing")
            value.update({
                "decision_schema_version": decision.schema_version,
                "perspective_policy_id": (
                    decision.authority_metadata.perspective_policy_id
                ),
                "perspective_policy_version": (
                    decision.authority_metadata.perspective_policy_version
                ),
                "perspective_policy_hash": (
                    decision.authority_metadata.perspective_policy_hash
                ),
                "observation_encoder_contract_hash": (
                    self.observation_encoder.contract_hash
                ),
                "action_encoder_schema": DECISION_ACTION_ENCODER_SCHEMA_VERSION,
                "action_encoder_contract_hash": self.action_encoder.contract_hash,
                "catalog_hash": catalog.catalog_hash,
            })
        return value

    def _require_session_id(self) -> str:
        if self._session_id is None:
            raise NativeTrainingError("training_env_not_reset")
        return self._session_id

    def _require_controller(self) -> SideController:
        if self._controller is None:
            raise NativeTrainingError("training_env_not_reset")
        return self._controller

    def _require_initialized(self) -> None:
        self._require_open()
        self._require_session_id()

    def _require_active(self) -> None:
        self._require_initialized()
        if self._failed:
            raise NativeTrainingError("training_episode_failed")
        if self._ended:
            raise NativeTrainingError("training_episode_ended_reset_required")

    def _require_open(self) -> None:
        if self._closed:
            raise NativeTrainingError("training_env_closed")


def _require_agent_action_sequence(value) -> tuple[AgentAction, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("opponent act_batch must return a sequence")
    return tuple(value)


def _validate_opponent_actions(
    side: str,
    selected: Sequence[AgentAction],
    valid_actions,
) -> tuple[str, ...]:
    legal = {item.action_id: item for item in valid_actions}
    result: list[str] = []
    seen: set[str] = set()
    for value in selected:
        if not isinstance(value, AgentAction):
            raise TypeError(
                "opponent Agent selections must contain AgentAction values"
            )
        if value.action.side != side:
            raise ValueError(f"opponent_action_side_mismatch:{side}")
        action_id = value.valid_action_id
        if action_id not in legal:
            raise ValueError(f"opponent_action_not_valid:{side}")
        if action_id in seen:
            raise ValueError(f"opponent_action_duplicate:{side}:{action_id}")
        seen.add(action_id)
        result.append(action_id)
    return tuple(result)
