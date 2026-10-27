from __future__ import annotations

from wargame_native_engine.weapon_names import weapon_display_name
from wargame_native_engine.adjudication.fire_readiness import weapon_binding_failure
from wargame_native_engine.adjudication.request import fire_elevation_level_difference

from collections import defaultdict
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from wargame_native_engine.adjudication import (
    AdjudicationTableSet, DiceRoller, DirectFireAdjudicator,
    DirectFireRequest, DirectFireResolution, DirectFireResourcePolicy,
    FireResolution,
    GuidedFireCommitEffect, GuidedFireEffectPlan, GuidedFireRecoveryUpdate,
    GuidedFireRequest, GuidedFireResolver, GuidedFireStageResult,
    GuidedFireValidationResult, WeaponProfile,
)
from wargame_native_engine.adjudication.boarding import (
    BoardRequest,
    BoardValidationResult,
    UnloadRequest,
    UnloadValidationResult,
)
from wargame_native_engine.adjudication.guided_fire import (
    _build_validated_guided_fire_request,
)
from wargame_native_engine.adjudication.indirect_fire import (
    IndirectFireEffectResolutionResult,
    IndirectFireEffectStatus,
    IndirectFireEffectBundle,
    IndirectFireImpactValidationResult,
    IndirectFireMissResolution,
    IndirectFireObservationPolicy,
    IndirectFirePlanRequest,
    IndirectFirePlanValidationEvidence,
    IndirectFirePlanValidationResult,
    IndirectFireResolution,
    _build_validated_indirect_fire_plan_request,
)
from wargame_native_engine.adjudication.indirect_fire_service import (
    IndirectFireLifecycleService,
)
from wargame_native_engine.adjudication.fire_effect_query import (
    FireEffectQuery,
    FireEffectQueryError,
    direct_fire_probabilities,
    projected_fire_inputs,
    projected_guider,
    query_result_base,
)
from wargame_native_engine.adjudication.weapon_catalog import build_weapon_catalog
from wargame_native_engine.adjudication.indirect_effect_query import indirect_fire_probabilities
from wargame_native_engine.compat.source_frame.models import ArmorClass, UnitDomain
from wargame_native_engine.core.action import (
    ACTION_PRIORITY, ActionDecision, ActionType, EngineAction, ValidAction,
    valid_action_sort_key,
)
from wargame_native_engine.core.action_router import ActionRouter, RoutedAction
from wargame_native_engine.core.boarding import (
    BoardCapability,
    BoardingCapabilityAssessment,
    BoardingCapabilityStatus,
    BoardingTransitionStatus,
    BoardingTransitionType,
    UnloadCapability,
    _encode_board_action,
    _encode_unload_action,
    board_capability_sort_key,
    unload_capability_sort_key,
)
from wargame_native_engine.core.boarding_runtime import (
    SourceBoardingIntent,
    advance_source_boarding_transitions,
    sync_mounted_passenger_positions,
    apply_source_boarding_intents,
)
from wargame_native_engine.core.concealment import (
    ConcealmentCapability,
    ConcealmentPosture,
    concealment_capability_sort_key,
)
from wargame_native_engine.core.empty_fire import (
    EMPTY_FIRE_EXPOSURE_FRAMES, EmptyFireIntent, advance_empty_fire_exposure,
    empty_fire_readiness_error, has_empty_fire_exposure, mark_empty_fire_exposure,
)
from wargame_native_engine.core.destruction import destroy_source_operator
from wargame_native_engine.core.event import (
    EngineEvent,
    EventType,
    event_sort_key,
    is_green_judge_event,
)
from wargame_native_engine.core.fire_capability import (
    GuidedFireCapability,
    IndirectFireCapability,
    IndirectFireCapabilityStatus,
    IndirectFireTargetHexStatus,
    IndirectFireTargetHexValidation,
    guided_fire_capability_sort_key,
    indirect_fire_capability_sort_key,
)
from wargame_native_engine.core.fire_intent import (
    FireIntent,
    FireMode,
    HexTarget,
    IndirectFireIntent,
    UnitTarget,
    _build_validated_indirect_fire_intent,
)
from wargame_native_engine.core.indirect_fire import (
    FireMission,
    FireMissionObservationEvidence,
    IndirectFireActiveAreaView,
    IndirectFireMissionAreaView,
    can_cancel_mission,
    fire_mission_sort_key,
)
from wargame_native_engine.core.observation import (
    OperatorObservation,
    SideObservation,
    objective_view,
    side_safe_enemy_observation,
)
from wargame_native_engine.core.objective_runtime import (
    SourceCaptureIntent,
    apply_source_capture_intents,
    award_controlled_objective_scores,
    score_objectives,
    stage_source_capture,
)
from wargame_native_engine.core.operator import (
    MovementTaskPauseReason,
    OperatorState,
)
from wargame_native_engine.core.result import GameResult, GameStatus, StepResult
from wargame_native_engine.core.rng import NativeRNG
from wargame_native_engine.core.tactical_overlay import (
    FireTarget,
    FireTargets,
    UnitTacticalOverlay,
    VisibilityDomainField,
    VisibilityField,
    WeaponEnvelope,
    WeaponEnvelopeEntry,
)
from wargame_native_engine.core.region_visibility import (
    RegionVisibilityQuery,
    RegionVisibilityQueryError,
    RegionVisibilityResult,
    evaluate_region_visibility,
)
from wargame_native_engine.core.world import Stage, WorldState
from wargame_native_engine.rules.operator_identity import (
    resolve_operator_canonical_identity,
)
from wargame_native_engine.io.scenario import (
    NativeScenario,
    native_map_identity_hash,
)
from wargame_native_engine.io.serialization import canonical_hash, json_clone
from wargame_native_engine.movement.map_adapter import (
    SourceHexMap,
    SourceMovementMapReader,
    StaticMovementMapReader,
)
from wargame_native_engine.movement.models import MovementMode, RoutePlan
from wargame_native_engine.movement.planner import MovementPlanner
from wargame_native_engine.movement.policy import (
    SourceMovementPolicy, load_source_movement_policy,
)
from wargame_native_engine.movement.runtime import MovementRuntime
from wargame_native_engine.movement.route_planner import (
    DetachedMovementCostQuery,
    DeterministicRoutePlanner,
    PathPlanner,
    PathPlanningRequest,
    full_friendly_ground_hexes,
)
from wargame_native_engine.rules.config import Ruleset, load_ruleset
from wargame_native_engine.rules.boarding import (
    BOARDING_EXPLICIT_DISABLED,
    BoardingAuthority,
    BoardingProfile,
    BoardingTypeAssignmentAuthority,
    load_boarding_authority,
    load_boarding_type_assignment_authority,
)
from wargame_native_engine.rules.guidance import (
    GuidanceAffiliationEvidence,
    GuidanceProfile,
    GuidanceScope,
    load_guidance_authority,
)
from wargame_native_engine.rules.indirect_fire import (
    ArtilleryClass,
    INDIRECT_FIRE_AUTHORITY_SCHEMA_VERSION,
    INDIRECT_FIRE_EXECUTION_POLICY_ID,
    INDIRECT_FIRE_EXECUTION_POLICY_VERSION,
    IndirectFireAmmunitionAuthority,
    IndirectFireAuthority,
    IndirectFireAuthorityDescriptor,
    IndirectFireCancellationReason,
    IndirectFireDamagePolicy,
    IndirectFireExecutionPolicy,
    IndirectFireMissionState,
    IndirectFireProfile,
    IndirectRangePolicy,
    IndirectTargetDomain,
    ScatterDirectionPolicy,
    build_indirect_fire_authority_descriptor,
    load_indirect_fire_ammunition_authority,
    load_indirect_fire_authority,
    load_indirect_fire_damage_policy,
    load_indirect_fire_execution_policy,
    load_indirect_fire_observation_authority,
    load_indirect_fire_replay_identity,
    load_legacy_indirect_fire_replay_identity,
)
from wargame_native_engine.rules.operator_lifecycle import (
    LifecycleExpiryBehavior,
    OperatorLifecycleAuthority,
    load_operator_lifecycle_authority,
)
from wargame_native_engine.rules.objective_capture import (
    EXPLICIT_CAPTURE_MODE,
    LEGACY_CAPTURE_MODE,
    ObjectiveCaptureService,
    load_objective_capture_policy,
    resolve_objective_capture_policy_identity,
)
from wargame_native_engine.rules.perspective import load_perspective_policy
from wargame_native_engine.rules.loitering_munition import (
    LoiteringMunitionAttackAuthority,
    LoiteringMunitionAttackPolicy,
    load_loitering_munition_attack_authority,
)
from wargame_native_engine.rules.terminal_rules import TerminalRules
from wargame_native_engine.core.time import (
    require_positive_milliseconds,
    resolve_delta_time_ms,
)
from wargame_native_engine.systems.combat import validate_attack
from wargame_native_engine.systems.damage import apply_damage
from wargame_native_engine.systems.deployment import validate_deployment
from wargame_native_engine.systems.movement import (
    distance, move_toward, validate_move,
)
from wargame_native_engine.systems.terminal import evaluate_terminal
from wargame_native_engine.systems.visibility import can_see
from wargame_native_engine.visibility import (
    HexCell, HexMapProfile, LineOfSightEvidence, ObservationProfiles,
    VisibilityResult, evaluate_line_of_sight, evaluate_visibility, hex_distance,
    within_maximum_observation_distance,
)
from wargame_native_engine.version import ADJUDICATION_SEMANTICS_VERSION

if TYPE_CHECKING:
    from wargame_native_engine.data.map_runtime import MapRuntimeData


@dataclass(frozen=True, slots=True)
class _SourceFireIntent:
    routed: RoutedAction
    contract: FireIntent
    request: DirectFireRequest
    resolution: DirectFireResolution
    loitering_attack_policy: LoiteringMunitionAttackPolicy | None = None


@dataclass(frozen=True, slots=True)
class _SourceGuidedFireIntent:
    routed: RoutedAction
    contract: FireIntent
    stage: GuidedFireStageResult
    commit_effect: GuidedFireCommitEffect
    direct_request: DirectFireRequest


@dataclass(frozen=True, slots=True)
class _SourceIndirectFireIntent:
    routed: RoutedAction
    contract: IndirectFireIntent
    request: IndirectFirePlanRequest
    mission: FireMission


@dataclass(frozen=True, slots=True)
class _SourceConcealmentIntent:
    routed: RoutedAction
    capability: ConcealmentCapability


class NativeEngine:
    """Authoritative runtime for the documented Native simplified rules."""

    def __init__(self, scenario: NativeScenario, *,
                 max_ticks: int | None = None,
                 ruleset: Ruleset | str | Path | None = None,
                 map_runtime_data: "MapRuntimeData | None" = None,
                 path_planner: PathPlanner | None = None,
                 enable_boarding_type_assignments: bool = True,
                 mounted_position_policy: str | None = None,
                 objective_capture_policy: str | Mapping[str, Any] | None = None
                 ) -> None:
        if mounted_position_policy not in {None, "native-mounted-position-sync/1.0"}:
            raise ValueError("unknown mounted position policy")
        self.mounted_position_policy = mounted_position_policy
        self.scenario = scenario
        if (
            map_runtime_data is not None
            and (
                map_runtime_data.map_id != scenario.map.map_id
                or map_runtime_data.map_sha256
                != native_map_identity_hash(scenario.map)
            )
        ):
            raise ValueError("map runtime data does not match scenario map")
        self.map_runtime_data = map_runtime_data
        self.ruleset = (ruleset if isinstance(ruleset, Ruleset)
                        else load_ruleset(ruleset))
        self._boarding_authority = load_boarding_authority()
        if objective_capture_policy == "legacy-unversioned":
            self._capture_policy = load_objective_capture_policy("1")
            self._capture_policy_recorded = False
        elif isinstance(objective_capture_policy, Mapping):
            self._capture_policy = resolve_objective_capture_policy_identity(
                objective_capture_policy
            )
            self._capture_policy_recorded = True
        else:
            self._capture_policy = load_objective_capture_policy(
                "3" if objective_capture_policy is None
                else objective_capture_policy
            )
            self._capture_policy_recorded = True
        self._capture_service: ObjectiveCaptureService | None = None
        if type(enable_boarding_type_assignments) is not bool:
            raise TypeError("enable_boarding_type_assignments must be bool")
        self._boarding_type_assignments_enabled = enable_boarding_type_assignments
        self._boarding_type_assignment_authority = (
            load_boarding_type_assignment_authority()
        )
        self._guidance_authority = load_guidance_authority()
        self._operator_lifecycle_authority = (
            load_operator_lifecycle_authority()
        )
        self._loitering_munition_attack_authority = (
            load_loitering_munition_attack_authority()
        )
        self._indirect_fire_authority = load_indirect_fire_authority()
        self._indirect_fire_execution_policy = (
            load_indirect_fire_execution_policy()
        )
        self._indirect_fire_ammunition_authority = (
            load_indirect_fire_ammunition_authority()
        )
        self._indirect_fire_observation_authority = (
            load_indirect_fire_observation_authority()
        )
        self._indirect_fire_damage_policy = load_indirect_fire_damage_policy()
        self._indirect_fire_observation_policy: (
            IndirectFireObservationPolicy | None
        ) = None
        self._indirect_fire_spotter_evidence = (
            self._indirect_fire_observation_authority.authority_basis
        )
        self._indirect_fire_authority_descriptors = {
            profile.profile_id: build_indirect_fire_authority_descriptor(
                profile,
                self._indirect_fire_authority,
                self._indirect_fire_execution_policy,
                self._indirect_fire_ammunition_authority,
                self._indirect_fire_observation_authority,
                self._indirect_fire_damage_policy,
            )
            for profile in self._indirect_fire_authority.profiles
            if self._indirect_fire_ammunition_authority.mapping_for_weapon(
                profile.weapon_id
            ) is not None
        }
        self.terminal_rules = TerminalRules(
            max_ticks=(scenario.terminal_rules.max_ticks if max_ticks is None
                       else int(max_ticks)),
            elimination=scenario.terminal_rules.elimination,
            objective_score_to_win=scenario.terminal_rules.objective_score_to_win,
            mission_score_enabled=self.ruleset.terminal.mission_score_enabled,
            mutual_elimination_result=self.ruleset.terminal.mutual_elimination_result,
            tie_result=self.ruleset.terminal.tie_result,
            evaluation_order=self.ruleset.terminal.evaluation_order,
        )
        self._world: WorldState | None = None
        self._rng: NativeRNG | None = None
        self._seed = 0
        self._closed = False
        self._action_counts = {side: 0 for side in scenario.side_order}
        self._error_counts = {side: 0 for side in scenario.side_order}
        self._last_events: tuple[EngineEvent, ...] = ()
        self._source_policy: SourceMovementPolicy | None = None
        self._source_map: SourceHexMap | None = None
        self._source_visibility_map: HexMapProfile | None = None
        self._source_observation_profiles = ObservationProfiles()
        self._source_valid_actions_cache: dict[
            str, tuple[Mapping[str, Any], ...]
        ] = {}
        self._source_valid_actions_state_hash: str | None = None
        self._movement: MovementRuntime | None = None
        self._movement_map_reader: StaticMovementMapReader | None = None
        self._route_planner: PathPlanner | None = None
        self._action_router: ActionRouter | None = None
        self._adjudication_tables: AdjudicationTableSet | None = None
        if scenario.map.hexes:
            self._source_policy = load_source_movement_policy()
            self._source_map = SourceHexMap(scenario.map, self._source_policy)
            self._movement_map_reader = SourceMovementMapReader(
                self._source_map,
                None if map_runtime_data is None else map_runtime_data.movement,
            )
            if self._capture_policy.mode == EXPLICIT_CAPTURE_MODE:
                self._capture_service = ObjectiveCaptureService(
                    self._movement_map_reader,
                    self._capture_policy,
                )
            movement_planner = MovementPlanner(
                self._movement_map_reader,
                self._source_policy,
            )
            self._movement = MovementRuntime(
                movement_planner,
                self._source_policy)
            self._route_planner = (
                DeterministicRoutePlanner()
                if path_planner is None
                else path_planner
            )
            self._action_router = ActionRouter()
            self._adjudication_tables = AdjudicationTableSet(
                Path(__file__).resolve().parents[3]
                / "config/adjudication/source_tables_v1/native_adjudication_tables_final.json")
            self._source_visibility_map = HexMapProfile(
                cells={
                    item.hex_id: HexCell(
                        item.hex_id, float(item.elevation),
                        self._source_policy.terrain(item.terrain).value)
                    for item in scenario.map.hexes
                },
                elevation_kind=scenario.map.elevation_kind,
                elevation_unit_m=scenario.map.elevation_unit_m,
            )
        self._indirect_fire_service = IndirectFireLifecycleService(
            source_map=self._source_map,
            adjudication_tables=self._adjudication_tables,
            suppression_ms=(
                None
                if self._source_policy is None
                else self._source_policy.suppression_ms
            ),
            operator_lifecycle_authority=self._operator_lifecycle_authority,
        )

    def reset(self, seed: int = 42) -> WorldState:
        if self._closed:
            raise RuntimeError("engine_closed")
        self._seed = int(seed)
        self._rng = NativeRNG(self._seed)
        self._world = self.scenario.initial_world()
        self._validate_boarding_snapshot(self._world)
        self._validate_operator_lifecycle_snapshot(self._world)
        self._validate_loitering_attack_snapshot(self._world)
        self._world.elapsed_time_ms = self._world.tick * 1000
        self._action_counts = {side: 0 for side in self.scenario.side_order}
        self._error_counts = {side: 0 for side in self.scenario.side_order}
        reset_data = {"scenario_id": self.scenario.scenario_id,
                      "scenario_hash": self.scenario.scenario_hash,
                      "seed": self._seed,
                      "ruleset_id": self.ruleset.ruleset_id,
                      "ruleset_version": self.ruleset.ruleset_version,
                      "ruleset_hash": self.ruleset.content_hash,
                      "adjudication_semantics_version": (
                          ADJUDICATION_SEMANTICS_VERSION)}
        if self._capture_policy_recorded and self._source_policy is not None:
            reset_data["objective_capture_policy"] = (
                self._capture_policy.identity_record()
            )
        if self._boarding_type_assignments_enabled:
            reset_data["boarding_type_assignment_authority"] = (
                self._boarding_type_assignment_authority.identity_record()
            )
        self._last_events = (EngineEvent(
            0, EventType.ENGINE_RESET,
            data=reset_data,
        ),)
        self._source_valid_actions_cache.clear()
        self._source_valid_actions_state_hash = None
        if self._source_policy is not None:
            self._refresh_source_observation()
        return WorldState.from_dict(self._world.to_dict())

    def observe(self, side: str) -> SideObservation:
        world = self._require_world()
        self._require_side(side)
        active_boarding = {}
        for transition in world.boarding_transitions:
            if transition.status is not BoardingTransitionStatus.ACTIVE:
                continue
            for operator_id in (transition.carrier_id, transition.passenger_id):
                if operator_id in active_boarding:
                    raise RuntimeError(
                        "operator has multiple active boarding transitions"
                    )
                active_boarding[operator_id] = transition
        friendly = tuple(
            OperatorObservation.from_state(
                item,
                boarding_transition_type=(
                    None
                    if item.operator_id not in active_boarding
                    else active_boarding[item.operator_id].transition_type.value
                ),
                boarding_transition_remaining_ms=(
                    None
                    if item.operator_id not in active_boarding
                    else active_boarding[item.operator_id].remaining_ms
                ),
            )
            for item in self._friendly(side)
        )
        perspective_policy = load_perspective_policy()
        visible = tuple(
            side_safe_enemy_observation(item, policy=perspective_policy)
            for item in self._visible_enemies(side)
        )
        actions = self.valid_actions(side)
        return SideObservation(
            side=side, tick=world.tick, stage=world.stage,
            friendly_operators=friendly, visible_enemies=visible,
            objectives=tuple(objective_view(item)
                             for item in sorted(world.objectives.values(),
                                                key=lambda item: item.objective_id)),
            legal_action_ids=tuple(item.action_id for item in actions),
            mission={"terminal_rules": {
                "elimination": self.terminal_rules.elimination,
                "objective_score_to_win": self.terminal_rules.objective_score_to_win,
                "max_ticks": self.terminal_rules.max_ticks,
            }},
        )

    def own_weapon_inventory(self, side: str) -> tuple[Mapping[str, Any], ...]:
        """Return detached carried-weapon resources for one controlling side.

        This is a read model, not a legal-action projection.  It intentionally
        includes carried weapons whose current attack would be rejected.  The
        ammunition mapping is resolved from the same Native authorities used
        by adjudication, and values come only from the current WorldState.
        """

        self._require_side(side)
        world = self._require_world()
        values: list[Mapping[str, Any]] = []
        indirect_weapon_ids = {
            profile.weapon_id for profile in self._indirect_fire_authority.profiles
        }
        for operator in sorted(
            world.operators.values(), key=lambda item: item.operator_id
        ):
            if operator.side != side:
                continue
            ammunition = self._ammunition(operator)
            resources: list[dict[str, Any]] = []
            for weapon_id in operator.weapon_ids:
                modes: set[str] = set()
                ammunition_key: str | None = None
                resource_policy = "UNKNOWN"
                ammunition_cost: int | None = None
                try:
                    profile = self._source_weapon_profile(int(weapon_id))
                except ValueError:
                    profile = None
                if profile is not None:
                    modes.update(value.value for value in profile.supported_fire_modes)
                    ammunition_key = profile.ammunition_key
                    resource_policy = "AMMUNITION_KEY"
                    if self._loitering_attack_policy_for(
                        operator, int(weapon_id), profile,
                    ) is not None:
                        ammunition_key = None
                        resource_policy = "SELF_AS_MUNITION"
                indirect = (
                    self._indirect_fire_ammunition_authority
                    .mapping_for_weapon(int(weapon_id))
                )
                if indirect is not None and int(weapon_id) in indirect_weapon_ids:
                    modes.add(FireMode.INDIRECT.value)
                    if ammunition_key is None and resource_policy == "UNKNOWN":
                        ammunition_key = indirect.ammunition_key
                        resource_policy = "AMMUNITION_KEY"
                    ammunition_cost = indirect.ammo_cost_per_mission
                resources.append({
                    "weapon_id": int(weapon_id),
                    "display_name": weapon_display_name(int(weapon_id)),
                    "attack_modes": sorted(modes),
                    "resource_policy": resource_policy,
                    "ammunition_key": ammunition_key,
                    "ammunition": (
                        ammunition.get(ammunition_key)
                        if ammunition_key is not None else None
                    ),
                    "ammunition_cost": ammunition_cost,
                })
            by_pool: dict[str, list[int]] = defaultdict(list)
            for resource in resources:
                key = resource["ammunition_key"]
                if isinstance(key, str):
                    by_pool[key].append(int(resource["weapon_id"]))
            for resource in resources:
                key = resource["ammunition_key"]
                resource["shared_with_weapon_ids"] = (
                    sorted(by_pool[key])
                    if isinstance(key, str) and len(by_pool[key]) > 1 else []
                )
            values.append({
                "operator_id": operator.operator_id,
                "weapons": resources,
                "ammunition_pools": dict(sorted(ammunition.items())),
                "weapon_locked": operator.weapon_locked,
                "command_lost": operator.command_lost,
                "locked_weapon_id": operator.locked_weapon_id,
                "weapon_unfolded": operator.weapon_unfolded,
                "weapon_cooldown_remaining_ms": (
                    operator.weapon_cooldown_remaining_ms
                ),
            })
        return tuple(json_clone(values))

    def get_weapon_catalog(self) -> dict[str, Any]:
        """Return known weapon metadata without reading any live operator."""
        source = self.scenario.metadata.get("source_compatible", {})
        profiles = source.get("weapons", {}) if isinstance(source, Mapping) else {}
        return build_weapon_catalog(
            profiles, self._adjudication_tables,
            guidance_authority=self._guidance_authority,
            indirect_authority=self._indirect_fire_authority,
            ammunition_authority=self._indirect_fire_ammunition_authority,
        )

    def query_fire_effect_from_projection(
        self,
        side: str,
        observation: Mapping[str, Any],
        source_frame: Mapping[str, Any],
        valid_actions: Sequence[Mapping[str, Any]],
        own_weapon_inventory: Sequence[Mapping[str, Any]],
        *,
        query: FireEffectQuery,
    ) -> dict[str, Any]:
        """Analyze one frozen seat view and explicit enemy-state assumptions.

        No WorldState, live RNG, or enemy scenario template is consulted.  The
        contact ID is only an authorization/position/model lookup in the side
        projection, never permission to reveal concealed target statistics.
        """
        self._require_side(side)
        if not isinstance(query, FireEffectQuery):
            raise TypeError("query must be FireEffectQuery")
        attacker, target, resource = projected_fire_inputs(
            side, observation, source_frame, own_weapon_inventory, query,
        )
        result = query_result_base(query, observation, valid_actions,
                                   self._adjudication_tables)
        unresolved = {
            "status": "UNRESOLVED", "damage_probability": None,
            "expected_damage": None, "damage_distribution": [],
            "outcome_distribution": [], "can_execute_under_assumptions": None,
        }
        if self._adjudication_tables is None:
            return {**result, **unresolved,
                    "reason_code": "ADJUDICATION_TABLES_UNAVAILABLE"}
        if target is None:
            return {**result, **unresolved, "reason_code": "TARGET_PROFILE_REQUIRED"}
        if self._source_map is None or self._source_visibility_map is None:
            return {**result, **unresolved, "reason_code": "SOURCE_MAP_UNAVAILABLE"}
        try:
            left = self._source_map.cell(attacker.current_hex)
            right = self._source_map.cell(target.current_hex)
        except (TypeError, ValueError) as exc:
            raise FireEffectQueryError("TARGET_HEX_NOT_FOUND", str(exc)) from exc
        if query.fire_mode == FireMode.INDIRECT.value:
            return self._query_indirect_fire_effect(query, attacker, target, result, unresolved)
        try:
            weapon = self._source_weapon_profile(query.weapon_id)
        except ValueError as exc:
            return {**result, **unresolved, "reason_code": str(exc)}
        resource_policy = DirectFireResourcePolicy.AMMUNITION_KEY
        loitering_policy = None
        if resource.get("resource_policy") == "SELF_AS_MUNITION":
            weapon = replace(weapon, ammunition_key=None)
            resource_policy = DirectFireResourcePolicy.SELF_AS_MUNITION
            loitering_policy = self._loitering_attack_policy_for(
                attacker, query.weapon_id, weapon,
            )
            if loitering_policy is None:
                return {**result, **unresolved,
                        "reason_code": "LOITERING_ATTACK_IDENTITY_MISMATCH"}
        try:
            elevation_difference = fire_elevation_level_difference(
                self._source_visibility_map, attacker.current_hex, target.current_hex,
                distance=hex_distance(attacker.current_hex, target.current_hex),
                weapon=weapon, fire_mode=FireMode(query.fire_mode), resource_policy=resource_policy,
            )
        except ValueError as exc:
            return {**result, **unresolved, "reason_code": "ELEVATION_LEVEL_UNRESOLVED",
                    "reason_detail": str(exc)}
        profile = query.target_profile
        observer = attacker
        if query.fire_mode == FireMode.GUIDED.value:
            if query.guider_id is None:
                return {**result, **unresolved, "reason_code": "GUIDER_REQUIRED"}
            observer = projected_guider(
                side, observation, source_frame, own_weapon_inventory, query.guider_id,
            )
            if FireMode.GUIDED not in weapon.supported_fire_modes:
                return {**result, **unresolved, "reason_code": "WEAPON_GUIDED_MODE_UNSUPPORTED"}
            if query.target_id is None or not result["can_execute"]:
                return {**result, **unresolved, "reason_code": "GUIDED_CAPABILITY_NOT_AVAILABLE"}
            result["guidance_evidence"] = "Frozen legal GUIDED action validates ownership, affiliation and readiness"
        visibility = evaluate_visibility(
            self._source_visibility_map,
            observer_hex=observer.current_hex, target_hex=target.current_hex,
            observer_domain=_unit_domain(observer.operator_type),
            target_domain=_unit_domain(target.operator_type),
            observer_is_drone=_uses_drone_observation_profile(
                observer, self._operator_lifecycle_authority,
            ),
            target_concealed=profile["concealed"],
            target_terrain=self._source_map.terrain(target.current_hex).value,
            line_of_sight_evidence=self._runtime_line_of_sight(observer, target),
        )
        request = DirectFireRequest(
            tick=int(observation["tick"]), episode_seed=0,
            action_id="read-only-fire-effect-query",
            attacker=attacker, target=target, weapon=weapon,
            visibility=visibility, distance=hex_distance(attacker.current_hex, target.current_hex),
            target_domain=_unit_domain(target.operator_type),
            target_armor=ArmorClass(profile["armor_class"]),
            ammunition_before=resource.get("ammunition"),
            weapon_cooldown_before=(attacker.weapon_cooldown_remaining_ms + 999) // 1000,
            weapon_unfolded=attacker.weapon_unfolded,
            weapon_locked=attacker.weapon_locked,
            locked_weapon_id=attacker.locked_weapon_id,
            elevation_difference=elevation_difference,
            attacker_suppressed=attacker.suppression_remaining_ms > 0,
            attacker_moving=(not attacker.stopped
                             or attacker.movement_mode == MovementMode.MARCHING.value),
            target_terrain=self._source_map.terrain(target.current_hex).value,
            target_concealed=profile["concealed"], target_moving=profile["moving"],
            target_stacked=profile["stacked"], target_marching=profile["marching"],
            attacker_transitioning=(attacker.transition_kind is not None
                                    or attacker.move_to_stop_remaining_ms > 0)
            and not (loitering_policy is not None
                     and loitering_policy.can_interrupt_movement_route),
            attacker_hovering=(attacker.stopped
                               or query.fire_mode == FireMode.GUIDED.value
                               or resource_policy is DirectFireResourcePolicy.SELF_AS_MUNITION),
            target_external_direct_fire_prohibited=profile["close_combat"],
            resource_policy=resource_policy,
            fire_mode=FireMode(query.fire_mode),
        )
        result["source"]["weapon_profile_hash"] = canonical_hash({
            "weapon_id": weapon.weapon_id, "weapon_class": weapon.weapon_class,
            "maximum_range": weapon.maximum_range,
            "target_domains": sorted(domain.value for domain in weapon.target_domains),
            "ammunition_key": weapon.ammunition_key,
            "cooldown_ticks": weapon.cooldown_ticks,
            "requires_unfolded": weapon.requires_unfolded,
            "can_fire_while_moving": weapon.can_fire_while_moving,
            "supported_fire_modes": sorted(mode.value for mode in weapon.supported_fire_modes),
        })
        result["geometry"] = {"distance": request.distance,
                              "target_hex": target.current_hex,
                              "visibility_under_assumptions": visibility.to_dict()}
        probabilities = direct_fire_probabilities(self._adjudication_tables, request)
        if query.fire_mode == FireMode.GUIDED.value:
            result["hit_probability"] = (
                sum(item["probability"] for item in probabilities["outcome_distribution"]
                    if not item["no_effect"])
                if probabilities["status"] == "RESOLVED" else None
            )
            result["hit_probability_reason"] = (
                "GUIDED_HIT_EVENT: any non-NO_EFFECT outcome, including suppression; not a separate accuracy roll"
            )
        return {**result, **probabilities}

    def _query_indirect_fire_effect(
        self,
        query: FireEffectQuery,
        attacker: OperatorState,
        target: OperatorState,
        result: dict[str, Any],
        unresolved: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Combine conditional impact probabilities with frozen launch readiness."""
        if query.indirect_profile is None:
            return {**result, **unresolved, "reason_code": "INDIRECT_PROFILE_REQUIRED"}
        indirect = self._indirect_fire_authority.profile_for_weapon(query.weapon_id)
        if indirect is None:
            return {**result, **unresolved, "reason_code": "WEAPON_INDIRECT_MODE_UNSUPPORTED"}
        probabilities = indirect_fire_probabilities(
            self._adjudication_tables, source_map=self._source_map,
            profile=indirect, damage_policy=self._indirect_fire_damage_policy,
            execution_policy=self._indirect_fire_execution_policy,
            attacker=attacker, target=target, target_profile=query.target_profile,
            observation_policy=query.indirect_profile["observation_policy"],
            planned_hex=query.indirect_profile.get("planned_hex"),
        )
        blockers = list(_indirect_fire_execution_blockers(
            self._indirect_fire_authority, indirect, attacker,
            ammunition_authority=self._indirect_fire_ammunition_authority,
            damage_policy=self._indirect_fire_damage_policy,
            descriptor=self._indirect_fire_authority_descriptors.get(indirect.profile_id),
            observation_policy=IndirectFireObservationPolicy(query.indirect_profile["observation_policy"]),
        ))
        planned_hex = query.indirect_profile.get("planned_hex", target.current_hex)
        if query.target_id is not None and planned_hex != target.current_hex:
            blockers.append("TARGET_HEX_MISMATCH")
        if probabilities.get("reason_code") == "TARGET_HEX_NOT_FOUND":
            blockers.append("TARGET_HEX_NOT_FOUND")
        ready = attacker.alive and attacker.deployed and not blockers
        probabilities["can_execute_under_assumptions"] = ready
        result["can_execute"] = ready if query.target_id is not None else None
        result["execution_evidence"] = "frozen owned resources and indirect authority"
        result["execution_blockers"] = blockers
        if not attacker.alive or not attacker.deployed:
            result["execution_blockers"].append("shooter_not_operational")
        return {**result, **probabilities}

    def direct_fire_intent_action(
        self,
        side: str,
        actor_id: int,
        weapon_id: int,
        target_id: int | None = None,
        *,
        target_hex: int | None = None,
    ) -> EngineAction:
        """Authorize either a visible unit or an explicit target-free map cell.

        Resource/state checks stay in step adjudication. A coordinate must never
        be resolved into an implicit enemy identity, even when occupied.
        """
        self._require_side(side)
        if (target_id is None) == (target_hex is None):
            raise ValueError("EXACTLY_ONE_FIRE_TARGET_REQUIRED")
        for name, value in (("actor_id", actor_id), ("weapon_id", weapon_id),
                            ("target_id", target_id), ("target_hex", target_hex)):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name.upper()}_INVALID")
        world = self._require_world()
        actor = world.operators.get(actor_id)
        if actor is None:
            raise ValueError("ACTOR_NOT_FOUND")
        if actor.side != side:
            raise ValueError("ACTOR_NOT_CONTROLLED")
        if weapon_id not in actor.weapon_ids:
            raise ValueError("WEAPON_NOT_CARRIED")
        parameters = {"weapon_id": weapon_id}
        if target_hex is not None:
            if self._source_map is None:
                raise ValueError("SOURCE_MAP_UNAVAILABLE")
            try:
                self._source_map.cell(target_hex)
            except ValueError as exc:
                raise ValueError("TARGET_HEX_NOT_FOUND") from exc
            parameters["target_hex"] = target_hex
        elif target_id not in {
            value.operator_id for value in self._visible_enemies(side)
        }:
            raise ValueError("TARGET_NOT_VISIBLE")
        return EngineAction(side, actor_id, ActionType.SHOOT,
                            target_id=target_id, parameters=parameters)

    def guidance_profile_for_operator(
        self,
        operator_id: int,
    ) -> GuidanceProfile | None:
        """Return only an explicitly assigned Native guidance profile.

        Source compatibility metadata and ``raw_attributes`` are deliberately
        not consulted here.  Exact Source type/subtype mappings are conversion
        facts, not permission to mutate a loaded world's authority.
        """

        if type(operator_id) is not int or operator_id < 0:
            raise ValueError("operator_id must be a non-negative exact int")
        operator = self._require_world().operators.get(operator_id)
        if operator is None:
            raise ValueError("OPERATOR_NOT_FOUND")
        if operator.guidance_profile_id is None:
            return None
        return self._guidance_authority.profile(operator.guidance_profile_id)

    def guidance_affiliation_for_operator(
        self,
        operator_id: int,
    ) -> tuple[int, GuidanceAffiliationEvidence] | None:
        """Return one explicit, evidenced affiliation without inferring links."""

        if type(operator_id) is not int or operator_id < 0:
            raise ValueError("operator_id must be a non-negative exact int")
        world = self._require_world()
        operator = world.operators.get(operator_id)
        if operator is None:
            raise ValueError("OPERATOR_NOT_FOUND")
        affiliation_id = operator.guidance_affiliation_id
        evidence = operator.guidance_affiliation_evidence
        if affiliation_id is None or evidence is None:
            return None
        affiliated = world.operators.get(affiliation_id)
        if affiliated is None or affiliated.side != operator.side:
            return None
        return affiliation_id, evidence

    def get_guided_fire_capabilities(
        self,
        side: str,
    ) -> tuple[GuidedFireCapability, ...]:
        """Return deterministic opportunities from one detached pre-state.

        This query uses each guider's current per-unit observation evidence. It
        deliberately does not require shooter LOS and never emits an action,
        consumes RNG, changes state, or invokes an adjudicator.
        """

        self._require_side(side)
        pre_state = WorldState.from_dict(self._require_world().to_dict())
        return self._guided_fire_capabilities_from_pre_state(pre_state, side)

    def get_board_capabilities(
        self,
        side: str,
    ) -> tuple[BoardCapability, ...]:
        """Return current BOARD opportunities from one detached pre-state."""

        self._require_side(side)
        pre_state = WorldState.from_dict(self._require_world().to_dict())
        return self._board_capabilities_from_pre_state(pre_state, side)

    def get_unload_capabilities(
        self,
        side: str,
    ) -> tuple[UnloadCapability, ...]:
        """Return current UNLOAD opportunities from one detached pre-state."""

        self._require_side(side)
        pre_state = WorldState.from_dict(self._require_world().to_dict())
        return self._unload_capabilities_from_pre_state(pre_state, side)

    def get_boarding_capability_assessments(
        self,
        side: str,
    ) -> tuple[BoardingCapabilityAssessment, ...]:
        """Explain BOARD/UNLOAD availability for every own operator."""

        self._require_side(side)
        pre_state = WorldState.from_dict(self._require_world().to_dict())
        board = self._board_capabilities_from_pre_state(pre_state, side)
        unload = self._unload_capabilities_from_pre_state(pre_state, side)
        board_actors = {value.passenger_id for value in board}
        unload_actors = {value.carrier_id for value in unload}
        result: list[BoardingCapabilityAssessment] = []
        for operator in sorted(
            (value for value in pre_state.operators.values() if value.side == side),
            key=lambda value: value.operator_id,
        ):
            result.append(self._board_assessment(
                operator, pre_state, operator.operator_id in board_actors,
            ))
            result.append(self._unload_assessment(
                operator, pre_state, operator.operator_id in unload_actors,
            ))
        return tuple(sorted(
            result,
            key=lambda value: (value.actor_id, value.operation_type.value),
        ))

    def _board_assessment(
        self,
        passenger: OperatorState,
        pre_state: WorldState,
        available: bool,
    ) -> BoardingCapabilityAssessment:
        operation = BoardingTransitionType.BOARD
        if available:
            return BoardingCapabilityAssessment(
                operation, passenger.operator_id,
                BoardingCapabilityStatus.AVAILABLE,
            )
        if passenger.mounted_on is not None or passenger.state.upper() == "ON_BOARD":
            return BoardingCapabilityAssessment(
                operation, passenger.operator_id,
                BoardingCapabilityStatus.NOT_APPLICABLE,
                ("ALREADY_ON_BOARD",),
            )
        if _unit_domain(passenger.operator_type) is not UnitDomain.PERSONNEL:
            return BoardingCapabilityAssessment(
                operation, passenger.operator_id,
                BoardingCapabilityStatus.NOT_APPLICABLE,
                ("UNIT_TYPE_NOT_PASSENGER",),
            )
        blockers = self._boarding_operator_blockers(passenger, pre_state)
        candidates: list[tuple[OperatorState, BoardingProfile, bool]] = []
        for carrier in pre_state.operators.values():
            if carrier.side != passenger.side or carrier.operator_id == passenger.operator_id:
                continue
            profile, derived = self._boarding_profile_for_carrier(carrier)
            if profile is None:
                continue
            if derived and self._boarding_type_assignment_authority.profile_id_for(
                passenger.operator_type, passenger.subtype, "PASSENGER"
            ) != profile.profile_id:
                continue
            if _unit_domain(passenger.operator_type).value not in profile.passenger_domains:
                continue
            candidates.append((carrier, profile, derived))
        if not candidates:
            blockers.append("NO_COMPATIBLE_CARRIER")
        else:
            same_hex = [
                value for value in candidates
                if value[0].current_hex == passenger.current_hex
                and passenger.current_hex is not None
            ]
            if not same_hex:
                blockers.append("NO_COMPATIBLE_CARRIER_SAME_HEX")
            else:
                carrier_reasons = [
                    self._boarding_carrier_blocker(
                        carrier, profile, pre_state, require_empty=True,
                    )
                    for carrier, profile, _derived in same_hex
                ]
                blockers.extend(
                    reason for reason in carrier_reasons if reason is not None
                )
        return BoardingCapabilityAssessment(
            operation,
            passenger.operator_id,
            BoardingCapabilityStatus.BLOCKED,
            tuple(sorted(set(blockers or ["BOARDING_UNAVAILABLE"]))),
        )

    def _unload_assessment(
        self,
        carrier: OperatorState,
        pre_state: WorldState,
        available: bool,
    ) -> BoardingCapabilityAssessment:
        operation = BoardingTransitionType.UNLOAD
        if available:
            return BoardingCapabilityAssessment(
                operation, carrier.operator_id,
                BoardingCapabilityStatus.AVAILABLE,
            )
        profile, _derived = self._boarding_profile_for_carrier(carrier)
        if profile is None:
            return BoardingCapabilityAssessment(
                operation, carrier.operator_id,
                BoardingCapabilityStatus.NOT_APPLICABLE,
                ("UNIT_TYPE_NOT_CARRIER",),
            )
        if carrier.passenger_id is None:
            return BoardingCapabilityAssessment(
                operation, carrier.operator_id,
                BoardingCapabilityStatus.NOT_APPLICABLE,
                ("NO_PASSENGER_ON_BOARD",),
            )
        reason = self._boarding_carrier_blocker(
            carrier, profile, pre_state, require_empty=False,
        )
        return BoardingCapabilityAssessment(
            operation,
            carrier.operator_id,
            BoardingCapabilityStatus.BLOCKED,
            (reason or "UNLOAD_UNAVAILABLE",),
        )

    @staticmethod
    def _boarding_operator_blockers(
        operator: OperatorState,
        pre_state: WorldState,
    ) -> list[str]:
        values: list[str] = []
        if not operator.alive or operator.state.upper() == "DESTROYED":
            values.append("DESTROYED")
        if not operator.deployed:
            values.append("NOT_DEPLOYED")
        if operator.current_hex is None:
            values.append("CURRENT_HEX_REQUIRED")
        if not _boarding_operator_stopped(operator):
            values.append("MOVING")
        if _boarding_operator_suppressed(operator):
            values.append("SUPPRESSED")
        if operator.movement_locked or _operator_has_boarding_transition(
            pre_state, operator.operator_id
        ):
            values.append("TRANSITION_IN_PROGRESS")
        return values

    def _boarding_carrier_blocker(
        self,
        carrier: OperatorState,
        profile: BoardingProfile,
        pre_state: WorldState,
        *,
        require_empty: bool,
    ) -> str | None:
        if require_empty and carrier.passenger_id is not None:
            return "CARRIER_FULL"
        blockers = self._boarding_operator_blockers(carrier, pre_state)
        if blockers:
            return blockers[0]
        if _unit_domain(carrier.operator_type).value not in profile.carrier_domains:
            return "UNIT_TYPE_NOT_CARRIER"
        return None

    def get_concealment_capabilities(
        self,
        side: str,
    ) -> tuple[ConcealmentCapability, ...]:
        """Return deterministic ENTER_CONCEALMENT availability.

        The query is detached and read-only.  Consumers receive both available
        and blocked records so Human/Agent adapters can show the authoritative
        reason without reimplementing posture rules.
        """

        self._require_side(side)
        pre_state = WorldState.from_dict(self._require_world().to_dict())
        return self._concealment_capabilities_from_pre_state(pre_state, side)

    def _concealment_capabilities_from_pre_state(
        self,
        pre_state: WorldState,
        side: str,
    ) -> tuple[ConcealmentCapability, ...]:
        values = [
            ConcealmentCapability(
                operator_id=operator.operator_id,
                available=(reason := self._concealment_blocker(
                    operator,
                    pre_state,
                )) is None,
                reason="AVAILABLE" if reason is None else reason,
            )
            for operator in sorted(
                (
                    item for item in pre_state.operators.values()
                    if item.side == side
                ),
                key=lambda item: item.operator_id,
            )
        ]
        return tuple(sorted(values, key=concealment_capability_sort_key))

    @staticmethod
    def _concealment_blocker(
        operator: OperatorState,
        pre_state: WorldState,
    ) -> str | None:
        if not operator.alive or operator.state.upper() == "DESTROYED":
            return "DESTROYED"
        if operator.mounted_on is not None or operator.state.upper() == "ON_BOARD":
            return "ON_BOARD"
        if not operator.deployed:
            return "NOT_DEPLOYED"
        domain = _unit_domain(operator.operator_type)
        if domain is UnitDomain.AIR:
            return "AIR_UNIT_NOT_SUPPORTED"
        if domain not in {UnitDomain.PERSONNEL, UnitDomain.VEHICLE}:
            return "UNIT_DOMAIN_NOT_SUPPORTED"
        if operator.concealed:
            return "ALREADY_CONCEALED"
        if operator.movement_mode == MovementMode.MARCHING.value:
            return "MARCHING"
        if operator.movement_mode in {
            MovementMode.CHARGE_1.value,
            MovementMode.CHARGE_2.value,
        }:
            return "CHARGE"
        if (
            operator.segment_to_hex is not None
            or operator.speed != 0
            or not operator.stopped
            or operator.move_to_stop_remaining_ms != 0
        ):
            return "MOVING"
        if (
            operator.suppression_remaining_ms != 0
            or operator.state.upper() == "SUPPRESSED"
        ):
            return "SUPPRESSED"
        if (
            operator.transition_kind is not None
            or operator.transition_remaining_ms != 0
            or _operator_has_boarding_transition(
                pre_state,
                operator.operator_id,
            )
        ):
            return "TRANSITION_IN_PROGRESS"
        if operator.movement_locked:
            return "TRANSITION_IN_PROGRESS"
        if operator.current_hex is None:
            return "CURRENT_HEX_REQUIRED"
        return None

    def board_action(self, capability: BoardCapability) -> EngineAction:
        """Encode one current Engine capability as a replayable BOARD action."""

        validation = self.build_board_request(capability)
        if not validation.valid or validation.request is None:
            raise ValueError(validation.reason)
        request = validation.request
        return _encode_board_action(request.side, request)

    def unload_action(self, capability: UnloadCapability) -> EngineAction:
        """Encode one current Engine capability as a replayable UNLOAD action."""

        validation = self.build_unload_request(capability)
        if not validation.valid or validation.request is None:
            raise ValueError(validation.reason)
        request = validation.request
        return _encode_unload_action(request.side, request)

    def build_board_request(
        self,
        capability: BoardCapability,
    ) -> BoardValidationResult:
        """Bind a current BOARD capability to an immutable request."""

        if not isinstance(capability, BoardCapability):
            raise TypeError("capability must be BoardCapability")
        pre_state = WorldState.from_dict(self._require_world().to_dict())
        passenger = pre_state.operators.get(capability.passenger_id)
        if passenger is None:
            return BoardValidationResult(False, "PASSENGER_NOT_FOUND")
        action = _encode_board_action(passenger.side, capability)
        return self._build_board_request_from_pre_state(
            capability,
            pre_state,
            action_id=action.action_id,
        )

    def build_unload_request(
        self,
        capability: UnloadCapability,
    ) -> UnloadValidationResult:
        """Bind a current UNLOAD capability to an immutable request."""

        if not isinstance(capability, UnloadCapability):
            raise TypeError("capability must be UnloadCapability")
        pre_state = WorldState.from_dict(self._require_world().to_dict())
        carrier = pre_state.operators.get(capability.carrier_id)
        if carrier is None:
            return UnloadValidationResult(False, "CARRIER_NOT_FOUND")
        action = _encode_unload_action(carrier.side, capability)
        return self._build_unload_request_from_pre_state(
            capability,
            pre_state,
            action_id=action.action_id,
        )

    def validate_board_request(
        self,
        request: BoardRequest,
    ) -> BoardValidationResult:
        """Reject stale or forged BOARD requests against current authority."""

        if not isinstance(request, BoardRequest):
            raise TypeError("request must be BoardRequest")
        pre_state = WorldState.from_dict(self._require_world().to_dict())
        return self._validate_board_request_against_pre_state(request, pre_state)

    def validate_unload_request(
        self,
        request: UnloadRequest,
    ) -> UnloadValidationResult:
        """Reject stale or forged UNLOAD requests against current authority."""

        if not isinstance(request, UnloadRequest):
            raise TypeError("request must be UnloadRequest")
        pre_state = WorldState.from_dict(self._require_world().to_dict())
        return self._validate_unload_request_against_pre_state(request, pre_state)

    def _board_capabilities_from_pre_state(
        self,
        pre_state: WorldState,
        side: str,
    ) -> tuple[BoardCapability, ...]:
        capabilities: list[BoardCapability] = []
        carriers = tuple(sorted(
            (
                (operator, profile, derived)
                for operator in pre_state.operators.values()
                for profile, derived in (
                    self._boarding_profile_for_carrier(operator),
                )
                if operator.side == side
                and operator.alive
                and operator.deployed
                and operator.current_hex is not None
                and profile is not None
            ),
            key=lambda item: item[0].operator_id,
        ))
        passengers = tuple(sorted(
            (
                operator for operator in pre_state.operators.values()
                if operator.side == side
                and operator.alive
                and operator.deployed
                and operator.current_hex is not None
                and operator.mounted_on is None
                and operator.passenger_id is None
            ),
            key=lambda item: item.operator_id,
        ))
        for carrier, profile, derived in carriers:
            if not self._boarding_carrier_ready(
                carrier,
                profile,
                pre_state,
                require_empty=True,
                require_unsuppressed=True,
            ):
                continue
            for passenger in passengers:
                if not self._boarding_passenger_ready(
                    passenger,
                    profile,
                    pre_state,
                    require_type_assignment=derived,
                ):
                    continue
                if passenger.current_hex != carrier.current_hex:
                    continue
                capabilities.append(BoardCapability(
                    passenger_id=passenger.operator_id,
                    carrier_id=carrier.operator_id,
                    profile_id=profile.profile_id,
                    authority_version=self._boarding_authority.authority_version,
                    authority_hash=self._boarding_authority.content_hash,
                ))
        return tuple(sorted(capabilities, key=board_capability_sort_key))

    def _unload_capabilities_from_pre_state(
        self,
        pre_state: WorldState,
        side: str,
    ) -> tuple[UnloadCapability, ...]:
        capabilities: list[UnloadCapability] = []
        for carrier in sorted(
            pre_state.operators.values(), key=lambda item: item.operator_id
        ):
            if (
                carrier.side != side
                or not carrier.alive
                or not carrier.deployed
                or carrier.current_hex is None
                or carrier.passenger_id is None
            ):
                continue
            profile, _derived = self._boarding_profile_for_carrier(carrier)
            passenger = pre_state.operators.get(carrier.passenger_id)
            if (
                profile is None
                or passenger is None
                or passenger.side != side
                or not passenger.alive
                or passenger.mounted_on != carrier.operator_id
                or passenger.deployed
                or not self._boarding_carrier_ready(
                    carrier,
                    profile,
                    pre_state,
                    require_empty=False,
                    require_unsuppressed=True,
                )
            ):
                continue
            capabilities.append(UnloadCapability(
                carrier_id=carrier.operator_id,
                passenger_id=passenger.operator_id,
                profile_id=profile.profile_id,
                authority_version=self._boarding_authority.authority_version,
                authority_hash=self._boarding_authority.content_hash,
            ))
        return tuple(sorted(capabilities, key=unload_capability_sort_key))

    def _boarding_carrier_ready(
        self,
        carrier: OperatorState,
        profile: BoardingProfile,
        pre_state: WorldState,
        *,
        require_empty: bool,
        require_unsuppressed: bool,
    ) -> bool:
        return (
            _unit_domain(carrier.operator_type).value in profile.carrier_domains
            and (not require_empty or carrier.passenger_id is None)
            and not carrier.movement_locked
            and _boarding_operator_stopped(carrier)
            and (
                not require_unsuppressed
                or not _boarding_operator_suppressed(carrier)
            )
            and not _operator_has_boarding_transition(
                pre_state, carrier.operator_id
            )
        )

    def _boarding_passenger_ready(
        self,
        passenger: OperatorState,
        profile: BoardingProfile,
        pre_state: WorldState,
        *,
        require_type_assignment: bool = False,
    ) -> bool:
        return (
            _unit_domain(passenger.operator_type).value
            in profile.passenger_domains
            and (
                not require_type_assignment
                or self._boarding_type_assignment_authority.profile_id_for(
                    passenger.operator_type,
                    passenger.subtype,
                    "PASSENGER",
                ) == profile.profile_id
            )
            and _boarding_operator_stopped(passenger)
            and not _boarding_operator_suppressed(passenger)
            and not _operator_has_boarding_transition(
                pre_state, passenger.operator_id
            )
        )

    def _boarding_profile_for_carrier(
        self,
        carrier: OperatorState,
    ) -> tuple[BoardingProfile | None, bool]:
        """Resolve explicit authority first, then one exact typed assignment.

        ``None`` means the scenario omitted an explicit choice.  Any explicit
        string, including the reserved DISABLED value or an unknown profile,
        prevents fallback so explicit disable/fail-closed semantics win.
        """

        explicit = carrier.boarding_profile_id
        if explicit is not None:
            if explicit == BOARDING_EXPLICIT_DISABLED:
                return None, False
            return self._boarding_authority.profile(explicit), False
        if not self._boarding_type_assignments_enabled:
            return None, False
        profile_id = self._boarding_type_assignment_authority.profile_id_for(
            carrier.operator_type,
            carrier.subtype,
            "CARRIER",
        )
        if profile_id is None:
            return None, False
        return self._boarding_authority.profile(profile_id), True

    def _build_board_request_from_pre_state(
        self,
        capability: BoardCapability,
        pre_state: WorldState,
        *,
        action_id: str,
    ) -> BoardValidationResult:
        passenger = pre_state.operators.get(capability.passenger_id)
        if passenger is None:
            return BoardValidationResult(False, "PASSENGER_NOT_FOUND")
        current = self._board_capabilities_from_pre_state(
            pre_state,
            passenger.side,
        )
        if capability not in current:
            return BoardValidationResult(False, "CAPABILITY_NOT_CURRENT")
        request = BoardRequest(
            tick=pre_state.tick,
            action_id=action_id,
            side=passenger.side,
            actor_id=passenger.operator_id,
            carrier_id=capability.carrier_id,
            passenger_id=capability.passenger_id,
            profile_id=capability.profile_id,
            authority_version=capability.authority_version,
            authority_hash=capability.authority_hash,
        )
        return BoardValidationResult(True, "VALID", request)

    def _build_unload_request_from_pre_state(
        self,
        capability: UnloadCapability,
        pre_state: WorldState,
        *,
        action_id: str,
    ) -> UnloadValidationResult:
        carrier = pre_state.operators.get(capability.carrier_id)
        if carrier is None:
            return UnloadValidationResult(False, "CARRIER_NOT_FOUND")
        current = self._unload_capabilities_from_pre_state(
            pre_state,
            carrier.side,
        )
        if capability not in current:
            return UnloadValidationResult(False, "CAPABILITY_NOT_CURRENT")
        request = UnloadRequest(
            tick=pre_state.tick,
            action_id=action_id,
            side=carrier.side,
            actor_id=carrier.operator_id,
            carrier_id=capability.carrier_id,
            passenger_id=capability.passenger_id,
            profile_id=capability.profile_id,
            authority_version=capability.authority_version,
            authority_hash=capability.authority_hash,
        )
        return UnloadValidationResult(True, "VALID", request)

    def _validate_board_request_against_pre_state(
        self,
        request: BoardRequest,
        pre_state: WorldState,
    ) -> BoardValidationResult:
        expected_action = _encode_board_action(request.side, request)
        if (
            request.tick != pre_state.tick
            or request.actor_id != request.passenger_id
            or request.action_id != expected_action.action_id
        ):
            return BoardValidationResult(False, "REQUEST_NOT_CURRENT")
        try:
            capability = BoardCapability(
                request.passenger_id,
                request.carrier_id,
                request.profile_id,
                request.authority_version,
                request.authority_hash,
            )
        except (TypeError, ValueError):
            return BoardValidationResult(False, "INVALID_REQUEST")
        current = self._build_board_request_from_pre_state(
            capability,
            pre_state,
            action_id=request.action_id,
        )
        if not current.valid or current.request != request:
            return BoardValidationResult(False, current.reason)
        return current

    def _validate_unload_request_against_pre_state(
        self,
        request: UnloadRequest,
        pre_state: WorldState,
    ) -> UnloadValidationResult:
        expected_action = _encode_unload_action(request.side, request)
        if (
            request.tick != pre_state.tick
            or request.actor_id != request.carrier_id
            or request.action_id != expected_action.action_id
        ):
            return UnloadValidationResult(False, "REQUEST_NOT_CURRENT")
        try:
            capability = UnloadCapability(
                request.carrier_id,
                request.passenger_id,
                request.profile_id,
                request.authority_version,
                request.authority_hash,
            )
        except (TypeError, ValueError):
            return UnloadValidationResult(False, "INVALID_REQUEST")
        current = self._build_unload_request_from_pre_state(
            capability,
            pre_state,
            action_id=request.action_id,
        )
        if not current.valid or current.request != request:
            return UnloadValidationResult(False, current.reason)
        return current

    def get_indirect_fire_capabilities(
        self,
        side: str,
    ) -> tuple[IndirectFireCapability, ...]:
        """Return side-owned indirect-fire authority without enabling fire.

        The result is computed from one detached pre-state.  It is not added
        to ``valid_actions`` and cannot create a mission, consume RNG, emit an
        event, or mutate authoritative state.  Incomplete authority is
        represented as an explicit BLOCKED capability rather than guessed.
        """

        self._require_side(side)
        pre_state = WorldState.from_dict(self._require_world().to_dict())
        return self._indirect_fire_capabilities_from_pre_state(pre_state, side)

    def indirect_fire_authority_descriptor(
        self,
        profile_id: str,
    ) -> IndirectFireAuthorityDescriptor | None:
        """Return one immutable executable-authority identity, if complete."""

        if not isinstance(profile_id, str) or not profile_id.strip():
            raise ValueError("profile_id must be a non-empty string")
        return self._indirect_fire_authority_descriptors.get(profile_id)

    def indirect_fire_replay_identity(self) -> Mapping[str, str]:
        """Return the exact authority identity required by INDIRECT replay."""

        return load_indirect_fire_replay_identity()

    def _indirect_fire_capabilities_from_pre_state(
        self,
        pre_state: WorldState,
        side: str,
    ) -> tuple[IndirectFireCapability, ...]:
        """Generate capability records from exactly the supplied pre-state."""

        if not isinstance(pre_state, WorldState):
            raise TypeError("pre_state must be WorldState")
        self._require_side(side)
        capabilities: list[IndirectFireCapability] = []
        for shooter in sorted(
            pre_state.operators.values(), key=lambda item: item.operator_id
        ):
            if shooter.side != side or not shooter.alive or not shooter.deployed:
                continue
            for weapon_id in sorted(set(shooter.weapon_ids)):
                profile = self._indirect_fire_authority.profile_for_weapon(weapon_id)
                if profile is None:
                    continue
                blockers = _indirect_fire_execution_blockers(
                    self._indirect_fire_authority,
                    profile,
                    shooter,
                    ammunition_authority=(
                        self._indirect_fire_ammunition_authority
                    ),
                    damage_policy=self._indirect_fire_damage_policy,
                    descriptor=self._indirect_fire_authority_descriptors.get(
                        profile.profile_id
                    ),
                    observation_policy=self._indirect_fire_observation_policy,
                )
                capabilities.append(IndirectFireCapability(
                    shooter_id=shooter.operator_id,
                    weapon_id=weapon_id,
                    artillery_class=profile.artillery_class,
                    profile_id=profile.profile_id,
                    range_policy=profile.range_policy,
                    minimum_range=profile.minimum_range,
                    maximum_range=profile.maximum_range,
                    target_policy=profile.target_domain,
                    status=(
                        IndirectFireCapabilityStatus.BLOCKED
                        if blockers
                        else IndirectFireCapabilityStatus.AVAILABLE
                    ),
                    blockers=blockers,
                ))
        return tuple(sorted(
            capabilities,
            key=indirect_fire_capability_sort_key,
        ))

    def validate_indirect_fire_target_hex(
        self,
        capability: IndirectFireCapability,
        target_hex: int,
    ) -> IndirectFireTargetHexValidation:
        """Validate a hex selection without creating an executable intent.

        Callers may hold stale or self-constructed capability records, so this
        method first recomputes the current side-bound capability.  Authority
        gaps fail closed before any range interpretation.
        """

        if not isinstance(capability, IndirectFireCapability):
            raise TypeError("capability must be IndirectFireCapability")
        if type(target_hex) is not int or target_hex < 0:
            raise ValueError("target_hex must be a non-negative exact int")
        pre_state = WorldState.from_dict(self._require_world().to_dict())
        return self._validate_indirect_fire_target_hex_from_pre_state(
            capability,
            target_hex,
            pre_state,
        )

    def _validate_indirect_fire_target_hex_from_pre_state(
        self,
        capability: IndirectFireCapability,
        target_hex: int,
        pre_state: WorldState,
    ) -> IndirectFireTargetHexValidation:
        """Validate a target against the same detached capability pre-state."""

        shooter = pre_state.operators.get(capability.shooter_id)
        if shooter is None:
            return IndirectFireTargetHexValidation(
                IndirectFireTargetHexStatus.INVALID,
                "CAPABILITY_SHOOTER_NOT_FOUND",
                capability.shooter_id,
                capability.weapon_id,
                target_hex,
            )
        current = self._indirect_fire_capabilities_from_pre_state(
            pre_state,
            shooter.side,
        )
        if capability not in current:
            return IndirectFireTargetHexValidation(
                IndirectFireTargetHexStatus.INVALID,
                "CAPABILITY_NOT_CURRENT",
                capability.shooter_id,
                capability.weapon_id,
                target_hex,
            )
        if self._source_map is None or shooter.current_hex is None:
            return IndirectFireTargetHexValidation(
                IndirectFireTargetHexStatus.INVALID,
                "TARGET_HEX_VALIDATION_UNAVAILABLE",
                capability.shooter_id,
                capability.weapon_id,
                target_hex,
            )
        try:
            self._source_map.cell(target_hex)
        except ValueError:
            return IndirectFireTargetHexValidation(
                IndirectFireTargetHexStatus.INVALID,
                "TARGET_HEX_NOT_FOUND",
                capability.shooter_id,
                capability.weapon_id,
                target_hex,
            )
        if capability.range_policy is (
            IndirectRangePolicy.SCENARIO_DEFINED_GLOBAL_HEX
        ):
            return IndirectFireTargetHexValidation(
                IndirectFireTargetHexStatus.VALID,
                "TARGET_HEX_VALID_SCENARIO_GLOBAL",
                capability.shooter_id,
                capability.weapon_id,
                target_hex,
            )
        if capability.minimum_range is None or capability.maximum_range is None:
            return IndirectFireTargetHexValidation(
                IndirectFireTargetHexStatus.BLOCKED,
                "BLOCKED_RANGE_UNKNOWN",
                capability.shooter_id,
                capability.weapon_id,
                target_hex,
            )
        if capability.target_policy is IndirectTargetDomain.UNKNOWN:
            return IndirectFireTargetHexValidation(
                IndirectFireTargetHexStatus.BLOCKED,
                "BLOCKED_TARGET_DOMAIN_UNKNOWN",
                capability.shooter_id,
                capability.weapon_id,
                target_hex,
            )
        if capability.status is not IndirectFireCapabilityStatus.AVAILABLE:
            return IndirectFireTargetHexValidation(
                IndirectFireTargetHexStatus.BLOCKED,
                "BLOCKED_CAPABILITY_UNAVAILABLE",
                capability.shooter_id,
                capability.weapon_id,
                target_hex,
            )
        target_distance = hex_distance(shooter.current_hex, target_hex)
        if not capability.minimum_range <= target_distance <= capability.maximum_range:
            return IndirectFireTargetHexValidation(
                IndirectFireTargetHexStatus.VALID,
                "TARGET_HEX_OUT_OF_RANGE_ZERO_DAMAGE",
                capability.shooter_id,
                capability.weapon_id,
                target_hex,
                target_distance,
            )
        return IndirectFireTargetHexValidation(
            IndirectFireTargetHexStatus.VALID,
            "TARGET_HEX_VALID",
            capability.shooter_id,
            capability.weapon_id,
            target_hex,
            target_distance,
        )

    def build_indirect_fire_plan_request(
        self,
        capability: IndirectFireCapability,
        planned_hex: int,
    ) -> IndirectFirePlanValidationResult:
        """Build immutable planning evidence from one current pre-state.

        This method cannot create a mission or action.  It performs no RNG
        draw and writes no world, event, recorder, or replay state.
        """

        if not isinstance(capability, IndirectFireCapability):
            raise TypeError("capability must be IndirectFireCapability")
        if type(planned_hex) is not int or planned_hex < 0:
            raise ValueError("planned_hex must be a non-negative exact int")
        pre_state = WorldState.from_dict(self._require_world().to_dict())
        return self._build_indirect_fire_plan_request_from_pre_state(
            capability,
            planned_hex,
            pre_state,
        )

    def indirect_fire_action(
        self,
        capability: IndirectFireCapability,
        planned_hex: int,
        *,
        target_id: int | None = None,
    ) -> EngineAction:
        """Return one explicit, authority-bound FIRE/INDIRECT action.

        The target hex is validated by the Engine; no hex action list is
        generated and weapon identity never implies a fire mode.
        """

        pre_state = WorldState.from_dict(self._require_world().to_dict())
        validation = self._build_indirect_fire_plan_request_from_pre_state(
            capability,
            planned_hex,
            pre_state,
        )
        if not validation.valid or validation.request is None:
            raise ValueError(validation.reason)
        descriptor = self._indirect_fire_authority_descriptors.get(
            capability.profile_id
        )
        if descriptor is None:
            raise ValueError("CAPABILITY_BLOCKED")
        shooter = pre_state.operators[capability.shooter_id]
        self._validate_indirect_damage_target(pre_state, shooter, planned_hex, target_id)
        # Action intent keeps the pre-0.4.2 authority payload unchanged.  The
        # new boundary semantics is archive/engine identity, not user intent.
        identity = load_legacy_indirect_fire_replay_identity()
        return EngineAction(
            side=shooter.side,
            actor_id=capability.shooter_id,
            action_type=ActionType.FIRE,
            target_id=target_id,
            parameters={
                "fire_mode": FireMode.INDIRECT.value,
                "weapon_id": capability.weapon_id,
                "target_hex": planned_hex,
                "execution_policy_id": (
                    validation.request.execution_policy_id
                ),
                **identity,
            },
        )

    def _validate_indirect_damage_target(
        self, pre_state: WorldState, shooter: OperatorState,
        planned_hex: int, target_id: int | None,
    ) -> None:
        if target_id is None:
            return
        if type(target_id) is not int or target_id < 0:
            raise ValueError("TARGET_ID_INVALID")
        target = pre_state.operators.get(target_id)
        if target is None or not target.alive or not target.deployed:
            raise ValueError("TARGET_NOT_OPERATIONAL")
        if target.side == shooter.side:
            raise ValueError("TARGET_NOT_ENEMY")
        if target_id not in {value.operator_id for value in self._visible_enemies(shooter.side)}:
            raise ValueError("TARGET_NOT_VISIBLE")
        if target.current_hex != planned_hex:
            raise ValueError("TARGET_HEX_MISMATCH")

    def _build_indirect_fire_plan_request_from_pre_state(
        self,
        capability: IndirectFireCapability,
        planned_hex: int,
        pre_state: WorldState,
        *,
        action_id: str | None = None,
    ) -> IndirectFirePlanValidationResult:
        """Build planning evidence from exactly the supplied pre-state."""

        if not isinstance(capability, IndirectFireCapability):
            raise TypeError("capability must be IndirectFireCapability")
        if type(planned_hex) is not int or planned_hex < 0:
            raise ValueError("planned_hex must be a non-negative exact int")
        if not isinstance(pre_state, WorldState):
            raise TypeError("pre_state must be WorldState")
        shooter = pre_state.operators.get(capability.shooter_id)
        if shooter is None or shooter.side not in self.scenario.side_order:
            return _indirect_fire_plan_failure(
                "CAPABILITY_BLOCKED",
                "shooter_not_found",
            )
        if not shooter.alive or not shooter.deployed:
            return _indirect_fire_plan_failure(
                "CAPABILITY_BLOCKED",
                "shooter_not_operational",
            )

        profile = self._indirect_fire_authority.profile(
            capability.profile_id
        )
        if (
            profile is None
            or profile.weapon_id != capability.weapon_id
            or capability.weapon_id not in shooter.weapon_ids
            or profile.supported_fire_mode != "INDIRECT"
        ):
            return _indirect_fire_plan_failure(
                "WEAPON_NOT_AUTHORIZED",
                "weapon_not_authorized",
            )

        current = self._indirect_fire_capabilities_from_pre_state(
            pre_state,
            shooter.side,
        )
        matching = next(
            (
                item for item in current
                if item.shooter_id == capability.shooter_id
                and item.weapon_id == capability.weapon_id
                and item.profile_id == capability.profile_id
            ),
            None,
        )
        if matching is None:
            return _indirect_fire_plan_failure(
                "WEAPON_NOT_AUTHORIZED",
                "weapon_not_authorized",
            )
        if "indirect_fire_ammunition_unavailable" in matching.blockers:
            return _indirect_fire_plan_failure(
                "INDIRECT_FIRE_AMMUNITION_UNAVAILABLE",
                "indirect_fire_ammunition_unavailable",
            )
        if matching.status is not IndirectFireCapabilityStatus.AVAILABLE:
            return IndirectFirePlanValidationResult(
                valid=False,
                reason="CAPABILITY_BLOCKED",
                blockers=matching.blockers,
            )
        if capability != matching:
            return _indirect_fire_plan_failure(
                "CAPABILITY_BLOCKED",
                "capability_not_current",
            )

        target_validation = self._validate_indirect_fire_target_hex_from_pre_state(
            matching,
            planned_hex,
            pre_state,
        )
        if target_validation.status is not IndirectFireTargetHexStatus.VALID:
            return _indirect_fire_plan_failure(
                target_validation.reason,
                target_validation.reason.lower(),
            )

        policy = self._indirect_fire_execution_policy
        if (
            not isinstance(policy, IndirectFireExecutionPolicy)
            or policy.policy_id != INDIRECT_FIRE_EXECUTION_POLICY_ID
            or policy.policy_version != INDIRECT_FIRE_EXECUTION_POLICY_VERSION
        ):
            return _indirect_fire_plan_failure(
                "POLICY_UNAVAILABLE",
                "execution_policy_unavailable",
            )
        evidence = IndirectFirePlanValidationEvidence(
            checked_profile_id=profile.profile_id,
            checked_policy_hash=policy.content_hash,
            planned_hex_validated=True,
            range_policy=profile.range_policy,
        )
        ammo_before = tuple(sorted(self._ammunition(shooter).items()))
        action_record = {
            "contract": "indirect-fire-plan-request/1.0",
            "tick": pre_state.tick,
            "shooter_id": shooter.operator_id,
            "weapon_id": profile.weapon_id,
            "planned_hex": planned_hex,
            "artillery_profile_id": profile.profile_id,
            "execution_policy_id": policy.policy_id,
            "execution_policy_hash": policy.content_hash,
            "authority_version": INDIRECT_FIRE_AUTHORITY_SCHEMA_VERSION,
        }
        request = _build_validated_indirect_fire_plan_request(
            tick=pre_state.tick,
            action_id=(
                "indirect-plan-" + canonical_hash(action_record)[:24]
                if action_id is None
                else action_id
            ),
            shooter_id=shooter.operator_id,
            weapon_id=profile.weapon_id,
            planned_hex=planned_hex,
            artillery_profile_id=profile.profile_id,
            execution_policy_id=policy.policy_id,
            execution_policy_hash=policy.content_hash,
            ammo_before=ammo_before,
            authority_version=INDIRECT_FIRE_AUTHORITY_SCHEMA_VERSION,
            validation_evidence=evidence,
        )
        return IndirectFirePlanValidationResult(
            valid=True,
            reason="PLAN_REQUEST_VALID",
            blockers=(),
            request=request,
        )

    def create_indirect_fire_mission(
        self,
        plan_request: IndirectFirePlanRequest,
    ) -> FireMission:
        """Atomically create one PLANNED mission from current plan evidence.

        This trusted internal API is not an Action/Human/Agent entry point.
        Ammunition identity and cost come only from the loaded, hashed Native
        authority.  No RNG is consumed and no impact result is generated.
        """

        if not isinstance(plan_request, IndirectFirePlanRequest):
            raise TypeError("plan_request must be IndirectFirePlanRequest")
        authoritative = self._require_world()
        if authoritative.done:
            raise RuntimeError("episode_already_finished")
        pre_state = WorldState.from_dict(authoritative.to_dict())
        mission = self._build_indirect_fire_mission_from_pre_state(
            plan_request,
            pre_state,
        )
        working = WorldState.from_dict(pre_state.to_dict())
        event = self._apply_indirect_fire_mission_to_working_state(
            working,
            plan_request,
            mission,
            action_sequence=-1,
        )
        working.last_judge_info = [event.to_dict()]
        self._commit_world(authoritative, working)
        self._last_events = (event,)
        self._source_valid_actions_cache.clear()
        self._source_valid_actions_state_hash = None
        return mission

    def _build_indirect_fire_mission_from_pre_state(
        self,
        plan_request: IndirectFirePlanRequest,
        pre_state: WorldState,
        *,
        action_stream_bound: bool = False,
    ) -> FireMission:
        shooter = pre_state.operators.get(plan_request.shooter_id)
        if shooter is None:
            raise ValueError("INDIRECT_FIRE_SHOOTER_NOT_FOUND")
        capability = next((
            item
            for item in self._indirect_fire_capabilities_from_pre_state(
                pre_state,
                shooter.side,
            )
            if item.shooter_id == plan_request.shooter_id
            and item.weapon_id == plan_request.weapon_id
            and item.profile_id == plan_request.artillery_profile_id
        ), None)
        if capability is None:
            raise ValueError("INDIRECT_FIRE_CAPABILITY_NOT_CURRENT")
        validation = self._build_indirect_fire_plan_request_from_pre_state(
            capability,
            plan_request.planned_hex,
            pre_state,
            action_id=plan_request.action_id,
        )
        if not validation.valid or validation.request != plan_request:
            raise ValueError("INDIRECT_FIRE_PLAN_REQUEST_STALE")
        return self._indirect_fire_service.build_mission(
            plan_request,
            pre_state,
            authority=self._indirect_fire_authority,
            ammunition_authority=self._indirect_fire_ammunition_authority,
            execution_policy=self._indirect_fire_execution_policy,
            action_stream_bound=action_stream_bound,
        )

    def _apply_indirect_fire_mission_to_working_state(
        self,
        working: WorldState,
        request: IndirectFirePlanRequest,
        mission: FireMission,
        *,
        action_sequence: int,
    ) -> EngineEvent:
        return self._indirect_fire_service.apply_mission_creation(
            working,
            request,
            mission,
            authority=self._indirect_fire_authority,
            replay_identity=self.indirect_fire_replay_identity(),
            action_sequence=action_sequence,
        )

    def can_cancel_indirect_fire_mission(self, mission_id: str) -> bool:
        """Return current cancellation eligibility without changing state."""

        if not isinstance(mission_id, str) or not mission_id:
            raise ValueError("mission_id must be a non-empty string")
        mission = next(
            (
                item for item in self._require_world().indirect_fire_missions
                if item.mission_id == mission_id
            ),
            None,
        )
        if mission is None:
            raise ValueError("INDIRECT_FIRE_MISSION_NOT_FOUND")
        return can_cancel_mission(mission)

    def get_indirect_fire_active_areas(
        self,
        side: str,
    ) -> tuple[IndirectFireActiveAreaView, ...]:
        """Return own-side ACTIVE areas without exposing opponent authority."""

        if side not in self.scenario.side_order:
            raise ValueError(f"unknown side: {side}")
        world = self._require_world()
        return tuple(
            IndirectFireActiveAreaView(
                mission_id=mission.mission_id,
                shooter_id=mission.shooter_id,
                impact_hex=int(mission.impact_hex),
                active_remaining_seconds=mission.active_remaining_seconds,
                affected_operator_ids=mission.affected_operator_ids,
            )
            for mission in sorted(
                world.indirect_fire_missions,
                key=fire_mission_sort_key,
            )
            if mission.phase is IndirectFireMissionState.ACTIVE
            and mission.impact_hex is not None
            and (mission.launch_side if mission.action_stream_bound else (
                world.operators[mission.shooter_id].side
                if mission.shooter_id in world.operators else None
            )) == side
        )

    def get_indirect_fire_mission_areas(
        self,
        side: str,
    ) -> tuple[IndirectFireMissionAreaView, ...]:
        """Return own accepted waiting/active missions without enemy data."""

        if side not in self.scenario.side_order:
            raise ValueError(f"unknown side: {side}")
        world = self._require_world()
        visible_phases = {
            IndirectFireMissionState.PLANNED,
            IndirectFireMissionState.FLYING,
            IndirectFireMissionState.ACTIVE,
        }
        return tuple(
            IndirectFireMissionAreaView(
                mission_id=mission.mission_id,
                shooter_id=mission.shooter_id,
                weapon_id=mission.weapon_id,
                phase=mission.phase,
                planned_hex=mission.planned_hex,
                display_hex=(
                    mission.planned_hex
                    if mission.impact_hex is None
                    else mission.impact_hex
                ),
                remaining_flight_seconds=mission.remaining_flight_seconds,
                active_remaining_seconds=mission.active_remaining_seconds,
                affected_operator_ids=mission.affected_operator_ids,
            )
            for mission in sorted(
                world.indirect_fire_missions,
                key=fire_mission_sort_key,
            )
            if mission.phase in visible_phases
            and (mission.launch_side if mission.action_stream_bound else (
                world.operators[mission.shooter_id].side
                if mission.shooter_id in world.operators else None
            )) == side
        )

    def cancel_indirect_fire_mission(
        self,
        mission_id: str,
        reason: IndirectFireCancellationReason,
    ) -> FireMission:
        """Atomically cancel one FLYING mission without refunding ammunition."""

        if not isinstance(mission_id, str) or not mission_id:
            raise ValueError("mission_id must be a non-empty string")
        if not isinstance(reason, IndirectFireCancellationReason):
            raise TypeError("reason must be IndirectFireCancellationReason")
        authoritative = self._require_world()
        working = WorldState.from_dict(authoritative.to_dict())
        cancelled, event = self._indirect_fire_service.cancel_mission(
            working, mission_id, reason,
        )
        working.last_judge_info = [event.to_dict()]
        self._commit_world(authoritative, working)
        self._last_events = (event,)
        self._source_valid_actions_cache.clear()
        self._source_valid_actions_state_hash = None
        return cancelled

    def resolve_indirect_fire_impact(
        self,
        mission_id: str,
    ) -> IndirectFireImpactValidationResult:
        """Resolve one ACTIVE mission against detached state and cloned RNG.

        This Phase D-1 query never commits the cloned RNG, changes mission
        phase, emits an event, or applies unit effects. Unsupported observation
        policies fail closed before scatter sampling.
        """

        if not isinstance(mission_id, str) or not mission_id:
            raise ValueError("mission_id must be a non-empty string")
        authoritative = self._require_world()
        if self._rng is None:
            raise RuntimeError("engine_not_reset")
        pre_state = WorldState.from_dict(authoritative.to_dict())
        rng_snapshot = self._rng.snapshot()
        observation_evidence = self._indirect_fire_observation_at_impact(
            pre_state, mission_id
        )
        result = self._indirect_fire_service.resolve_impact(
            pre_state,
            rng_snapshot,
            mission_id,
            authority=self._indirect_fire_authority,
            execution_policy=self._indirect_fire_execution_policy,
            observation_authority=self._indirect_fire_observation_authority,
            observation_evidence=observation_evidence,
            spotter_evidence=self._indirect_fire_spotter_evidence,
        )
        if self._rng.snapshot() != rng_snapshot:
            raise RuntimeError("INDIRECT_FIRE_AUTHORITATIVE_RNG_CHANGED")
        return result

    def _indirect_fire_observation_at_impact(
        self,
        pre_state: WorldState,
        mission_id: str,
    ) -> FireMissionObservationEvidence:
        """Determine correction from the detached authoritative landing state."""

        mission = next((
            value for value in pre_state.indirect_fire_missions
            if value.mission_id == mission_id
        ), None)
        if mission is None:
            raise ValueError("INDIRECT_FIRE_MISSION_NOT_FOUND")
        shooter = pre_state.operators.get(mission.shooter_id)
        firing_side = mission.launch_side if mission.action_stream_bound else (
            None if shooter is None else shooter.side
        )
        if firing_side is None:
            raise ValueError("INDIRECT_FIRE_SHOOTER_NOT_FOUND")
        if self._source_map is None or self._source_visibility_map is None:
            raise ValueError("SOURCE_VISIBILITY_MAP_UNAVAILABLE")
        if self._indirect_fire_observation_policy is (
            IndirectFireObservationPolicy.UNRESOLVED
        ):
            return FireMissionObservationEvidence(
                correction_level="UNRESOLVED",
                state_tick=pre_state.tick,
                state_time_ms=pre_state.elapsed_time_ms,
                state_hash=canonical_hash(pre_state.to_dict()),
                planned_hex=mission.planned_hex,
                firing_side=firing_side,
                policy_id=self._indirect_fire_observation_authority.policy_id,
                policy_hash=self._indirect_fire_observation_authority.content_hash,
                source_interface=(
                    self._indirect_fire_observation_authority
                    .observation_interface
                ),
                eligible_observer_ids=(),
                grid_observer_ids=(),
                target_observations=(),
                selected_observer_id=None,
                selected_target_id=None,
                reason="observation_policy_unresolved",
            )

        observers = tuple(sorted((
            operator for operator in pre_state.operators.values()
            if operator.side == firing_side
            and operator.alive
            and operator.deployed
            and operator.current_hex is not None
            and self._is_indirect_fire_observer(operator)
        ), key=lambda value: value.operator_id))
        targets = tuple(sorted((
            operator for operator in pre_state.operators.values()
            if operator.side != firing_side
            and operator.alive
            and operator.deployed
            and operator.current_hex == mission.planned_hex
        ), key=lambda value: value.operator_id))
        grid_observers: list[int] = []
        target_observations: list[tuple[int, int]] = []
        for observer in observers:
            observer_domain = _unit_domain(observer.operator_type)
            observer_is_drone = self._is_canonical_drone(observer)
            ground_los = self._line_of_sight_to_ground_hex(
                observer, mission.planned_hex
            )
            grid_visible = any(
                evaluate_visibility(
                    self._source_visibility_map,
                    observer_hex=int(observer.current_hex),
                    target_hex=mission.planned_hex,
                    observer_domain=observer_domain,
                    target_domain=target_domain,
                    observer_is_drone=observer_is_drone,
                    target_concealed=False,
                    target_terrain=self._source_map.terrain(
                        mission.planned_hex
                    ).value,
                    observation_profiles=self._source_observation_profiles,
                    line_of_sight_evidence=ground_los,
                ).observable
                for target_domain in (UnitDomain.PERSONNEL, UnitDomain.VEHICLE)
            )
            if not grid_visible:
                continue
            grid_observers.append(observer.operator_id)
            for target in targets:
                visibility = evaluate_visibility(
                    self._source_visibility_map,
                    observer_hex=int(observer.current_hex),
                    target_hex=mission.planned_hex,
                    observer_domain=observer_domain,
                    target_domain=_unit_domain(target.operator_type),
                    observer_is_drone=observer_is_drone,
                    target_is_drone=self._is_canonical_drone(target),
                    target_concealed=target.concealed,
                    target_terrain=self._source_map.terrain(
                        mission.planned_hex
                    ).value,
                    observation_profiles=self._source_observation_profiles,
                    line_of_sight_evidence=self._runtime_line_of_sight(
                        observer, target
                    ),
                )
                if visibility.observable:
                    target_observations.append((
                        observer.operator_id, target.operator_id,
                    ))
        pairs = tuple(sorted(target_observations))
        if pairs:
            correction_level = "TARGET_CORRECTED"
            selected_observer, selected_target = pairs[0]
            reason = "observer_sees_planned_hex_and_target"
        elif grid_observers:
            correction_level = "GRID_CORRECTED"
            selected_observer = grid_observers[0]
            selected_target = None
            reason = (
                "planned_hex_visible_but_no_target_visible"
                if targets else "visible_planned_hex_is_empty"
            )
        else:
            correction_level = "UNOBSERVED"
            selected_observer = None
            selected_target = None
            reason = (
                "no_eligible_observers"
                if not observers else "no_observer_can_see_planned_hex"
            )
        return FireMissionObservationEvidence(
            correction_level=correction_level,
            state_tick=pre_state.tick,
            state_time_ms=pre_state.elapsed_time_ms,
            state_hash=canonical_hash(pre_state.to_dict()),
            planned_hex=mission.planned_hex,
            firing_side=firing_side,
            policy_id=self._indirect_fire_observation_authority.policy_id,
            policy_hash=self._indirect_fire_observation_authority.content_hash,
            source_interface=(
                self._indirect_fire_observation_authority.observation_interface
            ),
            eligible_observer_ids=tuple(
                observer.operator_id for observer in observers
            ),
            grid_observer_ids=tuple(grid_observers),
            target_observations=pairs,
            selected_observer_id=selected_observer,
            selected_target_id=selected_target,
            reason=reason,
        )

    def _is_canonical_drone(self, operator: OperatorState) -> bool:
        return resolve_operator_canonical_identity(
            operator,
            self._operator_lifecycle_authority,
            allow_compatibility_fallback=False,
        ).canonical_identity == "DRONE"

    def _is_indirect_fire_observer(self, operator: OperatorState) -> bool:
        domain = _unit_domain(operator.operator_type)
        return domain in {UnitDomain.PERSONNEL, UnitDomain.VEHICLE} or (
            domain is UnitDomain.AIR and self._is_canonical_drone(operator)
        )

    def resolve_indirect_fire_effects(
        self,
        impact_resolution: IndirectFireResolution,
        *,
        _affected_operator_ids: tuple[int, ...] | None = None,
    ) -> IndirectFireEffectResolutionResult:
        """Resolve D-2A damage contracts without committing any effect."""

        if not isinstance(impact_resolution, IndirectFireResolution):
            raise TypeError("impact_resolution must be IndirectFireResolution")
        authoritative = self._require_world()
        if self._rng is None:
            raise RuntimeError("engine_not_reset")
        if self._source_map is None or self._adjudication_tables is None:
            return _indirect_fire_effect_failure("impact_runtime_unavailable")
        before_world = authoritative.to_dict()
        before_rng = self._rng.snapshot()
        before_events = self._last_events
        result = self._indirect_fire_service.resolve_effects(
            WorldState.from_dict(before_world),
            before_rng,
            impact_resolution,
            pre_state_hash=canonical_hash(before_world),
            authority=self._indirect_fire_authority,
            damage_policy=self._indirect_fire_damage_policy,
            affected_operator_ids=_affected_operator_ids,
        )
        if (
            authoritative.to_dict() != before_world
            or self._rng.snapshot() != before_rng
            or self._last_events != before_events
        ):
            raise RuntimeError("INDIRECT_FIRE_EFFECT_RESOLUTION_MUTATED_ENGINE")
        return result

    def commit_indirect_fire_effect_bundle(
        self,
        bundle: IndirectFireEffectBundle,
    ) -> EngineEvent:
        """Atomically apply one fully resolved ACTIVE mission impact.

        Resolution is recomputed against the current authoritative pre-state
        before any working-state mutation.  Commit never rerolls scatter or
        damage; it restores the already staged RNG ``after`` state only after
        the detached world has been completely validated.
        """

        if not isinstance(bundle, IndirectFireEffectBundle):
            raise TypeError("bundle must be IndirectFireEffectBundle")
        authoritative = self._require_world()
        if self._rng is None:
            raise RuntimeError("engine_not_reset")
        if self._source_policy is None:
            raise ValueError("INDIRECT_FIRE_REQUIRES_SOURCE_RUNTIME")
        before_world = authoritative.to_dict()
        before_rng = self._rng.snapshot()
        if canonical_hash(before_world) != bundle.resolution_evidence.pre_state_hash:
            raise ValueError("INDIRECT_FIRE_EFFECT_PRESTATE_MISMATCH")
        if before_rng != bundle.rng_transition.before.to_record():
            raise ValueError("INDIRECT_FIRE_EFFECT_RNG_PRESTATE_MISMATCH")
        if (
            self._adjudication_tables is None
            or self._adjudication_tables.content_hash
            != bundle.resolution_evidence.adjudication_table_hash
        ):
            raise ValueError("INDIRECT_FIRE_EFFECT_TABLE_MISMATCH")
        policy = self._indirect_fire_execution_policy
        if (
            not isinstance(policy, IndirectFireExecutionPolicy)
            or bundle.execution_policy_id != policy.policy_id
            or bundle.execution_policy_hash != policy.content_hash
        ):
            raise ValueError("INDIRECT_FIRE_EFFECT_POLICY_MISMATCH")
        if any(
            effect.resolution_evidence.status
            is not IndirectFireEffectStatus.RESOLVED
            for effect in bundle.damage_effects
        ):
            raise ValueError("INDIRECT_FIRE_EFFECT_UNRESOLVED")
        if (
            bundle.damage_rng_transition is None
            or bundle.damage_rng_transition.after != bundle.rng_transition.after
            or bundle.damage_rng_transition.before.draws
            < bundle.rng_transition.before.draws
        ):
            raise ValueError("INDIRECT_FIRE_EFFECT_RNG_TRANSITION_MISMATCH")

        mission = next(
            (
                value for value in authoritative.indirect_fire_missions
                if value.mission_id == bundle.mission_id
            ),
            None,
        )
        if mission is None:
            raise ValueError("INDIRECT_FIRE_MISSION_NOT_FOUND")
        if mission.phase is not IndirectFireMissionState.ACTIVE:
            raise ValueError("INDIRECT_FIRE_MISSION_NOT_ACTIVE")
        if (
            mission.shooter_id != bundle.shooter_id
            or mission.weapon_id != bundle.weapon_id
            or mission.planned_hex != bundle.planned_hex
            or mission.execution_policy_id != bundle.execution_policy_id
            or mission.execution_policy_hash != bundle.execution_policy_hash
        ):
            raise ValueError("INDIRECT_FIRE_EFFECT_MISSION_MISMATCH")

        impact = self.resolve_indirect_fire_impact(bundle.mission_id)
        if not impact.valid or impact.resolution is None:
            raise ValueError("INDIRECT_FIRE_IMPACT_NOT_CURRENT")
        resolved = self.resolve_indirect_fire_effects(impact.resolution)
        if (
            resolved.status is not IndirectFireEffectStatus.RESOLVED
            or resolved.effect_bundle != bundle
        ):
            raise ValueError("INDIRECT_FIRE_EFFECT_BUNDLE_NOT_CURRENT")

        working = WorldState.from_dict(before_world)
        committed_events = self._apply_indirect_fire_area_bundle(
            working,
            bundle,
            initialize_area=True,
        )
        committed_events = (
            *committed_events,
            *self._apply_boarding_carrier_destruction(working),
        )
        working.last_judge_info = [event.to_dict() for event in committed_events]
        committed_rng = NativeRNG.restore(bundle.rng_transition.after.to_record())

        # _commit_world validates a complete detached clone before its bounded
        # field assignments.  RNG/events remain unpublished until it returns.
        self._commit_world(authoritative, working)
        self._rng = committed_rng
        self._last_events = committed_events
        self._source_valid_actions_cache.clear()
        self._source_valid_actions_state_hash = None
        return committed_events[0]

    def commit_indirect_fire_miss_resolution(
        self,
        miss: IndirectFireMissResolution,
    ) -> EngineEvent:
        """Atomically commit one current sampled out-of-map miss contract."""

        if not isinstance(miss, IndirectFireMissResolution):
            raise TypeError("miss must be IndirectFireMissResolution")
        authoritative = self._require_world()
        if self._rng is None:
            raise RuntimeError("engine_not_reset")
        current = self.resolve_indirect_fire_impact(miss.mission_id)
        if not current.valid or current.miss_resolution != miss:
            raise ValueError("INDIRECT_FIRE_MISS_NOT_CURRENT")

        working = WorldState.from_dict(authoritative.to_dict())
        event = self._apply_indirect_fire_miss(working, miss)
        working.last_judge_info = [event.to_dict()]
        committed_rng = NativeRNG.restore(miss.rng_transition.after.to_record())

        # World, RNG, events and caches publish only after the detached world
        # has passed the same complete validation used by a normal step.
        self._commit_world(authoritative, working)
        self._rng = committed_rng
        self._last_events = (event,)
        self._source_valid_actions_cache.clear()
        self._source_valid_actions_state_hash = None
        return event

    def _apply_indirect_fire_area_bundle(
        self,
        working: WorldState,
        bundle: IndirectFireEffectBundle,
        *,
        initialize_area: bool,
    ) -> tuple[EngineEvent, ...]:
        """Apply a staged area bundle to the current detached working world."""

        return self._indirect_fire_service.apply_area_bundle(
            working,
            bundle,
            initialize_area=initialize_area,
        )

    def guided_fire_action(
        self,
        capability: GuidedFireCapability,
    ) -> EngineAction:
        """Return a canonical executable action for one current capability."""

        request = self.build_guided_fire_request(capability)
        world = self._require_world()
        guider = world.operators.get(request.guider_id)
        if guider is None:
            raise ValueError("GUIDED_FIRE_GUIDER_NOT_FOUND")
        return ActionRouter.guided_fire_action(guider.side, capability)

    def build_guided_fire_request(
        self,
        capability: GuidedFireCapability,
    ) -> GuidedFireRequest:
        """Revalidate one capability and return detached preview evidence.

        The capability tuple is not trusted merely because a caller can hold
        or construct it.  All authority is recomputed from one current detached
        pre-state before the non-executable request is created.
        """

        if not isinstance(capability, GuidedFireCapability):
            raise TypeError("capability must be GuidedFireCapability")
        pre_state = WorldState.from_dict(self._require_world().to_dict())
        return self._build_guided_fire_request_from_pre_state(
            capability,
            pre_state,
        )

    def _build_guided_fire_request_from_pre_state(
        self,
        capability: GuidedFireCapability,
        pre_state: WorldState,
    ) -> GuidedFireRequest:
        """Build a request from the caller's one detached pre-state."""

        if not isinstance(capability, GuidedFireCapability):
            raise TypeError("capability must be GuidedFireCapability")
        if not isinstance(pre_state, WorldState):
            raise TypeError("pre_state must be WorldState")
        guider = pre_state.operators.get(capability.guider_id)
        shooter = pre_state.operators.get(capability.shooter_id)
        target = pre_state.operators.get(capability.target_id)
        if guider is None:
            raise ValueError("GUIDED_FIRE_GUIDER_NOT_FOUND")
        if shooter is None:
            raise ValueError("GUIDED_FIRE_SHOOTER_NOT_FOUND")
        if target is None:
            raise ValueError("GUIDED_FIRE_TARGET_NOT_FOUND")

        profile = self._guidance_authority.profile(
            capability.guidance_profile_id
        )
        if profile is None or guider.guidance_profile_id != profile.profile_id:
            raise ValueError("GUIDED_FIRE_PROFILE_UNAVAILABLE")
        try:
            weapon = self._source_weapon_profile(capability.weapon_id)
        except ValueError as exc:
            raise ValueError("GUIDED_FIRE_WEAPON_UNAVAILABLE") from exc
        if (
            FireMode.GUIDED not in weapon.supported_fire_modes
            or capability.weapon_id not in shooter.weapon_ids
        ):
            raise ValueError("GUIDED_FIRE_WEAPON_UNSUPPORTED")
        current = self._guided_fire_capabilities_from_pre_state(
            pre_state,
            guider.side,
        )
        if capability not in current:
            raise ValueError("GUIDED_FIRE_CAPABILITY_STALE")
        visibility = self._guided_fire_visibility(guider, target)
        if not visibility.observable:
            raise ValueError("GUIDED_FIRE_TARGET_NOT_VISIBLE_TO_GUIDER")
        if shooter.current_hex is None or target.current_hex is None:
            raise ValueError("CURRENT_HEX_REQUIRED")
        return _build_validated_guided_fire_request(
            capability,
            tick=pre_state.tick,
            guider_visibility_evidence=visibility,
            shooter_distance=hex_distance(
                int(shooter.current_hex), int(target.current_hex)
            ),
            ammunition_before=self._ammunition(shooter).get(
                weapon.ammunition_key,
                0,
            ),
            cooldown_before=shooter.weapon_cooldown_remaining_ms,
        )

    def validate_guided_fire_request(
        self,
        request: GuidedFireRequest,
    ) -> GuidedFireValidationResult:
        """Validate a request against exactly one detached current pre-state."""

        if not isinstance(request, GuidedFireRequest):
            raise TypeError("request must be GuidedFireRequest")
        pre_state = WorldState.from_dict(self._require_world().to_dict())
        return self._validate_guided_fire_request_against_pre_state(
            request,
            pre_state,
        )

    def build_guided_fire_effect_plan(
        self,
        request: GuidedFireRequest,
    ) -> GuidedFireEffectPlan:
        """Build resource effects without damage, RNG, events, or mutation."""

        if not isinstance(request, GuidedFireRequest):
            raise TypeError("request must be GuidedFireRequest")
        pre_state = WorldState.from_dict(self._require_world().to_dict())
        validation = self._validate_guided_fire_request_against_pre_state(
            request,
            pre_state,
        )
        if not validation.valid:
            raise ValueError(
                f"GUIDED_FIRE_VALIDATION_FAILED:{validation.reason}"
            )
        return self._guided_fire_effect_plan_for_validated_request(request)

    def resolve_guided_fire_request(
        self,
        request: GuidedFireRequest,
    ) -> FireResolution:
        """Stage deterministic damage/resources without committing runtime."""

        if not isinstance(request, GuidedFireRequest):
            raise TypeError("request must be GuidedFireRequest")
        if self._rng is None or self._adjudication_tables is None:
            raise RuntimeError("guided-fire runtime is unavailable")
        pre_state = WorldState.from_dict(self._require_world().to_dict())
        validation = self._validate_guided_fire_request_against_pre_state(
            request,
            pre_state,
        )
        if not validation.valid:
            raise ValueError(
                f"GUIDED_FIRE_VALIDATION_FAILED:{validation.reason}"
            )
        effect_plan = self._guided_fire_effect_plan_for_validated_request(
            request
        )
        direct_request = self._guided_direct_fire_request(request, pre_state)
        detached_rng = NativeRNG.restore(self._rng.snapshot())
        return GuidedFireResolver(
            self._adjudication_tables,
            detached_rng,
        ).resolve(validation, direct_request, effect_plan)

    def stage_guided_fire_intent(
        self,
        intent: FireIntent,
    ) -> GuidedFireStageResult:
        """Route and stage one GUIDED intent without authoritative mutation.

        The detached world is the immutable pre-state for request construction,
        validation, and resolution.  RNG is cloned once and its transition is
        returned only as an EffectBundle; no Engine event is published.
        """

        staged = self.stage_guided_fire_intents((intent,))
        return staged[0]

    def stage_guided_fire_intents(
        self,
        intents: Sequence[FireIntent],
    ) -> tuple[GuidedFireStageResult, ...]:
        """Stage a deterministic GUIDED batch against one detached pre-state.

        This is the internal execution-pipeline boundary intended to be called
        after an orchestrator has sealed a batch.  It deliberately performs no
        conflict resolution or authoritative commit; those belong to Phase 4C-2.
        """

        if isinstance(intents, (str, bytes)) or not isinstance(
            intents, Sequence
        ):
            raise TypeError("intents must be a sequence of FireIntent")
        values = tuple(intents)
        if any(not isinstance(intent, FireIntent) for intent in values):
            raise TypeError("intents must contain only FireIntent")
        if self._rng is None or self._adjudication_tables is None:
            raise RuntimeError("guided-fire runtime is unavailable")
        pre_state = WorldState.from_dict(self._require_world().to_dict())
        detached_rng = NativeRNG.restore(self._rng.snapshot())
        ordered = tuple(sorted(values, key=_guided_fire_intent_sort_key))
        router = self._action_router
        if router is None:
            raise RuntimeError("guided-fire router is unavailable")
        staged: list[GuidedFireStageResult] = []
        for intent in ordered:
            routed = router.route_fire_intent(
                intent,
                guided_stage=lambda candidate: (
                    self._stage_guided_fire_intent_against_pre_state(
                        candidate,
                        pre_state,
                        detached_rng,
                    )
                ),
            )
            if not isinstance(routed, GuidedFireStageResult):
                raise RuntimeError("GUIDED intent unexpectedly left GUIDED route")
            staged.append(routed)
        return tuple(staged)

    def _stage_guided_fire_intent_against_pre_state(
        self,
        intent: FireIntent,
        pre_state: WorldState,
        detached_rng: NativeRNG,
        *,
        action_id: str | None = None,
    ) -> GuidedFireStageResult:
        if intent.mode is not FireMode.GUIDED:
            raise ValueError("GUIDED_FIRE_STAGE_REQUIRES_GUIDED_INTENT")
        if not isinstance(intent.target, UnitTarget):
            raise ValueError("GUIDED_FIRE_REQUIRES_UNIT_TARGET")
        if intent.guider_id is None:
            raise ValueError("GUIDER_REQUIRED")
        shooter = pre_state.operators.get(intent.shooter_id)
        if shooter is None:
            raise ValueError("GUIDED_FIRE_SHOOTER_NOT_FOUND")
        available = self._guided_fire_capabilities_from_pre_state(
            pre_state,
            shooter.side,
        )
        matching = tuple(
            item
            for item in available
            if (
                item.guider_id == intent.guider_id
                and item.shooter_id == intent.shooter_id
                and item.weapon_id == intent.weapon_id
                and item.target_id == intent.target.unit_id
            )
        )
        if len(matching) != 1:
            raise ValueError("GUIDED_FIRE_CAPABILITY_NOT_AVAILABLE")
        request = self._build_guided_fire_request_from_pre_state(
            matching[0],
            pre_state,
        )
        router = self._action_router
        if router is None:
            raise RuntimeError("guided-fire router is unavailable")
        router.route_guided_fire_request(intent, request)
        validation = self._validate_guided_fire_request_against_pre_state(
            request,
            pre_state,
        )
        if not validation.valid:
            raise ValueError(
                f"GUIDED_FIRE_VALIDATION_FAILED:{validation.reason}"
            )
        effect_plan = self._guided_fire_effect_plan_for_validated_request(
            request
        )
        direct_request = self._guided_direct_fire_request(
            request,
            pre_state,
            action_id=action_id,
        )
        if self._adjudication_tables is None:
            raise RuntimeError("guided-fire runtime is unavailable")
        resolution = GuidedFireResolver(
            self._adjudication_tables,
            detached_rng,
        ).resolve(validation, direct_request, effect_plan)
        return GuidedFireStageResult(
            request=request,
            validation=validation,
            resolution=resolution,
            effect_bundle=resolution.effect_bundle,
        )

    def _guided_fire_effect_plan_for_validated_request(
        self,
        request: GuidedFireRequest,
    ) -> GuidedFireEffectPlan:
        profile = self._guidance_authority.profile(request.guidance_profile_id)
        if profile is None:
            raise RuntimeError("validated guidance profile disappeared")
        # The guidance profile recovery reserves both participants in the
        # joint opportunity. Weapon cooldown remains a separate direct-fire
        # effect for the future adjudicator/commit phase.
        recovery_updates = tuple(sorted(
            (
                GuidedFireRecoveryUpdate(
                    operator_id=request.shooter_id,
                    recovery_remaining_ms=profile.recovery_seconds * 1000,
                ),
                GuidedFireRecoveryUpdate(
                    operator_id=request.guider_id,
                    recovery_remaining_ms=profile.recovery_seconds * 1000,
                ),
            ),
            key=lambda item: item.operator_id,
        ))
        return GuidedFireEffectPlan(
            shooter_id=request.shooter_id,
            guider_id=request.guider_id,
            weapon_id=request.weapon_id,
            target_id=request.target_id,
            ammo_consume=1,
            recovery_updates=recovery_updates,
        )

    def _guided_direct_fire_request(
        self,
        request: GuidedFireRequest,
        pre_state: WorldState,
        *,
        action_id: str | None = None,
    ) -> DirectFireRequest:
        shooter = pre_state.operators[request.shooter_id]
        target = pre_state.operators[request.target_id]
        # Internal-only action identity keeps GUIDED dice/adjudication evidence
        # deterministic without changing EngineAction or Replay serialization.
        internal_action = EngineAction(
            shooter.side,
            shooter.operator_id,
            ActionType.SHOOT,
            target_id=target.operator_id,
            parameters={
                "weapon_id": request.weapon_id,
                "fire_mode": FireMode.GUIDED.value,
                "guider_id": request.guider_id,
            },
        )
        direct_request = self._direct_fire_request(
            internal_action,
            shooter,
            target,
            world=pre_state,
            weapon_id=request.weapon_id,
        )
        return replace(
            direct_request,
            action_id=(direct_request.action_id if action_id is None else action_id),
            visibility=request.guider_visibility_evidence,
            distance=request.shooter_distance,
            fire_mode=FireMode.GUIDED,
            # Guided readiness has its own established shooter validation.
            attacker_hovering=True,
            ammunition_before=request.ammunition_before,
            weapon_cooldown_before=(request.cooldown_before + 999) // 1000,
        )

    def _validate_guided_fire_request_against_pre_state(
        self,
        request: GuidedFireRequest,
        pre_state: WorldState,
    ) -> GuidedFireValidationResult:
        def result(valid: bool, reason: str) -> GuidedFireValidationResult:
            return GuidedFireValidationResult(valid, reason, request)

        if pre_state.done:
            return result(False, "WORLD_DONE")
        if request.tick != pre_state.tick:
            return result(False, "STALE_REQUEST_TICK")
        guider = pre_state.operators.get(request.guider_id)
        if guider is None:
            return result(False, "GUIDER_NOT_FOUND")
        shooter = pre_state.operators.get(request.shooter_id)
        if shooter is None:
            return result(False, "SHOOTER_NOT_FOUND")
        target = pre_state.operators.get(request.target_id)
        if target is None:
            return result(False, "TARGET_NOT_FOUND")

        profile = self._guidance_authority.profile(
            request.guidance_profile_id
        )
        if profile is None or guider.guidance_profile_id != profile.profile_id:
            return result(False, "GUIDANCE_PROFILE_UNAVAILABLE")
        try:
            weapon = self._source_weapon_profile(request.weapon_id)
        except ValueError:
            return result(False, "WEAPON_NOT_FOUND")
        if FireMode.GUIDED not in weapon.supported_fire_modes:
            return result(False, "WEAPON_GUIDED_MODE_UNSUPPORTED")
        if request.weapon_id not in shooter.weapon_ids:
            return result(False, "WEAPON_NOT_CARRIED")

        if guider.side != shooter.side:
            return result(False, "GUIDER_SHOOTER_SIDE_MISMATCH")
        if target.side == shooter.side:
            return result(False, "SAME_SIDE_TARGET")
        if not target.alive:
            return result(False, "TARGET_NOT_ALIVE")
        if not target.deployed:
            return result(False, "TARGET_NOT_DEPLOYED")
        if not guider.alive:
            return result(False, "GUIDER_NOT_ALIVE")
        if not guider.deployed:
            return result(False, "GUIDER_NOT_DEPLOYED")
        if guider.guided_fire_recovery_remaining_ms != 0:
            return result(False, "GUIDER_RECOVERING")
        restrictions = (
            profile.requires_stopped,
            profile.requires_unfolded,
            profile.prohibited_while_suppressed,
        )
        if any(value is None for value in restrictions):
            return result(False, "GUIDANCE_PROFILE_UNRESOLVED")
        if profile.requires_stopped and (not guider.stopped or guider.moving):
            return result(False, "GUIDER_MUST_STOP")
        if profile.requires_unfolded and not guider.weapon_unfolded:
            return result(False, "GUIDER_WEAPON_NOT_UNFOLDED")
        if (
            profile.prohibited_while_suppressed
            and guider.suppression_remaining_ms > 0
        ):
            return result(False, "GUIDER_SUPPRESSED")

        binding_failure = weapon_binding_failure(weapon_id=weapon.weapon_id,
            locked=shooter.weapon_locked, locked_weapon_id=shooter.locked_weapon_id)
        if binding_failure is not None:
            return result(False, binding_failure)
        if not shooter.alive:
            return result(False, "SHOOTER_NOT_ALIVE")
        if not shooter.deployed:
            return result(False, "SHOOTER_NOT_DEPLOYED")
        if shooter.guided_fire_recovery_remaining_ms != 0:
            return result(False, "SHOOTER_RECOVERING")
        if not self._guidance_scope_allows(profile, guider, shooter):
            return result(False, "GUIDANCE_SCOPE_NOT_AUTHORIZED")
        ammunition = self._ammunition(shooter).get(weapon.ammunition_key, 0)
        if ammunition <= 0:
            return result(False, "AMMUNITION_UNAVAILABLE")
        if shooter.weapon_cooldown_remaining_ms != 0:
            return result(False, "WEAPON_ON_COOLDOWN")
        if shooter.transition_kind is not None:
            return result(False, "SHOOTER_TRANSITIONING")
        if shooter.move_to_stop_remaining_ms != 0:
            return result(False, "SHOOTER_TRANSITIONING")
        if (
            (not shooter.stopped or shooter.moving)
            and not weapon.can_fire_while_moving
        ):
            return result(False, "SHOOTER_MUST_STOP")
        if weapon.requires_unfolded and not shooter.weapon_unfolded:
            return result(False, "WEAPON_NOT_UNFOLDED")

        if (
            guider.current_hex is None
            or shooter.current_hex is None
            or target.current_hex is None
        ):
            return result(False, "CURRENT_HEX_REQUIRED")
        if _unit_domain(target.operator_type) not in weapon.target_domains:
            return result(False, "UNSUPPORTED_TARGET_DOMAIN")
        shooter_distance = hex_distance(
            int(shooter.current_hex),
            int(target.current_hex),
        )
        if shooter_distance > weapon.maximum_range:
            return result(False, "TARGET_OUT_OF_RANGE")
        visibility = self._guided_fire_visibility(guider, target)
        if not visibility.observable:
            return result(False, "TARGET_NOT_OBSERVED_BY_GUIDER")

        # These checks bind the detached request evidence to this exact
        # pre-state.  Shooter visibility is intentionally absent.
        if request.shooter_distance != shooter_distance:
            return result(False, "REQUEST_DISTANCE_MISMATCH")
        if request.ammunition_before != ammunition:
            return result(False, "REQUEST_AMMUNITION_MISMATCH")
        if request.cooldown_before != shooter.weapon_cooldown_remaining_ms:
            return result(False, "REQUEST_COOLDOWN_MISMATCH")
        if (
            request.guider_visibility_evidence.to_dict()
            != visibility.to_dict()
        ):
            return result(False, "REQUEST_VISIBILITY_EVIDENCE_MISMATCH")
        return result(True, "LEGAL")

    def _guided_fire_capabilities_from_pre_state(
        self,
        pre_state: WorldState,
        side: str,
    ) -> tuple[GuidedFireCapability, ...]:
        if pre_state.done or self._source_map is None:
            return ()

        friendlies = tuple(sorted(
            (item for item in pre_state.operators.values() if item.side == side),
            key=lambda item: item.operator_id,
        ))
        enemies = tuple(sorted(
            (
                item
                for item in pre_state.operators.values()
                if item.side != side and item.alive and item.deployed
            ),
            key=lambda item: item.operator_id,
        ))
        capabilities: list[GuidedFireCapability] = []
        for guider in friendlies:
            profile = (
                None
                if guider.guidance_profile_id is None
                else self._guidance_authority.profile(guider.guidance_profile_id)
            )
            if profile is None or not self._guidance_guider_ready(guider, profile):
                continue
            visible_target_ids = {
                int(value)
                for value in guider.raw_attributes.get("see_enemy_bop_ids", ())
                if type(value) is int and value >= 0
            }
            if not visible_target_ids:
                continue
            for shooter in friendlies:
                if not self._guidance_scope_allows(profile, guider, shooter):
                    continue
                for weapon_id in sorted(shooter.weapon_ids):
                    try:
                        weapon = self._source_weapon_profile(weapon_id)
                    except ValueError:
                        continue
                    if not self._guided_weapon_ready(shooter, weapon):
                        continue
                    for target in enemies:
                        if (
                            target.operator_id not in visible_target_ids
                            or target.current_hex is None
                            or _unit_domain(target.operator_type)
                            not in weapon.target_domains
                            or hex_distance(
                                int(shooter.current_hex), int(target.current_hex)
                            ) > weapon.maximum_range
                        ):
                            continue
                        capabilities.append(GuidedFireCapability(
                            guider_id=guider.operator_id,
                            shooter_id=shooter.operator_id,
                            weapon_id=int(weapon_id),
                            target_id=target.operator_id,
                            guidance_profile_id=profile.profile_id,
                        ))
        return tuple(sorted(capabilities, key=guided_fire_capability_sort_key))

    def _guided_fire_visibility(
        self,
        guider: OperatorState,
        target: OperatorState,
    ) -> VisibilityResult:
        """Recompute exact guider evidence through the observation evaluator."""

        if self._source_map is None or self._source_visibility_map is None:
            raise ValueError("SOURCE_VISIBILITY_MAP_UNAVAILABLE")
        if guider.current_hex is None or target.current_hex is None:
            raise ValueError("CURRENT_HEX_REQUIRED")
        return evaluate_visibility(
            self._source_visibility_map,
            observer_hex=guider.current_hex,
            target_hex=target.current_hex,
            observer_domain=_unit_domain(guider.operator_type),
            target_domain=_unit_domain(target.operator_type),
            observer_is_drone=_uses_drone_observation_profile(
                guider, self._operator_lifecycle_authority
            ),
            target_is_drone=_uses_drone_observation_profile(
                target, self._operator_lifecycle_authority
            ),
            target_concealed=target.concealed,
            target_terrain=self._source_map.terrain(target.current_hex).value,
            line_of_sight_evidence=self._runtime_line_of_sight(guider, target),
        )

    @staticmethod
    def _guidance_guider_ready(
        guider: OperatorState,
        profile: GuidanceProfile,
    ) -> bool:
        if (
            not guider.alive
            or not guider.deployed
            or guider.current_hex is None
            or guider.movement_locked
            or guider.guided_fire_recovery_remaining_ms != 0
        ):
            return False
        # None is an unresolved authority fact, not permission to omit a rule.
        restrictions = (
            profile.requires_stopped,
            profile.requires_unfolded,
            profile.prohibited_while_suppressed,
        )
        if any(value is None for value in restrictions):
            return False
        if profile.requires_stopped and (not guider.stopped or guider.moving):
            return False
        if profile.requires_unfolded and not guider.weapon_unfolded:
            return False
        if profile.prohibited_while_suppressed and guider.suppression_remaining_ms > 0:
            return False
        return True

    @staticmethod
    def _guidance_scope_allows(
        profile: GuidanceProfile,
        guider: OperatorState,
        shooter: OperatorState,
    ) -> bool:
        if (
            not shooter.alive
            or not shooter.deployed
            or shooter.current_hex is None
            or shooter.movement_locked
            or shooter.guided_fire_recovery_remaining_ms != 0
        ):
            return False
        if profile.guidance_scope is GuidanceScope.AFFILIATED_SHOOTER:
            return (
                guider.guidance_affiliation_id == shooter.operator_id
                and guider.guidance_affiliation_evidence is not None
            )
        if profile.guidance_scope is GuidanceScope.SIDE_WIDE:
            return _unit_domain(shooter.operator_type) is UnitDomain.VEHICLE
        return False

    def _guided_weapon_ready(
        self,
        shooter: OperatorState,
        weapon: WeaponProfile,
    ) -> bool:
        if (
            type(weapon.weapon_id) is not int
            or weapon.weapon_id not in shooter.weapon_ids
            or FireMode.GUIDED not in weapon.supported_fire_modes
            or weapon_binding_failure(weapon_id=weapon.weapon_id,
                locked=shooter.weapon_locked, locked_weapon_id=shooter.locked_weapon_id) is not None
            or shooter.movement_locked
            or shooter.weapon_cooldown_remaining_ms != 0
            or shooter.transition_kind is not None
            or shooter.move_to_stop_remaining_ms != 0
            or (
                (not shooter.stopped or shooter.moving)
                and not weapon.can_fire_while_moving
            )
            or (weapon.requires_unfolded and not shooter.weapon_unfolded)
        ):
            return False
        return self._ammunition(shooter).get(weapon.ammunition_key, 0) > 0

    def valid_actions(self, side: str) -> tuple[ValidAction, ...]:
        world = self._require_world()
        self._require_side(side)
        if world.done:
            return ()
        if self._source_policy is not None:
            state_hash = canonical_hash(world.to_dict())
            if state_hash != self._source_valid_actions_state_hash:
                self._source_valid_actions_cache.clear()
                self._source_valid_actions_state_hash = state_hash
            records = self._source_valid_actions_cache.get(side)
            if records is None:
                records = tuple(
                    item.to_dict() for item in self._source_valid_actions(side)
                )
                self._source_valid_actions_cache[side] = records
            return tuple(
                ValidAction(
                    EngineAction.from_dict(item["action"]),
                    int(item["priority"]),
                    str(item["source"]),
                )
                for item in records
            )
        values: list[ValidAction] = []
        if world.stage is Stage.DEPLOYMENT:
            for operator in self._friendly(side):
                if operator.alive and not operator.deployed:
                    position = self.scenario.deployment_positions[side][operator.operator_id]
                    action = EngineAction(side, operator.operator_id,
                                          ActionType.DEPLOY,
                                          target_position=position)
                    values.append(ValidAction(action, ACTION_PRIORITY[action.action_type]))
            if (not values
                    and not world.sides[side].deployment_complete):
                action = EngineAction(side, None, ActionType.END_DEPLOYMENT)
                values.append(ValidAction(action, ACTION_PRIORITY[action.action_type]))
            if world.sides[side].deployment_complete:
                action = EngineAction(side, None, ActionType.WAIT)
                values.append(ValidAction(action, ACTION_PRIORITY[action.action_type]))
        elif world.stage is Stage.COMBAT:
            enemies = [item for item in world.operators.values()
                       if item.side != side and item.alive and item.deployed]
            for operator in self._friendly(side):
                if not operator.alive or not operator.deployed:
                    continue
                weapon_id = operator.weapon_ids[0] if operator.weapon_ids else None
                targets = ([] if weapon_id is None else [
                    item for item in enemies if validate_attack(
                        operator, item, combat_rules=self.ruleset.combat,
                        visibility_rules=self.ruleset.visibility,
                        weapon_id=weapon_id) is None])
                for target in sorted(targets, key=lambda item: item.operator_id):
                    action = EngineAction(side, operator.operator_id,
                                          ActionType.ATTACK,
                                          target_id=target.operator_id,
                                          parameters={"weapon_id": weapon_id})
                    values.append(ValidAction(action, ACTION_PRIORITY[action.action_type]))
                if not targets and enemies:
                    target = min(enemies, key=lambda item: (
                        distance(operator.position, item.position), item.operator_id))
                    configured_range = self.ruleset.movement.for_operator(operator)
                    limit = min(configured_range,
                                operator.max_speed or configured_range)
                    destination = move_toward(operator.position, target.position, limit)
                    if destination != operator.position and validate_move(
                        operator, destination, width=self.scenario.map.width,
                        height=self.scenario.map.height,
                        blocked_positions=self.scenario.map.blocked_positions,
                        rules=self.ruleset.movement,
                        occupied_positions=self._occupied_positions(
                            exclude=operator.operator_id),
                    ) is None:
                        action = EngineAction(side, operator.operator_id,
                                              ActionType.MOVE,
                                              target_position=destination)
                        values.append(ValidAction(action,
                                                  ACTION_PRIORITY[action.action_type]))
                wait = EngineAction(side, operator.operator_id, ActionType.WAIT)
                values.append(ValidAction(wait, ACTION_PRIORITY[wait.action_type]))
            if not values:
                wait = EngineAction(side, None, ActionType.WAIT)
                values.append(ValidAction(wait, ACTION_PRIORITY[wait.action_type]))
        return tuple(sorted(values, key=valid_action_sort_key))

    def plan_move(
        self,
        side: str,
        unit_id: int,
        destination_hex: int,
        movement_mode: MovementMode | str = MovementMode.NORMAL,
    ) -> RoutePlan:
        """Read-only destination proposal using the execution cost model."""

        world = self._require_world()
        self._require_side(side)
        mode = (
            movement_mode
            if isinstance(movement_mode, MovementMode)
            else MovementMode(str(movement_mode).upper())
        )
        if world.done:
            return RoutePlan(
                False, "SESSION_TERMINAL", int(unit_id), None,
                int(destination_hex), mode,
            )
        try:
            operator = world.operators[int(unit_id)]
        except KeyError as exc:
            raise ValueError(f"unknown unit: {unit_id}") from exc
        if operator.side != side:
            raise ValueError("unit is not controlled by side")
        if (
            self._route_planner is None
            or self._movement_map_reader is None
            or self._movement is None
            or self._source_policy is None
        ):
            return RoutePlan(
                False, "HEX_MAP_REQUIRED", int(unit_id), operator.current_hex,
                int(destination_hex), mode,
            )
        full_hexes = full_friendly_ground_hexes(
            world,
            side=operator.side,
            exclude=operator.operator_id,
            stack_limit=self._source_policy.friendly_ground_stack_limit,
        )
        request = PathPlanningRequest.from_operator(
            operator,
            int(destination_hex),
            mode,
            full_hexes,
        )
        # The replaceable algorithm receives a request-scoped rule facade,
        # never the MovementPlanner instance used by authoritative execution.
        planner_policy = replace(
            self._source_policy,
            _document=self._source_policy.to_dict(),
        )
        planner_cost_query = DetachedMovementCostQuery(
            MovementPlanner(self._movement_map_reader, planner_policy),
            request.unit,
            request.movement_mode,
        )
        candidate = self._route_planner.plan(
            request,
            self._movement_map_reader,
            planner_cost_query,
        )
        authoritative_cost_query = DetachedMovementCostQuery(
            self._movement.planner,
            request.unit,
            request.movement_mode,
        )
        return self._confirm_route_candidate(
            request, candidate, authoritative_cost_query,
        )

    @staticmethod
    def _route_failure(request: PathPlanningRequest, reason: str) -> RoutePlan:
        return RoutePlan(
            False,
            str(reason),
            request.unit.operator_id,
            request.unit.current_hex,
            request.destination_hex,
            request.movement_mode,
        )

    def _confirm_route_candidate(
        self,
        request: PathPlanningRequest,
        candidate: RoutePlan,
        cost_query: DetachedMovementCostQuery,
    ) -> RoutePlan:
        """Validate a planner proposal without trusting its rule metadata."""

        if not isinstance(candidate, RoutePlan):
            raise TypeError("path planner must return RoutePlan")
        expected = (
            request.unit.operator_id,
            request.unit.current_hex,
            request.destination_hex,
            request.movement_mode,
        )
        actual = (
            candidate.unit_id,
            candidate.source_hex,
            candidate.destination_hex,
            candidate.movement_mode,
        )
        if actual != expected:
            return self._route_failure(request, "PLANNER_CONTEXT_MISMATCH")
        if not candidate.reachable:
            return self._route_failure(request, candidate.reason_code)

        try:
            path = tuple(int(value) for value in candidate.ordered_path)
        except (TypeError, ValueError, OverflowError):
            return self._route_failure(request, "PLANNER_INVALID_PATH")
        if not path:
            return self._route_failure(request, "PATH_REQUIRED")
        if path[-1] != request.destination_hex:
            return self._route_failure(request, "PLANNER_DESTINATION_MISMATCH")
        if not self._movement_map_reader.contains(request.destination_hex):
            return self._route_failure(request, "UNKNOWN_DESTINATION")
        if any(not self._movement_map_reader.contains(value) for value in path):
            return self._route_failure(request, "PLANNER_PATH_UNKNOWN_HEX")

        initial = cost_query.validate_operator()
        if not initial.legal:
            return self._route_failure(request, initial.reason_code)
        current = request.start_hex
        blocked = frozenset(request.full_friendly_ground_hexes)
        segments = []
        for target in path:
            legality, segment = cost_query.plan_segment(current, target)
            if not legality.legal or segment is None:
                return self._route_failure(request, legality.reason_code)
            if target in blocked:
                return self._route_failure(request, "FRIENDLY_STACK_LIMIT")
            segments.append(segment)
            current = target
        return RoutePlan(
            True,
            "LEGAL",
            request.unit.operator_id,
            request.start_hex,
            request.destination_hex,
            request.movement_mode,
            path,
            sum(item.traversal_ms for item in segments),
            tuple(segments),
        )

    def query_region_visibility_from_projection(
        self,
        side: str,
        actor_id: int,
        observation: Mapping[str, Any],
        source_frame: Mapping[str, Any],
        query: RegionVisibilityQuery,
    ) -> RegionVisibilityResult:
        """Query a bounded hypothetical region without reading live WorldState.

        Identity and location come from the selected friendly snapshot. Static
        own-unit metadata supplies only the same profile/LOS authority used by
        ordinary observation; caller assertions cannot alter that identity.
        """

        self._require_side(side)
        if type(actor_id) is not int or actor_id < 0:
            raise RegionVisibilityQueryError("invalid_arguments", "invalid actor_id")
        if not isinstance(query, RegionVisibilityQuery):
            raise RegionVisibilityQueryError("invalid_arguments", "invalid query")
        if not isinstance(observation, Mapping) or not isinstance(source_frame, Mapping):
            raise RegionVisibilityQueryError("invalid_arguments", "invalid side projection")
        friendly = _record_by_id(
            observation.get("friendly_operators"), actor_id, "operator_id"
        )
        if friendly is None or friendly.get("side") != side:
            raise RegionVisibilityQueryError("actor_not_owned", "actor is not controlled by side")
        source = _record_by_id(source_frame.get("operators"), actor_id, "obj_id")
        if source is None or source.get("owner") != side:
            raise RegionVisibilityQueryError("actor_not_owned", "projection lacks own actor evidence")
        template = next((value for value in self.scenario.operators
                         if value.operator_id == actor_id and value.side == side), None)
        if template is None:
            raise RegionVisibilityQueryError("actor_unavailable", "actor template is unavailable")
        observer = OperatorState.from_dict(template.to_dict())
        observer.operator_type = str(friendly["operator_type"])
        observer.subtype = friendly["subtype"]
        observer.current_hex = _exact_nonnegative_record_int(source.get("cur_hex"), "cur_hex")
        observer.altitude = float(friendly["altitude"])
        observer.lifecycle_profile_id = friendly.get("lifecycle_profile_id")
        if not _exact_record_bool(friendly.get("alive"), "alive") or not _exact_record_bool(
            friendly.get("deployed"), "deployed"
        ):
            raise RegionVisibilityQueryError("actor_unavailable", "actor is not active and deployed")
        domain = _unit_domain(observer.operator_type)
        if domain is UnitDomain.UNKNOWN:
            raise RegionVisibilityQueryError("visibility_unavailable", "unsupported observer domain")
        if self._source_map is None or self._source_visibility_map is None:
            raise RegionVisibilityQueryError("visibility_unavailable", "a Source-compatible hex map is required")

        height = _source_visibility_height_m(observer, self._operator_lifecycle_authority)
        use_static_los = self.map_runtime_data is not None and height is not None

        def line_of_sight(target_hex: int) -> LineOfSightEvidence:
            try:
                if use_static_los:
                    return self.map_runtime_data.visibility.line_of_sight(
                        observer.current_hex, target_hex,
                        observer_height_m=height,
                        target_height_m=query.target_profile.height_m,
                    )
                return evaluate_line_of_sight(
                    self._source_visibility_map, observer.current_hex, target_hex
                )
            except ValueError as exc:
                raise RegionVisibilityQueryError(
                    "visibility_unavailable", str(exc)
                ) from exc

        return evaluate_region_visibility(
            query=query,
            actor={
                "actor_id": actor_id, "current_hex": observer.current_hex,
                "operator_type": observer.operator_type, "subtype": observer.subtype,
                "altitude": observer.altitude, "visibility_height_m": height,
            },
            observer_domain=domain,
            observer_is_drone=_uses_drone_observation_profile(
                observer, self._operator_lifecycle_authority
            ),
            profile=self._source_visibility_map,
            observation_profiles=self._source_observation_profiles,
            terrain_at=lambda hex_id: self._source_map.terrain(hex_id).value,
            line_of_sight=line_of_sight,
            map_id=self.scenario.map.map_id,
            los_authority=("source_static_visibility_lookup" if use_static_los
                           else "native_terrain_line_of_sight"),
        )

    def query_unit_tactical_overlay(
        self,
        side: str,
        unit_id: int,
    ) -> UnitTacticalOverlay:
        """Project read-only ground-target tactical space for one controlled unit.

        Visibility delegates to the same observation evaluator used by the
        authoritative observation refresh.  Weapon envelopes use the same
        static LOS authority and weapon profiles, but intentionally ignore
        enemy occupancy and current weapon readiness.  Current fire targets
        remain a projection of the existing legal ``SHOOT`` actions.
        """

        world = self._require_world()
        self._require_side(side)
        if type(unit_id) is not int or unit_id < 0:
            raise ValueError("unit_id must be a non-negative exact int")
        try:
            operator = world.operators[unit_id]
        except KeyError as exc:
            raise ValueError(f"unknown unit: {unit_id}") from exc
        if operator.side != side:
            raise ValueError("unit is not controlled by side")
        fire_targets = []
        for value in self.valid_actions(side):
            action = value.action
            if (
                action.action_type is ActionType.SHOOT
                and action.actor_id == unit_id
                and action.target_id is not None
            ):
                target = world.operators.get(action.target_id)
                if target is None or target.current_hex is None:
                    raise RuntimeError("legal SHOOT target lacks a current hex")
                fire_targets.append(FireTarget(
                    action.target_id,
                    target.current_hex,
                ))

        return self._build_unit_tactical_overlay(operator, fire_targets)

    def query_unit_tactical_overlay_from_projection(
        self,
        side: str,
        unit_id: int,
        observation: Mapping[str, Any],
        source_frame: Mapping[str, Any],
        valid_actions: Sequence[Mapping[str, Any]],
    ) -> UnitTacticalOverlay:
        """Query one frozen side projection without reading live WorldState.

        Realtime ticks evolve a private working state.  This projection form
        lets a browser query the exact observation version it received while
        such a tick is running.  Only the selected friendly unit, enemies
        already visible in that side projection, and that projection's frozen
        legal actions participate.
        """

        self._require_side(side)
        if type(unit_id) is not int or unit_id < 0:
            raise ValueError("unit_id must be a non-negative exact int")
        if not isinstance(observation, Mapping):
            raise TypeError("observation must be a mapping")
        if not isinstance(source_frame, Mapping):
            raise TypeError("source_frame must be a mapping")

        friendly = _record_by_id(
            observation.get("friendly_operators"), unit_id, "operator_id"
        )
        if friendly is None:
            raise ValueError("unit is not controlled by side")
        if friendly.get("side") != side:
            raise ValueError("unit is not controlled by side")
        source_operator = _record_by_id(
            source_frame.get("operators"), unit_id, "obj_id"
        )
        if source_operator is None or source_operator.get("owner") != side:
            raise ValueError("side projection lacks controlled unit evidence")

        template = next(
            (value for value in self.scenario.operators
             if value.operator_id == unit_id),
            None,
        )
        if template is None:
            raise ValueError(f"unknown unit: {unit_id}")
        projected = OperatorState.from_dict(template.to_dict())
        projected.operator_type = str(friendly.get(
            "operator_type", projected.operator_type
        ))
        projected.subtype = friendly.get("subtype", projected.subtype)
        projected.current_hex = _exact_nonnegative_record_int(
            source_operator.get("cur_hex"), "friendly cur_hex"
        )
        position = friendly.get("position")
        if (not isinstance(position, Sequence)
                or isinstance(position, (str, bytes))
                or len(position) != 2):
            raise ValueError("friendly position must contain x and y")
        projected.x = float(position[0])
        projected.y = float(position[1])
        projected.altitude = float(friendly.get("altitude", projected.altitude))
        projected.lifecycle_profile_id = friendly.get("lifecycle_profile_id", projected.lifecycle_profile_id)
        projected.alive = _exact_record_bool(friendly.get("alive"), "alive")
        projected.deployed = _exact_record_bool(
            friendly.get("deployed"), "deployed"
        )
        weapon_ids = source_operator.get("carry_weapon_ids")
        if not isinstance(weapon_ids, Sequence) or isinstance(
            weapon_ids, (str, bytes)
        ):
            raise ValueError("friendly carry_weapon_ids must be a sequence")
        projected.weapon_ids = sorted({
            _exact_nonnegative_record_int(value, "weapon_id")
            for value in weapon_ids
        })

        visible_ids = {
            _exact_nonnegative_record_int(value.get("operator_id"), "target_id")
            for value in _record_sequence(
                observation.get("visible_enemies"), "visible_enemies"
            )
        }
        source_visible = {}
        for value in _record_sequence(
            source_frame.get("operators"), "source operators"
        ):
            target_id = _exact_nonnegative_record_int(
                value.get("obj_id"), "target_id"
            )
            if value.get("owner") != side and target_id in visible_ids:
                source_visible[target_id] = value
        fire_targets = []
        for value in valid_actions:
            if not isinstance(value, Mapping):
                raise TypeError("valid action records must be mappings")
            action = value.get("action")
            if not isinstance(action, Mapping):
                raise ValueError("valid action record lacks action mapping")
            if (
                action.get("side") == side
                and action.get("action_type") == ActionType.SHOOT.value
                and action.get("actor_id") == unit_id
                and action.get("target_id") is not None
            ):
                target_id = _exact_nonnegative_record_int(
                    action.get("target_id"), "target_id"
                )
                target = source_visible.get(target_id)
                if target is None:
                    raise ValueError(
                        "legal SHOOT target is absent from visible side projection"
                    )
                fire_targets.append(FireTarget(
                    target_id,
                    _exact_nonnegative_record_int(
                        target.get("cur_hex"), "target cur_hex"
                    ),
                ))

        return self._build_unit_tactical_overlay(projected, fire_targets)

    def _build_unit_tactical_overlay(
        self,
        operator: OperatorState,
        fire_targets: Sequence[FireTarget],
    ) -> UnitTacticalOverlay:
        if not operator.alive or not operator.deployed:
            raise ValueError("unit is not active")
        if operator.current_hex is None:
            raise ValueError("unit has no current hex")
        observer_domain = _unit_domain(operator.operator_type)
        if observer_domain is UnitDomain.UNKNOWN:
            raise ValueError("tactical overlay requires a supported observer domain")
        observer_is_drone = _uses_drone_observation_profile(
            operator, self._operator_lifecycle_authority,
        )
        if self._source_map is None or self._source_visibility_map is None:
            raise ValueError("tactical overlay requires a source-compatible hex map")

        cells = tuple(sorted(
            self.scenario.map.hexes, key=lambda value: value.hex_id
        ))
        line_of_sight: dict[int, LineOfSightEvidence] = {}

        def ground_los(hex_id: int) -> LineOfSightEvidence:
            cached = line_of_sight.get(hex_id)
            if cached is None:
                cached = self._line_of_sight_to_ground_hex(operator, hex_id)
                line_of_sight[hex_id] = cached
            return cached

        visibility_domains = []
        for target_domain in (UnitDomain.PERSONNEL, UnitDomain.VEHICLE):
            visible_hexes = []
            for cell in cells:
                if not within_maximum_observation_distance(
                    observer_hex=operator.current_hex,
                    target_hex=cell.hex_id,
                    observer_domain=observer_domain,
                    observer_is_drone=observer_is_drone,
                    target_domain=target_domain,
                    observation_profiles=self._source_observation_profiles,
                ):
                    continue
                result = evaluate_visibility(
                    self._source_visibility_map,
                    observer_hex=operator.current_hex,
                    target_hex=cell.hex_id,
                    observer_domain=observer_domain,
                    observer_is_drone=observer_is_drone,
                    target_domain=target_domain,
                    target_concealed=False,
                    target_terrain=self._source_map.terrain(cell.hex_id).value,
                    observation_profiles=self._source_observation_profiles,
                    line_of_sight_evidence=ground_los(cell.hex_id),
                )
                if result.observable:
                    visible_hexes.append(cell.hex_id)
            visibility_domains.append(VisibilityDomainField(
                target_domain.value,
                tuple(visible_hexes),
            ))

        supported_ground_domains = {UnitDomain.PERSONNEL, UnitDomain.VEHICLE}
        weapons = []
        for weapon_id in sorted(operator.weapon_ids):
            profile = self._source_weapon_profile(weapon_id)
            target_domains = tuple(sorted(
                value.value
                for value in profile.target_domains & supported_ground_domains
            ))
            envelope_hexes = tuple(
                cell.hex_id
                for cell in cells
                if target_domains
                and hex_distance(operator.current_hex, cell.hex_id)
                <= profile.maximum_range
                and ground_los(cell.hex_id).visible
            )
            weapons.append(WeaponEnvelopeEntry(
                int(weapon_id),
                profile.maximum_range,
                target_domains,
                envelope_hexes,
            ))

        return UnitTacticalOverlay(
            operator.operator_id,
            VisibilityField(tuple(visibility_domains)),
            WeaponEnvelope(tuple(weapons)),
            FireTargets(tuple(sorted(
                set(fire_targets), key=lambda value: (value.target_id, value.hex_id)
            ))),
        )

    def step(self, actions: Mapping[str, EngineAction | None]
             | Sequence[EngineAction], *,
             delta_time_seconds: int | float | None = None,
             delta_time_ms: int | None = None) -> StepResult:
        return self._step_ms(
            actions, delta_time_ms=resolve_delta_time_ms(
                delta_time_seconds=delta_time_seconds,
                delta_time_ms=delta_time_ms,
            )
        )

    def step_ms(self, actions: Mapping[str, EngineAction | None]
                | Sequence[EngineAction], *, delta_time_ms: int) -> StepResult:
        """Advance once using the authoritative exact-millisecond contract."""

        return self.step(
            actions,
            delta_time_ms=require_positive_milliseconds(delta_time_ms),
        )

    def _step_ms(self, actions: Mapping[str, EngineAction | None]
                 | Sequence[EngineAction], *, delta_time_ms: int) -> StepResult:
        milliseconds = require_positive_milliseconds(delta_time_ms)
        if self._source_policy is None:
            if milliseconds != 1_000:
                raise ValueError(
                    "legacy native-default step requires delta_time_ms=1000")
            if not isinstance(actions, Mapping):
                raise TypeError(
                    "legacy native-default actions must be a side mapping")

        authoritative_world = self._require_world()
        if self._rng is None:
            raise RuntimeError("engine_not_reset")
        authoritative_rng = self._rng
        authoritative_action_counts = self._action_counts
        authoritative_error_counts = self._error_counts
        authoritative_last_events = self._last_events
        authoritative_cache = self._source_valid_actions_cache
        authoritative_cache_hash = self._source_valid_actions_state_hash

        # A tick evolves a detached working runtime.  Replacing these private
        # references is the commit point; an exception restores the exact
        # authoritative runtime, including RNG and monotonic sequences stored
        # in WorldState.
        self._world = WorldState.from_dict(authoritative_world.to_dict())
        self._rng = NativeRNG.restore(authoritative_rng.snapshot())
        self._action_counts = dict(authoritative_action_counts)
        self._error_counts = dict(authoritative_error_counts)
        self._last_events = tuple(authoritative_last_events)
        self._source_valid_actions_cache = dict(authoritative_cache)
        self._source_valid_actions_state_hash = authoritative_cache_hash
        try:
            if self._source_policy is not None:
                result = self._step_source(actions, delta_time_ms=milliseconds)
            else:
                result = self._step_legacy(actions)
        except BaseException:
            self._world = authoritative_world
            self._rng = authoritative_rng
            self._action_counts = authoritative_action_counts
            self._error_counts = authoritative_error_counts
            self._last_events = authoritative_last_events
            self._source_valid_actions_cache = authoritative_cache
            self._source_valid_actions_state_hash = authoritative_cache_hash
            raise
        working_world = self._require_world()
        try:
            self._commit_world(authoritative_world, working_world)
        except BaseException:
            self._world = authoritative_world
            self._rng = authoritative_rng
            self._action_counts = authoritative_action_counts
            self._error_counts = authoritative_error_counts
            self._last_events = authoritative_last_events
            self._source_valid_actions_cache = authoritative_cache
            self._source_valid_actions_state_hash = authoritative_cache_hash
            raise
        self._world = authoritative_world
        return result

    @staticmethod
    def _commit_world(authoritative: WorldState, working: WorldState) -> None:
        """Commit a successful detached tick while preserving trusted refs."""
        # Complete every allocating/validating operation before touching the
        # authoritative object.  The remaining assignments cannot invoke rule
        # code or user data hooks.
        committed = WorldState.from_dict(working.to_dict())
        for name in (
            "tick", "stage", "terminal", "winner", "terminal_reason",
            "truncated", "elapsed_time_ms", "next_action_sequence",
            "next_event_sequence", "movement_evidence", "last_actions",
            "last_judge_info", "indirect_fire_missions",
            "boarding_transitions",
        ):
            setattr(authoritative, name, getattr(committed, name))
        for collection_name in ("operators", "sides", "objectives"):
            destination = getattr(authoritative, collection_name)
            source = getattr(committed, collection_name)
            for key in tuple(destination):
                if key not in source:
                    del destination[key]
            for key, source_item in source.items():
                destination_item = destination.get(key)
                if destination_item is None:
                    destination[key] = source_item
                    continue
                for field in fields(source_item):
                    setattr(destination_item, field.name,
                            getattr(source_item, field.name))

    def _step_legacy(self, actions: Mapping[str, EngineAction | None]) -> StepResult:
        world = self._require_world()
        if world.done:
            raise RuntimeError("episode_already_finished")
        if not isinstance(actions, Mapping):
            raise TypeError("actions must be a side-to-action mapping")
        tick_before = world.tick
        stage_before = world.stage
        available = {side: {item.action_id: item
                            for item in self.valid_actions(side)}
                     for side in self.scenario.side_order}
        submitted: list[EngineAction] = []
        accepted: list[EngineAction] = []
        decisions: list[ActionDecision] = []
        events: list[EngineEvent] = []
        for side in self.scenario.side_order:
            action = actions.get(side)
            if action is None:
                action = EngineAction(side, None, ActionType.WAIT)
            if not isinstance(action, EngineAction):
                raise TypeError(f"action for {side} must be EngineAction or None")
            submitted.append(action)
            code = None
            if action.side != side:
                code = "side_mismatch"
            elif action.action_id not in available[side]:
                code = "action_not_in_valid_actions"
            if code is not None:
                self._error_counts[side] += 1
                decisions.append(ActionDecision(action.action_id, False, code,
                                                "native validation rejected action"))
                events.append(EngineEvent(
                    tick_before, EventType.ACTION_REJECTED, side,
                    action.actor_id, action.target_id,
                    {"action_id": action.action_id, "code": code},
                ))
            else:
                self._action_counts[side] += 1
                decisions.append(ActionDecision(action.action_id, True, "accepted",
                                                "accepted for native resolution"))
                accepted.append(action)

        accepted.sort(key=lambda item: (
            ACTION_PRIORITY[item.action_type], item.side,
            -1 if item.actor_id is None else item.actor_id,
            -1 if item.target_id is None else item.target_id,
            item.action_id))
        for action in accepted:
            if action.action_type is ActionType.DEPLOY:
                self._apply_deploy(action, events)
            elif action.action_type is ActionType.END_DEPLOYMENT:
                world.sides[action.side].deployment_complete = True
                events.append(EngineEvent(tick_before, EventType.DEPLOYMENT_ENDED,
                                          action.side,
                                          data={"action_id": action.action_id}))
            elif action.action_type is ActionType.MOVE:
                self._apply_move(action, events)
            elif action.action_type is ActionType.CHANGE_STATE:
                self._apply_state(action, events)
            elif action.action_type is ActionType.WAIT:
                events.append(EngineEvent(tick_before, EventType.WAITED,
                                          action.side, action.actor_id,
                                          data={"action_id": action.action_id}))

        if (world.stage is Stage.DEPLOYMENT
                and all(item.deployment_complete for item in world.sides.values())):
            world.stage = Stage.COMBAT
            events.append(EngineEvent(tick_before, EventType.COMBAT_STARTED,
                                      data={"completed_sides": list(self.scenario.side_order)}))

        damage: dict[int, float] = defaultdict(float)
        attackers: dict[int, list[int]] = defaultdict(list)
        for action in accepted:
            if action.action_type is not ActionType.ATTACK:
                continue
            attacker = world.operators[action.actor_id]  # validated binding
            target = world.operators[action.target_id]
            events.append(EngineEvent(
                tick_before, EventType.ATTACK_DECLARED, action.side,
                attacker.operator_id, target.operator_id,
                {"action_id": action.action_id,
                 "weapon_id": action.parameters.get("weapon_id")},
            ))
            weapon_id = int(action.parameters["weapon_id"])
            attack_damage = self.ruleset.combat.damage(attacker, weapon_id)
            damage[target.operator_id] += attack_damage
            attackers[target.operator_id].append(attacker.operator_id)
            events.append(EngineEvent(
                tick_before, EventType.TARGET_HIT, action.side,
                attacker.operator_id, target.operator_id,
                {"damage": attack_damage},
            ))
        for target_id in sorted(damage):
            target = world.operators[target_id]
            before, after, destroyed = apply_damage(
                target, damage[target_id], self.ruleset.combat)
            events.append(EngineEvent(
                tick_before, EventType.OPERATOR_DAMAGED, target.side,
                target_id=target_id,
                data={"before": before, "after": after,
                      "damage": damage[target_id],
                      "attacker_ids": sorted(attackers[target_id])},
            ))
            if destroyed:
                events.append(EngineEvent(
                    tick_before, EventType.OPERATOR_DESTROYED, target.side,
                    target_id=target_id,
                    data={"attacker_ids": sorted(attackers[target_id])},
                ))

        world.tick += 1
        if (world.stage is Stage.COMBAT
                and self.ruleset.terminal.mission_score_enabled):
            for event in score_objectives(
                world,
                float_tolerance=self.ruleset.movement.float_tolerance,
            ):
                events.append(EngineEvent(
                    world.tick,
                    EventType.OBJECTIVE_CONTROLLED,
                    event.controlling_side,
                    target_id=event.objective_id,
                    data={"controlling_side": event.controlling_side},
                ))
        terminal = evaluate_terminal(world, self.terminal_rules,
                                     self.scenario.side_order)
        if terminal.terminal or terminal.truncated:
            world.terminal = terminal.terminal
            world.truncated = terminal.truncated
            world.winner = terminal.winner
            world.terminal_reason = terminal.reason
            world.stage = Stage.TERMINAL
            event_type = (EventType.GAME_TERMINATED if terminal.terminal
                          else EventType.GAME_TRUNCATED)
            events.append(EngineEvent(
                world.tick, event_type,
                side=terminal.winner,
                data={"reason": terminal.reason,
                      "winner": terminal.winner},
            ))
        events.sort(key=event_sort_key)
        self._last_events = tuple(events)
        return StepResult(tick_before, world.tick, stage_before, world.stage,
                          tuple(sorted(submitted, key=lambda item: (
                              item.side, item.actor_id is None,
                              -1 if item.actor_id is None else item.actor_id,
                              item.action_id))),
                          tuple(sorted(decisions, key=lambda item: item.action_id)),
                          tuple(events),
                          world.terminal, world.truncated)

    def _step_source(self, actions: Mapping[str, EngineAction | None]
                     | Sequence[EngineAction], *,
                     delta_time_ms: int) -> StepResult:
        world = self._require_world()
        if world.done:
            raise RuntimeError("episode_already_finished")
        if world.stage is not Stage.COMBAT:
            raise RuntimeError("source-compatible actions require combat stage")
        if self._action_router is None or self._movement is None:
            raise RuntimeError("source-compatible runtime is unavailable")
        tick_before, stage_before = world.tick, world.stage
        pre_state = WorldState.from_dict(world.to_dict())
        routed = self._action_router.normalize(
            actions, side_order=self.scenario.side_order,
            start_sequence=world.next_action_sequence)
        world.next_action_sequence += len(routed)
        common_errors: dict[int, str | None] = {}
        seen_actors: set[int] = set()
        for item in routed:
            error = self._action_router.common_error(
                item, pre_state, self.scenario.side_order)
            actor_id = item.action.actor_id
            if error is None and actor_id is not None:
                if actor_id in seen_actors:
                    error = "DUPLICATE_ACTOR_ACTION"
                seen_actors.add(actor_id)
            common_errors[item.action_sequence] = error

        boarding_by_carrier: defaultdict[int, list[RoutedAction]] = defaultdict(list)
        carrier_actions: set[int] = set()
        for item in routed:
            if common_errors[item.action_sequence] is not None:
                continue
            if item.action.action_type is ActionType.BOARD:
                if item.action.target_id is not None:
                    boarding_by_carrier[item.action.target_id].append(item)
            elif (
                item.action.actor_id is not None
                and item.action.action_type is not ActionType.WAIT
            ):
                carrier_actions.add(item.action.actor_id)
        for carrier_id, contenders in boarding_by_carrier.items():
            if len(contenders) > 1:
                for item in contenders:
                    common_errors[item.action_sequence] = (
                        "BOARDING_CARRIER_CONFLICT"
                    )
            elif carrier_id in carrier_actions:
                common_errors[contenders[0].action_sequence] = (
                    "BOARDING_CARRIER_ACTION_CONFLICT"
                )

        accepted: list[RoutedAction] = []
        decisions: list[ActionDecision] = []
        events: list[EngineEvent] = []
        empty_fire_intents: list[EmptyFireIntent] = []
        fire_intents: list[_SourceFireIntent] = []
        guided_fire_intents: list[_SourceGuidedFireIntent] = []
        indirect_fire_intents: list[_SourceIndirectFireIntent] = []
        boarding_intents: list[SourceBoardingIntent] = []
        concealment_intents: list[_SourceConcealmentIntent] = []
        capture_intents: list[SourceCaptureIntent] = []
        for item in routed:
            action = item.action
            error = common_errors[item.action_sequence]
            if error is None:
                if action.action_type is ActionType.SHOOT:
                    if action.target_id is None and "target_hex" in action.parameters:
                        error, empty = self._stage_empty_fire(item, pre_state)
                        if empty is not None:
                            empty_fire_intents.append(empty)
                    else:
                        error, intent = self._stage_source_shoot(item, pre_state, events)
                        if intent is not None:
                            fire_intents.append(intent)
                elif action.action_type is ActionType.FIRE:
                    mode = str(action.parameters.get("fire_mode", "")).upper()
                    if mode == FireMode.GUIDED.value:
                        error, guided = self._stage_source_guided_fire(
                            item,
                            pre_state,
                            events,
                        )
                        if guided is not None:
                            guided_fire_intents.append(guided)
                    elif mode == FireMode.INDIRECT.value:
                        error, indirect = self._stage_source_indirect_fire(
                            item,
                            pre_state,
                        )
                        if indirect is not None:
                            indirect_fire_intents.append(indirect)
                    else:
                        error = "INVALID_FIRE_MODE"
                elif action.action_type in {ActionType.BOARD, ActionType.UNLOAD}:
                    error, boarding = self._stage_source_boarding(
                        item,
                        pre_state,
                    )
                    if boarding is not None:
                        boarding_intents.append(boarding)
                elif action.action_type is ActionType.ENTER_CONCEALMENT:
                    error, concealment = self._stage_source_concealment(
                        item,
                        pre_state,
                    )
                    if concealment is not None:
                        concealment_intents.append(concealment)
                elif action.action_type is ActionType.CAPTURE:
                    error, capture = stage_source_capture(
                        self._capture_service,
                        item,
                        pre_state,
                    )
                    if capture is not None:
                        capture_intents.append(capture)
                else:
                    error = self._dispatch_source_action(item, events)
            if error is None:
                accepted.append(item)
                self._action_counts[action.side] += 1
                decisions.append(ActionDecision(
                    action.action_id, True, "accepted",
                    "accepted by source-compatible action router"))
            else:
                if action.side in self._error_counts:
                    self._error_counts[action.side] += 1
                decisions.append(ActionDecision(
                    action.action_id, False, error,
                    "source-compatible validation rejected action"))
                events.append(self._source_event(
                    EventType.ACTION_REJECTED, action.side, action.actor_id,
                    action.target_id, item.action_sequence,
                    {"action_id": action.action_id, "code": error}))

        advance_empty_fire_exposure(world)
        self._apply_source_concealment_intents(concealment_intents, events)
        self._apply_source_boarding_intents(boarding_intents, events)
        for event in apply_source_capture_intents(
            world, capture_intents, self._capture_policy
        ):
            events.append(self._source_event(
                EventType.OBJECTIVE_CAPTURED,
                event.side,
                event.actor_id,
                event.objective_id,
                event.action_sequence,
                event.data,
            ))
        self._apply_empty_fire_intents(empty_fire_intents, events)
        self._apply_source_fire_intents(
            fire_intents,
            guided_fire_intents,
            events,
        )
        events.extend(self._apply_boarding_carrier_destruction(world))
        self._apply_source_indirect_fire_intents(
            indirect_fire_intents,
            events,
        )
        self._advance_source_operator_time(delta_time_ms, events)
        self._advance_source_boarding_transitions(delta_time_ms, events)
        if self.mounted_position_policy is not None:
            sync_mounted_passenger_positions(world)
        active_delta_by_mission = (
            self._indirect_fire_service.advance_to_resolution(
                world, delta_time_ms,
            )
        )
        self._resolve_active_indirect_fire_missions(events)
        events.extend(self._apply_boarding_carrier_destruction(world))
        expired_mission_ids = (
            self._indirect_fire_service.advance_active_areas(
                world,
                delta_time_ms,
                active_delta_by_mission,
            )
        )
        self._emit_expired_indirect_fire_missions(
            expired_mission_ids,
            events,
        )
        self._refresh_source_observation()
        world.last_actions = [item.action.to_dict() for item in routed]
        world.last_judge_info = [item.to_dict() for item in events
                                 if is_green_judge_event(item.event_type)]
        if self.ruleset.terminal.mission_score_enabled:
            if self._capture_policy.mode == LEGACY_CAPTURE_MODE:
                for event in score_objectives(
                    world,
                    float_tolerance=self.ruleset.movement.float_tolerance,
                ):
                    events.append(EngineEvent(
                        world.tick,
                        EventType.OBJECTIVE_CONTROLLED,
                        event.controlling_side,
                        target_id=event.objective_id,
                        data={"controlling_side": event.controlling_side},
                    ))
            else:
                award_controlled_objective_scores(world)
        terminal = evaluate_terminal(world, self.terminal_rules,
                                     self.scenario.side_order)
        if terminal.terminal or terminal.truncated:
            world.terminal = terminal.terminal
            world.truncated = terminal.truncated
            world.winner = terminal.winner
            world.terminal_reason = terminal.reason
            world.stage = Stage.TERMINAL
            events.append(self._source_event(
                EventType.GAME_TERMINATED if terminal.terminal
                else EventType.GAME_TRUNCATED,
                terminal.winner, None, None, world.next_action_sequence,
                {"reason": terminal.reason, "winner": terminal.winner}))
        events.sort(key=event_sort_key)
        self._last_events = tuple(events)
        return StepResult(
            tick_before, world.tick, stage_before, world.stage,
            tuple(item.action for item in routed), tuple(decisions), tuple(events),
            world.terminal, world.truncated,
            tuple(item.action_sequence for item in routed), delta_time_ms, True)

    def _dispatch_source_action(self, routed: RoutedAction,
                                events: list[EngineEvent]) -> str | None:
        world = self._require_world()
        action, sequence = routed.action, routed.action_sequence
        if action.action_type is ActionType.WAIT:
            events.append(self._source_event(
                EventType.WAITED, action.side, action.actor_id, None, sequence,
                {"action_id": action.action_id}))
            return None
        operator = world.operators[int(action.actor_id)]
        if action.action_type is ActionType.MOVE:
            raw_path = action.parameters.get("path",
                                             action.parameters.get("move_path"))
            if not isinstance(raw_path, (list, tuple)):
                return "PATH_REQUIRED"
            try:
                path = [int(item) for item in raw_path]
                mode = MovementMode(str(action.parameters.get(
                    "movement_mode", operator.movement_mode)).upper())
            except (TypeError, ValueError):
                return "INVALID_MOVEMENT_PARAMETERS"
            legality = self._movement.planner.validate_path(
                operator, path, mode, world)
            if not legality.legal:
                return legality.reason_code
            self._expose_operator(
                operator,
                events,
                action_sequence=sequence,
                action_id=action.action_id,
                reason="MOVE",
            )
            self._movement.start(
                world, operator, path=path, mode=mode,
                action_id=action.action_id, action_sequence=sequence,
                events=events)
            events.extend(
                self._indirect_fire_service.invalidate_active_areas_for_shooter(
                    world,
                    operator.operator_id,
                    action_id=action.action_id,
                    action_sequence=sequence,
                )
            )
            return None
        if action.action_type is ActionType.STOP_MOVE:
            if operator.move_to_stop_remaining_ms > 0:
                return "STOP_ALREADY_IN_PROGRESS"
            if not operator.moving:
                return "ACTOR_NOT_MOVING"
            self._movement.request_stop(
                world, operator, action_id=action.action_id,
                action_sequence=sequence, events=events)
            return None
        if action.action_type is ActionType.SHOOT:
            raise RuntimeError("SHOOT must be resolved through joint staging")
        if action.action_type is ActionType.FIRE:
            raise RuntimeError("FIRE must be resolved through joint staging")
        if action.action_type in {ActionType.BOARD, ActionType.UNLOAD}:
            raise RuntimeError(
                "BOARD/UNLOAD must be resolved through boarding staging"
            )
        if action.action_type is ActionType.CAPTURE:
            raise RuntimeError(
                "CAPTURE must be resolved through joint staging"
            )
        if action.action_type is ActionType.ENTER_CONCEALMENT:
            raise RuntimeError(
                "ENTER_CONCEALMENT must be resolved through posture staging"
            )
        if action.action_type in {ActionType.WEAPON_LOCK, ActionType.WEAPON_UNLOCK,
                                  ActionType.WEAPON_UNFOLD}:
            if action.target_id is not None or action.target_position is not None:
                return "INVALID_WEAPON_POSTURE_TARGET"
            if action.action_type is ActionType.WEAPON_LOCK:
                if set(action.parameters) != {"weapon_id"}:
                    return "WEAPON_ID_REQUIRED"
                weapon_id = action.parameters["weapon_id"]
                if type(weapon_id) is not int or weapon_id not in operator.weapon_ids:
                    return "WEAPON_NOT_CARRIED"
                return self._start_source_transition(
                    operator, "WEAPON_LOCK", sequence, action.action_id, events,
                    weapon_id=weapon_id)
            if action.parameters:
                return "INVALID_WEAPON_POSTURE_PARAMETERS"
            if action.action_type is ActionType.WEAPON_UNLOCK:
                return self._unlock_source_weapon(operator, sequence, action.action_id, events)
            return self._start_source_transition(
                operator, "WEAPON_UNFOLD", sequence, action.action_id, events)
        if action.action_type is ActionType.CHANGE_STATE:
            return self._change_source_state(routed, events)
        return "UNSUPPORTED_ACTION_TYPE"

    def _stage_source_concealment(
        self,
        routed: RoutedAction,
        pre_state: WorldState,
    ) -> tuple[str | None, _SourceConcealmentIntent | None]:
        action = routed.action
        if action.target_id is not None or action.target_position is not None:
            return "INVALID_CONCEALMENT_TARGET", None
        if action.parameters:
            return "INVALID_CONCEALMENT_PARAMETERS", None
        capability = next(
            (
                item
                for item in self._concealment_capabilities_from_pre_state(
                    pre_state,
                    action.side,
                )
                if item.operator_id == action.actor_id
            ),
            None,
        )
        if capability is None:
            return "ACTOR_NOT_FOUND", None
        if not capability.available:
            return capability.reason, None
        return None, _SourceConcealmentIntent(routed, capability)

    def _apply_source_concealment_intents(
        self,
        intents: Sequence[_SourceConcealmentIntent],
        events: list[EngineEvent],
    ) -> None:
        world = self._require_world()
        for intent in sorted(
            intents,
            key=lambda item: item.routed.action_sequence,
        ):
            action = intent.routed.action
            operator = world.operators[intent.capability.operator_id]
            previous = operator.concealment_posture
            if previous is not ConcealmentPosture.EXPOSED:
                raise RuntimeError("CONCEALMENT_PRESTATE_MISMATCH")
            operator.set_concealment_posture(ConcealmentPosture.CONCEALED)
            events.append(self._source_event(
                EventType.UNIT_CONCEALED,
                operator.side,
                operator.operator_id,
                None,
                intent.routed.action_sequence,
                {
                    "action_id": action.action_id,
                    "operator_id": operator.operator_id,
                    "previous_posture": previous.value,
                    "new_posture": ConcealmentPosture.CONCEALED.value,
                },
            ))

    def _expose_operator(
        self,
        operator: OperatorState,
        events: list[EngineEvent],
        *,
        action_sequence: int,
        action_id: str,
        reason: str,
    ) -> None:
        previous = operator.concealment_posture
        if previous is not ConcealmentPosture.CONCEALED:
            return
        operator.set_concealment_posture(ConcealmentPosture.EXPOSED)
        events.append(self._source_event(
            EventType.UNIT_EXPOSED,
            operator.side,
            operator.operator_id,
            None,
            action_sequence,
            {
                "action_id": action_id,
                "operator_id": operator.operator_id,
                "reason": reason,
                "previous_posture": previous.value,
                "new_posture": ConcealmentPosture.EXPOSED.value,
            },
        ))

    def _start_source_transition(self, operator: Any, kind: str,
                                 action_sequence: int, action_id: str,
                                 events: list[EngineEvent], *,
                                 weapon_id: int | None = None) -> str | None:
        if kind == "WEAPON_LOCK" and operator.command_lost:
            return "ACTOR_CONTROL_LOST"
        if not operator.stopped or operator.moving or operator.move_to_stop_remaining_ms > 0:
            return "ACTOR_MUST_BE_STOPPED"
        if operator.transition_kind is not None or operator.transition_remaining_ms > 0:
            return "TRANSITION_IN_PROGRESS"
        if operator.weapon_locked or operator.locked_weapon_id is not None:
            return "WEAPON_ALREADY_LOCKED" if kind == "WEAPON_LOCK" else "WEAPON_UNLOCK_REQUIRED"
        if kind == "WEAPON_UNFOLD" and operator.weapon_unfolded:
            return "WEAPON_ALREADY_UNFOLDED"
        operator.transition_kind = kind
        operator.transition_remaining_ms = int(self._source_policy.transition_ms)
        operator.state = "TRANSITIONING"
        operator.stopped = False
        if kind == "WEAPON_LOCK":
            operator.locked_weapon_id = weapon_id
            operator.weapon_unfolded = False
            event_type = EventType.WEAPON_LOCK_STARTED
        else:
            event_type = EventType.WEAPON_UNFOLD_STARTED
        events.append(self._source_event(
            event_type, operator.side, operator.operator_id, None,
            action_sequence, {"action_id": action_id,
                              "duration_ms": operator.transition_remaining_ms,
                              "transition_kind": kind,
                              "locked_weapon_id": operator.locked_weapon_id}))
        return None

    def _unlock_source_weapon(self, operator: Any, action_sequence: int,
                              action_id: str, events: list[EngineEvent]) -> str | None:
        if operator.command_lost:
            return "ACTOR_CONTROL_LOST"
        if not operator.stopped or operator.moving or operator.move_to_stop_remaining_ms > 0:
            return "ACTOR_MUST_BE_STOPPED"
        if operator.transition_kind is not None or operator.transition_remaining_ms > 0:
            return "TRANSITION_IN_PROGRESS"
        if not operator.weapon_locked and operator.locked_weapon_id is None:
            return "WEAPON_NOT_LOCKED"
        previous = operator.locked_weapon_id
        operator.weapon_locked = False
        operator.locked_weapon_id = None
        events.append(self._source_event(
            EventType.WEAPON_UNLOCKED, operator.side, operator.operator_id, None,
            action_sequence, {"action_id": action_id, "previous_locked_weapon_id": previous}))
        return None

    def _change_source_state(self, routed: RoutedAction,
                             events: list[EngineEvent]) -> str | None:
        if self._source_map is None or self._source_policy is None:
            return "SOURCE_MAP_UNAVAILABLE"
        action, sequence = routed.action, routed.action_sequence
        operator = self._require_world().operators[int(action.actor_id)]
        target = str(action.parameters.get("state", "")).upper()
        if target in {"MARCHING", "NORMAL"}:
            if not operator.stopped or operator.moving:
                return "ACTOR_MUST_BE_STOPPED"
            if operator.transition_kind is not None:
                return "TRANSITION_IN_PROGRESS"
            if target == "MARCHING":
                if operator.operator_type.upper() != "VEHICLE":
                    return "MARCHING_REQUIRES_VEHICLE"
                if not operator.weapon_locked:
                    return "WEAPON_NOT_LOCKED"
                if operator.current_hex is None or not any(
                        int(item) > 0
                        for item in self._source_map.cell(operator.current_hex).roads):
                    return "MARCHING_REQUIRES_ROAD"
                kind = "ENTER_MARCHING"
            else:
                if operator.movement_mode != MovementMode.MARCHING.value:
                    return "ACTOR_NOT_MARCHING"
                kind = "EXIT_MARCHING"
            operator.transition_kind = kind
            operator.transition_remaining_ms = self._source_policy.transition_ms
            operator.state = "TRANSITIONING"
            operator.stopped = False
            events.append(self._source_event(
                EventType.MOVEMENT_MODE_CHANGE_STARTED, operator.side,
                operator.operator_id, None, sequence,
                {"action_id": action.action_id, "target_state": target,
                 "transition_kind": kind,
                 "duration_ms": operator.transition_remaining_ms}))
            return None
        if target in {MovementMode.CHARGE_1.value,
                      MovementMode.CHARGE_2.value}:
            if operator.operator_type.upper() != "PERSONNEL":
                return "CHARGE_REQUIRES_PERSONNEL"
            if operator.fatigue_level >= 2:
                return "FATIGUE_PREVENTS_MOVEMENT"
            if target == MovementMode.CHARGE_2.value and operator.fatigue_level >= 1:
                return "FATIGUE_PREVENTS_CHARGE_2"
            before = operator.movement_mode
            operator.movement_mode = target
            events.append(self._source_event(
                EventType.OPERATOR_STATE_CHANGED, operator.side,
                operator.operator_id, None, sequence,
                {"action_id": action.action_id, "before": before,
                 "after": target}))
            return None
        return "UNSUPPORTED_STATE_CHANGE"

    def _stage_empty_fire(
        self, routed: RoutedAction, pre_state: WorldState,
    ) -> tuple[str | None, EmptyFireIntent | None]:
        action = routed.action
        target_hex = action.parameters.get("target_hex")
        weapon_id = action.parameters.get("weapon_id")
        if action.target_id is not None or action.target_position is not None:
            return "AMBIGUOUS_FIRE_TARGET", None
        if type(target_hex) is not int or target_hex < 0:
            return "TARGET_HEX_INVALID", None
        if type(weapon_id) is not int or weapon_id < 0:
            return "WEAPON_REQUIRED", None
        if self._source_map is None or self._adjudication_tables is None:
            return "SOURCE_MAP_UNAVAILABLE", None
        try:
            self._source_map.cell(target_hex)
        except ValueError:
            return "TARGET_HEX_NOT_FOUND", None
        attacker = pre_state.operators[int(action.actor_id)]
        if attacker.current_hex is None:
            return "CURRENT_HEX_REQUIRED", None
        try:
            weapon = self._source_weapon_profile(weapon_id)
            if self._loitering_attack_policy_for(attacker, weapon_id, weapon) is not None:
                return "EMPTY_FIRE_RESOURCE_POLICY_UNSUPPORTED", None
        except ValueError as exc:
            return str(exc), None
        ammunition = self._ammunition(attacker).get(weapon.ammunition_key, 0)
        error = empty_fire_readiness_error(attacker, weapon, ammunition)
        if error is not None:
            return error, None
        cooldown = (self._adjudication_tables.timing("direct_fire_cooldown_seconds")
                    if weapon.cooldown_ticks is None else weapon.cooldown_ticks)
        return None, EmptyFireIntent(routed, target_hex, weapon, ammunition,
                                     int(cooldown) * 1000)

    def _apply_empty_fire_intents(
        self, intents: Sequence[EmptyFireIntent], events: list[EngineEvent],
    ) -> None:
        world = self._require_world()
        for intent in sorted(intents, key=lambda item: item.routed.action_sequence):
            action, sequence = intent.routed.action, intent.routed.action_sequence
            attacker = world.operators[int(action.actor_id)]
            ammunition = self._ammunition(attacker)
            key = intent.weapon.ammunition_key
            if ammunition.get(key, 0) != intent.ammunition_before:
                raise RuntimeError("EMPTY_FIRE_RESOURCE_PRESTATE_MISMATCH")
            ammunition[key] = intent.ammunition_before - 1
            attacker.raw_attributes["ammunition"] = ammunition
            source = attacker.raw_attributes.get("source_frame")
            if isinstance(source, dict):
                source["remain_bullet_nums"] = dict(ammunition)
            attacker.weapon_cooldown_remaining_ms = intent.cooldown_ms
            mark_empty_fire_exposure(attacker)
            common = {"action_id": action.action_id, "weapon_id": intent.weapon.weapon_id,
                      "target_hex": intent.target_hex, "empty_fire": True,
                      "no_effect": True}
            for kind, extra in (
                (EventType.DIRECT_FIRE_REQUESTED, {}),
                (EventType.DIRECT_FIRE_VALIDATED, {"reason_code": "EMPTY_FIRE_NO_EFFECT"}),
                (EventType.AMMUNITION_CONSUMED, {"ammunition_key": key,
                 "before": intent.ammunition_before, "after": ammunition[key]}),
                (EventType.WEAPON_COOLDOWN_STARTED, {"after": intent.cooldown_ms // 1000}),
                (EventType.UNIT_EXPOSED, {"reason": "EMPTY_FIRE",
                 "duration_frames": EMPTY_FIRE_EXPOSURE_FRAMES}),
            ):
                events.append(self._source_event(kind, action.side, action.actor_id,
                                                None, sequence, {**common, **extra}))

    def _stage_source_shoot(
        self,
        routed: RoutedAction,
        pre_state: WorldState,
        events: list[EngineEvent],
    ) -> tuple[str | None, _SourceFireIntent | None]:
        action, sequence = routed.action, routed.action_sequence
        if action.target_position is not None or "target_hex" in action.parameters:
            return "AMBIGUOUS_FIRE_TARGET", None
        if action.target_id is None or action.target_id not in pre_state.operators:
            return "TARGET_NOT_FOUND", None
        try:
            fire_intent = self._action_router.route_fire(action)
            if not isinstance(fire_intent, FireIntent):
                raise RuntimeError("SHOOT unexpectedly left DIRECT route")
            if not isinstance(fire_intent.target, UnitTarget):
                raise ValueError("DIRECT_FIRE_REQUIRES_UNIT_TARGET")
            attacker = pre_state.operators[fire_intent.shooter_id]
            target = pre_state.operators[fire_intent.target.unit_id]
            request = self._direct_fire_request(
                action, attacker, target, world=pre_state,
                weapon_id=fire_intent.weapon_id)
        except ValueError as exc:
            return str(exc), None
        if self._rng is None or self._adjudication_tables is None:
            raise RuntimeError("source direct-fire runtime is unavailable")
        resolution = DirectFireAdjudicator(
            self._adjudication_tables, DiceRoller(self._rng)).stage(request)
        for source_event in resolution.events:
            events.append(self._annotate_source_event(source_event, sequence))
        if resolution.outcome is None:
            return (resolution.error or resolution.legality.reason_code), None
        loitering_policy = self._loitering_attack_policy_for(
            attacker,
            int(fire_intent.weapon_id),
            request.weapon,
        )
        return None, _SourceFireIntent(
            routed, fire_intent, request, resolution, loitering_policy)

    def _stage_source_boarding(
        self,
        routed: RoutedAction,
        pre_state: WorldState,
    ) -> tuple[str | None, SourceBoardingIntent | None]:
        """Stage BOARD/UNLOAD against the sealed tick pre-state."""

        action = routed.action
        if action.target_id is None:
            return "TARGET_REQUIRED", None
        profile_id = action.parameters.get("profile_id")
        authority_version = action.parameters.get("authority_version")
        authority_hash = action.parameters.get("authority_hash")
        try:
            if action.action_type is ActionType.BOARD:
                capability = BoardCapability(
                    passenger_id=int(action.actor_id),
                    carrier_id=int(action.target_id),
                    profile_id=str(profile_id),
                    authority_version=str(authority_version),
                    authority_hash=str(authority_hash),
                )
                validation = self._build_board_request_from_pre_state(
                    capability,
                    pre_state,
                    action_id=action.action_id,
                )
                if not validation.valid or validation.request is None:
                    return validation.reason, None
                return None, SourceBoardingIntent(
                    routed,
                    BoardingTransitionType.BOARD,
                    validation.request,
                )
            if action.action_type is ActionType.UNLOAD:
                capability = UnloadCapability(
                    carrier_id=int(action.actor_id),
                    passenger_id=int(action.target_id),
                    profile_id=str(profile_id),
                    authority_version=str(authority_version),
                    authority_hash=str(authority_hash),
                )
                validation = self._build_unload_request_from_pre_state(
                    capability,
                    pre_state,
                    action_id=action.action_id,
                )
                if not validation.valid or validation.request is None:
                    return validation.reason, None
                return None, SourceBoardingIntent(
                    routed,
                    BoardingTransitionType.UNLOAD,
                    validation.request,
                )
        except (TypeError, ValueError):
            return "INVALID_BOARDING_REQUEST", None
        return "UNSUPPORTED_ACTION_TYPE", None

    def _apply_source_boarding_intents(
        self,
        intents: Sequence[SourceBoardingIntent],
        events: list[EngineEvent],
    ) -> None:
        world = self._require_world()
        for event in apply_source_boarding_intents(
            world,
            intents,
            self._boarding_authority,
        ):
            events.append(self._source_event(
                event.event_type,
                event.side,
                event.actor_id,
                event.target_id,
                event.action_sequence,
                event.data,
            ))

    def _advance_source_boarding_transitions(
        self,
        delta_time_ms: int,
        events: list[EngineEvent],
    ) -> None:
        world = self._require_world()
        for event in advance_source_boarding_transitions(
            world,
            delta_time_ms,
        ):
            events.append(self._source_event(
                event.event_type,
                event.side,
                event.actor_id,
                event.target_id,
                event.action_sequence,
                event.data,
            ))

    def _advance_source_operator_time(
        self,
        delta_time_ms: int,
        events: list[EngineEvent],
    ) -> None:
        """Advance movement and finite operator lifetimes on one time axis.

        Splitting at the nearest lifecycle boundary makes expiry independent of
        env-step size while leaving Movement segment semantics unchanged.
        """

        if self._movement is None:
            raise RuntimeError("source movement runtime is unavailable")
        world = self._require_world()
        remaining_delta = int(delta_time_ms)
        self._expire_source_operator_lifecycles(events)
        while remaining_delta > 0:
            finite_boundaries = [
                int(operator.lifecycle_remaining_ms)
                for operator in world.operators.values()
                if operator.alive
                and operator.lifecycle_remaining_ms is not None
                and operator.lifecycle_remaining_ms > 0
            ]
            duration = min([remaining_delta, *finite_boundaries])
            self._movement.advance(world, duration, events)
            for operator in sorted(
                world.operators.values(), key=lambda item: item.operator_id
            ):
                if not operator.alive or operator.lifecycle_remaining_ms is None:
                    continue
                operator.lifecycle_remaining_ms = max(
                    0,
                    operator.lifecycle_remaining_ms - duration,
                )
            remaining_delta -= duration
            self._expire_source_operator_lifecycles(events)

    def _expire_source_operator_lifecycles(
        self,
        events: list[EngineEvent],
    ) -> None:
        world = self._require_world()
        for operator in sorted(
            world.operators.values(), key=lambda item: item.operator_id
        ):
            if (
                not operator.alive
                or operator.lifecycle_profile_id is None
                or operator.lifecycle_remaining_ms != 0
            ):
                continue
            profile = self._operator_lifecycle_authority.profile(
                operator.lifecycle_profile_id
            )
            if profile is None:
                raise RuntimeError("OPERATOR_LIFECYCLE_PROFILE_UNAVAILABLE")
            if profile.expiry_behavior is not LifecycleExpiryBehavior.DESTROY_OPERATOR:
                raise RuntimeError("OPERATOR_LIFECYCLE_EXPIRY_UNSUPPORTED")
            before = int(operator.blood)
            destroy_source_operator(
                world,
                operator,
                consume_lifecycle=True,
            )
            events.append(self._source_event(
                EventType.UNIT_DESTROYED,
                operator.side,
                operator.operator_id,
                operator.operator_id,
                -1,
                {
                    "target_blood_before": before,
                    "target_blood_after": 0,
                    "losses": before,
                    "reason_code": "LIFECYCLE_EXPIRED",
                    "lifecycle_profile_id": profile.profile_id,
                    "lifecycle_authority_version": (
                        self._operator_lifecycle_authority.authority_version
                    ),
                    "lifecycle_authority_hash": (
                        self._operator_lifecycle_authority.content_hash
                    ),
                },
            ))

    @staticmethod
    def _mark_source_suppression_movement_task(
        operator: OperatorState,
    ) -> None:
        """Bind a moving personnel task to the frozen suppression resume rule."""

        if (
            operator.operator_type.upper() == "PERSONNEL"
            and operator.moving
            and not operator.movement_stop_requested
        ):
            operator.movement_task_pause_reason = (
                MovementTaskPauseReason.SUPPRESSION
            )

    def _validate_operator_lifecycle_snapshot(
        self,
        world: WorldState,
    ) -> None:
        """Reject unknown or inconsistent finite-lifecycle authority."""

        for operator in sorted(
            world.operators.values(), key=lambda item: item.operator_id
        ):
            profile_id = operator.lifecycle_profile_id
            remaining = operator.lifecycle_remaining_ms
            if profile_id is None and remaining is None:
                continue
            if profile_id is None or remaining is None:
                raise ValueError("OPERATOR_LIFECYCLE_CONTRACT_INCOMPLETE")
            profile = self._operator_lifecycle_authority.profile(profile_id)
            if profile is None:
                raise ValueError("OPERATOR_LIFECYCLE_PROFILE_UNKNOWN")
            if operator.operator_type.upper() != profile.operator_domain:
                raise ValueError("OPERATOR_LIFECYCLE_DOMAIN_MISMATCH")
            if remaining > profile.maximum_lifetime_ms:
                raise ValueError("OPERATOR_LIFECYCLE_DURATION_EXCEEDS_AUTHORITY")
            if operator.alive and remaining == 0:
                raise ValueError("LIVING_OPERATOR_LIFECYCLE_ALREADY_EXPIRED")
            compatibility_identity = str(
                operator.raw_attributes.get("canonical_type", "")
            ).upper()
            if (
                compatibility_identity
                and compatibility_identity != profile.canonical_identity
            ):
                raise ValueError("OPERATOR_LIFECYCLE_IDENTITY_MISMATCH")

    def _validate_loitering_attack_snapshot(self, world: WorldState) -> None:
        """Fail closed when an operator advertises loitering attack authority."""

        for operator in sorted(
            world.operators.values(), key=lambda item: item.operator_id
        ):
            for weapon_id in sorted(operator.weapon_ids):
                policy = self._loitering_munition_attack_authority.policy_for(
                    lifecycle_profile_id=operator.lifecycle_profile_id,
                    weapon_id=weapon_id,
                )
                if policy is None:
                    continue
                lifecycle = self._operator_lifecycle_authority.profile(
                    operator.lifecycle_profile_id or ""
                )
                if (
                    lifecycle is None
                    or lifecycle.canonical_identity != policy.canonical_identity
                    or lifecycle.operator_domain != operator.operator_type.upper()
                ):
                    raise ValueError("LOITERING_ATTACK_IDENTITY_MISMATCH")

    @staticmethod
    def _apply_boarding_carrier_destruction(
        working: WorldState,
    ) -> tuple[EngineEvent, ...]:
        """Apply the minimal mounted-passenger destruction policy.

        Ordinary carrier damage has no passenger effect.  Only destruction of
        an explicitly related carrier cascades to its mounted passenger.  The
        caller supplies the detached working world of an atomic transaction.
        """

        events: list[EngineEvent] = []
        for carrier in sorted(
            working.operators.values(), key=lambda item: item.operator_id
        ):
            if carrier.alive or carrier.passenger_id is None:
                continue
            passenger = working.operators.get(carrier.passenger_id)
            if passenger is None or passenger.mounted_on != carrier.operator_id:
                raise RuntimeError("BOARDING_CARRIER_RELATION_MISMATCH")
            if not passenger.alive:
                continue
            passenger_before = int(passenger.blood)
            destroy_source_operator(working, passenger)
            passenger.deployed = False
            passenger.current_hex = None
            passenger.state = "destroyed"
            passenger.suppression_remaining_ms = 0
            sequence = working.next_event_sequence
            working.next_event_sequence += 1
            events.append(EngineEvent(
                tick=working.tick,
                event_type=EventType.UNIT_DESTROYED,
                side=passenger.side,
                actor_id=carrier.operator_id,
                target_id=passenger.operator_id,
                data={
                    "associated_vehicle_id": carrier.operator_id,
                    "passenger_id": passenger.operator_id,
                    "target_blood_before": passenger_before,
                    "target_blood_after": 0,
                    "losses": passenger_before,
                    "boarding_cascade": True,
                    "reason_code": "CARRIER_DESTROYED",
                    "event_time_ms": working.elapsed_time_ms,
                    "action_sequence": -1,
                    "event_sequence": sequence,
                },
            ))
        return tuple(events)

    def _stage_source_guided_fire(
        self,
        routed: RoutedAction,
        pre_state: WorldState,
        events: list[EngineEvent],
    ) -> tuple[str | None, _SourceGuidedFireIntent | None]:
        """Stage one replayable GUIDED action against the sealed pre-state."""

        if self._action_router is None or self._rng is None:
            raise RuntimeError("source guided-fire runtime is unavailable")
        action = routed.action
        rng_before = self._rng.snapshot()
        detached_rng = NativeRNG.restore(rng_before)
        try:
            staged = self._action_router.route_fire(
                action,
                guided_stage=lambda intent: (
                    self._stage_guided_fire_intent_against_pre_state(
                        intent,
                        pre_state,
                        detached_rng,
                        action_id=action.action_id,
                    )
                ),
            )
            if not isinstance(staged, GuidedFireStageResult):
                raise RuntimeError("FIRE unexpectedly left GUIDED route")
            if staged.resolution.error is not None:
                return staged.resolution.error, None
            commit_effect = GuidedFireCommitEffect.from_stage(staged)
            direct_request = self._guided_direct_fire_request(
                staged.request,
                pre_state,
                action_id=action.action_id,
            )
        except ValueError as exc:
            return str(exc), None

        transition = commit_effect.rng_transition
        if (
            transition.seed != int(rng_before["seed"])
            or transition.state_before != int(rng_before["state"])
            or transition.draws_before != int(rng_before["draws"])
            or detached_rng.snapshot() != {
                "seed": transition.seed,
                "state": transition.state_after,
                "draws": transition.draws_after,
            }
        ):
            raise RuntimeError("GUIDED_FIRE_RNG_TRANSITION_MISMATCH")
        # This is the tick's working RNG, not the authoritative pre-tick RNG.
        # The outer transaction publishes it only if the whole tick commits.
        self._rng = NativeRNG.restore(detached_rng.snapshot())
        for evidence in commit_effect.evidence:
            tagged = EngineEvent(
                tick=evidence.tick,
                event_type=evidence.event_type,
                side=evidence.side,
                actor_id=evidence.actor_id,
                target_id=evidence.target_id,
                data={
                    **evidence.data,
                    "fire_mode": FireMode.GUIDED.value,
                    "guider_id": commit_effect.guider_id,
                },
            )
            events.append(self._annotate_source_event(
                tagged,
                routed.action_sequence,
            ))
        return None, _SourceGuidedFireIntent(
            routed=routed,
            contract=self._action_router.fire_intent(action),
            stage=staged,
            commit_effect=commit_effect,
            direct_request=direct_request,
        )

    def _stage_source_indirect_fire(
        self,
        routed: RoutedAction,
        pre_state: WorldState,
    ) -> tuple[str | None, _SourceIndirectFireIntent | None]:
        """Bind an explicit FIRE/INDIRECT action to the sealed pre-state."""

        if self._action_router is None:
            raise RuntimeError("source indirect-fire runtime is unavailable")
        action = routed.action
        try:
            transport = self._action_router.fire_intent(action)
            if (
                transport.mode is not FireMode.INDIRECT
                or not isinstance(transport.target, HexTarget)
            ):
                raise ValueError("INDIRECT_FIRE_REQUIRES_HEX_TARGET")
            shooter = pre_state.operators.get(transport.shooter_id)
            if shooter is None:
                raise ValueError("INDIRECT_FIRE_SHOOTER_NOT_FOUND")
            capability = next((
                value
                for value in self._indirect_fire_capabilities_from_pre_state(
                    pre_state,
                    shooter.side,
                )
                if value.shooter_id == transport.shooter_id
                and value.weapon_id == transport.weapon_id
            ), None)
            if capability is None or (
                capability.status is not IndirectFireCapabilityStatus.AVAILABLE
            ):
                if capability is not None and "indirect_fire_ammunition_unavailable" in capability.blockers:
                    raise ValueError("INDIRECT_FIRE_AMMUNITION_UNAVAILABLE")
                if capability is not None and (
                    "indirect_fire_planned_cooldown_remaining"
                    in capability.blockers
                ):
                    raise ValueError(
                        "INDIRECT_FIRE_PLANNED_COOLDOWN_ACTIVE"
                    )
                raise ValueError("CAPABILITY_BLOCKED")
            descriptor = self._indirect_fire_authority_descriptors.get(
                capability.profile_id
            )
            if descriptor is None:
                raise ValueError("CAPABILITY_BLOCKED")
            identity = load_legacy_indirect_fire_replay_identity()
            declared = {
                "indirect_fire_lifecycle_version": action.parameters.get(
                    "indirect_fire_lifecycle_version"
                ),
                "indirect_fire_authority_version": action.parameters.get(
                    "indirect_fire_authority_version"
                ),
                "indirect_fire_authority_hash": action.parameters.get(
                    "indirect_fire_authority_hash"
                ),
                "execution_policy_hash": action.parameters.get(
                    "execution_policy_hash"
                ),
                "ammo_mapping_hash": action.parameters.get(
                    "ammo_mapping_hash"
                ),
                "observation_policy_hash": action.parameters.get(
                    "observation_policy_hash"
                ),
                "damage_policy_hash": action.parameters.get(
                    "damage_policy_hash"
                ),
            }
            if declared["execution_policy_hash"] != identity[
                "execution_policy_hash"
            ] or action.parameters.get("execution_policy_id") != (
                self._indirect_fire_execution_policy.policy_id
            ):
                raise ValueError("POLICY_HASH_MISMATCH")
            if declared != identity:
                raise ValueError("INDIRECT_FIRE_AUTHORITY_MISMATCH")
            validation = self._build_indirect_fire_plan_request_from_pre_state(
                capability,
                transport.target.hex_id,
                pre_state,
                action_id=action.action_id,
            )
            if not validation.valid or validation.request is None:
                raise ValueError(validation.reason)
            request = validation.request
            mission = self._build_indirect_fire_mission_from_pre_state(
                request,
                pre_state,
                action_stream_bound=True,
            )
            self._validate_indirect_damage_target(
                pre_state, shooter, transport.target.hex_id, action.target_id,
            )
            in_range = capability.range_policy is IndirectRangePolicy.SCENARIO_DEFINED_GLOBAL_HEX
            if not in_range and capability.minimum_range is not None and capability.maximum_range is not None:
                distance = hex_distance(shooter.current_hex, transport.target.hex_id)
                in_range = capability.minimum_range <= distance <= capability.maximum_range
            mission = replace(
                mission, damage_target_id=action.target_id,
                damage_allowed=action.target_id is not None and in_range,
            )
            contract = _build_validated_indirect_fire_intent(
                tick=pre_state.tick,
                shooter_id=transport.shooter_id,
                weapon_id=transport.weapon_id,
                target=transport.target,
                execution_policy_id=request.execution_policy_id,
                execution_policy_hash=request.execution_policy_hash,
                authority_version=descriptor.authority_version,
                authority_hash=identity["indirect_fire_authority_hash"],
                ammo_mapping_hash=descriptor.ammo_mapping_hash,
                observation_policy_hash=descriptor.observation_policy_hash,
                damage_policy_hash=descriptor.damage_policy_hash,
            )
        except ValueError as exc:
            return str(exc), None
        return None, _SourceIndirectFireIntent(
            routed=routed,
            contract=contract,
            request=request,
            mission=mission,
        )

    def _apply_source_indirect_fire_intents(
        self,
        intents: Sequence[_SourceIndirectFireIntent],
        events: list[EngineEvent],
    ) -> None:
        world = self._require_world()
        for intent in sorted(
            intents,
            key=lambda value: value.routed.action_sequence,
        ):
            event = self._apply_indirect_fire_mission_to_working_state(
                world,
                intent.request,
                intent.mission,
                action_sequence=intent.routed.action_sequence,
            )
            events.append(event)
            if not intent.mission.damage_allowed:
                shooter = world.operators[intent.mission.shooter_id]
                mark_empty_fire_exposure(shooter)
                events.append(self._indirect_fire_service.mission_event(
                    world, EventType.UNIT_EXPOSED, intent.mission,
                    {"reason": "EMPTY_FIRE", "duration_frames": EMPTY_FIRE_EXPOSURE_FRAMES},
                    action_sequence=intent.routed.action_sequence,
                ))

    def _resolve_active_indirect_fire_missions(
        self,
        events: list[EngineEvent],
    ) -> None:
        world = self._require_world()
        active = tuple(sorted((
            mission
            for mission in world.indirect_fire_missions
            if mission.phase is IndirectFireMissionState.ACTIVE
            and mission.action_stream_bound
        ), key=fire_mission_sort_key))
        for mission in active:
            if not mission.damage_allowed:
                resolved = replace(
                    mission, phase=IndirectFireMissionState.RESOLVED,
                    active_remaining_seconds=0, active_elapsed_remainder_ms=0,
                    cancelable=False,
                )
                world.indirect_fire_missions = tuple(
                    resolved if value.mission_id == mission.mission_id else value
                    for value in world.indirect_fire_missions
                )
                events.append(self._indirect_fire_service.mission_event(
                    world, EventType.INDIRECT_FIRE_RESOLVED, resolved,
                    {"outcome": "EMPTY_FIRE" if mission.damage_target_id is None else "OUT_OF_RANGE_ZERO_DAMAGE",
                     "planned_hex": mission.planned_hex, "affected_units": []},
                ))
                continue
            if mission.impact_hex is None:
                impact = self.resolve_indirect_fire_impact(mission.mission_id)
                if not impact.valid:
                    raise RuntimeError(impact.reason)
                if impact.miss_resolution is not None:
                    events.append(self._apply_indirect_fire_miss(
                        world,
                        impact.miss_resolution,
                    ))
                    self._rng = NativeRNG.restore(
                        impact.miss_resolution.rng_transition.after.to_record()
                    )
                    continue
                if impact.resolution is None:
                    raise RuntimeError("INDIRECT_FIRE_IMPACT_RESULT_MISSING")
                resolution = impact.resolution
                initialize_area = True
            else:
                current_ids = self._indirect_fire_service.mission_target_ids(
                    world,
                    mission.impact_hex,
                    mission,
                    excluded_operator_ids=mission.affected_operator_ids,
                )
                if not current_ids:
                    continue
                resolution = self._active_indirect_fire_resolution(
                    mission,
                    current_ids,
                )
                initialize_area = False
            effects = self.resolve_indirect_fire_effects(
                resolution,
                _affected_operator_ids=resolution.affected_operator_ids,
            )
            if (
                effects.status is not IndirectFireEffectStatus.RESOLVED
                or effects.effect_bundle is None
            ):
                raise RuntimeError(
                    "INDIRECT_FIRE_EFFECT_UNRESOLVED:"
                    + ",".join(effects.blockers)
                )
            events.extend(
                self._apply_indirect_fire_area_bundle(
                    world,
                    effects.effect_bundle,
                    initialize_area=initialize_area,
                )
            )
            self._rng = NativeRNG.restore(
                effects.effect_bundle.rng_transition.after.to_record()
            )

    def _apply_indirect_fire_miss(
        self,
        working: WorldState,
        miss: IndirectFireMissResolution,
    ) -> EngineEvent:
        """Commit one sampled out-of-map miss to the detached working tick."""

        if self._rng is None:
            raise RuntimeError("INDIRECT_FIRE_IMPACT_RUNTIME_UNAVAILABLE")
        return self._indirect_fire_service.apply_miss(
            working,
            self._rng.snapshot(),
            miss,
        )

    def _active_indirect_fire_resolution(
        self,
        mission: FireMission,
        operator_ids: tuple[int, ...],
    ) -> IndirectFireResolution:
        """Build no-reroll impact evidence for first-time ACTIVE entrants."""

        world = self._require_world()
        if self._rng is None:
            raise RuntimeError("INDIRECT_FIRE_IMPACT_RUNTIME_UNAVAILABLE")
        return self._indirect_fire_service.active_resolution(
            world,
            self._rng.snapshot(),
            mission,
            operator_ids,
            spotter_evidence=self._indirect_fire_spotter_evidence,
        )

    def _emit_expired_indirect_fire_missions(
        self,
        mission_ids: Sequence[str],
        events: list[EngineEvent],
    ) -> None:
        world = self._require_world()
        events.extend(
            self._indirect_fire_service.expired_events(world, mission_ids)
        )

    def _apply_source_fire_intents(
        self,
        intents: Sequence[_SourceFireIntent],
        guided_intents: Sequence[_SourceGuidedFireIntent],
        events: list[EngineEvent],
    ) -> None:
        """Apply a sealed batch using the configured aggregate policy."""
        if not intents and not guided_intents:
            return
        if (self._adjudication_tables is None or self._source_policy is None
                or self._rng is None):
            raise RuntimeError("source direct-fire runtime is unavailable")
        world = self._require_world()

        all_intents: list[_SourceFireIntent | _SourceGuidedFireIntent] = sorted(
            [*intents, *guided_intents],
            key=lambda item: item.routed.action_sequence,
        )

        # Only the firing platform exposes itself.  A GUIDED guider supplies
        # target acquisition evidence but does not fire and therefore keeps
        # its current posture.  All intents were accepted against one sealed
        # pre-state before this working-state mutation occurs.
        guided_sequences = {
            item.routed.action_sequence for item in guided_intents
        }
        for intent in all_intents:
            shooter = world.operators[intent.contract.shooter_id]
            request = _source_fire_direct_request(intent)
            if (request.permits_out_of_range_attempt
                    and request.distance > request.weapon.maximum_range):
                mark_empty_fire_exposure(shooter)
                events.append(self._source_event(
                    EventType.UNIT_EXPOSED, shooter.side, shooter.operator_id,
                    None, intent.routed.action_sequence,
                    {"action_id": intent.routed.action.action_id,
                     "reason": "OUT_OF_RANGE_EMPTY_FIRE",
                     "duration_frames": EMPTY_FIRE_EXPOSURE_FRAMES},
                ))
                continue
            self._expose_operator(
                shooter,
                events,
                action_sequence=intent.routed.action_sequence,
                action_id=intent.routed.action.action_id,
                reason=(
                    "GUIDED_FIRE_SHOOTER"
                    if intent.routed.action_sequence in guided_sequences
                    else "DIRECT_FIRE"
                ),
            )

        # Every accepted shooter was eligible in the same pre-state. Resource
        # deltas are therefore aggregated rather than sequentially applying
        # multiple independently staged ``before -> after`` records.
        resource_deltas: defaultdict[
            tuple[int, str], list[tuple[int, int, int]]
        ] = defaultdict(list)
        cooldowns: defaultdict[int, list[int]] = defaultdict(list)
        recovery_updates: defaultdict[int, list[int]] = defaultdict(list)
        for intent in intents:
            if intent.loitering_attack_policy is not None:
                if (
                    intent.request.resource_policy
                    is not DirectFireResourcePolicy.SELF_AS_MUNITION
                    or intent.request.weapon.ammunition_key is not None
                    or intent.request.ammunition_before is not None
                    or intent.resolution.ammunition_after is not None
                ):
                    raise RuntimeError("LOITERING_ATTACK_RESOURCE_POLICY_MISMATCH")
                continue
            ammunition_key = intent.request.weapon.ammunition_key
            if ammunition_key is None or intent.request.ammunition_before is None:
                raise RuntimeError("DIRECT_FIRE_AMMUNITION_CONTRACT_INCOMPLETE")
            if intent.resolution.ammunition_after is None:
                raise RuntimeError("DIRECT_FIRE_AMMUNITION_EFFECT_MISSING")
            resource_deltas[
                (
                    intent.contract.shooter_id,
                    ammunition_key,
                )
            ].append((
                intent.request.ammunition_before,
                intent.resolution.ammunition_after,
                intent.routed.action_sequence,
            ))
            cooldowns[intent.contract.shooter_id].append(
                intent.resolution.cooldown_after * 1000
            )
        for intent in guided_intents:
            effect = intent.commit_effect
            resource_deltas[
                (effect.shooter_id, effect.ammunition_key)
            ].append((
                effect.ammunition_before,
                effect.ammunition_after,
                intent.routed.action_sequence,
            ))
            cooldowns[effect.shooter_id].append(effect.cooldown_after_ms)
            for update in effect.recovery_updates:
                recovery_updates[update.operator_id].append(
                    update.recovery_remaining_ms
                )

        for (shooter_id, ammunition_key), deltas in sorted(
            resource_deltas.items()
        ):
            attacker = world.operators[shooter_id]
            ammunition = self._ammunition(attacker)
            before_values = {before for before, _, _ in deltas}
            current = ammunition.get(ammunition_key, 0)
            if before_values != {current}:
                raise RuntimeError("FIRE_RESOURCE_PRESTATE_MISMATCH")
            consumed = sum(before - after for before, after, _ in deltas)
            if consumed < 0 or consumed > current:
                raise RuntimeError("FIRE_AGGREGATE_AMMUNITION_UNAVAILABLE")
            ammunition[ammunition_key] = current - consumed
            attacker.raw_attributes["ammunition"] = ammunition
            source = attacker.raw_attributes.get("source_frame")
            if isinstance(source, dict):
                source["remain_bullet_nums"] = dict(ammunition)
        for shooter_id, values in sorted(cooldowns.items()):
            world.operators[shooter_id].weapon_cooldown_remaining_ms = max(
                values
            )
        for operator_id, values in sorted(recovery_updates.items()):
            world.operators[operator_id].guided_fire_recovery_remaining_ms = max(
                values
            )

        by_target: defaultdict[
            int, list[_SourceFireIntent | _SourceGuidedFireIntent]
        ] = defaultdict(list)
        for intent in all_intents:
            request = _source_fire_direct_request(intent)
            if (request.permits_out_of_range_attempt
                    and request.distance > request.weapon.maximum_range):
                # Resources/exposure were applied above. A wasted shot must
                # not touch its target, even to refresh existing suppression.
                continue
            target = intent.contract.target
            if not isinstance(target, UnitTarget):
                raise RuntimeError("fire staging requires a unit target")
            by_target[target.unit_id].append(intent)

        adjudicator = DirectFireAdjudicator(
            self._adjudication_tables, DiceRoller(self._rng))
        destruction_causes: defaultdict[
            int, list[tuple[str, EngineEvent]]
        ] = defaultdict(list)
        self_consuming_operator_ids = {
            intent.contract.shooter_id
            for intent in intents
            if intent.loitering_attack_policy is not None
        }
        for target_id in sorted(by_target):
            target_intents = sorted(
                by_target[target_id],
                key=lambda item: item.routed.action_sequence,
            )
            target = world.operators[target_id]
            before = int(target.blood)
            requested_losses = sum(
                _source_fire_requested_losses(intent)
                for intent in target_intents
            )
            losses = min(before, requested_losses)
            after = max(0, before - losses)
            suppression = any(
                _source_fire_suppression(intent)
                for intent in target_intents
            )
            target.blood = after
            target.health = target.max_health * after / int(target.max_blood)
            adjudication_ids = [
                _source_fire_adjudication_id(intent)
                for intent in target_intents
            ]
            primary = target_intents[0]
            primary_request = _source_fire_direct_request(primary)
            primary_adjudication_id = _source_fire_adjudication_id(primary)
            if losses:
                effect = adjudicator._event(
                    primary_request, EventType.LOSSES_APPLIED, {
                        "target_blood_before": before,
                        "target_blood_after": after,
                        "losses": losses,
                        "aggregate_requested_losses": requested_losses,
                        "adjudication_id": primary_adjudication_id,
                        "aggregate_adjudication_ids": adjudication_ids,
                        "resolution_policy": "aggregate_then_apply",
                    })
                effect = _tag_guided_aggregate_event(effect, primary)
                events.append(self._annotate_source_event(
                    effect, primary.routed.action_sequence))
            if suppression and after > 0:
                target.state = "SUPPRESSED"
                duration = self._adjudication_tables.document[
                    "runtime_rules"]["suppression"][
                        "effective_duration_seconds"]
                target.raw_attributes["suppression_remaining_seconds"] = int(
                    duration)
                effect = adjudicator._event(
                    primary_request, EventType.SUPPRESSION_APPLIED, {
                        "target_blood_after": after,
                        "duration_seconds": int(duration),
                        "timer_policy": "RESET_TO_FULL_DURATION",
                        "adjudication_id": primary_adjudication_id,
                        "aggregate_adjudication_ids": adjudication_ids,
                        "resolution_policy": "aggregate_then_apply",
                    })
                effect = _tag_guided_aggregate_event(effect, primary)
                events.append(self._annotate_source_event(
                    effect, primary.routed.action_sequence))
            if after == 0:
                # Preserve the established single-cause direct-fire state
                # transition and therefore its historical World/replay hash.
                # Commit-level cause aggregation below owns only destruction
                # fact deduplication and any additional self-as-ammunition
                # cleanup required for the same operator.
                if target_id not in self_consuming_operator_ids:
                    destroy_source_operator(world, target)
                effect = adjudicator._event(
                    primary_request, EventType.UNIT_DESTROYED, {
                        "target_blood_after": 0,
                        "adjudication_id": primary_adjudication_id,
                        "aggregate_adjudication_ids": adjudication_ids,
                        "resolution_policy": "aggregate_then_apply",
                    })
                effect = _tag_guided_aggregate_event(effect, primary)
                annotated_effect = self._annotate_source_event(
                    effect,
                    primary.routed.action_sequence,
                )
                # Keep the established insertion position for ordinary
                # single-cause combat facts. A later self-as-ammunition cause
                # replaces this staged fact at the same atomic boundary.
                events.append(annotated_effect)
                destruction_causes[target_id].append((
                    "FIRE_COMBAT_DESTROYED",
                    annotated_effect,
                ))
            elif target.state == "SUPPRESSED":
                # Preserve the established runtime rule: any accepted direct
                # fire against an already suppressed target resets its timer,
                # including a table outcome with no additional effect.
                target.suppression_remaining_ms = self._source_policy.suppression_ms
                self._mark_source_suppression_movement_task(target)

            passenger_ids = sorted({
                passenger.operator_id
                for intent in target_intents
                for passenger in _source_fire_direct_request(
                    intent
                ).carried_passengers
            })
            if requested_losses and passenger_ids:
                for passenger_id in passenger_ids:
                    passenger = world.operators[passenger_id]
                    passenger_before = int(passenger.blood)
                    passenger_after = max(
                        0, passenger_before - requested_losses)
                    passenger.blood = passenger_after
                    passenger.health = (
                        passenger.max_health * passenger_after
                        / int(passenger.max_blood))
                    if passenger_after == 0:
                        destroy_source_operator(world, passenger)
                    effect = EngineEvent(
                        world.tick, EventType.LOSSES_APPLIED, passenger.side,
                        primary_request.attacker.operator_id, passenger_id,
                        {"associated_vehicle_id": target_id,
                         "target_blood_before": passenger_before,
                         "target_blood_after": passenger_after,
                         "losses": passenger_before - passenger_after,
                         "adjudication_id": primary_adjudication_id,
                         "aggregate_adjudication_ids": adjudication_ids,
                         "resolution_policy": "aggregate_then_apply"},
                    )
                    events.append(self._annotate_source_event(
                        effect, primary.routed.action_sequence))

        # A validated and resolved loitering attack consumes the attacking
        # Operator itself.  This happens in the same working-world transaction
        # as the target effects above; validation failures never reach here.
        for intent in sorted(
            (
                item
                for item in intents
                if item.loitering_attack_policy is not None
            ),
            key=lambda item: item.routed.action_sequence,
        ):
            policy = intent.loitering_attack_policy
            if policy is None:  # pragma: no cover - narrowed by generator
                raise RuntimeError("LOITERING_ATTACK_POLICY_MISSING")
            attacker = world.operators[intent.contract.shooter_id]
            before = int(intent.request.attacker.blood)
            destruction_causes[attacker.operator_id].append((
                policy.destruction_reason_code,
                self._source_event(
                    EventType.UNIT_DESTROYED,
                    attacker.side,
                    attacker.operator_id,
                    attacker.operator_id,
                    intent.routed.action_sequence,
                    {
                        "action_id": intent.routed.action.action_id,
                        "target_blood_before": before,
                        "target_blood_after": 0,
                        "losses": before,
                        "reason_code": policy.destruction_reason_code,
                        "weapon_id": policy.weapon_id,
                        "target_id": intent.contract.target.unit_id,
                        "adjudication_id": intent.resolution.adjudication_id,
                        "consume_condition": policy.consume_condition,
                        "resource_policy": policy.resource_policy,
                        "attack_policy_id": policy.policy_id,
                        "attack_authority_version": (
                            self._loitering_munition_attack_authority
                            .authority_version
                        ),
                        "attack_authority_hash": (
                            self._loitering_munition_attack_authority.content_hash
                        ),
                    },
                ),
            ))

        # Destruction is a fact of the complete fire commit, not of whichever
        # cause happens to be applied first.  Preserve every independently
        # staged cause, perform the state transition once per operator, and
        # publish at most one UNIT_DESTROYED fact for this commit.  No primary
        # cause is inferred when multiple valid causes coexist.
        for operator_id in sorted(destruction_causes):
            operator = world.operators[operator_id]
            causes = tuple(sorted(
                destruction_causes[operator_id],
                key=_source_destruction_cause_sort_key,
            ))
            has_additional_destruction_policy = any(
                cause_code != "FIRE_COMBAT_DESTROYED"
                for cause_code, _ in causes
            )
            if has_additional_destruction_policy:
                destroy_source_operator(
                    world,
                    operator,
                    consume_lifecycle=any(
                        cause_code == "LOITERING_MUNITION_CONSUMED"
                        for cause_code, _ in causes
                    ),
                )
            if len(causes) == 1 and not has_additional_destruction_policy:
                continue
            staged_event_ids = {id(event) for _, event in causes}
            events[:] = [
                event for event in events if id(event) not in staged_event_ids
            ]
            events.append(_source_destruction_fact(operator, causes))

        # The summary event is constructed only inside the successful working
        # transaction.  It becomes externally visible together with the World
        # and RNG at the outer atomic commit point.
        for intent in sorted(
            guided_intents,
            key=lambda item: item.routed.action_sequence,
        ):
            effect = intent.commit_effect
            events.append(self._source_event(
                EventType.GUIDED_FIRE_RESOLVED,
                world.operators[effect.shooter_id].side,
                effect.shooter_id,
                effect.target_id,
                intent.routed.action_sequence,
                {
                    "action_id": intent.routed.action.action_id,
                    "fire_mode": FireMode.GUIDED.value,
                    "guider_id": effect.guider_id,
                    "shooter_id": effect.shooter_id,
                    "weapon_id": effect.weapon_id,
                    "target_id": effect.target_id,
                    "hit": effect.hit,
                    "damage_summary": [
                        {
                            "target_id": item.target_id,
                            "vehicle_losses": item.vehicle_losses,
                            "squad_losses": item.squad_losses,
                            "suppression_applied": item.suppression_applied,
                            "destroyed": item.destroyed,
                            "no_effect": item.no_effect,
                        }
                        for item in effect.damage_effects
                    ],
                    "adjudication_id": effect.adjudication_id,
                    "resolution_policy": "aggregate_then_apply",
                },
            ))

    def _direct_fire_request(self, action: EngineAction, attacker: Any,
                             target: Any, *,
                             world: WorldState | None = None,
                             weapon_id: int | None = None) -> DirectFireRequest:
        state = self._require_world() if world is None else world
        if (self._source_map is None or self._source_visibility_map is None
                or self._source_policy is None):
            raise ValueError("SOURCE_MAP_UNAVAILABLE")
        if attacker.current_hex is None or target.current_hex is None:
            raise ValueError("CURRENT_HEX_REQUIRED")
        requested_weapon_id = (action.parameters.get("weapon_id")
                               if weapon_id is None else weapon_id)
        if requested_weapon_id is None:
            raise ValueError("WEAPON_REQUIRED")
        requested_weapon_id = int(requested_weapon_id)
        profile = self._source_weapon_profile(requested_weapon_id)
        loitering_policy = self._loitering_attack_policy_for(
            attacker,
            requested_weapon_id,
            profile,
        )
        resource_policy = DirectFireResourcePolicy.AMMUNITION_KEY
        if loitering_policy is not None:
            profile = replace(profile, ammunition_key=None)
            resource_policy = DirectFireResourcePolicy.SELF_AS_MUNITION
        target_domain = _unit_domain(target.operator_type)
        armor_value = str(target.raw_attributes.get(
            "armor_class",
            target.raw_attributes.get("normalized_value", {}).get(
                "armor_class", "UNKNOWN"))).upper()
        try:
            armor = ArmorClass(armor_value)
        except ValueError as exc:
            raise ValueError("UNKNOWN_ARMOR_CLASS") from exc
        if target_domain is UnitDomain.VEHICLE and armor is ArmorClass.UNKNOWN:
            raise ValueError("UNKNOWN_ARMOR_CLASS")
        visibility = evaluate_visibility(
            self._source_visibility_map,
            observer_hex=attacker.current_hex, target_hex=target.current_hex,
            observer_domain=_unit_domain(attacker.operator_type),
            target_domain=target_domain,
            observer_is_drone=_uses_drone_observation_profile(
                attacker, self._operator_lifecycle_authority
            ),
            target_is_drone=_uses_drone_observation_profile(
                target, self._operator_lifecycle_authority
            ),
            target_concealed=target.concealed,
            target_terrain=self._source_map.terrain(target.current_hex).value,
            line_of_sight_evidence=self._runtime_line_of_sight(
                attacker,
                target,
            ))
        ammunition = self._ammunition(attacker)
        passengers = tuple(
            state.operators[int(item)]
            for item in target.raw_attributes.get("passenger_ids", [])
            if int(item) in state.operators)
        return DirectFireRequest(
            tick=state.tick, episode_seed=self._seed,
            action_id=action.action_id, attacker=attacker, target=target,
            weapon=profile, visibility=visibility, distance=visibility.distance,
            target_domain=target_domain, target_armor=armor,
            ammunition_before=(
                None
                if resource_policy is DirectFireResourcePolicy.SELF_AS_MUNITION
                else ammunition.get(profile.ammunition_key, 0)
            ),
            weapon_cooldown_before=(attacker.weapon_cooldown_remaining_ms + 999) // 1000,
            weapon_unfolded=attacker.weapon_unfolded,
            weapon_locked=attacker.weapon_locked,
            locked_weapon_id=attacker.locked_weapon_id,
            attacker_hovering=(
                attacker.stopped
                or resource_policy is DirectFireResourcePolicy.SELF_AS_MUNITION
            ),
            elevation_difference=fire_elevation_level_difference(
                self._source_visibility_map, attacker.current_hex, target.current_hex,
                distance=visibility.distance, weapon=profile, resource_policy=resource_policy,
                fire_mode=FireMode(action.parameters.get("fire_mode", FireMode.DIRECT.value))),
            attacker_suppressed=attacker.suppression_remaining_ms > 0,
            attacker_moving=(not attacker.stopped
                             or attacker.movement_mode == MovementMode.MARCHING.value),
            target_terrain=self._source_map.terrain(target.current_hex).value,
            target_concealed=target.concealed,
            target_moving=not target.stopped,
            target_stacked=sum(
                1 for item in state.operators.values()
                if item.alive and item.current_hex == target.current_hex) > 1,
            target_marching=target.movement_mode == MovementMode.MARCHING.value,
            attacker_transitioning=(attacker.transition_kind is not None
                                    or attacker.move_to_stop_remaining_ms > 0)
            and not (
                loitering_policy is not None
                and loitering_policy.can_interrupt_movement_route
            ),
            target_external_direct_fire_prohibited=target.state == "CLOSE_COMBAT",
            carried_passengers=passengers,
            metadata=(
                {}
                if loitering_policy is None
                else {
                    "loitering_attack_policy_id": loitering_policy.policy_id,
                    "loitering_attack_authority_version": (
                        self._loitering_munition_attack_authority.authority_version
                    ),
                    "loitering_attack_authority_hash": (
                        self._loitering_munition_attack_authority.content_hash
                    ),
                    "consume_condition": loitering_policy.consume_condition,
                }
            ),
            resource_policy=resource_policy,
            fire_mode=FireMode(action.parameters.get("fire_mode", FireMode.DIRECT.value)))

    def _loitering_attack_policy_for(
        self,
        attacker: OperatorState,
        weapon_id: int,
        weapon: WeaponProfile,
    ) -> LoiteringMunitionAttackPolicy | None:
        policy = self._loitering_munition_attack_authority.policy_for(
            lifecycle_profile_id=attacker.lifecycle_profile_id,
            weapon_id=weapon_id,
        )
        if policy is None:
            return None
        lifecycle = self._operator_lifecycle_authority.profile(
            attacker.lifecycle_profile_id or ""
        )
        if (
            lifecycle is None
            or lifecycle.canonical_identity != policy.canonical_identity
            or attacker.operator_type.upper() != lifecycle.operator_domain
        ):
            raise ValueError("LOITERING_ATTACK_IDENTITY_MISMATCH")
        if (
            weapon.weapon_class != policy.weapon_class
            or weapon.maximum_range != policy.maximum_range
            or tuple(sorted(item.value for item in weapon.target_domains))
            != policy.target_domains
        ):
            raise ValueError("LOITERING_ATTACK_WEAPON_AUTHORITY_MISMATCH")
        if not policy.requires_observable_target:
            raise ValueError("LOITERING_ATTACK_OBSERVATION_POLICY_UNSUPPORTED")
        return policy

    def _source_weapon_profile(self, weapon_id: int) -> WeaponProfile:
        source = self.scenario.metadata.get("source_compatible", {})
        weapons = source.get("weapons", {}) if isinstance(source, Mapping) else {}
        value = weapons.get(str(weapon_id), weapons.get(weapon_id))
        if not isinstance(value, Mapping):
            raise ValueError("WEAPON_PROFILE_UNAVAILABLE")
        try:
            domains = frozenset(_unit_domain(str(item))
                                for item in value["target_domains"])
            return WeaponProfile(
                weapon_id=weapon_id, weapon_class=str(value["weapon_class"]),
                maximum_range=int(value["maximum_range"]),
                target_domains=domains,
                ammunition_key=str(value.get("ammunition_key", "0")),
                cooldown_ticks=(None if value.get("cooldown_seconds") is None
                                else int(value["cooldown_seconds"])),
                requires_unfolded=bool(value.get("requires_unfolded", True)),
                can_fire_while_moving=bool(value.get("can_fire_while_moving", False)),
                supported_fire_modes=(
                    self._guidance_authority.supported_fire_modes(weapon_id)
                ))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("INVALID_WEAPON_PROFILE") from exc

    @staticmethod
    def _ammunition(operator: Any) -> dict[str, int]:
        value = operator.raw_attributes.get("ammunition")
        if not isinstance(value, Mapping):
            source = operator.raw_attributes.get("source_frame", {})
            value = source.get("remain_bullet_nums", {}) if isinstance(
                source, Mapping) else {}
        return {str(key): int(item) for key, item in value.items()}

    def _annotate_source_event(self, event: EngineEvent,
                               action_sequence: int) -> EngineEvent:
        world = self._require_world()
        sequence = world.next_event_sequence
        world.next_event_sequence += 1
        return EngineEvent(
            tick=event.tick, event_type=event.event_type, side=event.side,
            actor_id=event.actor_id, target_id=event.target_id,
            data={**event.data, "event_time_ms": world.elapsed_time_ms,
                  "action_sequence": action_sequence,
                  "event_sequence": sequence})

    def _source_event(self, event_type: EventType, side: str | None,
                      actor_id: int | None, target_id: int | None,
                      action_sequence: int,
                      data: Mapping[str, Any]) -> EngineEvent:
        world = self._require_world()
        sequence = world.next_event_sequence
        world.next_event_sequence += 1
        return EngineEvent(
            tick=world.tick, event_type=event_type, side=side,
            actor_id=actor_id, target_id=target_id,
            data={**data, "event_time_ms": world.elapsed_time_ms,
                  "action_sequence": action_sequence,
                  "event_sequence": sequence})

    def is_terminal(self) -> bool:
        return self._require_world().done

    def game_status(self) -> GameStatus:
        """Read lifecycle scalars without serializing or hashing WorldState."""
        world = self._require_world()
        return GameStatus(world.terminal, world.truncated, world.winner,
                          _game_outcome(world), world.stage)

    def result(self) -> GameResult:
        world = self._require_world()
        loser = None
        if world.winner is not None and len(self.scenario.side_order) == 2:
            loser = next(side for side in self.scenario.side_order
                         if side != world.winner)
        return GameResult(
            terminal=world.terminal, truncated=world.truncated,
            winner=world.winner, loser=loser, outcome=_game_outcome(world),
            reason=world.terminal_reason, final_stage=world.stage,
            ticks=world.tick, action_counts=dict(self._action_counts),
            error_counts=dict(self._error_counts), seed=self._seed,
            ruleset_id=self.ruleset.ruleset_id,
            ruleset_version=self.ruleset.ruleset_version,
            ruleset_hash=self.ruleset.content_hash,
            world_hash=canonical_hash(world.to_dict()),
        )

    def sim_time_ms(self) -> int:
        """Return the authoritative clock without constructing a snapshot."""

        value = self._require_world().elapsed_time_ms
        if type(value) is not int or value < 0:
            raise RuntimeError("invalid_authoritative_sim_time")
        return value

    def snapshot(self) -> Mapping[str, Any]:
        world = self._require_world()
        if self._rng is None:
            raise RuntimeError("engine_not_reset")
        value = {
            "schema_version": "native-snapshot/2.0",
            "scenario_hash": self.scenario.scenario_hash,
            "ruleset_id": self.ruleset.ruleset_id,
            "ruleset_version": self.ruleset.ruleset_version,
            "ruleset_hash": self.ruleset.content_hash,
            "adjudication_semantics_version": (
                ADJUDICATION_SEMANTICS_VERSION),
            "seed": self._seed, "rng": self._rng.snapshot(),
            "world": world.to_dict(),
            "action_counts": dict(self._action_counts),
            "error_counts": dict(self._error_counts),
        }
        if self.mounted_position_policy is not None:
            value["mounted_position_policy"] = self.mounted_position_policy
        if self._source_policy is not None:
            value["movement_policy"] = {
                "movement_policy_id": self._source_policy.policy_id,
                "movement_policy_version": self._source_policy.policy_version,
                "movement_policy_hash": self._source_policy.content_hash,
            }
            if self._capture_policy_recorded:
                value["objective_capture_policy"] = (
                    self._capture_policy.identity_record()
                )
        if self._adjudication_tables is not None:
            value["adjudication_table_set"] = {
                "adjudication_table_set_id": self._adjudication_tables.table_set_id,
                "adjudication_table_set_version": (
                    self._adjudication_tables.table_set_version),
                "adjudication_table_set_hash": (
                    self._adjudication_tables.content_hash),
            }
        if self.map_runtime_data is not None:
            value["map_runtime_data"] = self.map_runtime_data.identity_record()
        if self._boarding_type_assignments_enabled:
            value["boarding_type_assignment_authority"] = (
                self._boarding_type_assignment_authority.identity_record()
            )
        if _world_uses_operator_lifecycle(world):
            value["operator_lifecycle_authority"] = (
                self._operator_lifecycle_authority.identity_record()
            )
        if _world_uses_loitering_attack_authority(
            world,
            self._loitering_munition_attack_authority,
        ):
            value["loitering_munition_attack_authority"] = (
                self._loitering_munition_attack_authority.identity_record()
            )
        return value

    def restore(self, snapshot: Mapping[str, Any]) -> WorldState:
        if self._closed:
            raise RuntimeError("engine_closed")
        if snapshot.get("schema_version") != "native-snapshot/2.0":
            raise ValueError("unsupported snapshot schema")
        if snapshot.get("scenario_hash") != self.scenario.scenario_hash:
            raise ValueError("snapshot scenario hash mismatch")
        if (snapshot.get("ruleset_id") != self.ruleset.ruleset_id
                or snapshot.get("ruleset_version") != self.ruleset.ruleset_version
                or snapshot.get("ruleset_hash") != self.ruleset.content_hash):
            raise ValueError("snapshot ruleset mismatch")
        if snapshot.get("adjudication_semantics_version") != (
                ADJUDICATION_SEMANTICS_VERSION):
            raise ValueError("snapshot adjudication semantics mismatch")
        if snapshot.get("mounted_position_policy") != self.mounted_position_policy:
            raise ValueError("snapshot mounted position policy mismatch")
        capture_identity = snapshot.get("objective_capture_policy")
        restored_capture_policy = self._capture_policy
        restored_capture_recorded = self._capture_policy_recorded
        if self._source_policy is not None:
            if capture_identity is None:
                restored_capture_policy = load_objective_capture_policy("1")
                restored_capture_recorded = False
            else:
                try:
                    matched = resolve_objective_capture_policy_identity(
                        capture_identity
                    )
                except ValueError as exc:
                    raise ValueError(
                        "snapshot objective capture policy mismatch"
                    ) from exc
                restored_capture_policy = matched
                restored_capture_recorded = True
        elif capture_identity is not None:
            raise ValueError("snapshot requires hex objective capture policy")
        movement = snapshot.get("movement_policy")
        if self._source_policy is not None:
            expected = {"movement_policy_id": self._source_policy.policy_id,
                        "movement_policy_version": self._source_policy.policy_version,
                        "movement_policy_hash": self._source_policy.content_hash}
            if movement != expected:
                raise ValueError("snapshot movement policy mismatch")
        elif movement is not None:
            raise ValueError("snapshot requires source-compatible movement")
        adjudication = snapshot.get("adjudication_table_set")
        if self._adjudication_tables is not None:
            expected_adjudication = {
                "adjudication_table_set_id": self._adjudication_tables.table_set_id,
                "adjudication_table_set_version": (
                    self._adjudication_tables.table_set_version),
                "adjudication_table_set_hash": self._adjudication_tables.content_hash,
            }
            if adjudication != expected_adjudication:
                raise ValueError("snapshot adjudication table set mismatch")
        elif adjudication is not None:
            raise ValueError("snapshot requires source-compatible adjudication")
        runtime_data = snapshot.get("map_runtime_data")
        if self.map_runtime_data is not None:
            if runtime_data != self.map_runtime_data.identity_record():
                raise ValueError("snapshot map runtime data mismatch")
        elif runtime_data is not None:
            raise ValueError("snapshot requires map runtime data")
        boarding_assignment = snapshot.get(
            "boarding_type_assignment_authority"
        )
        if self._boarding_type_assignments_enabled:
            if boarding_assignment != (
                self._boarding_type_assignment_authority.identity_record()
            ):
                raise ValueError(
                    "snapshot boarding type assignment authority mismatch"
                )
        elif boarding_assignment is not None:
            raise ValueError(
                "snapshot unexpectedly requires boarding type assignment authority"
            )
        restored_seed = int(snapshot["seed"])
        restored_rng = NativeRNG.restore(snapshot["rng"])
        restored_world = WorldState.from_dict(snapshot["world"])
        self._validate_indirect_fire_mission_snapshot(restored_world)
        self._validate_boarding_snapshot(restored_world)
        self._validate_operator_lifecycle_snapshot(restored_world)
        self._validate_loitering_attack_snapshot(restored_world)
        lifecycle_identity = snapshot.get("operator_lifecycle_authority")
        if _world_uses_operator_lifecycle(restored_world):
            if lifecycle_identity != (
                self._operator_lifecycle_authority.identity_record()
            ):
                raise ValueError("snapshot operator lifecycle authority mismatch")
        elif lifecycle_identity is not None:
            raise ValueError("snapshot unexpectedly requires operator lifecycle authority")
        loitering_identity = snapshot.get(
            "loitering_munition_attack_authority"
        )
        if _world_uses_loitering_attack_authority(
            restored_world,
            self._loitering_munition_attack_authority,
        ):
            if loitering_identity != (
                self._loitering_munition_attack_authority.identity_record()
            ):
                raise ValueError(
                    "snapshot loitering-munition attack authority mismatch"
                )
        elif loitering_identity is not None:
            raise ValueError(
                "snapshot unexpectedly requires loitering-munition attack authority"
            )
        restored_action_counts = {
            str(key): int(item) for key, item in snapshot["action_counts"].items()
        }
        restored_error_counts = {
            str(key): int(item) for key, item in snapshot["error_counts"].items()
        }
        self._seed = restored_seed
        self._rng = restored_rng
        self._world = restored_world
        self._action_counts = restored_action_counts
        self._error_counts = restored_error_counts
        self._capture_policy = restored_capture_policy
        self._capture_policy_recorded = restored_capture_recorded
        self._capture_service = (
            ObjectiveCaptureService(
                self._movement_map_reader,
                restored_capture_policy,
            )
            if restored_capture_policy.mode == EXPLICIT_CAPTURE_MODE
            and self._movement_map_reader is not None
            else None
        )
        self._last_events = ()
        self._source_valid_actions_cache.clear()
        self._source_valid_actions_state_hash = None
        if self._source_policy is not None:
            self._refresh_source_observation()
        return WorldState.from_dict(self._world.to_dict())

    def _validate_indirect_fire_mission_snapshot(
        self,
        world: WorldState,
    ) -> None:
        """Fail fast before restoring an unknown mission authority contract."""

        policy = self._indirect_fire_execution_policy
        if world.indirect_fire_missions and not isinstance(
            policy,
            IndirectFireExecutionPolicy,
        ):
            raise ValueError("snapshot indirect-fire mission semantics mismatch")
        for mission in world.indirect_fire_missions:
            profile = self._indirect_fire_authority.profile(mission.profile_id)
            if (
                mission.authority_version
                != INDIRECT_FIRE_AUTHORITY_SCHEMA_VERSION
                or profile is None
                or profile.weapon_id != mission.weapon_id
                or mission.execution_policy_id != policy.policy_id
                or mission.execution_policy_hash != policy.content_hash
            ):
                raise ValueError("snapshot indirect-fire mission semantics mismatch")

    def _validate_boarding_snapshot(self, world: WorldState) -> None:
        """Reject relation or authority drift before installing a snapshot."""

        transition_pairs = {
            (item.carrier_id, item.passenger_id)
            for item in world.boarding_transitions
            if item.status is BoardingTransitionStatus.ACTIVE
        }
        for transition in world.boarding_transitions:
            profile = self._boarding_authority.profile(transition.profile_id)
            carrier = world.operators.get(transition.carrier_id)
            passenger = world.operators.get(transition.passenger_id)
            if (
                profile is None
                or transition.authority_version
                != self._boarding_authority.authority_version
                or transition.authority_hash
                != self._boarding_authority.content_hash
                or carrier is None
                or passenger is None
            ):
                raise ValueError("snapshot boarding semantics mismatch")
            if transition.status is BoardingTransitionStatus.ACTIVE and (
                carrier.passenger_id != passenger.operator_id
                or passenger.mounted_on != carrier.operator_id
                or not carrier.movement_locked
            ):
                raise ValueError("snapshot active boarding semantics mismatch")
        for passenger in world.operators.values():
            if passenger.mounted_on is None:
                continue
            carrier = world.operators.get(passenger.mounted_on)
            carrier_profile = (
                (None, False)
                if carrier is None
                else self._boarding_profile_for_carrier(carrier)
            )
            if (
                carrier is None
                or carrier.passenger_id != passenger.operator_id
                or carrier.side != passenger.side
                or carrier_profile[0] is None
                or (
                    carrier_profile[1]
                    and self._boarding_type_assignment_authority.profile_id_for(
                        passenger.operator_type,
                        passenger.subtype,
                        "PASSENGER",
                    ) != carrier_profile[0].profile_id
                )
                or passenger.deployed
            ):
                raise ValueError("snapshot boarding relation mismatch")
            # A stable ON_BOARD relation may exist after BOARD completes; an
            # active transition is required only while the carrier is locked.
            if carrier.movement_locked and (
                carrier.operator_id,
                passenger.operator_id,
            ) not in transition_pairs:
                raise ValueError("snapshot boarding lock mismatch")
        for carrier in world.operators.values():
            if carrier.passenger_id is None:
                continue
            passenger = world.operators.get(carrier.passenger_id)
            if passenger is None or passenger.mounted_on != carrier.operator_id:
                raise ValueError("snapshot boarding carrier relation mismatch")

    def close(self) -> None:
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    def _apply_deploy(self, action: EngineAction,
                      events: list[EngineEvent]) -> None:
        world = self._require_world()
        operator = world.operators[action.actor_id]
        position = action.target_position
        if position is None:
            raise RuntimeError("validated deploy missing position")
        error = validate_deployment(
            position, width=self.scenario.map.width,
            height=self.scenario.map.height,
            blocked_positions=self.scenario.map.blocked_positions,
            rules=self.ruleset.movement,
            occupied_positions=self._occupied_positions(
                exclude=operator.operator_id),
        )
        if error is not None:
            raise RuntimeError(f"validated deploy failed:{error}")
        before = operator.position
        operator.x, operator.y = position
        operator.deployed = True
        events.append(EngineEvent(
            world.tick, EventType.OPERATOR_DEPLOYED, action.side,
            operator.operator_id,
            data={"before": list(before), "after": list(position),
                  "action_id": action.action_id},
        ))

    def _apply_move(self, action: EngineAction,
                    events: list[EngineEvent]) -> None:
        world = self._require_world()
        operator = world.operators[action.actor_id]
        position = action.target_position
        if position is None:
            raise RuntimeError("validated move missing position")
        error = validate_move(
            operator, position, width=self.scenario.map.width,
            height=self.scenario.map.height,
            blocked_positions=self.scenario.map.blocked_positions,
            rules=self.ruleset.movement,
            occupied_positions=self._occupied_positions(
                exclude=operator.operator_id),
        )
        if error is not None:
            raise RuntimeError(f"validated move failed:{error}")
        before = operator.position
        operator.x, operator.y = position
        operator.speed = distance(before, position)
        events.append(EngineEvent(
            world.tick, EventType.OPERATOR_MOVED, action.side,
            operator.operator_id,
            data={"before": list(before), "after": list(position),
                  "distance": operator.speed, "action_id": action.action_id},
        ))

    def _apply_state(self, action: EngineAction,
                     events: list[EngineEvent]) -> None:
        world = self._require_world()
        operator = world.operators[action.actor_id]
        before = operator.state
        operator.state = str(action.parameters.get("state", "ready"))
        events.append(EngineEvent(
            world.tick, EventType.OPERATOR_STATE_CHANGED, action.side,
            operator.operator_id,
            data={"before": before, "after": operator.state,
                  "action_id": action.action_id},
        ))

    def _friendly(self, side: str) -> list[Any]:
        world = self._require_world()
        return sorted((item for item in world.operators.values()
                       if item.side == side), key=lambda item: item.operator_id)

    def _source_valid_actions(self, side: str) -> tuple[ValidAction, ...]:
        world = self._require_world()
        if self._movement is None or self._source_map is None:
            return ()
        values: list[ValidAction] = []
        for operator in self._friendly(side):
            if not operator.alive or not operator.deployed:
                continue
            if operator.mounted_on is not None:
                continue
            if operator.movement_locked:
                wait = EngineAction(side, operator.operator_id, ActionType.WAIT)
                values.append(ValidAction(
                    wait,
                    ACTION_PRIORITY[wait.action_type],
                    "source_action_router",
                ))
                continue
            if operator.moving:
                action = EngineAction(side, operator.operator_id,
                                      ActionType.STOP_MOVE)
                values.append(ValidAction(action, ACTION_PRIORITY[action.action_type],
                                          "source_action_router"))
            elif (operator.stopped and operator.transition_kind is None
                  and operator.current_hex is not None):
                try:
                    mode = MovementMode(operator.movement_mode)
                except ValueError:
                    mode = MovementMode.NORMAL
                for target in sorted(item for item in
                                     self._source_map.cell(operator.current_hex).neighbors
                                     if item >= 0):
                    legality = self._movement.planner.validate_path(
                        operator, [target], mode, world)
                    if legality.legal:
                        action = EngineAction(
                            side, operator.operator_id, ActionType.MOVE,
                            parameters={"path": [target],
                                        "movement_mode": mode.value})
                        values.append(ValidAction(
                            action, ACTION_PRIORITY[action.action_type],
                            "source_action_router"))
            if self._capture_service is not None:
                for objective in sorted(
                    world.objectives.values(),
                    key=lambda item: item.objective_id,
                ):
                    assessment = self._capture_service.assess(
                        world,
                        side=side,
                        actor_id=operator.operator_id,
                        objective_id=objective.objective_id,
                    )
                    if not assessment.allowed:
                        continue
                    action = EngineAction(
                        side,
                        operator.operator_id,
                        ActionType.CAPTURE,
                        target_id=objective.objective_id,
                    )
                    values.append(ValidAction(
                        action,
                        ACTION_PRIORITY[action.action_type],
                        "native_objective_capture_v2",
                    ))
            visible_targets = {
                int(value)
                for value in operator.raw_attributes.get(
                    "see_enemy_bop_ids", ()
                )
            }
            for weapon_id in sorted(operator.weapon_ids):
                for target in sorted(world.operators.values(),
                                     key=lambda item: item.operator_id):
                    if (
                        target.side == side
                        or not target.alive
                        or target.operator_id not in visible_targets
                    ):
                        continue
                    action = EngineAction(
                        side, operator.operator_id, ActionType.SHOOT,
                        target_id=target.operator_id,
                        parameters={"weapon_id": weapon_id})
                    try:
                        request = self._direct_fire_request(action, operator, target)
                        legal = DirectFireAdjudicator(
                            self._adjudication_tables,
                            DiceRoller(self._rng)).validate(request)
                    except (TypeError, ValueError):
                        continue
                    if legal.legal:
                        values.append(ValidAction(
                            action, ACTION_PRIORITY[action.action_type],
                            "source_action_router"))
            if (
                operator.weapon_ids
                and operator.stopped
                and not operator.moving
                and operator.move_to_stop_remaining_ms == 0
                and operator.transition_kind is None
                and operator.transition_remaining_ms == 0
            ):
                if (not operator.command_lost
                        and (operator.weapon_locked or operator.locked_weapon_id is not None)):
                    action = EngineAction(side, operator.operator_id, ActionType.WEAPON_UNLOCK)
                    values.append(ValidAction(action, ACTION_PRIORITY[action.action_type],
                                              "source_action_router"))
                elif not operator.command_lost:
                    for weapon_id in operator.weapon_ids:
                        action = EngineAction(side, operator.operator_id,
                                              ActionType.WEAPON_LOCK,
                                              parameters={"weapon_id": weapon_id})
                        values.append(ValidAction(action, ACTION_PRIORITY[action.action_type],
                                                  "source_action_router"))
                if (not operator.weapon_unfolded and not operator.weapon_locked
                        and operator.locked_weapon_id is None):
                    action = EngineAction(side, operator.operator_id,
                                          ActionType.WEAPON_UNFOLD)
                    values.append(ValidAction(action, ACTION_PRIORITY[action.action_type],
                                              "source_action_router"))
            wait = EngineAction(side, operator.operator_id, ActionType.WAIT)
            values.append(ValidAction(wait, ACTION_PRIORITY[wait.action_type],
                                      "source_action_router"))
        pre_state = WorldState.from_dict(world.to_dict())
        for capability in self._board_capabilities_from_pre_state(
            pre_state,
            side,
        ):
            action = _encode_board_action(side, capability)
            values.append(ValidAction(
                action,
                ACTION_PRIORITY[action.action_type],
                "source_boarding_capability",
            ))
        for capability in self._unload_capabilities_from_pre_state(
            pre_state,
            side,
        ):
            action = _encode_unload_action(side, capability)
            values.append(ValidAction(
                action,
                ACTION_PRIORITY[action.action_type],
                "source_boarding_capability",
            ))
        for capability in self._concealment_capabilities_from_pre_state(
            pre_state,
            side,
        ):
            if not capability.available:
                continue
            action = EngineAction(
                side=side,
                actor_id=capability.operator_id,
                action_type=ActionType.ENTER_CONCEALMENT,
            )
            values.append(ValidAction(
                action,
                ACTION_PRIORITY[action.action_type],
                "source_concealment_capability",
            ))
        for capability in self._guided_fire_capabilities_from_pre_state(
            pre_state,
            side,
        ):
            action = ActionRouter.guided_fire_action(side, capability)
            values.append(ValidAction(
                action,
                ACTION_PRIORITY[action.action_type],
                "source_guided_fire_capability",
            ))
        if not values:
            action = EngineAction(side, None, ActionType.WAIT)
            values.append(ValidAction(action, ACTION_PRIORITY[action.action_type],
                                      "source_action_router"))
        return tuple(sorted(values, key=valid_action_sort_key))

    def _refresh_source_observation(self) -> None:
        if self._source_map is None or self._source_visibility_map is None:
            return
        world = self._require_world()
        self._source_valid_actions_cache.clear()
        self._source_valid_actions_state_hash = None
        ordered_operators = tuple(sorted(
            world.operators.values(), key=lambda item: item.operator_id,
        ))
        for observer in ordered_operators:
            visible: list[int] = []
            if observer.alive and observer.deployed and observer.current_hex is not None:
                observer_domain = _unit_domain(observer.operator_type)
                for target in ordered_operators:
                    if (target.side == observer.side or not target.alive
                            or not target.deployed or target.current_hex is None):
                        continue
                    if has_empty_fire_exposure(target):
                        visible.append(target.operator_id)
                        continue
                    target_domain = _unit_domain(target.operator_type)
                    observer_is_drone = _uses_drone_observation_profile(
                        observer, self._operator_lifecycle_authority
                    )
                    target_is_drone = _uses_drone_observation_profile(
                        target, self._operator_lifecycle_authority
                    )
                    if not within_maximum_observation_distance(
                        observer_hex=observer.current_hex,
                        target_hex=target.current_hex,
                        observer_domain=observer_domain,
                        target_domain=target_domain,
                        observer_is_drone=observer_is_drone,
                        target_is_drone=target_is_drone,
                        observation_profiles=self._source_observation_profiles,
                    ):
                        continue
                    result = evaluate_visibility(
                        self._source_visibility_map,
                        observer_hex=observer.current_hex,
                        target_hex=target.current_hex,
                        observer_domain=observer_domain,
                        target_domain=target_domain,
                        observer_is_drone=observer_is_drone,
                        target_is_drone=target_is_drone,
                        target_concealed=target.concealed,
                        target_terrain=self._source_map.terrain(
                            target.current_hex).value,
                        line_of_sight_evidence=self._runtime_line_of_sight(
                            observer,
                            target,
                        ))
                    if result.observable:
                        visible.append(target.operator_id)
            observer.raw_attributes["see_enemy_bop_ids"] = visible

    def _visible_enemies(self, side: str) -> list[Any]:
        world = self._require_world()
        if self._source_policy is not None:
            visible_ids = {int(target_id)
                           for observer in self._friendly(side)
                           for target_id in observer.raw_attributes.get(
                               "see_enemy_bop_ids", [])}
            return sorted((item for item in world.operators.values()
                           if item.side != side and item.operator_id in visible_ids),
                          key=lambda item: item.operator_id)
        friendly = self._friendly(side)
        return sorted((target for target in world.operators.values()
                       if target.side != side
                       and any(can_see(observer, target, self.ruleset.visibility)
                               for observer in friendly)),
                      key=lambda item: item.operator_id)

    def _occupied_positions(self, *, exclude: int) -> tuple[tuple[float, float], ...]:
        world = self._require_world()
        return tuple(sorted(item.position for item in world.operators.values()
                            if item.operator_id != exclude and item.deployed
                            and item.alive))

    def _require_world(self) -> WorldState:
        if self._world is None:
            raise RuntimeError("engine_not_reset")
        return self._world

    def _require_side(self, side: str) -> None:
        if side not in self.scenario.side_order:
            raise ValueError(f"unknown side: {side}")

    def _runtime_line_of_sight(
        self,
        observer: Any,
        target: Any,
    ) -> LineOfSightEvidence | None:
        """Return exact static lookup evidence when both heights are mapped.

        Unsupported air-height categories deliberately fall back to the
        documented Native algorithm rather than being guessed into a source
        matrix mode.
        """

        if self.map_runtime_data is None:
            return None
        observer_height = _source_visibility_height_m(
            observer, self._operator_lifecycle_authority
        )
        target_height = _source_visibility_height_m(
            target, self._operator_lifecycle_authority
        )
        if observer_height is None or target_height is None:
            return None
        return self.map_runtime_data.visibility.line_of_sight(
            int(observer.current_hex),
            int(target.current_hex),
            observer_height_m=observer_height,
            target_height_m=target_height,
        )

    def _line_of_sight_to_ground_hex(
        self,
        observer: Any,
        target_hex: int,
    ) -> LineOfSightEvidence:
        """Use the current LOS authority for a hypothetical ground endpoint."""

        if self._source_visibility_map is None:
            raise ValueError("SOURCE_VISIBILITY_MAP_UNAVAILABLE")
        observer_height = _source_visibility_height_m(
            observer, self._operator_lifecycle_authority
        )
        if self.map_runtime_data is not None and observer_height is not None:
            return self.map_runtime_data.visibility.line_of_sight(
                int(observer.current_hex),
                int(target_hex),
                observer_height_m=observer_height,
                target_height_m=0,
            )
        return evaluate_line_of_sight(
            self._source_visibility_map,
            int(observer.current_hex),
            int(target_hex),
        )


def _indirect_fire_execution_blockers(
    authority: IndirectFireAuthority,
    profile: IndirectFireProfile,
    shooter: OperatorState,
    *,
    ammunition_authority: IndirectFireAmmunitionAuthority,
    damage_policy: IndirectFireDamagePolicy,
    descriptor: IndirectFireAuthorityDescriptor | None,
    observation_policy: IndirectFireObservationPolicy,
) -> tuple[str, ...]:
    """Translate unresolved authority into stable public blocker codes."""

    blockers: list[str] = []
    binding_failure = weapon_binding_failure(weapon_id=profile.weapon_id,
        locked=shooter.weapon_locked, locked_weapon_id=shooter.locked_weapon_id)
    if binding_failure is not None:
        blockers.append(binding_failure.lower())
    if profile.range_policy is not IndirectRangePolicy.SCENARIO_DEFINED_GLOBAL_HEX:
        if profile.minimum_range is None:
            blockers.append("artillery_minimum_range")
        if profile.maximum_range is None:
            blockers.append("artillery_maximum_range")
    if profile.target_domain is IndirectTargetDomain.UNKNOWN:
        blockers.append("indirect_target_domain")
    resource = authority.resource_policy(profile.resource_policy_id)
    ammunition = ammunition_authority.mapping_for_weapon(profile.weapon_id)
    if (
        resource is None
        or ammunition is None
        or resource.ammo_cost != ammunition.ammo_cost_per_mission
    ):
        blockers.append("ammunition_consumption_per_mission")
    if ammunition is not None:
        inventory = NativeEngine._ammunition(shooter)
        if inventory.get(ammunition.ammunition_key, 0) < ammunition.ammo_cost_per_mission:
            blockers.append("indirect_fire_ammunition_unavailable")
    if observation_policy is IndirectFireObservationPolicy.UNRESOLVED:
        blockers.append("impact_observation_policy")
    if not isinstance(damage_policy, IndirectFireDamagePolicy):
        blockers.append("indirect_damage_policy")
    if descriptor is None:
        blockers.append("indirect_authority_descriptor")
    scatter = authority.scatter_profile(profile.scatter_model_id)
    if scatter is None or scatter.direction_policy is ScatterDirectionPolicy.UNKNOWN:
        blockers.append("scatter_direction")
    if shooter.weapon_cooldown_remaining_ms > 0:
        blockers.append("weapon_cooldown_remaining")
    if shooter.indirect_fire_planned_cooldown_remaining_ms > 0:
        blockers.append("indirect_fire_planned_cooldown_remaining")
    return tuple(sorted(set(blockers)))


def _boarding_operator_stopped(operator: OperatorState) -> bool:
    """Use only authoritative runtime state; never infer from unit identity."""

    return (
        operator.stopped
        and not operator.moving
        and operator.speed == 0
        and operator.move_to_stop_remaining_ms == 0
        and operator.transition_kind is None
        and operator.transition_remaining_ms == 0
    )


def _boarding_operator_suppressed(operator: OperatorState) -> bool:
    return (
        operator.suppression_remaining_ms > 0
        or operator.state.upper() == "SUPPRESSED"
    )


def _operator_has_boarding_transition(
    world: WorldState,
    operator_id: int,
) -> bool:
    return any(
        item.status is BoardingTransitionStatus.ACTIVE
        and (item.carrier_id == operator_id or item.passenger_id == operator_id)
        for item in world.boarding_transitions
    )


def _indirect_fire_plan_failure(
    reason: str,
    *blockers: str,
) -> IndirectFirePlanValidationResult:
    return IndirectFirePlanValidationResult(
        valid=False,
        reason=reason,
        blockers=tuple(sorted(set(blockers))),
    )


def _indirect_fire_effect_failure(
    *blockers: str,
) -> IndirectFireEffectResolutionResult:
    return IndirectFireEffectResolutionResult(
        status=IndirectFireEffectStatus.INVALID,
        blockers=tuple(sorted(set(blockers))),
        effect_bundle=None,
    )


def _record_sequence(value: Any, field: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{field} must be a sequence")
    if not all(isinstance(item, Mapping) for item in value):
        raise ValueError(f"{field} must contain mappings")
    return tuple(value)


def _record_by_id(
    value: Any,
    expected_id: int,
    id_field: str,
) -> Mapping[str, Any] | None:
    matches = tuple(
        item for item in _record_sequence(value, id_field)
        if item.get(id_field) == expected_id
    )
    if len(matches) > 1:
        raise ValueError(f"duplicate {id_field}: {expected_id}")
    return None if not matches else matches[0]


def _exact_nonnegative_record_int(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a non-negative exact int")
    return value


def _exact_record_bool(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be an exact bool")
    return value


def _unit_domain(value: str) -> UnitDomain:
    try:
        return UnitDomain(str(value).upper())
    except ValueError:
        return UnitDomain.UNKNOWN


def _uses_drone_observation_profile(
    operator: Any,
    authority: OperatorLifecycleAuthority,
) -> bool:
    """Use only explicit canonical platform mappings as rule authority."""

    return _operator_canonical_identity(operator, authority) in {
        "DRONE",
        "LOITERING_MUNITION",
    }


def _source_visibility_height_m(
    operator: Any,
    authority: OperatorLifecycleAuthority,
) -> int | None:
    explicit = operator.raw_attributes.get("visibility_height_m")
    if type(explicit) is int and explicit in {0, 20, 200, 500}:
        return explicit
    domain = _unit_domain(operator.operator_type)
    if domain in {UnitDomain.PERSONNEL, UnitDomain.VEHICLE}:
        return 0
    canonical = _operator_canonical_identity(operator, authority)
    if canonical in {"DRONE", "LOITERING_MUNITION"}:
        return 200
    altitude = float(operator.altitude)
    if altitude in {0.0, 20.0, 200.0, 500.0} and altitude != 0.0:
        return int(altitude)
    return None


def _operator_canonical_identity(
    operator: Any,
    authority: OperatorLifecycleAuthority,
) -> str:
    """Resolve rule identity from typed authority, then exact Source mapping.

    Finite-lifecycle profiles remain the strongest typed authority.  Ordinary
    Source-compatible Operators can use their exact ``source-T-S`` subtype
    through the versioned mapping.  Raw canonical metadata remains a legacy
    fallback and cannot override either authority source.
    """

    resolution = resolve_operator_canonical_identity(
        operator,
        authority,
        allow_compatibility_fallback=True,
    )
    return resolution.canonical_identity or ""


def _world_uses_operator_lifecycle(world: WorldState) -> bool:
    return any(
        operator.lifecycle_profile_id is not None
        for operator in world.operators.values()
    )


def _world_uses_loitering_attack_authority(
    world: WorldState,
    authority: LoiteringMunitionAttackAuthority,
) -> bool:
    return any(
        authority.policy_for(
            lifecycle_profile_id=operator.lifecycle_profile_id,
            weapon_id=weapon_id,
        ) is not None
        for operator in world.operators.values()
        for weapon_id in operator.weapon_ids
    )


def _source_fire_requested_losses(
    intent: _SourceFireIntent | _SourceGuidedFireIntent,
) -> int:
    if isinstance(intent, _SourceFireIntent):
        outcome = intent.resolution.outcome
        if outcome is None:
            raise RuntimeError("accepted direct fire lacks an outcome")
        return int(outcome.vehicle_losses) + int(outcome.squad_losses)
    return sum(
        int(item.vehicle_losses) + int(item.squad_losses)
        for item in intent.commit_effect.damage_effects
    )


def _source_fire_suppression(
    intent: _SourceFireIntent | _SourceGuidedFireIntent,
) -> bool:
    if isinstance(intent, _SourceFireIntent):
        outcome = intent.resolution.outcome
        if outcome is None:
            raise RuntimeError("accepted direct fire lacks an outcome")
        return bool(outcome.suppression_applied)
    return any(
        item.suppression_applied
        for item in intent.commit_effect.damage_effects
    )


def _source_fire_adjudication_id(
    intent: _SourceFireIntent | _SourceGuidedFireIntent,
) -> str:
    if isinstance(intent, _SourceFireIntent):
        return intent.resolution.adjudication_id
    return intent.commit_effect.adjudication_id


def _source_fire_direct_request(
    intent: _SourceFireIntent | _SourceGuidedFireIntent,
) -> DirectFireRequest:
    if isinstance(intent, _SourceFireIntent):
        return intent.request
    return intent.direct_request


def _tag_guided_aggregate_event(
    event: EngineEvent,
    primary: _SourceFireIntent | _SourceGuidedFireIntent,
) -> EngineEvent:
    if isinstance(primary, _SourceFireIntent):
        return event
    return EngineEvent(
        tick=event.tick,
        event_type=event.event_type,
        side=event.side,
        actor_id=event.actor_id,
        target_id=event.target_id,
        data={
            **event.data,
            "fire_mode": FireMode.GUIDED.value,
            "guider_id": primary.commit_effect.guider_id,
        },
    )


def _source_destruction_cause_sort_key(
    cause: tuple[str, EngineEvent],
) -> tuple[int, int, str, int, int]:
    cause_code, event = cause
    return (
        int(event.data.get("action_sequence", -1)),
        int(event.data.get("event_sequence", -1)),
        cause_code,
        -1 if event.actor_id is None else event.actor_id,
        -1 if event.target_id is None else event.target_id,
    )


def _source_destruction_fact(
    operator: OperatorState,
    causes: tuple[tuple[str, EngineEvent], ...],
) -> EngineEvent:
    """Return one commit fact while retaining every staged death cause."""

    if not causes:
        raise ValueError("SOURCE_DESTRUCTION_CAUSE_REQUIRED")
    if len(causes) == 1:
        return causes[0][1]
    ordered = tuple(sorted(causes, key=_source_destruction_cause_sort_key))
    event_time_ms = min(
        int(event.data.get("event_time_ms", event.tick * 1000))
        for _, event in ordered
    )
    action_sequence = min(
        int(event.data.get("action_sequence", -1))
        for _, event in ordered
    )
    event_sequence = min(
        int(event.data.get("event_sequence", -1))
        for _, event in ordered
    )
    blood_before = max(
        (
            int(event.data["target_blood_before"])
            for _, event in ordered
            if event.data.get("target_blood_before") is not None
        ),
        default=int(operator.max_blood),
    )
    return EngineEvent(
        tick=event_time_ms // 1000,
        event_type=EventType.UNIT_DESTROYED,
        side=operator.side,
        actor_id=operator.operator_id,
        target_id=operator.operator_id,
        data={
            "operator_id": operator.operator_id,
            "target_blood_before": blood_before,
            "target_blood_after": 0,
            "destruction_fact_policy": "ONE_EVENT_PER_OPERATOR_PER_COMMIT",
            "primary_reason_code": None,
            "cause_codes": [cause_code for cause_code, _ in ordered],
            "causes": [
                {
                    "cause_code": cause_code,
                    "event": event.to_dict(),
                }
                for cause_code, event in ordered
            ],
            "event_time_ms": event_time_ms,
            "action_sequence": action_sequence,
            "event_sequence": event_sequence,
        },
    )


def _guided_fire_intent_sort_key(
    intent: FireIntent,
) -> tuple[int, int, int, int]:
    if intent.mode is not FireMode.GUIDED:
        raise ValueError(
            f"GUIDED_FIRE_BATCH_REQUIRES_GUIDED_INTENT:{intent.mode.value}"
        )
    if not isinstance(intent.target, UnitTarget):
        raise ValueError("GUIDED_FIRE_REQUIRES_UNIT_TARGET")
    if intent.guider_id is None:
        raise ValueError("GUIDER_REQUIRED")
    return (
        intent.shooter_id,
        intent.guider_id,
        intent.weapon_id,
        intent.target.unit_id,
    )


def _game_outcome(world: WorldState) -> str:
    """Share scalar outcome rules without routing result through virtual hooks."""
    return ("truncated" if world.truncated else
            "ongoing" if not world.terminal else
            "draw" if world.winner is None else "win")
