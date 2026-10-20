"""Internal indirect-fire lifecycle primitives for Native simplified rules.

The service never owns an authoritative ``WorldState`` or ``NativeRNG``.  Pure
resolution methods consume detached state and RNG snapshots; apply methods
mutate only the caller-supplied working world.  Publication remains the
responsibility of ``NativeEngine``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from wargame_native_engine.adjudication.indirect_fire import (
    IndirectFireDamageEffect,
    IndirectFireEffectBundle,
    IndirectFireEffectResolutionResult,
    IndirectFireEffectStatus,
    IndirectFireImpactValidationResult,
    IndirectFireMissResolution,
    IndirectFireObservationPolicy,
    IndirectFirePlanRequest,
    IndirectFireResolution,
    IndirectFireResolutionEvidence,
    IndirectFireRngState,
    IndirectFireRngTransition,
    IndirectFireScatterResult,
    IndirectFireTargetEvidence,
    IndirectFireTriState,
    _build_validated_indirect_fire_impact_request,
    resolve_indirect_fire_effects as resolve_damage_effects,
)
from wargame_native_engine.adjudication.table_loader import AdjudicationTableSet
from wargame_native_engine.compat.source_frame.models import ArmorClass, UnitDomain
from wargame_native_engine.core.destruction import destroy_source_operator
from wargame_native_engine.core.event import EngineEvent, EventType
from wargame_native_engine.core.indirect_fire import (
    FireMission,
    FireMissionObservationEvidence,
    FireMissionScatterEvidence,
    SCATTER_OUT_OF_MAP_OUTCOME,
    advance_indirect_fire_active_missions,
    advance_indirect_fire_missions,
    cancel_fire_mission,
    fire_mission_sort_key,
    indirect_fire_active_delta_ms,
)
from wargame_native_engine.core.operator import MovementTaskPauseReason
from wargame_native_engine.core.rng import NativeRNG
from wargame_native_engine.core.world import WorldState
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.movement.map_adapter import SourceHexMap
from wargame_native_engine.movement.models import MovementMode
from wargame_native_engine.rules.indirect_fire import (
    INDIRECT_FIRE_AUTHORITY_SCHEMA_VERSION,
    IndirectFireAmmoConsumeTiming,
    IndirectFireAmmunitionAuthority,
    IndirectFireAuthority,
    IndirectFireCancellationReason,
    IndirectFireDamagePolicy,
    IndirectFireExecutionPolicy,
    IndirectFireMissionState,
    IndirectFireObservationAuthority,
)
from wargame_native_engine.rules.operator_identity import (
    resolve_operator_canonical_identity,
)
from wargame_native_engine.rules.operator_lifecycle import (
    OperatorLifecycleAuthority,
)
from wargame_native_engine.visibility import hex_distance


@dataclass(frozen=True, slots=True)
class IndirectFireLifecycleService:
    """Read-only dependencies plus pure/apply lifecycle operations."""

    source_map: SourceHexMap | None
    adjudication_tables: AdjudicationTableSet | None
    suppression_ms: int | None
    operator_lifecycle_authority: OperatorLifecycleAuthority

    def applicable_target_ids(
        self,
        world: WorldState,
        impact_hex: int,
        *,
        excluded_operator_ids: Sequence[int] = (),
    ) -> tuple[int, ...]:
        """Return occupants to which ground indirect-fire damage may apply.

        Only an explicitly identified AIR/DRONE is outside this Native ground
        damage path.  Every unresolved identity and every other air identity
        remains a candidate so the existing damage resolver fails closed
        instead of silently granting immunity.
        """

        excluded = frozenset(excluded_operator_ids)
        return tuple(sorted(
            operator.operator_id
            for operator in world.operators.values()
            if operator.alive
            and operator.deployed
            and operator.current_hex == impact_hex
            and operator.operator_id not in excluded
            and not self._is_confirmed_drone(operator)
        ))

    def _is_confirmed_drone(self, operator: Any) -> bool:
        if _unit_domain(operator.operator_type) is not UnitDomain.AIR:
            return False
        identity = resolve_operator_canonical_identity(
            operator,
            self.operator_lifecycle_authority,
            allow_compatibility_fallback=False,
        )
        return identity.canonical_identity == "DRONE"

    def build_mission(
        self,
        request: IndirectFirePlanRequest,
        pre_state: WorldState,
        *,
        authority: IndirectFireAuthority,
        ammunition_authority: IndirectFireAmmunitionAuthority,
        execution_policy: IndirectFireExecutionPolicy,
        action_stream_bound: bool,
    ) -> FireMission:
        """Build a mission after the facade has revalidated plan currency."""

        shooter = pre_state.operators.get(request.shooter_id)
        if shooter is None:
            raise ValueError("INDIRECT_FIRE_SHOOTER_NOT_FOUND")
        profile = authority.profile(request.artillery_profile_id)
        if profile is None or profile.weapon_id != request.weapon_id:
            raise ValueError("INDIRECT_FIRE_PROFILE_UNAVAILABLE")
        resource = authority.resource_policy(profile.resource_policy_id)
        mapping = ammunition_authority.mapping_for_weapon(request.weapon_id)
        if (
            resource is None
            or mapping is None
            or resource.ammo_cost != mapping.ammo_cost_per_mission
        ):
            raise ValueError("AMMO_MAPPING_UNRESOLVED")
        if (
            execution_policy.ammo_policy.consume_timing
            is not IndirectFireAmmoConsumeTiming.MISSION_CREATE
        ):
            raise ValueError("INDIRECT_FIRE_AMMO_CONSUME_TIMING_UNSUPPORTED")
        ammunition = _ammunition(shooter)
        if tuple(sorted(ammunition.items())) != request.ammo_before:
            raise ValueError("INDIRECT_FIRE_AMMUNITION_PRESTATE_MISMATCH")
        if ammunition.get(mapping.ammunition_key, 0) < mapping.ammo_cost_per_mission:
            raise ValueError("INDIRECT_FIRE_AMMUNITION_UNAVAILABLE")
        mission_id = "mission-" + canonical_hash({
            "contract": "native-indirect-fire-mission/1.0",
            "plan_request": request.to_record(),
        })[:24]
        if any(
            value.mission_id == mission_id
            for value in pre_state.indirect_fire_missions
        ):
            raise ValueError("INDIRECT_FIRE_MISSION_ALREADY_EXISTS")
        return FireMission(
            mission_id=mission_id,
            created_tick=pre_state.tick,
            shooter_id=request.shooter_id,
            weapon_id=request.weapon_id,
            planned_hex=request.planned_hex,
            profile_id=request.artillery_profile_id,
            authority_version=request.authority_version,
            execution_policy_id=request.execution_policy_id,
            execution_policy_hash=request.execution_policy_hash,
            phase=IndirectFireMissionState.PLANNED,
            created_at=pre_state.elapsed_time_ms,
            remaining_flight_seconds=profile.flight_time_seconds,
            ammo_consumed=mapping.ammo_cost_per_mission,
            ammunition_key=mapping.ammunition_key,
            cancelable=False,
            action_stream_bound=action_stream_bound,
            active_duration_seconds=profile.active_duration_seconds,
        )

    def apply_mission_creation(
        self,
        working: WorldState,
        request: IndirectFirePlanRequest,
        mission: FireMission,
        *,
        authority: IndirectFireAuthority,
        replay_identity: Mapping[str, str],
        action_sequence: int,
    ) -> EngineEvent:
        shooter = working.operators[mission.shooter_id]
        profile = authority.profile(mission.profile_id)
        if profile is None or profile.weapon_id != mission.weapon_id:
            raise ValueError("INDIRECT_FIRE_PROFILE_UNAVAILABLE")
        ammunition = _ammunition(shooter)
        before = ammunition.get(mission.ammunition_key, 0)
        if before < mission.ammo_consumed:
            raise ValueError("INDIRECT_FIRE_AMMUNITION_UNAVAILABLE")
        ammunition[mission.ammunition_key] = before - mission.ammo_consumed
        shooter.raw_attributes["ammunition"] = ammunition
        source = shooter.raw_attributes.get("source_frame")
        if isinstance(source, dict):
            source["remain_bullet_nums"] = dict(ammunition)
        shooter.indirect_fire_planned_cooldown_remaining_ms = max(
            shooter.indirect_fire_planned_cooldown_remaining_ms,
            profile.planned_cooldown_seconds * 1_000,
        )
        working.indirect_fire_missions = tuple(sorted(
            (*working.indirect_fire_missions, mission),
            key=fire_mission_sort_key,
        ))
        return self.mission_event(
            working,
            EventType.INDIRECT_FIRE_MISSION_CREATED,
            mission,
            {
                "action_id": request.action_id,
                "planned_hex": mission.planned_hex,
                "profile_id": mission.profile_id,
                "authority_version": mission.authority_version,
                "execution_policy_id": mission.execution_policy_id,
                "execution_policy_hash": mission.execution_policy_hash,
                "ammunition_key": mission.ammunition_key,
                "ammo_consumed": mission.ammo_consumed,
                "authority_hash": replay_identity[
                    "indirect_fire_authority_hash"
                ],
                "ammo_mapping_hash": replay_identity["ammo_mapping_hash"],
                "observation_policy_hash": replay_identity[
                    "observation_policy_hash"
                ],
                "damage_policy_hash": replay_identity["damage_policy_hash"],
            },
            action_sequence=action_sequence,
        )

    def cancel_mission(
        self,
        working: WorldState,
        mission_id: str,
        reason: IndirectFireCancellationReason,
    ) -> tuple[FireMission, EngineEvent]:
        mission = next((
            value for value in working.indirect_fire_missions
            if value.mission_id == mission_id
        ), None)
        if mission is None:
            raise ValueError("INDIRECT_FIRE_MISSION_NOT_FOUND")
        cancelled = cancel_fire_mission(mission, reason)
        shooter = working.operators.get(cancelled.shooter_id)
        if shooter is None:
            raise ValueError("INDIRECT_FIRE_SHOOTER_NOT_FOUND")
        cooldown_before = int(
            shooter.indirect_fire_planned_cooldown_remaining_ms
        )
        shooter.indirect_fire_planned_cooldown_remaining_ms = 0
        working.indirect_fire_missions = tuple(sorted(
            (
                cancelled if value.mission_id == mission_id else value
                for value in working.indirect_fire_missions
            ),
            key=fire_mission_sort_key,
        ))
        event = self.mission_event(
            working,
            EventType.INDIRECT_FIRE_MISSION_CANCELLED,
            cancelled,
            {
                "cancellation_reason": reason.value,
                "ammo_refunded": 0,
                "planned_cooldown_before_ms": cooldown_before,
                "planned_cooldown_after_ms": 0,
            },
        )
        return cancelled, event

    def invalidate_active_areas_for_shooter(
        self,
        working: WorldState,
        shooter_id: int,
        *,
        action_id: str,
        action_sequence: int,
    ) -> tuple[EngineEvent, ...]:
        """Cancel one shooter's ACTIVE areas when authoritative movement starts."""

        if type(shooter_id) is not int or shooter_id < 0:
            raise ValueError("shooter_id must be a non-negative exact int")
        active = tuple(sorted(
            (
                mission
                for mission in working.indirect_fire_missions
                if mission.shooter_id == shooter_id
                and mission.phase is IndirectFireMissionState.ACTIVE
            ),
            key=fire_mission_sort_key,
        ))
        if not active:
            return ()
        replacements: dict[str, FireMission] = {}
        events: list[EngineEvent] = []
        for mission in active:
            cancelled = replace(
                mission,
                phase=IndirectFireMissionState.CANCELLED,
                cancelable=False,
                cancellation_reason=(
                    IndirectFireCancellationReason.MISSION_INVALIDATED
                ),
                active_remaining_seconds=0,
                active_elapsed_remainder_ms=0,
                active_started_tick=None,
                impact_hex=None,
                affected_operator_ids=(),
                scatter_evidence=None,
                observation_evidence=None,
                terminal_outcome=None,
            )
            replacements[mission.mission_id] = cancelled
            events.append(self.mission_event(
                working,
                EventType.INDIRECT_FIRE_MISSION_CANCELLED,
                cancelled,
                {
                    "action_id": action_id,
                    "cancellation_reason": (
                        IndirectFireCancellationReason.MISSION_INVALIDATED.value
                    ),
                    "invalidation_reason": "SHOOTER_MOVEMENT_STARTED",
                    "cancelled_impact_hex": mission.impact_hex,
                    "cancelled_affected_operator_ids": list(
                        mission.affected_operator_ids
                    ),
                    "ammo_refunded": 0,
                    "planned_cooldown_changed": False,
                },
                action_sequence=action_sequence,
            ))
        working.indirect_fire_missions = tuple(sorted(
            (
                replacements.get(mission.mission_id, mission)
                for mission in working.indirect_fire_missions
            ),
            key=fire_mission_sort_key,
        ))
        return tuple(events)

    @staticmethod
    def advance_to_resolution(
        working: WorldState,
        delta_time_ms: int,
    ) -> Mapping[str, int]:
        active_deltas = dict(indirect_fire_active_delta_ms(
            working.indirect_fire_missions,
            delta_time_ms,
        ))
        working.indirect_fire_missions = advance_indirect_fire_missions(
            working.indirect_fire_missions,
            delta_time_ms,
            current_tick=working.tick,
        )
        return active_deltas

    @staticmethod
    def advance_active_areas(
        working: WorldState,
        delta_time_ms: int,
        active_delta_by_mission: Mapping[str, int],
    ) -> tuple[str, ...]:
        missions, expired = advance_indirect_fire_active_missions(
            working.indirect_fire_missions,
            delta_time_ms,
            current_tick=working.tick,
            delta_time_ms_by_mission=active_delta_by_mission,
        )
        working.indirect_fire_missions = missions
        return expired

    def resolve_impact(
        self,
        pre_state: WorldState,
        rng_snapshot: Mapping[str, int],
        mission_id: str,
        *,
        authority: IndirectFireAuthority,
        execution_policy: IndirectFireExecutionPolicy | None,
        observation_authority: IndirectFireObservationAuthority,
        observation_evidence: FireMissionObservationEvidence,
        spotter_evidence: str,
    ) -> IndirectFireImpactValidationResult:
        mission = next((
            value for value in pre_state.indirect_fire_missions
            if value.mission_id == mission_id
        ), None)
        if mission is None:
            return _impact_failure("MISSION_NOT_FOUND", "mission_not_found")
        if mission.phase is not IndirectFireMissionState.ACTIVE:
            return _impact_failure("MISSION_NOT_ACTIVE", "mission_not_active")
        if mission.impact_hex is not None:
            return _impact_failure(
                "MISSION_IMPACT_ALREADY_RESOLVED", "impact_hex"
            )
        profile = authority.profile(mission.profile_id)
        if (
            profile is None
            or profile.weapon_id != mission.weapon_id
            or mission.authority_version != INDIRECT_FIRE_AUTHORITY_SCHEMA_VERSION
            or not isinstance(execution_policy, IndirectFireExecutionPolicy)
            or mission.execution_policy_id != execution_policy.policy_id
            or mission.execution_policy_hash != execution_policy.content_hash
        ):
            return _impact_failure("POLICY_UNAVAILABLE", "mission_policy_mismatch")
        try:
            observation_policy = IndirectFireObservationPolicy(
                observation_evidence.correction_level
            )
        except ValueError:
            return _impact_failure("UNRESOLVED", "observation_policy")
        if observation_policy is IndirectFireObservationPolicy.UNRESOLVED:
            return _impact_failure("UNRESOLVED", "observation_policy")
        shooter = pre_state.operators.get(mission.shooter_id)
        if shooter is None or shooter.current_hex is None:
            return _impact_failure(
                "SHOOTER_EVIDENCE_UNAVAILABLE", "shooter_position"
            )
        if (
            observation_evidence.state_hash
            != canonical_hash(pre_state.to_dict())
            or observation_evidence.state_tick != pre_state.tick
            or observation_evidence.state_time_ms != pre_state.elapsed_time_ms
            or observation_evidence.planned_hex != mission.planned_hex
            or observation_evidence.firing_side
            != shooter.side
            or observation_evidence.policy_id
            != observation_authority.policy_id
            or observation_evidence.policy_hash
            != observation_authority.content_hash
            or observation_evidence.source_interface
            != observation_authority.observation_interface
        ):
            return _impact_failure("UNRESOLVED", "observation_evidence")
        if self.source_map is None or self.adjudication_tables is None:
            return _impact_failure(
                "IMPACT_RUNTIME_UNAVAILABLE", "source_hex_map"
            )
        try:
            self.source_map.cell(mission.planned_hex)
        except ValueError:
            return _impact_failure("PLANNED_HEX_NOT_FOUND", "planned_hex")

        before = IndirectFireRngState(
            seed=int(rng_snapshot["seed"]),
            state=int(rng_snapshot["state"]),
            draws=int(rng_snapshot["draws"]),
        )
        detached_rng = NativeRNG.restore(dict(rng_snapshot))
        direction_roll = detached_rng.randbelow(6) + 1
        distance_dice = (
            detached_rng.randbelow(6) + 1,
            detached_rng.randbelow(6) + 1,
        )
        distance_roll = sum(distance_dice)
        distance_band, band_index = _distance_band(hex_distance(
            int(shooter.current_hex), mission.planned_hex,
        ))
        table_name = {
            IndirectFireObservationPolicy.UNOBSERVED: "unobserved_dispersion",
            IndirectFireObservationPolicy.GRID_CORRECTED: (
                "grid_corrected_dispersion"
            ),
            IndirectFireObservationPolicy.TARGET_CORRECTED: (
                "target_corrected_dispersion"
            ),
        }[observation_policy]
        dispersion = self.adjudication_tables.document["indirect_fire"][
            table_name
        ]
        roll_result = dispersion["by_roll"][str(distance_roll)]
        table_result = str(
            roll_result[band_index]
            if isinstance(roll_result, list)
            else roll_result
        )
        scatter_distance = _scatter_distance(table_result)
        after_snapshot = detached_rng.snapshot()
        after = IndirectFireRngState(
            seed=int(after_snapshot["seed"]),
            state=int(after_snapshot["state"]),
            draws=int(after_snapshot["draws"]),
        )
        transition = IndirectFireRngTransition(before, after)
        scatter = IndirectFireScatterResult(
            direction_roll=direction_roll,
            direction_index=direction_roll - 1,
            distance_dice=distance_dice,
            distance_roll=distance_roll,
            distance_band=distance_band,
            table_id=f"indirect_fire.{table_name}",
            table_result=table_result,
            scatter_distance=scatter_distance,
        )
        evidence = IndirectFireResolutionEvidence(
            observation_policy=observation_policy,
            spotter_evidence=spotter_evidence,
            observation_evidence=observation_evidence,
            affected_hex_policy="IMPACT_HEX_ONLY",
            operator_order="OPERATOR_ID_ASCENDING",
            pre_state_hash=canonical_hash(pre_state.to_dict()),
            adjudication_table_hash=self.adjudication_tables.content_hash,
        )
        impact_hex = mission.planned_hex
        out_of_map = False
        for _ in range(scatter_distance):
            neighbor = int(
                self.source_map.cell(impact_hex).neighbors[direction_roll - 1]
            )
            if neighbor < 0:
                out_of_map = True
                break
            try:
                self.source_map.cell(neighbor)
            except ValueError:
                out_of_map = True
                break
            impact_hex = neighbor
        if out_of_map:
            return IndirectFireImpactValidationResult(
                valid=True,
                reason="IMPACT_SCATTER_OUT_OF_MAP",
                blockers=(),
                miss_resolution=IndirectFireMissResolution(
                    mission_id=mission.mission_id,
                    shooter_id=mission.shooter_id,
                    weapon_id=mission.weapon_id,
                    planned_hex=mission.planned_hex,
                    outcome=SCATTER_OUT_OF_MAP_OUTCOME,
                    execution_policy_id=mission.execution_policy_id,
                    execution_policy_hash=mission.execution_policy_hash,
                    scatter_result=scatter,
                    rng_transition=transition,
                    resolution_evidence=evidence,
                ),
            )
        request = _build_validated_indirect_fire_impact_request(
            mission_id=mission.mission_id,
            tick=pre_state.tick,
            shooter_id=mission.shooter_id,
            weapon_id=mission.weapon_id,
            planned_hex=mission.planned_hex,
            impact_hex=impact_hex,
            scatter_result=scatter,
            execution_policy_id=mission.execution_policy_id,
            execution_policy_hash=mission.execution_policy_hash,
            rng_before=before,
            rng_after=after,
            spotter_evidence=spotter_evidence,
            observation_policy=observation_policy,
        )
        affected = self.applicable_target_ids(pre_state, impact_hex)
        resolution = IndirectFireResolution(
            mission_id=mission.mission_id,
            shooter_id=mission.shooter_id,
            weapon_id=mission.weapon_id,
            planned_hex=mission.planned_hex,
            impact_hex=impact_hex,
            execution_policy_id=mission.execution_policy_id,
            execution_policy_hash=mission.execution_policy_hash,
            affected_operator_ids=affected,
            scatter_result=scatter,
            rng_transition=transition,
            resolution_evidence=evidence,
        )
        bundle = IndirectFireEffectBundle(
            mission_id=mission.mission_id,
            shooter_id=mission.shooter_id,
            weapon_id=mission.weapon_id,
            planned_hex=mission.planned_hex,
            impact_hex=impact_hex,
            execution_policy_id=mission.execution_policy_id,
            execution_policy_hash=mission.execution_policy_hash,
            affected_units=affected,
            scatter_result=scatter,
            resolution_evidence=evidence,
            rng_transition=transition,
        )
        return IndirectFireImpactValidationResult(
            valid=True,
            reason="IMPACT_RESOLUTION_VALID",
            blockers=(),
            request=request,
            resolution=resolution,
            effect_bundle=bundle,
        )

    def resolve_effects(
        self,
        pre_state: WorldState,
        rng_snapshot: Mapping[str, int],
        impact: IndirectFireResolution,
        *,
        pre_state_hash: str,
        authority: IndirectFireAuthority,
        damage_policy: IndirectFireDamagePolicy,
        affected_operator_ids: tuple[int, ...] | None = None,
    ) -> IndirectFireEffectResolutionResult:
        if self.source_map is None or self.adjudication_tables is None:
            return _effect_failure("impact_runtime_unavailable")
        if pre_state_hash != impact.resolution_evidence.pre_state_hash:
            return _effect_failure("pre_state_hash")
        if dict(rng_snapshot) != impact.rng_transition.before.to_record():
            return _effect_failure("authoritative_rng_state")
        if self.adjudication_tables.content_hash != (
            impact.resolution_evidence.adjudication_table_hash
        ):
            return _effect_failure("adjudication_table_hash")
        mission = next((
            value for value in pre_state.indirect_fire_missions
            if value.mission_id == impact.mission_id
        ), None)
        if mission is None or mission.phase is not IndirectFireMissionState.ACTIVE:
            return _effect_failure("mission_not_active")
        profile = authority.profile(mission.profile_id)
        if profile is None or profile.weapon_id != mission.weapon_id:
            return _effect_failure("artillery_profile")
        shooter = pre_state.operators.get(mission.shooter_id)
        if shooter is None or not shooter.alive or not shooter.deployed:
            return _effect_failure("shooter")
        occupants = self.applicable_target_ids(pre_state, impact.impact_hex)
        actual = occupants if affected_operator_ids is None else affected_operator_ids
        if (
            affected_operator_ids is not None
            and (
                affected_operator_ids != tuple(sorted(set(affected_operator_ids)))
                or any(value not in occupants for value in affected_operator_ids)
            )
        ):
            return _effect_failure("affected_operator_evidence")
        if actual != impact.affected_operator_ids:
            return _effect_failure("affected_operator_evidence")
        targets: list[IndirectFireTargetEvidence] = []
        for operator_id in actual:
            target = pre_state.operators[operator_id]
            armor_value = str(target.raw_attributes.get(
                "armor_class",
                target.raw_attributes.get("normalized_value", {}).get(
                    "armor_class", "UNKNOWN"
                ),
            )).upper()
            try:
                armor = ArmorClass(armor_value)
            except ValueError:
                armor = ArmorClass.UNKNOWN
            targets.append(IndirectFireTargetEvidence(
                operator_id=target.operator_id,
                domain=_unit_domain(target.operator_type),
                armor=armor,
                blood=int(target.blood),
                terrain=self.source_map.terrain(int(target.current_hex)).value,
                concealed=target.concealed,
                moving=not target.stopped,
                stacked=len(actual) > 1,
                marching=target.movement_mode == MovementMode.MARCHING.value,
            ))
        return resolve_damage_effects(
            impact,
            shooter_id=mission.shooter_id,
            weapon_id=mission.weapon_id,
            artillery_class=profile.artillery_class,
            shooter_strength=int(shooter.blood),
            targets=tuple(targets),
            table_document=self.adjudication_tables.document,
            table_content_hash=self.adjudication_tables.content_hash,
            damage_policy=damage_policy,
        )

    def apply_area_bundle(
        self,
        working: WorldState,
        bundle: IndirectFireEffectBundle,
        *,
        initialize_area: bool,
    ) -> tuple[EngineEvent, ...]:
        mission = next((
            value for value in working.indirect_fire_missions
            if value.mission_id == bundle.mission_id
        ), None)
        if mission is None or mission.phase is not IndirectFireMissionState.ACTIVE:
            raise ValueError("INDIRECT_FIRE_MISSION_NOT_ACTIVE")
        if initialize_area:
            if mission.impact_hex is not None:
                raise ValueError("INDIRECT_FIRE_IMPACT_ALREADY_COMMITTED")
        elif mission.impact_hex != bundle.impact_hex:
            raise ValueError("INDIRECT_FIRE_ACTIVE_HEX_MISMATCH")
        if set(mission.affected_operator_ids).intersection(bundle.affected_units):
            raise ValueError("INDIRECT_FIRE_OPERATOR_ALREADY_RESOLVED")
        blood_before = {
            effect.operator_id: int(working.operators[effect.operator_id].blood)
            for effect in bundle.damage_effects
            if effect.operator_id in working.operators
        }
        self._apply_damage_effects(working, bundle)
        blood_after = {
            effect.operator_id: int(working.operators[effect.operator_id].blood)
            for effect in bundle.damage_effects
            if effect.operator_id in working.operators
        }
        affected_ids = tuple(sorted({
            *mission.affected_operator_ids,
            *bundle.affected_units,
        }))
        scatter_evidence = (
            FireMissionScatterEvidence(
                direction_roll=bundle.scatter_result.direction_roll,
                direction_index=bundle.scatter_result.direction_index,
                distance_dice=bundle.scatter_result.distance_dice,
                distance_roll=bundle.scatter_result.distance_roll,
                distance_band=bundle.scatter_result.distance_band,
                table_id=bundle.scatter_result.table_id,
                table_result=bundle.scatter_result.table_result,
                scatter_distance=bundle.scatter_result.scatter_distance,
            )
            if initialize_area else mission.scatter_evidence
        )
        active = replace(
            mission,
            impact_hex=bundle.impact_hex,
            scatter_evidence=scatter_evidence,
            observation_evidence=(
                bundle.resolution_evidence.observation_evidence
                if initialize_area else mission.observation_evidence
            ),
            affected_operator_ids=affected_ids,
            active_started_tick=(
                working.tick if initialize_area else mission.active_started_tick
            ),
            active_remaining_seconds=(
                mission.active_duration_seconds
                if initialize_area else mission.active_remaining_seconds
            ),
        )
        working.indirect_fire_missions = tuple(sorted(
            (
                active if value.mission_id == mission.mission_id else value
                for value in working.indirect_fire_missions
            ),
            key=fire_mission_sort_key,
        ))
        events: list[EngineEvent] = []
        if initialize_area:
            events.append(self.active_started_event(working, active, bundle))
        events.extend(
            self.area_triggered_event(
                working,
                active,
                effect,
                target_blood_before=blood_before[effect.operator_id],
                target_blood_after=blood_after[effect.operator_id],
            )
            for effect in bundle.damage_effects
        )
        return tuple(events)

    def _apply_damage_effects(
        self,
        working: WorldState,
        bundle: IndirectFireEffectBundle,
    ) -> None:
        if self.suppression_ms is None:
            raise RuntimeError("INDIRECT_FIRE_REQUIRES_SOURCE_RUNTIME")
        for effect in bundle.damage_effects:
            target = working.operators.get(effect.operator_id)
            if (
                target is None
                or not target.alive
                or not target.deployed
                or target.current_hex != bundle.impact_hex
            ):
                raise ValueError("INDIRECT_FIRE_EFFECT_TARGET_NOT_CURRENT")
            before = int(target.blood)
            if effect.damage_result in {"NO_EFFECT", "SUPPRESS"}:
                losses = 0
            elif effect.damage_result.startswith("LOSS_"):
                try:
                    losses = int(effect.damage_result.split("_", 1)[1])
                except ValueError as exc:
                    raise ValueError(
                        "INDIRECT_FIRE_EFFECT_INVALID_DAMAGE_RESULT"
                    ) from exc
                if losses <= 0 or losses > before:
                    raise ValueError("INDIRECT_FIRE_EFFECT_INVALID_LOSSES")
            else:
                raise ValueError("INDIRECT_FIRE_EFFECT_INVALID_DAMAGE_RESULT")
            after = before - losses
            destroyed = (
                IndirectFireTriState.TRUE
                if after == 0 else IndirectFireTriState.FALSE
            )
            if effect.destroyed_result is not destroyed:
                raise ValueError("INDIRECT_FIRE_EFFECT_DESTROYED_MISMATCH")
            suppressed = (
                IndirectFireTriState.TRUE
                if effect.damage_result == "SUPPRESS"
                else IndirectFireTriState.FALSE
            )
            if effect.suppression_result is not suppressed:
                raise ValueError("INDIRECT_FIRE_EFFECT_SUPPRESSION_MISMATCH")
            target.blood = after
            target.health = target.max_health * after / int(target.max_blood)
            if after == 0:
                destroy_source_operator(working, target)
            elif suppressed is IndirectFireTriState.TRUE:
                target.state = "SUPPRESSED"
                target.suppression_remaining_ms = self.suppression_ms
                if (
                    target.operator_type.upper() == "PERSONNEL"
                    and target.moving
                    and not target.movement_stop_requested
                ):
                    target.movement_task_pause_reason = (
                        MovementTaskPauseReason.SUPPRESSION
                    )

    def apply_miss(
        self,
        working: WorldState,
        current_rng_snapshot: Mapping[str, int],
        miss: IndirectFireMissResolution,
    ) -> EngineEvent:
        if self.adjudication_tables is None:
            raise RuntimeError("INDIRECT_FIRE_IMPACT_RUNTIME_UNAVAILABLE")
        if canonical_hash(working.to_dict()) != (
            miss.resolution_evidence.pre_state_hash
        ):
            raise ValueError("INDIRECT_FIRE_MISS_PRESTATE_MISMATCH")
        if dict(current_rng_snapshot) != miss.rng_transition.before.to_record():
            raise ValueError("INDIRECT_FIRE_MISS_RNG_PRESTATE_MISMATCH")
        if self.adjudication_tables.content_hash != (
            miss.resolution_evidence.adjudication_table_hash
        ):
            raise ValueError("INDIRECT_FIRE_MISS_TABLE_MISMATCH")
        mission = next((
            value for value in working.indirect_fire_missions
            if value.mission_id == miss.mission_id
        ), None)
        if mission is None or mission.phase is not IndirectFireMissionState.ACTIVE:
            raise ValueError("INDIRECT_FIRE_MISSION_NOT_ACTIVE")
        if (
            mission.shooter_id != miss.shooter_id
            or mission.weapon_id != miss.weapon_id
            or mission.planned_hex != miss.planned_hex
            or mission.execution_policy_id != miss.execution_policy_id
            or mission.execution_policy_hash != miss.execution_policy_hash
            or mission.impact_hex is not None
        ):
            raise ValueError("INDIRECT_FIRE_MISS_MISSION_MISMATCH")
        resolved = replace(
            mission,
            phase=IndirectFireMissionState.RESOLVED,
            cancelable=False,
            active_remaining_seconds=0,
            active_elapsed_remainder_ms=0,
            active_started_tick=None,
            impact_hex=None,
            affected_operator_ids=(),
            scatter_evidence=FireMissionScatterEvidence(
                direction_roll=miss.scatter_result.direction_roll,
                direction_index=miss.scatter_result.direction_index,
                distance_dice=miss.scatter_result.distance_dice,
                distance_roll=miss.scatter_result.distance_roll,
                distance_band=miss.scatter_result.distance_band,
                table_id=miss.scatter_result.table_id,
                table_result=miss.scatter_result.table_result,
                scatter_distance=miss.scatter_result.scatter_distance,
            ),
            observation_evidence=miss.resolution_evidence.observation_evidence,
            terminal_outcome=miss.outcome,
        )
        working.indirect_fire_missions = tuple(sorted(
            (
                resolved if value.mission_id == mission.mission_id else value
                for value in working.indirect_fire_missions
            ),
            key=fire_mission_sort_key,
        ))
        return self.resolved_event(
            working, resolved, rng_transition=miss.rng_transition
        )

    def active_resolution(
        self,
        world: WorldState,
        rng_snapshot: Mapping[str, int],
        mission: FireMission,
        operator_ids: tuple[int, ...],
        *,
        spotter_evidence: str,
    ) -> IndirectFireResolution:
        if self.adjudication_tables is None:
            raise RuntimeError("INDIRECT_FIRE_IMPACT_RUNTIME_UNAVAILABLE")
        if (
            mission.impact_hex is None
            or mission.scatter_evidence is None
            or mission.observation_evidence is None
        ):
            raise ValueError("INDIRECT_FIRE_IMPACT_EVIDENCE_UNAVAILABLE")
        if operator_ids != tuple(sorted(set(operator_ids))):
            raise ValueError("INDIRECT_FIRE_OPERATOR_ORDER_INVALID")
        rng_state = IndirectFireRngState(
            seed=int(rng_snapshot["seed"]),
            state=int(rng_snapshot["state"]),
            draws=int(rng_snapshot["draws"]),
        )
        scatter = mission.scatter_evidence
        observation_policy = IndirectFireObservationPolicy(
            mission.observation_evidence.correction_level
        )
        scatter_result = IndirectFireScatterResult(
            direction_roll=scatter.direction_roll,
            direction_index=scatter.direction_index,
            distance_dice=scatter.distance_dice,
            distance_roll=scatter.distance_roll,
            distance_band=scatter.distance_band,
            table_id=scatter.table_id,
            table_result=scatter.table_result,
            scatter_distance=scatter.scatter_distance,
        )
        return IndirectFireResolution(
            mission_id=mission.mission_id,
            shooter_id=mission.shooter_id,
            weapon_id=mission.weapon_id,
            planned_hex=mission.planned_hex,
            impact_hex=mission.impact_hex,
            execution_policy_id=mission.execution_policy_id,
            execution_policy_hash=mission.execution_policy_hash,
            affected_operator_ids=operator_ids,
            scatter_result=scatter_result,
            rng_transition=IndirectFireRngTransition(rng_state, rng_state),
            resolution_evidence=IndirectFireResolutionEvidence(
                observation_policy=observation_policy,
                spotter_evidence=spotter_evidence,
                observation_evidence=mission.observation_evidence,
                affected_hex_policy="IMPACT_HEX_ONLY",
                operator_order="OPERATOR_ID_ASCENDING",
                pre_state_hash=canonical_hash(world.to_dict()),
                adjudication_table_hash=self.adjudication_tables.content_hash,
            ),
        )

    def expired_events(
        self,
        working: WorldState,
        mission_ids: Sequence[str],
    ) -> tuple[EngineEvent, ...]:
        by_id = {
            mission.mission_id: mission
            for mission in working.indirect_fire_missions
        }
        events: list[EngineEvent] = []
        for mission_id in sorted(mission_ids):
            mission = by_id.get(mission_id)
            if (
                mission is None
                or mission.phase is not IndirectFireMissionState.RESOLVED
            ):
                raise RuntimeError("INDIRECT_FIRE_ACTIVE_EXPIRY_MISMATCH")
            events.append(self.resolved_event(working, mission))
        return tuple(events)

    @staticmethod
    def active_started_event(
        working: WorldState,
        mission: FireMission,
        bundle: IndirectFireEffectBundle,
    ) -> EngineEvent:
        sequence = working.next_event_sequence
        working.next_event_sequence += 1
        shooter = working.operators.get(mission.shooter_id)
        return EngineEvent(
            tick=working.tick,
            event_type=EventType.INDIRECT_FIRE_ACTIVE_STARTED,
            side=None if shooter is None else shooter.side,
            actor_id=mission.shooter_id,
            data={
                "mission_id": mission.mission_id,
                "weapon_id": mission.weapon_id,
                "planned_hex": mission.planned_hex,
                "impact_hex": bundle.impact_hex,
                "active_remaining_seconds": mission.active_remaining_seconds,
                "scatter_evidence": _scatter_record(bundle.scatter_result),
                "observation_evidence": (
                    bundle.resolution_evidence.observation_evidence.to_dict()
                ),
                "event_time_ms": working.elapsed_time_ms,
                "action_sequence": -1,
                "event_sequence": sequence,
            },
        )

    @staticmethod
    def area_triggered_event(
        working: WorldState,
        mission: FireMission,
        effect: IndirectFireDamageEffect,
        *,
        target_blood_before: int,
        target_blood_after: int,
    ) -> EngineEvent:
        sequence = working.next_event_sequence
        working.next_event_sequence += 1
        shooter = working.operators.get(mission.shooter_id)
        return EngineEvent(
            tick=working.tick,
            event_type=EventType.INDIRECT_FIRE_AREA_TRIGGERED,
            side=None if shooter is None else shooter.side,
            actor_id=mission.shooter_id,
            target_id=effect.operator_id,
            data={
                "mission_id": mission.mission_id,
                "impact_hex": mission.impact_hex,
                "operator_id": effect.operator_id,
                "damage_summary": _damage_effect_record(
                    effect,
                    target_blood_before=target_blood_before,
                    target_blood_after=target_blood_after,
                ),
                "rng_transition": (
                    None
                    if effect.resolution_evidence.rng_transition is None
                    else effect.resolution_evidence.rng_transition.to_record()
                ),
                "event_time_ms": working.elapsed_time_ms,
                "action_sequence": -1,
                "event_sequence": sequence,
            },
        )

    @staticmethod
    def resolved_event(
        working: WorldState,
        mission: FireMission,
        *,
        rng_transition: IndirectFireRngTransition | None = None,
    ) -> EngineEvent:
        sequence = working.next_event_sequence
        working.next_event_sequence += 1
        shooter = working.operators.get(mission.shooter_id)
        data: dict[str, Any] = {
            "mission_id": mission.mission_id,
            "shooter_id": mission.shooter_id,
            "weapon_id": mission.weapon_id,
            "planned_hex": mission.planned_hex,
            "impact_hex": mission.impact_hex,
            "affected_units": list(mission.affected_operator_ids),
            "active_duration_seconds": mission.active_duration_seconds,
            "policy_hash": mission.execution_policy_hash,
            "event_time_ms": working.elapsed_time_ms,
            "action_sequence": -1,
            "event_sequence": sequence,
        }
        if mission.terminal_outcome is not None:
            data.update({
                "outcome": mission.terminal_outcome,
                "native_evidence": {
                    "scatter_evidence": (
                        None
                        if mission.scatter_evidence is None
                        else mission.scatter_evidence.to_dict()
                    ),
                    "rng_transition": (
                        None
                        if rng_transition is None
                        else rng_transition.to_record()
                    ),
                    "observation_evidence": (
                        None
                        if mission.observation_evidence is None
                        else mission.observation_evidence.to_dict()
                    ),
                },
            })
        return EngineEvent(
            tick=working.tick,
            event_type=EventType.INDIRECT_FIRE_RESOLVED,
            side=None if shooter is None else shooter.side,
            actor_id=mission.shooter_id,
            data=data,
        )

    @staticmethod
    def mission_event(
        working: WorldState,
        event_type: EventType,
        mission: FireMission,
        data: Mapping[str, Any],
        *,
        action_sequence: int = -1,
    ) -> EngineEvent:
        sequence = working.next_event_sequence
        working.next_event_sequence += 1
        shooter = working.operators.get(mission.shooter_id)
        return EngineEvent(
            tick=working.tick,
            event_type=event_type,
            side=None if shooter is None else shooter.side,
            actor_id=mission.shooter_id,
            data={
                **data,
                "mission_id": mission.mission_id,
                "event_time_ms": working.elapsed_time_ms,
                "action_sequence": action_sequence,
                "event_sequence": sequence,
            },
        )


def _ammunition(operator: Any) -> dict[str, int]:
    value = operator.raw_attributes.get("ammunition")
    if not isinstance(value, Mapping):
        source = operator.raw_attributes.get("source_frame", {})
        value = (
            source.get("remain_bullet_nums", {})
            if isinstance(source, Mapping) else {}
        )
    return {str(key): int(item) for key, item in value.items()}


def _impact_failure(
    reason: str,
    *blockers: str,
) -> IndirectFireImpactValidationResult:
    return IndirectFireImpactValidationResult(
        valid=False,
        reason=reason,
        blockers=tuple(sorted(set(blockers))),
    )


def _effect_failure(*blockers: str) -> IndirectFireEffectResolutionResult:
    return IndirectFireEffectResolutionResult(
        status=IndirectFireEffectStatus.INVALID,
        blockers=tuple(sorted(set(blockers))),
        effect_bundle=None,
    )


def _distance_band(distance: int) -> tuple[str, int]:
    if type(distance) is not int or distance < 0:
        raise ValueError("indirect-fire distance must be non-negative")
    if distance <= 20:
        return "1-20", 0
    if distance <= 40:
        return "21-40", 1
    if distance <= 60:
        return "41-60", 2
    return "61+", 3


def _scatter_distance(table_result: str) -> int:
    if table_result in {"HIT", "SCATTER"}:
        return 0
    if table_result.startswith("SCATTER_"):
        try:
            distance = int(table_result[len("SCATTER_"):])
        except ValueError as exc:
            raise ValueError(
                "invalid indirect-fire scatter table result"
            ) from exc
        if distance > 0:
            return distance
    raise ValueError("invalid indirect-fire scatter table result")


def _unit_domain(value: str) -> UnitDomain:
    try:
        return UnitDomain(str(value).upper())
    except ValueError:
        return UnitDomain.UNKNOWN


def _scatter_record(result: IndirectFireScatterResult) -> dict[str, object]:
    return {
        "direction_roll": result.direction_roll,
        "direction_index": result.direction_index,
        "distance_dice": list(result.distance_dice),
        "distance_roll": result.distance_roll,
        "distance_band": result.distance_band,
        "table_id": result.table_id,
        "table_result": result.table_result,
        "scatter_distance": result.scatter_distance,
    }


def _damage_effect_record(
    effect: IndirectFireDamageEffect,
    *,
    target_blood_before: int,
    target_blood_after: int,
) -> dict[str, object]:
    evidence = effect.resolution_evidence
    return {
        "operator_id": effect.operator_id,
        "impact_hex": effect.impact_hex,
        "weapon_id": effect.weapon_id,
        "artillery_class": effect.artillery_class.value,
        "damage_result": effect.damage_result,
        "suppression_result": effect.suppression_result.value,
        "destroyed_result": effect.destroyed_result.value,
        "target_blood_before": target_blood_before,
        "target_blood_after": target_blood_after,
        "actual_blood_loss": target_blood_before - target_blood_after,
        "modifiers": [
            {"name": value.name, "value": value.value}
            for value in effect.modifiers
        ],
        "resolution_evidence": {
            "status": evidence.status.value,
            "blockers": list(evidence.blockers),
            "base_table_id": evidence.base_table_id,
            "base_dice": (
                None if evidence.base_dice is None else list(evidence.base_dice)
            ),
            "base_result": evidence.base_result,
            "correction_die": evidence.correction_die,
            "modified_roll": evidence.modified_roll,
            "result_correction": evidence.result_correction,
            "table_content_hash": evidence.table_content_hash,
            "rng_transition": evidence.rng_transition.to_record(),
            "native_convention_flags": list(evidence.native_convention_flags),
        },
    }


__all__ = ["IndirectFireLifecycleService"]
