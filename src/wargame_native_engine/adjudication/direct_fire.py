from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from wargame_native_engine.adjudication.dice import DiceRecord, DiceSource
from wargame_native_engine.adjudication.modifiers import roll_modifiers
from wargame_native_engine.adjudication.fire_readiness import weapon_posture_failure, weapon_binding_failure
from wargame_native_engine.adjudication.outcome import CombatOutcome
from wargame_native_engine.adjudication.request import (
    ActionLegalityResult,
    DirectFireRequest,
    DirectFireResourcePolicy,
)
from wargame_native_engine.adjudication.table_loader import (
    AdjudicationTableSet, TableLookup, UnresolvedTableCellError,
)
from wargame_native_engine.compat.source_frame.models import UnitDomain
from wargame_native_engine.core.event import EngineEvent, EventType, event_sort_key
from wargame_native_engine.io.serialization import canonical_hash


@dataclass(frozen=True, slots=True)
class DirectFireResolution:
    adjudication_id: str
    legality: ActionLegalityResult
    outcome: CombatOutcome | None
    events: tuple[EngineEvent, ...]
    ammunition_after: int | None
    cooldown_after: int
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"adjudication_id": self.adjudication_id,
                "legality": self.legality.to_dict(),
                "outcome": None if self.outcome is None else self.outcome.to_dict(),
                "events": [item.to_dict() for item in self.events],
                "ammunition_after": self.ammunition_after,
                "cooldown_after": self.cooldown_after, "error": self.error}


class DirectFireAdjudicator:
    def __init__(self, tables: AdjudicationTableSet, dice: DiceSource) -> None:
        self.tables = tables
        self.dice = dice

    def validate(self, request: DirectFireRequest) -> ActionLegalityResult:
        posture_failure = weapon_posture_failure(
            moving=request.attacker_moving, locked=request.weapon_locked,
            unfolded=request.weapon_unfolded,
            requires_unfolded=request.weapon.requires_unfolded,
            can_fire_while_moving=request.weapon.can_fire_while_moving,
            ordinary_direct=request.permits_out_of_range_attempt,
        )
        binding_failure = weapon_binding_failure(weapon_id=request.weapon.weapon_id,
            locked=request.weapon_locked, locked_weapon_id=request.locked_weapon_id)
        checks: list[tuple[str, bool, str]] = [
            ("weapon_binding", binding_failure is None, "weapon must match the explicit lock"),
            ("units_alive", request.attacker.alive and request.target.alive,
             "attacker and target must exist and be alive"),
            ("enemy_target", request.attacker.side != request.target.side,
             "target must be an enemy"),
            ("weapon_carried", self._weapon_is_carried(request),
             "weapon must belong to attacker"),
            ("ammunition_available", self._attack_resource_available(request),
             "ammunition must be available"),
            ("target_domain_supported", request.target_domain in request.weapon.target_domains,
             "weapon must support target category"),
            ("target_observed", request.visibility.observable,
             "target must be observable under native rules"),
            ("within_weapon_range", request.distance <= request.weapon.maximum_range,
             "target must be within weapon range"),
            ("weapon_unfolded", posture_failure != "WEAPON_NOT_UNFOLDED",
             "stationary weapon must be unfolded"),
            ("moving_weapon_locked", posture_failure != "WEAPON_NOT_LOCKED",
             "moving direct fire requires a locked weapon"),
            ("weapon_cooled", request.weapon_cooldown_before <= 0,
             "weapon cooldown must have ended"),
            ("attacker_not_transitioning", not request.attacker_transitioning,
             "attacker state does not allow firing"),
            ("movement_allows_fire", posture_failure != "ATTACKER_MUST_STOP",
             "weapon cannot fire while moving"),
            ("air_unit_hovering", request.attacker.operator_type != UnitDomain.AIR.value
             or request.attacker_hovering, "air unit must hover to fire"),
            ("external_close_combat_clear",
             not request.target_external_direct_fire_prohibited,
             "external direct fire into close combat is prohibited"),
        ]
        evidence: dict[str, Any] = {
            "attacker_id": request.attacker.operator_id,
            "target_id": request.target.operator_id,
            "weapon_id": request.weapon.weapon_id,
            "distance": request.distance,
            "observation_evidence": request.visibility.to_dict(),
            "weapon_evidence": {"maximum_range": request.weapon.maximum_range,
                                "ammunition_before": request.ammunition_before,
                                "cooldown_before": request.weapon_cooldown_before,
                                "unfolded": request.weapon_unfolded,
                                "locked": request.weapon_locked,
                                "locked_weapon_id": request.locked_weapon_id},
            "state_evidence": {"attacker_state": request.attacker.state,
                               "target_state": request.target.state},
            "checks": {},
        }
        reason_codes = {
            "weapon_binding": binding_failure or "WEAPON_LOCKED_TO_OTHER_WEAPON",
            "units_alive": "UNIT_NOT_ALIVE", "enemy_target": "SAME_SIDE_TARGET",
            "weapon_carried": "WEAPON_NOT_CARRIED",
            "ammunition_available": "AMMUNITION_UNAVAILABLE",
            "target_domain_supported": "UNSUPPORTED_TARGET_DOMAIN",
            "target_observed": "TARGET_NOT_OBSERVED",
            "within_weapon_range": "TARGET_OUT_OF_RANGE",
            "weapon_unfolded": "WEAPON_NOT_UNFOLDED",
            "moving_weapon_locked": "WEAPON_NOT_LOCKED",
            "weapon_cooled": "WEAPON_ON_COOLDOWN",
            "attacker_not_transitioning": "ATTACKER_TRANSITIONING",
            "movement_allows_fire": "ATTACKER_MUST_STOP",
            "air_unit_hovering": "AIR_UNIT_MUST_HOVER",
            "external_close_combat_clear": "TARGET_IN_CLOSE_COMBAT",
        }
        if request.resource_policy is not DirectFireResourcePolicy.AMMUNITION_KEY:
            evidence["weapon_evidence"]["resource_policy"] = (
                request.resource_policy.value
            )
        for name, passed, description in checks:
            evidence["checks"][name] = passed
            if (name == "within_weapon_range"
                    and request.permits_out_of_range_attempt):
                evidence["range_effect"] = (
                    "NORMAL" if passed else "OUT_OF_RANGE_NO_EFFECT"
                )
                continue
            if not passed:
                evidence["failed_rule"] = description
                return ActionLegalityResult(False, reason_codes[name], evidence)
        return ActionLegalityResult(True, "LEGAL", evidence)

    def resolve(self, request: DirectFireRequest) -> DirectFireResolution:
        """Resolve and immediately apply one isolated direct-fire request.

        The source-compatible engine uses :meth:`stage` for a sealed joint
        batch.  Keeping this method mutating preserves the public, single-shot
        adjudicator contract used by focused rule tests and tools.
        """
        return self._resolve(request, apply_outcome=True)

    def stage(self, request: DirectFireRequest) -> DirectFireResolution:
        """Resolve against the request pre-state without mutating operators."""
        return self._resolve(request, apply_outcome=False)

    def _resolve(self, request: DirectFireRequest, *,
                 apply_outcome: bool) -> DirectFireResolution:
        adjudication_identity = {
            "tick": request.tick, "action_id": request.action_id,
            "attacker_id": request.attacker.operator_id,
            "target_id": request.target.operator_id,
            "weapon_id": request.weapon.weapon_id,
            "episode_seed": request.episode_seed,
            "table_set_hash": self.tables.content_hash,
        }
        if request.resource_policy is not DirectFireResourcePolicy.AMMUNITION_KEY:
            adjudication_identity["resource_policy"] = request.resource_policy.value
        adjudication_id = "adjudication-" + canonical_hash(
            adjudication_identity
        )[:24]
        events = [self._event(request, EventType.DIRECT_FIRE_REQUESTED,
                              {"adjudication_id": adjudication_id})]
        legality = self.validate(request)
        if not legality.legal:
            return DirectFireResolution(adjudication_id, legality, None,
                                        tuple(events), request.ammunition_before,
                                        request.weapon_cooldown_before,
                                        legality.reason_code)
        events.append(self._event(request, EventType.DIRECT_FIRE_VALIDATED,
                                  legality.to_dict()))
        if request.distance > request.weapon.maximum_range:
            # A legal ordinary DIRECT attempt still spends its shot, but never
            # consults damage tables or consumes RNG outside the weapon range.
            # In particular it cannot suppress or injure carried passengers.
            outcome = CombatOutcome(no_effect=True)
            events.append(self._event(request, EventType.COMBAT_RESULT_LOOKED_UP, {
                "table_id": "direct_fire.out_of_range",
                "distance": request.distance,
                "maximum_range": request.weapon.maximum_range,
                "reason_code": "OUT_OF_RANGE_NO_EFFECT",
                "final_outcome": outcome.to_dict(),
                "native_convention_flags": ["DIRECT_OUT_OF_RANGE_ZERO_EFFECT"],
            }))
        else:
            try:
                base_level = self._attack_level(request)
                events.append(self._event(request, EventType.ATTACK_LEVEL_SELECTED,
                                          base_level.to_dict()))
                elevation = self.tables.elevation_modifier(
                    request.elevation_difference, request.distance)
                final_level = max(0, int(base_level.value) + int(elevation.value))
                events.append(self._event(request, EventType.ATTACK_LEVEL_MODIFIED, {
                    "base_attack_level": base_level.value,
                    "elevation_modifier": elevation.value,
                    "final_attack_level": final_level,
                    "elevation_cell": elevation.to_dict(),
                }))
                outcome, resolution_events = self._resolve_tables(request, final_level)
                events.extend(resolution_events)
            except (ValueError, UnresolvedTableCellError) as exc:
                return DirectFireResolution(adjudication_id, legality, None,
                                            tuple(sorted(events, key=event_sort_key)),
                                            request.ammunition_before,
                                            request.weapon_cooldown_before, str(exc))

        outcome = self._normalize_outcome(request, outcome)
        if apply_outcome and request.distance <= request.weapon.maximum_range:
            outcome, application_events = self._apply_outcome(
                request, outcome, adjudication_id)
            events.extend(application_events)
        if request.resource_policy is DirectFireResourcePolicy.AMMUNITION_KEY:
            if request.ammunition_before is None:
                raise RuntimeError("ammunition resource lacks a pre-state value")
            ammunition_after = request.ammunition_before - 1
            cooldown_after = (self.tables.timing("direct_fire_cooldown_seconds")
                              if request.weapon.cooldown_ticks is None
                              else int(request.weapon.cooldown_ticks))
            events.append(self._event(request, EventType.AMMUNITION_CONSUMED, {
                "before": request.ammunition_before, "after": ammunition_after,
                "ammunition_key": request.weapon.ammunition_key,
            }))
            events.append(self._event(request, EventType.WEAPON_COOLDOWN_STARTED, {
                "before": request.weapon_cooldown_before, "after": cooldown_after,
            }))
        else:
            ammunition_after = None
            cooldown_after = request.weapon_cooldown_before
        return DirectFireResolution(adjudication_id, legality, outcome,
                                    tuple(sorted(events, key=event_sort_key)),
                                    ammunition_after, cooldown_after)

    def _resolve_tables(self, request: DirectFireRequest,
                        final_level: int) -> tuple[CombatOutcome, list[EngineEvent]]:
        events: list[EngineEvent] = []
        if final_level == 0:
            lookup = TableLookup("direct_fire.attack_level_zero",
                                 "final_attack_level=0", "NO_EFFECT")
            events.append(self._event(request, EventType.COMBAT_RESULT_LOOKED_UP, {
                **lookup.to_dict(), "base_outcome": "NO_EFFECT",
                "final_outcome": "NO_EFFECT", "native_convention_flags": [],
            }))
            return CombatOutcome(no_effect=True), events
        infantry_vs_vehicle = (request.target_domain is UnitDomain.VEHICLE
                               and request.weapon.weapon_class.lower()
                               == "infantry_light_weapon")
        if request.target_domain is UnitDomain.VEHICLE and not infantry_vs_vehicle:
            base_roll = self._roll_2d6(request, "vehicle_base_result", events)
            base = self.tables.vehicle_base_losses(int(request.target.blood),
                                                   final_level, base_roll.raw_total)
            base_outcome: Any = int(base.value)
        else:
            base_roll = self._roll_2d6(request, "personnel_shared_base_result", events)
            base = self.tables.personnel_base_result(final_level, base_roll.raw_total)
            base_outcome = base.value
        if request.target_domain is UnitDomain.VEHICLE:
            correction_roll = self._roll_2d6(request, "vehicle_loss_correction", events)
            modifiers = roll_modifiers(
                request, self.tables.roll_modifier_config("vehicle"))
            modified_roll = correction_roll.raw_total + sum(item.value for item in modifiers)
            correction = self.tables.vehicle_loss_correction(
                modified_roll, request.target_armor.value)
            convention_flags: list[str] = []
            base_losses, base_suppression = _base_loss_and_suppression(base_outcome)
            final_losses = _clamp(base_losses + int(correction.value),
                                  0, int(request.target.blood))
            suppression = base_suppression or final_losses > 0
            if isinstance(base_outcome, str) and int(correction.value) != 0:
                convention_flags.append(
                    "NATIVE_IMPLEMENTATION_CONVENTION:symbolic_base_preserved_while_vehicle_correction_changes_losses")
            outcome = CombatOutcome(vehicle_losses=final_losses,
                                    suppression_applied=suppression,
                                    no_effect=final_losses == 0 and not suppression)
        else:
            correction_roll = self._roll_d6(request, "personnel_result_correction", events)
            modifiers = roll_modifiers(
                request, self.tables.roll_modifier_config("personnel"))
            modified_roll = correction_roll.raw_total + sum(item.value for item in modifiers)
            correction = self.tables.personnel_result_correction(modified_roll)
            outcome, convention_flags = self._personnel_outcome(
                base_outcome, int(correction.value), int(request.target.blood))
        events.append(self._event(request, EventType.ROLL_MODIFIERS_APPLIED, {
            "roll_index": correction_roll.roll_index,
            "roll_purpose": ("vehicle_loss_correction" if request.target_domain
                             is UnitDomain.VEHICLE else "personnel_result_correction"),
            "raw_roll": correction_roll.raw_total,
            "roll_modifiers": [item.to_dict() for item in modifiers],
            "modified_roll": modified_roll,
            "correction_lookup": correction.to_dict(),
        }))
        events.append(self._event(request, EventType.COMBAT_RESULT_LOOKED_UP, {
            "base_result_lookup": base.to_dict(),
            "base_outcome": base_outcome,
            "result_correction": correction.value,
            "final_outcome": outcome.to_dict(),
            "table_id": base.table_id,
            "table_coordinates": base.cell,
            "native_convention_flags": convention_flags,
        }))
        return outcome, events

    def _personnel_outcome(self, base: Any, correction: int,
                           target_strength: int) -> tuple[CombatOutcome, list[str]]:
        if isinstance(base, int):
            losses = _clamp(base + correction, 0, target_strength)
            return (CombatOutcome(squad_losses=losses, no_effect=losses == 0), [])
        table = self.tables.document["direct_fire"]["personnel_result_modifiers"]
        ordered = list(table["application"]["symbolic_base_result"]["ordered_outcomes"])
        name = "SUPPRESS" if base == "SUPPRESS" else str(base)
        if name not in ordered:
            raise UnresolvedTableCellError(f"unknown symbolic personnel result: {base}")
        shifted = ordered[_clamp(ordered.index(name) + correction, 0, len(ordered) - 1)]
        flag = ["NATIVE_IMPLEMENTATION_CONVENTION:symbolic_personnel_result_order"]
        if shifted == "NO_EFFECT":
            return CombatOutcome(no_effect=True), flag
        if shifted == "SUPPRESS":
            return CombatOutcome(suppression_applied=True), flag
        losses = _clamp(int(shifted.split("_", 1)[1]), 0, target_strength)
        return CombatOutcome(squad_losses=losses, no_effect=losses == 0), flag

    def _attack_level(self, request: DirectFireRequest) -> TableLookup:
        weapon_class = request.weapon.weapon_class
        if (request.attacker.operator_type == UnitDomain.PERSONNEL.value
                and request.target_domain is UnitDomain.VEHICLE
                and weapon_class.lower() == "infantry_light_weapon"):
            table = "infantry_vs_vehicle_attack_levels"
        elif request.target_domain is UnitDomain.VEHICLE:
            table = "direct_fire_vehicle_attack_levels"
        else:
            table = "direct_fire_personnel_attack_levels"
        return self.tables.attack_level(table, weapon_class,
                                        int(request.attacker.blood), request.distance)

    def _roll_d6(self, request: DirectFireRequest, purpose: str,
                 events: list[EngineEvent]) -> DiceRecord:
        record = self.dice.roll_d6(tick=request.tick, action_id=request.action_id)
        self._record_roll(request, record, purpose, events)
        return record

    def _roll_2d6(self, request: DirectFireRequest, purpose: str,
                  events: list[EngineEvent]) -> DiceRecord:
        record = self.dice.roll_2d6(tick=request.tick, action_id=request.action_id)
        self._record_roll(request, record, purpose, events)
        return record

    def _record_roll(self, request: DirectFireRequest, record: DiceRecord,
                     purpose: str, events: list[EngineEvent]) -> None:
        events.append(self._event(request, EventType.DICE_ROLLED,
                                  {**record.to_dict(), "roll_purpose": purpose}))

    def _apply_outcome(self, request: DirectFireRequest, outcome: CombatOutcome,
                       adjudication_id: str) -> tuple[CombatOutcome, list[EngineEvent]]:
        events: list[EngineEvent] = []
        before = int(request.target.blood)
        losses = outcome.vehicle_losses + outcome.squad_losses
        suppression = outcome.suppression_applied
        after = max(0, before - losses)
        request.target.blood = after
        request.target.health = request.target.max_health * after / int(request.target.max_blood)
        if losses:
            events.append(self._event(request, EventType.LOSSES_APPLIED, {
                "target_blood_before": before, "target_blood_after": after,
                "losses": losses, "adjudication_id": adjudication_id,
            }))
        if suppression and after > 0:
            request.target.state = "SUPPRESSED"
            duration = self.tables.document["runtime_rules"]["suppression"][
                "effective_duration_seconds"]
            request.target.raw_attributes["suppression_remaining_seconds"] = int(duration)
            events.append(self._event(request, EventType.SUPPRESSION_APPLIED, {
                "target_blood_after": after,
                "duration_seconds": int(duration),
                "timer_policy": "RESET_TO_FULL_DURATION",
                "adjudication_id": adjudication_id,
            }))
        if after == 0:
            request.target.alive = False
            request.target.state = "destroyed"
            outcome = CombatOutcome(outcome.vehicle_losses, outcome.squad_losses,
                                    outcome.suppression_applied, True, outcome.no_effect)
            events.append(self._event(request, EventType.UNIT_DESTROYED, {
                "target_blood_after": 0, "adjudication_id": adjudication_id,
            }))
        if request.target_domain is UnitDomain.VEHICLE and losses:
            for passenger in sorted(request.carried_passengers,
                                    key=lambda item: item.operator_id):
                passenger_before = int(passenger.blood)
                passenger_after = max(0, passenger_before - losses)
                passenger.blood = passenger_after
                passenger.health = passenger.max_health * passenger_after / int(passenger.max_blood)
                if passenger_after == 0:
                    passenger.alive = False
                    passenger.state = "destroyed"
                events.append(EngineEvent(
                    request.tick, EventType.LOSSES_APPLIED, passenger.side,
                    request.attacker.operator_id, passenger.operator_id,
                    {"associated_vehicle_id": request.target.operator_id,
                     "target_blood_before": passenger_before,
                     "target_blood_after": passenger_after,
                     "losses": passenger_before - passenger_after,
                     "adjudication_id": adjudication_id},
                ))
        return outcome, events

    @staticmethod
    def _normalize_outcome(request: DirectFireRequest,
                           outcome: CombatOutcome) -> CombatOutcome:
        """Apply pre-state-dependent conventions without mutating the target."""
        vehicle_losses = outcome.vehicle_losses
        squad_losses = outcome.squad_losses
        suppression = outcome.suppression_applied
        if (request.target_domain is UnitDomain.PERSONNEL and suppression
                and request.target.state == "SUPPRESSED"):
            squad_losses += 1
        destroyed = (
            vehicle_losses + squad_losses >= int(request.target.blood)
        )
        return CombatOutcome(
            vehicle_losses=vehicle_losses,
            squad_losses=squad_losses,
            suppression_applied=suppression,
            destroyed=destroyed,
            no_effect=outcome.no_effect,
        )

    def _weapon_is_carried(self, request: DirectFireRequest) -> bool:
        expected = str(request.weapon.weapon_id)
        return expected in {str(item) for item in request.attacker.weapon_ids}

    @staticmethod
    def _attack_resource_available(request: DirectFireRequest) -> bool:
        if request.resource_policy is DirectFireResourcePolicy.AMMUNITION_KEY:
            return (
                isinstance(request.weapon.ammunition_key, str)
                and bool(request.weapon.ammunition_key)
                and type(request.ammunition_before) is int
                and request.ammunition_before > 0
            )
        if request.resource_policy is DirectFireResourcePolicy.SELF_AS_MUNITION:
            return (
                request.weapon.ammunition_key is None
                and request.ammunition_before is None
                and request.attacker.alive
            )
        return False

    def _event(self, request: DirectFireRequest, event_type: EventType,
               data: Mapping[str, Any]) -> EngineEvent:
        common = {
            "action_id": request.action_id,
            "attacker_id": request.attacker.operator_id,
            "target_id": request.target.operator_id,
            "weapon_id": request.weapon.weapon_id,
            "weapon_type": request.weapon.weapon_class,
            "target_domain": request.target_domain.value,
            "distance": request.distance,
            "attacker_blood": request.attacker.blood,
            "target_blood": request.target.blood,
            "target_armor": request.target_armor.value,
            "source_rule_set": {"table_set_id": self.tables.table_set_id,
                                "table_set_version": self.tables.table_set_version,
                                "table_set_hash": self.tables.content_hash,
                                "schema_version": self.tables.schema_version,
                                "source_document_hash": self.tables.source_document_hash},
        }
        if request.resource_policy is not DirectFireResourcePolicy.AMMUNITION_KEY:
            common["resource_policy"] = request.resource_policy.value
        common.update(data)
        return EngineEvent(request.tick, event_type, request.attacker.side,
                           request.attacker.operator_id, request.target.operator_id,
                           common)


def _base_loss_and_suppression(value: Any) -> tuple[int, bool]:
    if isinstance(value, int):
        return value, False
    if value == "SUPPRESS":
        return 0, True
    if value == "NO_EFFECT":
        return 0, False
    raise UnresolvedTableCellError(f"unsupported base combat result: {value}")


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value)))
