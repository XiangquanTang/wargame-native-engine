"""Recommended project-native RED/BLUE decision-window environment."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from wargame_native_engine.agents.seat import (
    AgentDecisionContext,
    LocalSeatAgent,
    SeatActionCapabilities,
    SeatDecision,
)
from wargame_native_engine.gateway import (
    CommandGatewayError,
    DeadlineMode,
    SessionPublication,
)
from wargame_native_engine.io.serialization import canonical_hash, json_clone
from wargame_native_engine.projection.decision_semantic import (
    DecisionProjectionContractError,
)
from wargame_native_engine.operations import ProductOperationService
from wargame_native_engine.operations.facade import MatchHandle
from wargame_native_engine.training.models import (
    AgentResetResult,
    AgentStepResult,
    CatalogActionSelection,
    DecisionTransition,
    SingleSeatResetResult,
    SingleSeatStepResult,
)
from wargame_native_engine.training.decision_encoder import (
    DecisionActionEncoder,
    DecisionEncoderConfig,
    DecisionObservationEncoder,
)
from wargame_native_engine.training.transitions import (
    write_decision_transitions_jsonl,
)
from wargame_native_engine.training.publication import (
    TrainingEpisodePublication,
    publish_training_episode,
)
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


MatchFactory = Callable[[int, int], MatchHandle]


class NativeMultiSeatError(RuntimeError):
    """Stable lifecycle or atomic decision execution failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        side: str | None = None,
        phase: str | None = None,
    ) -> None:
        self.code = str(code)
        self.message = str(message)
        self.side = None if side is None else str(side)
        self.phase = None if phase is None else str(phase)
        super().__init__(f"{self.code}:{self.message}")


class UnsupportedTrainingDecisionModeError(ValueError):
    """A training environment was attached to an autonomous deadline."""

    def __init__(self) -> None:
        super().__init__(
            "native_multi_seat_env_requires_synchronous_decision_windows"
        )


class TrainingDecisionSnapshotMismatchError(NativeMultiSeatError):
    """The current Gateway window no longer matches the emitted snapshot."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            "training_decision_snapshot_mismatch_reset_required",
            f"expected={expected};actual={actual}",
            phase="snapshot",
        )


@dataclass(frozen=True, slots=True)
class _DecisionSnapshot:
    episode_id: str
    decision_index: int
    red_decision_index: int
    blue_decision_index: int
    frame_index: int
    sim_time_ms: int
    red_payload_hash: str
    blue_payload_hash: str


class NativeDecisionSession:
    """Shared decision executor used by Agent runners and training adapters.

    It owns no Engine or WorldState and advances only through the product
    operation services attached to one existing ``MatchHandle``.
    """

    def __init__(
        self,
        match: MatchHandle,
        *,
        episode_id: str,
        run_namespace: str,
        seed: int,
        encoder_config: DecisionEncoderConfig | None = None,
        discount_config: SharedTrainingDiscountConfig | None = None,
        shaping_policy: EightFactorShapingPolicy | None = None,
        enable_eight_factor_shaping: bool = False,
    ) -> None:
        if not isinstance(match, MatchHandle):
            raise TypeError("match must be a MatchHandle")
        if not isinstance(episode_id, str) or not episode_id:
            raise ValueError("episode_id must be non-empty")
        self.run_namespace = _run_namespace(run_namespace)
        if type(seed) is not int:
            raise TypeError("seed must be an exact int")
        if match.gateway.deadline_mode is not DeadlineMode.SYNCHRONOUS:
            raise UnsupportedTrainingDecisionModeError()
        self.match = match
        self.episode_id = episode_id
        self.seed = seed
        if type(enable_eight_factor_shaping) is not bool:
            raise TypeError("enable_eight_factor_shaping must be an exact bool")
        self.base_reward_policy: BaseRewardPolicy = load_reward_policy()
        self.discount_config = (
            load_shared_training_discount_config()
            if discount_config is None else discount_config
        )
        if enable_eight_factor_shaping:
            self.shaping_policy = (
                load_eight_factor_shaping_policy(discount=self.discount_config)
                if shaping_policy is None else shaping_policy
            )
        else:
            if shaping_policy is not None:
                raise ValueError("disabled shaping cannot carry a shaping policy")
            self.shaping_policy = None
        self.composite_reward_policy: CompositeRewardPolicy = (
            build_composite_reward_policy(
                self.base_reward_policy, self.shaping_policy
            )
        )
        self.encoder_config = (
            DecisionEncoderConfig() if encoder_config is None else encoder_config
        )
        if not isinstance(self.encoder_config, DecisionEncoderConfig):
            raise TypeError("encoder_config must be DecisionEncoderConfig")
        self.observation_encoder = DecisionObservationEncoder(self.encoder_config)
        self.action_encoder = DecisionActionEncoder(self.encoder_config.max_actions)
        self.controllers = {
            "red": match.bind_controller(
                "red", controller_id=match.red_controller_id
            ),
            "blue": match.bind_controller(
                "blue", controller_id=match.blue_controller_id
            ),
        }
        self.product_services = {
            side: ProductOperationService(controller)
            for side, controller in self.controllers.items()
        }
        self._failed = False
        self._snapshot: _DecisionSnapshot | None = None

    def reset_result(self) -> AgentResetResult:
        self._require_synchronized()
        views = self._views()
        capabilities = self._capabilities(views)
        descriptor = self.match.status
        snapshot = self._build_snapshot(views, capabilities)
        if (
            descriptor.frame_index != snapshot.frame_index
            or descriptor.sim_time_ms != snapshot.sim_time_ms
        ):
            self._snapshot_mismatch(snapshot.decision_index, -1)
        self._snapshot = snapshot
        observations, action_spaces = self._encoded_policy_inputs(views)
        return AgentResetResult(
            episode_id=self.episode_id,
            run_namespace=self.run_namespace,
            observations=observations,
            capabilities=capabilities,
            action_spaces=action_spaces,
            decision_index=snapshot.decision_index,
            fact_frame_index=descriptor.frame_index,
            sim_time_ms=descriptor.sim_time_ms,
            seed=self.seed,
            scenario_id=self.match.bundle.scenario_identity.scenario_id,
            scenario_hash=self.match.bundle.scenario_identity.sha256,
            map_id=self.match.bundle.map_identity.map_id,
        )

    def decision_context(
        self, side: str, *, include_semantic_core: bool = False,
    ) -> AgentDecisionContext:
        self._require_synchronized()
        if type(include_semantic_core) is not bool:
            raise TypeError("include_semantic_core must be an exact bool")
        normalized = _side(side)
        views, capabilities, _ = self._validated_current_snapshot()
        view = views[normalized]
        return AgentDecisionContext(
            normalized,
            capabilities[normalized].decision_index,
            view.frame_index,
            view.sim_time_ms,
            json_clone(view.observation),
            deepcopy(capabilities[normalized]),
            (
                deepcopy(view.decision_semantic)
                if include_semantic_core else None
            ),
            deepcopy(view.action_catalog) if include_semantic_core else None,
        )

    def agent_reset_context(self, side: str, agent_id: str):
        return self.match.gateway.manager.agent_reset_context(
            self.match.session_id, _side(side), agent_id
        )

    def step(
        self,
        red_decision: SeatDecision,
        blue_decision: SeatDecision,
    ) -> AgentStepResult:
        self._require_synchronized()
        before_views, before_capabilities, snapshot = (
            self._validated_current_snapshot()
        )
        decisions = {
            "red": _decision(red_decision),
            "blue": _decision(blue_decision),
        }
        decision_index = snapshot.decision_index
        if any(
            value.decision_index != decision_index
            for value in before_capabilities.values()
        ):
            raise NativeMultiSeatError(
                "decision_snapshot_mismatch",
                "side capability snapshots do not share one window",
            )

        previews = {}
        for side in ("red", "blue"):
            try:
                previews[side] = self.product_services[side].preview_decision(
                    decisions[side],
                    request_id=(
                        f"native-agent:{self.episode_id}:"
                        f"{decision_index}:{side}:preview"
                    ),
                )
            except BaseException as exc:
                raise NativeMultiSeatError(
                    "decision_preview_failed",
                    f"{type(exc).__name__}:{exc}",
                    side=side,
                    phase="preview",
                ) from exc

        executions = {}
        submitted: list[str] = []
        for side in ("red", "blue"):
            try:
                executions[side] = self.product_services[side].execute_decision(
                    previews[side].preview_token,
                    request_id=(
                        f"native-agent:{self.episode_id}:"
                        f"{decision_index}:{side}:execute"
                    ),
                )
                submitted.append(side)
            except BaseException as exc:
                self._failed = True
                raise NativeMultiSeatError(
                    "decision_execution_failed_reset_required",
                    f"{type(exc).__name__}:{exc};submitted={','.join(submitted)}",
                    side=side,
                    phase="execute",
                ) from exc
            if side == "red" and executions[side].advanced:
                self._failed = True
                raise NativeMultiSeatError(
                    "red_submission_advanced",
                    "RED advanced before BLUE submission",
                    side=side,
                    phase="execute",
                )
            if side == "blue" and not executions[side].advanced:
                self._failed = True
                raise NativeMultiSeatError(
                    "joint_window_not_advanced",
                    "complete decision window did not advance",
                    side=side,
                    phase="execute",
                )

        return self._complete_step(
            before_views,
            before_capabilities,
            snapshot,
            decisions,
        )

    def step_action_indices(
        self,
        red_action_indices: Sequence[int],
        blue_action_indices: Sequence[int],
    ) -> AgentStepResult:
        """Resolve both sides against their emitted catalogs and submit once."""

        self._require_synchronized()
        before_views, before_capabilities, snapshot = (
            self._validated_current_snapshot()
        )
        _, spaces = self._encoded_policy_inputs(before_views)
        indices = {
            "red": red_action_indices,
            "blue": blue_action_indices,
        }
        selected = {}
        decisions = {}
        for side in ("red", "blue"):
            catalog = before_views[side].action_catalog
            if catalog is None:
                raise NativeMultiSeatError(
                    "decision_v1_catalog_missing",
                    "ControllerView lacks ActionCatalogSnapshot",
                    side=side,
                    phase="resolve",
                )
            selected[side] = self.action_encoder.resolve_batch(
                spaces[side], catalog, indices[side]
            )
            decisions[side] = CatalogActionSelection(selected[side])

        decision_index = snapshot.decision_index
        submitted: list[str] = []
        controller_ids = {
            "red": self.match.red_controller_id,
            "blue": self.match.blue_controller_id,
        }
        for side in ("red", "blue"):
            try:
                result = self.match.gateway.submit_batch(
                    self.match.session_id,
                    controller_ids[side],
                    request_id=(
                        f"native-training-catalog:{self.episode_id}:"
                        f"{decision_index}:{side}"
                    ),
                    expected_window=decision_index,
                    ordered_action_ids=selected[side],
                )
                submitted.append(side)
            except BaseException as exc:
                self._failed = True
                raise NativeMultiSeatError(
                    "catalog_decision_execution_failed_reset_required",
                    f"{type(exc).__name__}:{exc};submitted={','.join(submitted)}",
                    side=side,
                    phase="execute",
                ) from exc
            if side == "red" and result.advanced:
                self._failed = True
                raise NativeMultiSeatError(
                    "red_submission_advanced",
                    "RED advanced before BLUE submission",
                    side=side,
                    phase="execute",
                )
            if side == "blue" and not result.advanced:
                self._failed = True
                raise NativeMultiSeatError(
                    "joint_window_not_advanced",
                    "complete decision window did not advance",
                    side=side,
                    phase="execute",
                )
        return self._complete_step(
            before_views,
            before_capabilities,
            snapshot,
            decisions,
        )

    def _complete_step(
        self,
        before_views,
        before_capabilities,
        snapshot: _DecisionSnapshot,
        decisions,
    ) -> AgentStepResult:
        decision_index = snapshot.decision_index
        advance = self.match.gateway.last_advance(
            self.match.session_id, self.match.red_controller_id
        )
        if advance is None or advance.decision_window_index != decision_index:
            self._snapshot_mismatch(
                decision_index,
                -1 if advance is None else advance.decision_window_index,
            )
        after_views = self._views()
        after_capabilities = self._capabilities(after_views)
        before_observations, _ = self._encoded_policy_inputs(before_views)
        after_observations, after_action_spaces = self._encoded_policy_inputs(
            after_views
        )
        terminated = after_views["red"].terminated
        truncated = False if terminated else after_views["red"].truncated
        self._validate_completed_window(
            snapshot,
            advance,
            after_views,
            after_capabilities,
            ended=terminated or truncated,
        )
        native_result = self.match.gateway.manager.native_result(
            self.match.session_id
        )
        action_ids = {
            side: _submitted_action_ids(decisions[side])
            for side in ("red", "blue")
        }
        action_semantic_hashes = {
            side: canonical_hash(decisions[side].to_record())
            for side in ("red", "blue")
        }
        action_hash = joint_action_hash(
            action_ids["red"], action_ids["blue"],
            red_semantic_hash=action_semantic_hashes["red"],
            blue_semantic_hash=action_semantic_hashes["blue"],
        )
        identities = {
            side: _reward_transition_identity(
                episode_id=self.episode_id,
                run_namespace=self.run_namespace,
                side=side,
                before=before_views[side],
                after=after_views[side],
                advance=advance,
                submitted_action_ids=action_ids[side],
                submitted_action_semantic_hash=action_semantic_hashes[side],
                joint_actions_hash=action_hash,
            )
            for side in ("red", "blue")
        }
        truncation = (
            TrainingTruncationProvenance.native_max_ticks()
            if truncated
            else TrainingTruncationProvenance.none()
        )
        base_reward = build_joint_training_reward_record(
            red_transition_identity=identities["red"],
            blue_transition_identity=identities["blue"],
            terminated=terminated,
            truncated=truncated,
            winner=native_result.winner,
            terminal_reason_code=native_result.reason,
            truncation=truncation,
            policy=self.base_reward_policy,
        )
        joint_reward = self.compose_base_reward(base_reward)
        rewards = {
            "red": joint_reward.red.total_reward,
            "blue": joint_reward.blue.total_reward,
        }
        transitions = tuple(
            DecisionTransition(
                episode_id=self.episode_id,
                run_namespace=self.run_namespace,
                decision_index=decision_index,
                side=side,
                observation=json_clone(before_observations[side]),
                capabilities=deepcopy(before_capabilities[side]),
                decision=decisions[side],
                reward=rewards[side],
                next_observation=json_clone(after_observations[side]),
                next_capabilities=deepcopy(after_capabilities[side]),
                terminated=terminated,
                truncated=truncated,
                seed=self.seed,
                scenario_id=self.match.bundle.scenario_identity.scenario_id,
                scenario_hash=self.match.bundle.scenario_identity.sha256,
                map_id=self.match.bundle.map_identity.map_id,
                fact_frame_start=advance.first_frame_index,
                fact_frame_end=advance.last_frame_index,
                sim_time_start_ms=advance.sim_time_before_ms,
                sim_time_end_ms=advance.sim_time_after_ms,
                reward_record=joint_reward.side_record(side),
            )
            for side in ("red", "blue")
        )
        next_index = (
            decision_index
            if terminated or truncated
            else decision_index + 1
        )
        if not (terminated or truncated):
            self._snapshot = self._build_snapshot(
                after_views, after_capabilities
            )
        return AgentStepResult(
            episode_id=self.episode_id,
            run_namespace=self.run_namespace,
            observations=after_observations,
            capabilities=after_capabilities,
            action_spaces=after_action_spaces,
            rewards=rewards,
            terminated=terminated,
            truncated=truncated,
            decision_index=decision_index,
            next_decision_index=next_index,
            fact_frame_start=advance.first_frame_index,
            fact_frame_end=advance.last_frame_index,
            sim_time_start_ms=advance.sim_time_before_ms,
            sim_time_end_ms=advance.sim_time_after_ms,
            winner=after_views["red"].winner,
            outcome=after_views["red"].outcome,
            transitions=transitions,
            info={
                "decision_window_index": decision_index,
                "generated_frame_count": advance.generated_frame_count,
                "fact_frame_range_contract": "closed",
                "truncation_source": "native" if truncated else None,
                "referee_labels_exposed": False,
            },
            joint_reward_record=joint_reward,
        )

    def compose_base_reward(
        self, base_reward: JointTrainingRewardRecord
    ) -> JointTrainingRewardRecord:
        policy = self.shaping_policy
        if policy is None:
            return base_reward
        identity = base_reward.red.transition_identity
        manager = self.match.gateway.manager
        before = manager.frame_analysis(
            self.match.session_id, identity.frame_before
        )
        after = None if base_reward.red.terminated else manager.frame_analysis(
            self.match.session_id, identity.frame_after
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

    def _validated_current_snapshot(self):
        snapshot = self._snapshot
        if snapshot is None:
            raise NativeMultiSeatError(
                "training_decision_snapshot_uninitialized",
                "reset_result must bind the initial decision snapshot",
                phase="snapshot",
            )
        views = self._views()
        capabilities = self._capabilities(views)
        current = self._build_snapshot(views, capabilities)
        if current != snapshot:
            self._snapshot_mismatch(
                snapshot.decision_index, current.decision_index
            )
        return views, capabilities, snapshot

    def _build_snapshot(self, views, capabilities) -> _DecisionSnapshot:
        red_index = views["red"].step_index
        blue_index = views["blue"].step_index
        actual = red_index if red_index == blue_index else -1
        if (
            red_index != blue_index
            or capabilities["red"].decision_index != red_index
            or capabilities["blue"].decision_index != blue_index
            or views["red"].frame_index != views["blue"].frame_index
            or views["red"].sim_time_ms != views["blue"].sim_time_ms
            or views["red"].terminal != views["blue"].terminal
            or views["red"].terminated != views["blue"].terminated
            or views["red"].truncated != views["blue"].truncated
        ):
            expected = (
                red_index
                if self._snapshot is None
                else self._snapshot.decision_index
            )
            self._snapshot_mismatch(expected, actual)
        return _DecisionSnapshot(
            self.episode_id,
            red_index,
            red_index,
            blue_index,
            views["red"].frame_index,
            views["red"].sim_time_ms,
            _side_snapshot_hash(views["red"], capabilities["red"]),
            _side_snapshot_hash(views["blue"], capabilities["blue"]),
        )

    def _validate_completed_window(
        self,
        snapshot: _DecisionSnapshot,
        advance,
        views,
        capabilities,
        *,
        ended: bool,
    ) -> None:
        if (
            advance.decision_window_index != snapshot.decision_index
            or advance.first_frame_index != snapshot.frame_index + 1
            or advance.last_frame_index != views["red"].frame_index
            or advance.sim_time_before_ms != snapshot.sim_time_ms
            or advance.sim_time_after_ms != views["red"].sim_time_ms
        ):
            self._snapshot_mismatch(
                snapshot.decision_index, advance.decision_window_index
            )
        completed = self._build_snapshot(views, capabilities)
        expected_after = (
            snapshot.decision_index
            if ended
            else snapshot.decision_index + 1
        )
        if completed.decision_index != expected_after:
            self._snapshot_mismatch(expected_after, completed.decision_index)

    def _snapshot_mismatch(self, expected: int, actual: int) -> None:
        self._failed = True
        self._snapshot = None
        raise TrainingDecisionSnapshotMismatchError(expected, actual)

    def _views(self):
        try:
            return {
                side: self.controllers[side].observe()
                for side in ("red", "blue")
            }
        except DecisionProjectionContractError:
            expected = -1 if self._snapshot is None else (
                self._snapshot.decision_index
            )
            self._snapshot_mismatch(expected, -1)
            raise AssertionError("unreachable")

    def _capabilities(self, views) -> dict[str, SeatActionCapabilities]:
        return {
            side: self._capability(side, views[side])
            for side in ("red", "blue")
        }

    def _capability(self, side: str, view) -> SeatActionCapabilities:
        index = view.step_index
        actors = () if view.terminal else _friendly_actor_ids(view.observation)
        summary = (
            _empty_capability_summary()
            if view.terminal
            else self.product_services[side].capabilities(actors)
        )
        operations = summary.get("operations", {})
        operation_types = tuple(
            kind
            for kind, value in operations.items()
            if isinstance(value, Mapping)
            and int(value.get("applicable_count", 0)) > 0
        )
        return SeatActionCapabilities(
            side,
            index,
            actors,
            operation_types,
            () if view.terminal else view.valid_actions,
            summary,
        )

    def _encoded_policy_inputs(self, views):
        observations: dict[str, Mapping[str, Any]] = {}
        action_spaces = {}
        for side in ("red", "blue"):
            view = views[side]
            decision = view.decision_semantic
            catalog = view.action_catalog
            if decision is None or catalog is None:
                raise NativeMultiSeatError(
                    "decision_v1_controller_core_missing",
                    "ControllerView lacks Decision semantic/catalog",
                    side=side,
                    phase="encode",
                )
            encoded = self.observation_encoder.encode(decision)
            actions = self.action_encoder.encode(catalog)
            observations[side] = encoded.policy_record()
            action_spaces[side] = actions
        return observations, action_spaces

    def _require_synchronized(self) -> None:
        if self._failed:
            raise NativeMultiSeatError(
                "native_multi_seat_failed_reset_required",
                "decision execution failed; create or reset the episode",
            )


class NativeMultiSeatEnv:
    """Recommended project-native two-seat Agent environment."""

    def __init__(
        self,
        match_factory: MatchFactory,
        *,
        run_namespace: str,
        max_decision_windows: int | None = None,
        encoder_config: DecisionEncoderConfig | None = None,
        enable_eight_factor_shaping: bool = False,
        discount_config: SharedTrainingDiscountConfig | None = None,
        shaping_policy: EightFactorShapingPolicy | None = None,
    ) -> None:
        if not callable(match_factory):
            raise TypeError("match_factory must be callable")
        self.run_namespace = _run_namespace(run_namespace)
        if max_decision_windows is not None and (
            type(max_decision_windows) is not int
            or max_decision_windows <= 0
        ):
            raise ValueError("max_decision_windows must be positive or None")
        self._match_factory = match_factory
        self.max_decision_windows = max_decision_windows
        self.encoder_config = (
            DecisionEncoderConfig() if encoder_config is None else encoder_config
        )
        if not isinstance(self.encoder_config, DecisionEncoderConfig):
            raise TypeError("encoder_config must be DecisionEncoderConfig")
        self.enable_eight_factor_shaping = enable_eight_factor_shaping
        self.discount_config = discount_config
        self.shaping_policy = shaping_policy
        self._match: MatchHandle | None = None
        self._session: NativeDecisionSession | None = None
        self._transitions: list[DecisionTransition] = []
        self._reward_records: list[JointTrainingRewardRecord] = []
        self._episode_returns = {"red": 0.0, "blue": 0.0}
        self._decision_count = 0
        self._episode_sequence = 0
        self._ended = False
        self._closed = False

    def reset(self, seed: int = 42) -> AgentResetResult:
        self._require_open()
        if type(seed) is not int:
            raise TypeError("seed must be an exact int")
        self._close_match()
        self._episode_sequence += 1
        match = self._match_factory(seed, self._episode_sequence)
        if not isinstance(match, MatchHandle):
            raise TypeError("match_factory must return MatchHandle")
        episode_id = training_episode_id(
            self.run_namespace,
            match.bundle.scenario_identity.sha256,
            seed,
            self._episode_sequence,
        )
        try:
            session = NativeDecisionSession(
                match,
                episode_id=episode_id,
                run_namespace=self.run_namespace,
                seed=seed,
                encoder_config=self.encoder_config,
                enable_eight_factor_shaping=self.enable_eight_factor_shaping,
                discount_config=self.discount_config,
                shaping_policy=self.shaping_policy,
            )
            reset_result = session.reset_result()
        except BaseException:
            match.close()
            self._match = None
            self._session = None
            raise
        self._match = match
        self._session = session
        self._transitions.clear()
        self._reward_records.clear()
        self._episode_returns = {"red": 0.0, "blue": 0.0}
        self._decision_count = 0
        self._ended = match.status.terminal
        return reset_result

    def step(
        self,
        red_decision: SeatDecision,
        blue_decision: SeatDecision,
    ) -> AgentStepResult:
        self._require_active()
        result = self._require_session().step(red_decision, blue_decision)
        return self._accept_step_result(result)

    def step_action_indices(
        self,
        red_action_indices: Sequence[int],
        blue_action_indices: Sequence[int],
    ) -> AgentStepResult:
        """Decision-v1 joint policy step using each side's emitted catalog."""

        self._require_active()
        result = self._require_session().step_action_indices(
            red_action_indices, blue_action_indices
        )
        return self._accept_step_result(result)

    def _accept_step_result(self, result: AgentStepResult) -> AgentStepResult:
        self._decision_count += 1
        training_truncated = (
            not result.terminated
            and not result.truncated
            and self.max_decision_windows is not None
            and self._decision_count >= self.max_decision_windows
        )
        if training_truncated:
            assert self.max_decision_windows is not None
            provenance = TrainingTruncationProvenance.training_limit(
                RewardTruncationSource.TRAINING_MAX_DECISION_WINDOWS,
                self.max_decision_windows,
            )
            original = result.joint_reward_record
            base_reward = build_joint_training_reward_record(
                red_transition_identity=original.red.transition_identity,
                blue_transition_identity=original.blue.transition_identity,
                terminated=False,
                truncated=True,
                winner=None,
                terminal_reason_code=None,
                truncation=provenance,
                policy=self._require_session().base_reward_policy,
            )
            joint_reward = self._require_session().compose_base_reward(base_reward)
            transitions = tuple(
                replace(
                    value,
                    truncated=True,
                    reward=joint_reward.side_record(value.side).total_reward,
                    reward_record=joint_reward.side_record(value.side),
                )
                for value in result.transitions
            )
            info = json_clone(result.info)
            info["truncation_source"] = "training_max_decision_windows"
            result = replace(
                result,
                truncated=True,
                rewards={
                    "red": joint_reward.red.total_reward,
                    "blue": joint_reward.blue.total_reward,
                },
                transitions=transitions,
                info=info,
                joint_reward_record=joint_reward,
            )
        self._reward_records.append(deepcopy(result.joint_reward_record))
        for side in ("red", "blue"):
            self._episode_returns[side] += result.rewards[side]
        info = json_clone(result.info)
        info["training_supervision"] = {
            "classification": "TRAINING_SUPERVISION",
            "joint_reward_schema": result.joint_reward_record.schema_version,
            "joint_reward_hash": result.joint_reward_record.semantic_hash,
            "joint_transition_id": result.joint_reward_record.joint_transition_id,
            "reward_policy": result.joint_reward_record.reward_policy.to_dict(),
            "episode_return": dict(self._episode_returns),
        }
        result = replace(result, info=info)
        self._transitions.extend(deepcopy(result.transitions))
        self._ended = result.terminated or result.truncated
        return deepcopy(result)

    def decision_context(self, side: str) -> AgentDecisionContext:
        self._require_active()
        return deepcopy(self._require_session().decision_context(side))

    def agent_reset_context(self, side: str, agent_id: str):
        self._require_initialized()
        return deepcopy(
            self._require_session().agent_reset_context(side, agent_id)
        )

    @property
    def transitions(self) -> tuple[DecisionTransition, ...]:
        self._require_initialized()
        return deepcopy(tuple(self._transitions))

    @property
    def reward_records(self) -> tuple[JointTrainingRewardRecord, ...]:
        self._require_initialized()
        return deepcopy(tuple(self._reward_records))

    @property
    def episode_returns(self) -> Mapping[str, float]:
        self._require_initialized()
        return dict(self._episode_returns)

    @property
    def decision_window_count(self) -> int:
        return self._decision_count

    @property
    def match(self) -> MatchHandle:
        self._require_initialized()
        assert self._match is not None
        return self._match

    @property
    def episode_id(self) -> str:
        return self._require_session().episode_id

    def write_transitions(self, path: str | Path) -> Path:
        return write_decision_transitions_jsonl(path, self.transitions)

    def publish(
        self,
        output_dir: str | Path,
        *,
        overwrite: bool = False,
    ) -> SessionPublication:
        self._require_initialized()
        if not self._ended:
            raise NativeMultiSeatError(
                "native_multi_seat_active", "active episode cannot be published"
            )
        return self.match.publish(
            output_dir,
            truncated=not self.match.status.terminal,
            overwrite=overwrite,
        )

    def publish_training_package(
        self,
        output_dir: str | Path,
        *,
        summary_schema: str,
        outcome: str | None = None,
        summary_extra: Mapping[str, Any] | None = None,
        overwrite: bool = False,
    ) -> TrainingEpisodePublication:
        """Publish facts, analysis, transitions and summary as one sample."""

        self._require_initialized()
        if not self._ended:
            raise NativeMultiSeatError(
                "native_multi_seat_active", "active episode cannot be published"
            )
        return publish_training_episode(
            self.match,
            output_dir,
            run_namespace=self.run_namespace,
            episode_id=self.episode_id,
            transitions=self.transitions,
            joint_reward_records=self.reward_records,
            decision_window_count=self._decision_count,
            truncated=not self.match.status.terminal,
            outcome=outcome,
            summary_schema=summary_schema,
            summary_extra=summary_extra,
            overwrite=overwrite,
            shaping_policy=self._require_session().shaping_policy,
            discount_config=self._require_session().discount_config,
            composite_policy=self._require_session().composite_reward_policy,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._close_match()
        self._closed = True

    def _close_match(self) -> None:
        if self._match is not None:
            self._match.close()
        self._match = None
        self._session = None

    def _require_session(self) -> NativeDecisionSession:
        if self._session is None:
            raise NativeMultiSeatError(
                "native_multi_seat_not_reset", "call reset before use"
            )
        return self._session

    def _require_initialized(self) -> None:
        self._require_open()
        self._require_session()

    def _require_active(self) -> None:
        self._require_initialized()
        if self._ended:
            raise NativeMultiSeatError(
                "native_multi_seat_ended_reset_required",
                "episode ended; call reset before another step",
            )

    def _require_open(self) -> None:
        if self._closed:
            raise NativeMultiSeatError(
                "native_multi_seat_closed", "environment is closed"
            )


class SingleSeatTrainingAdapter:
    """One controlled seat over the recommended two-seat environment."""

    def __init__(
        self,
        environment: NativeMultiSeatEnv,
        *,
        controlled_side: str,
        opponent: LocalSeatAgent,
    ) -> None:
        if not isinstance(environment, NativeMultiSeatEnv):
            raise TypeError("environment must be NativeMultiSeatEnv")
        self.environment = environment
        self.controlled_side = _side(controlled_side)
        self.opponent_side = (
            "blue" if self.controlled_side == "red" else "red"
        )
        self.opponent = opponent
        self._reset = False
        self._opponent_decision_index: int | None = None
        self._opponent_cache_key: tuple[str, int] | None = None
        self._opponent_decision: SeatDecision | None = None

    def reset(self, seed: int = 42) -> SingleSeatResetResult:
        result = self.environment.reset(seed)
        try:
            self.opponent.reset(self.environment.agent_reset_context(
                self.opponent_side, self.opponent.agent_id
            ))
        except BaseException as exc:
            raise NativeMultiSeatError(
                "single_seat_opponent_reset_failed",
                f"{type(exc).__name__}:{exc}",
                side=self.opponent_side,
                phase="reset",
            ) from exc
        self._reset = True
        self._opponent_decision_index = None
        self._opponent_cache_key = None
        self._opponent_decision = None
        return SingleSeatResetResult(
            json_clone(result.observations[self.controlled_side]),
            deepcopy(result.capabilities[self.controlled_side]),
            deepcopy(result.action_spaces[self.controlled_side]),
            {
                "episode_id": result.episode_id,
                "decision_index": result.decision_index,
                "fact_frame_index": result.fact_frame_index,
                "sim_time_ms": result.sim_time_ms,
                "side": self.controlled_side,
            },
        )

    def step(self, decision: SeatDecision) -> SingleSeatStepResult:
        if not self._reset:
            raise NativeMultiSeatError(
                "single_seat_not_reset", "call reset before use"
            )
        controlled_decision = _decision(decision)
        try:
            context = self.environment.decision_context(self.opponent_side)
        except NativeMultiSeatError:
            self._reset = False
            self._clear_opponent_cache()
            raise
        cache_key = (self.environment.episode_id, context.decision_index)
        if self._opponent_cache_key == cache_key:
            assert self._opponent_decision is not None
            opponent_decision = deepcopy(self._opponent_decision)
        else:
            try:
                opponent_decision = self.opponent.act(context=deepcopy(context))
                opponent_decision = _decision(opponent_decision)
            except BaseException as exc:
                self._reset = False
                raise NativeMultiSeatError(
                    "single_seat_opponent_act_failed_reset_required",
                    f"{type(exc).__name__}:{exc}",
                    side=self.opponent_side,
                    phase="act",
                ) from exc
            self._opponent_decision_index = context.decision_index
            self._opponent_cache_key = cache_key
            self._opponent_decision = deepcopy(opponent_decision)
        decisions = {
            self.controlled_side: controlled_decision,
            self.opponent_side: opponent_decision,
        }
        try:
            result = self.environment.step(decisions["red"], decisions["blue"])
        except NativeMultiSeatError as exc:
            if exc.code != "decision_preview_failed":
                self._reset = False
                self._clear_opponent_cache()
            raise
        self._opponent_decision_index = None
        self._clear_opponent_cache()
        transition = next(
            value
            for value in result.transitions
            if value.side == self.controlled_side
        )
        return SingleSeatStepResult(
            json_clone(result.observations[self.controlled_side]),
            deepcopy(result.capabilities[self.controlled_side]),
            deepcopy(result.action_spaces[self.controlled_side]),
            result.rewards[self.controlled_side],
            result.terminated,
            result.truncated,
            {
                **json_clone(result.info),
                "episode_id": result.episode_id,
                "decision_index": result.decision_index,
                "fact_frame_range": [
                    result.fact_frame_start, result.fact_frame_end
                ],
                "sim_time_range_ms": [
                    result.sim_time_start_ms, result.sim_time_end_ms
                ],
                "winner": result.winner,
                "outcome": result.outcome,
                "side": self.controlled_side,
            },
            deepcopy(transition),
            deepcopy(transition.reward_record),
        )

    def close(self) -> None:
        try:
            self.opponent.close()
        finally:
            self.environment.close()

    def _clear_opponent_cache(self) -> None:
        self._opponent_decision_index = None
        self._opponent_cache_key = None
        self._opponent_decision = None


def mark_transition_pair_truncated(
    values: Sequence[DecisionTransition],
    *,
    configured_limit: int,
) -> tuple[DecisionTransition, ...]:
    """Mark a runner-level final decision as a non-Native truncation."""

    if not values:
        return ()
    by_side = {value.side: value for value in values}
    if set(by_side) != {"red", "blue"}:
        raise ValueError("truncated transition pair must contain RED and BLUE")
    provenance = TrainingTruncationProvenance.training_limit(
        RewardTruncationSource.TRAINING_MAX_DECISION_WINDOWS,
        configured_limit,
    )
    joint = build_joint_training_reward_record(
        red_transition_identity=by_side["red"].reward_record.transition_identity,
        blue_transition_identity=by_side["blue"].reward_record.transition_identity,
        terminated=False,
        truncated=True,
        winner=None,
        terminal_reason_code=None,
        truncation=provenance,
        policy=load_reward_policy(),
    )
    return tuple(
        replace(
            value,
            truncated=True,
            reward=joint.side_record(value.side).total_reward,
            reward_record=joint.side_record(value.side),
        )
        for value in values
    )


def _side(value: str) -> str:
    side = str(value).lower()
    if side not in {"red", "blue"}:
        raise ValueError("side must be RED or BLUE")
    return side


def _decision(value: SeatDecision) -> SeatDecision:
    if not isinstance(value, SeatDecision):
        raise TypeError("Agent decision must be a SeatDecision")
    return deepcopy(value)


def _friendly_actor_ids(observation: Mapping[str, Any]) -> tuple[int, ...]:
    values = observation.get("friendly_operators", ())
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise RuntimeError("side observation friendly_operators is invalid")
    result = []
    for value in values:
        if not isinstance(value, Mapping):
            raise RuntimeError("side observation friendly operator is invalid")
        unit_id = value.get("operator_id")
        if type(unit_id) is not int or unit_id < 0:
            raise RuntimeError("side observation operator_id is invalid")
        result.append(unit_id)
    return tuple(sorted(result))


def _empty_capability_summary() -> dict[str, object]:
    return {
        "selected_count": 0,
        "selected_units": [],
        "operations": {},
        "shoot_target_ids": [],
    }


def _submitted_action_ids(
    decision: SeatDecision | CatalogActionSelection,
) -> tuple[str, ...] | None:
    if isinstance(decision, CatalogActionSelection):
        return tuple(decision.action_ids)
    if not decision.operations:
        return ()
    return None


def _reward_transition_identity(
    *,
    episode_id: str,
    run_namespace: str,
    side: str,
    before,
    after,
    advance,
    submitted_action_ids: tuple[str, ...] | None,
    submitted_action_semantic_hash: str,
    joint_actions_hash: str,
):
    before_decision = before.decision_semantic
    before_catalog = before.action_catalog
    after_decision = after.decision_semantic
    if before_decision is None or before_catalog is None or after_decision is None:
        raise NativeMultiSeatError(
            "reward_decision_core_missing",
            "reward transition requires before/after Decision semantic evidence",
            side=side,
            phase="reward",
        )
    return build_reward_transition_identity(
        episode_id=episode_id,
        run_namespace=run_namespace,
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


def _run_namespace(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("run_namespace must be a non-empty string")
    if value != value.strip():
        raise ValueError("run_namespace must not contain surrounding whitespace")
    return value


def training_episode_id(
    run_namespace: str,
    scenario_hash: str,
    seed: int,
    episode_sequence: int,
) -> str:
    """Return the stable namespace/scenario/seed/local-sequence identity."""

    _run_namespace(run_namespace)
    if not isinstance(scenario_hash, str) or not scenario_hash:
        raise ValueError("scenario_hash must be non-empty")
    if type(seed) is not int:
        raise TypeError("seed must be an exact int")
    if type(episode_sequence) is not int or episode_sequence <= 0:
        raise TypeError("episode_sequence must be a positive exact int")
    identity = canonical_hash({
        "run_namespace": run_namespace,
        "scenario_hash": scenario_hash,
        "seed": seed,
        "episode_sequence": episode_sequence,
    })
    return f"native-training-{identity[:32]}"


def _side_snapshot_hash(view, capabilities: SeatActionCapabilities) -> str:
    return canonical_hash({
        "decision_index": view.step_index,
        "frame_index": view.frame_index,
        "sim_time_ms": view.sim_time_ms,
        "observation": view.observation,
        "valid_actions": view.valid_actions,
        "capabilities": capabilities.to_record(),
        "decision_semantic_hash": (
            None
            if view.decision_semantic is None
            else view.decision_semantic.semantic_hash
        ),
        "action_catalog_hash": (
            None if view.action_catalog is None else view.action_catalog.catalog_hash
        ),
    })
