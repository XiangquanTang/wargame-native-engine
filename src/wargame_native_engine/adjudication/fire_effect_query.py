"""Read-only fire-effect analysis over a frozen, side-authorized projection.

Enemy contact records intentionally omit armor, strength and tactical posture.
Those values must be explicit caller assumptions, even for a visible target.
Probabilities describe the declared fair-d6 rule model, never future RNG draws.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from itertools import product
from types import MappingProxyType
from typing import Any

from wargame_native_engine.adjudication.dice import FixedDiceRoller
from wargame_native_engine.adjudication.direct_fire import DirectFireAdjudicator
from wargame_native_engine.adjudication.request import DirectFireRequest
from wargame_native_engine.adjudication.table_loader import AdjudicationTableSet
from wargame_native_engine.compat.source_frame.mapping import SourceMappings, source_identity_codes
from wargame_native_engine.compat.source_frame.models import ArmorClass, UnitDomain
from wargame_native_engine.core.operator import OperatorState
from wargame_native_engine.io.serialization import canonical_hash


class FireEffectQueryError(ValueError):
    """Stable public validation failure, distinct from an unresolved rule table."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


_PROFILE_BOOLEANS = (
    "concealed", "suppressed", "moving", "marching", "stacked", "close_combat",
)
_ARMORS = tuple(value.value for value in ArmorClass if value is not ArmorClass.UNKNOWN)
MAX_QUERY_INTEGER = (1 << 53) - 1


def fire_effect_query_schema() -> dict[str, Any]:
    """Closed provider-neutral input schema; semantic checks remain in the DTO."""
    profile_properties = {
        "domain": {"type": "string", "enum": ["PERSONNEL", "VEHICLE"]},
        "blood": {"type": "integer", "minimum": 1, "maximum": MAX_QUERY_INTEGER},
        "armor_class": {"type": "string", "enum": list(_ARMORS)},
        **{name: {"type": "boolean"} for name in _PROFILE_BOOLEANS},
    }
    return {
        "type": "object", "additionalProperties": False,
        "required": ["actor_id", "weapon_id"],
        "properties": {
            **{name: {"type": "integer", "minimum": 0, "maximum": MAX_QUERY_INTEGER}
               for name in ("actor_id", "weapon_id", "target_id", "target_hex", "guider_id")},
            **{name: {"type": "string", "minLength": 1, "maxLength": 128,
                      "description": "Exact operator subtype, e.g. source-2-0; an assertion for a real unit, an explicit label for a hypothetical target."}
               for name in ("attacker_model", "target_model")},
            "target_profile": {
                "type": "object", "additionalProperties": False,
                "required": list(profile_properties), "properties": profile_properties,
                "description": "Explicit assumed enemy strength, armor and posture; never inferred from hidden enemy state.",
            },
            "indirect_profile": {
                "type": "object", "additionalProperties": False,
                "required": ["observation_policy"],
                "properties": {
                    "observation_policy": {"type": "string", "enum": [
                        "UNOBSERVED", "GRID_CORRECTED", "TARGET_CORRECTED",
                    ]},
                    "planned_hex": {"type": "integer", "minimum": 0, "maximum": MAX_QUERY_INTEGER},
                },
                "description": "Assumed observation policy at indirect impact; planned_hex defaults to the declared target position.",
            },
            "fire_mode": {"type": "string", "enum": ["DIRECT", "GUIDED", "INDIRECT"]},
        },
        "oneOf": [
            {"required": ["target_id"], "not": {"required": ["target_hex"]}},
            {"required": ["target_hex", "target_model"], "not": {"required": ["target_id"]}},
        ],
    }


@dataclass(frozen=True, slots=True)
class FireEffectQuery:
    actor_id: int
    weapon_id: int
    target_id: int | None = None
    target_hex: int | None = None
    attacker_model: str | None = None
    target_model: str | None = None
    target_profile: Mapping[str, Any] | None = None
    fire_mode: str = "DIRECT"
    guider_id: int | None = None
    indirect_profile: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        for name in ("actor_id", "weapon_id", "target_id", "target_hex", "guider_id"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 0 <= value <= MAX_QUERY_INTEGER):
                _reject("INVALID_ARGUMENTS", f"{name} must be a nonnegative safe integer")
        if self.actor_id is None or self.weapon_id is None:
            _reject("INVALID_ARGUMENTS", "actor_id and weapon_id are required")
        if (self.target_id is None) == (self.target_hex is None):
            _reject("EXACTLY_ONE_FIRE_TARGET_REQUIRED", "provide target_id or target_hex, not both")
        for name in ("attacker_model", "target_model"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or not value.strip() or len(value) > 128
            ):
                _reject("INVALID_ARGUMENTS", f"{name} must be a nonempty model identifier")
        if self.target_hex is not None and self.target_model is None:
            _reject("TARGET_MODEL_REQUIRED", "hypothetical target_hex requires target_model")
        if not isinstance(self.fire_mode, str) or self.fire_mode not in {"DIRECT", "GUIDED", "INDIRECT"}:
            _reject("INVALID_ARGUMENTS", "unknown fire_mode")
        if self.guider_id is not None and self.fire_mode != "GUIDED":
            _reject("INVALID_ARGUMENTS", "guider_id is only valid for GUIDED")
        if self.target_profile is not None:
            profile = _validate_target_profile(self.target_profile)
            object.__setattr__(self, "target_profile", MappingProxyType(profile))
        if self.indirect_profile is not None:
            if self.fire_mode != "INDIRECT":
                _reject("INVALID_ARGUMENTS", "indirect_profile is only valid for INDIRECT")
            profile = self.indirect_profile
            if not isinstance(profile, Mapping) or (
                "observation_policy" not in profile
                or profile.keys() - {"observation_policy", "planned_hex"}
            ):
                _reject("INVALID_INDIRECT_PROFILE", "indirect_profile requires observation_policy and optional planned_hex")
            policy = profile["observation_policy"]
            if not isinstance(policy, str) or policy not in {"UNOBSERVED", "GRID_CORRECTED", "TARGET_CORRECTED"}:
                _reject("INVALID_INDIRECT_PROFILE", "unknown indirect observation_policy")
            if "planned_hex" in profile and (type(profile["planned_hex"]) is not int or not 0 <= profile["planned_hex"] <= MAX_QUERY_INTEGER):
                _reject("INVALID_INDIRECT_PROFILE", "planned_hex must be a nonnegative safe integer")
            object.__setattr__(self, "indirect_profile", MappingProxyType(dict(profile)))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FireEffectQuery":
        schema = fire_effect_query_schema()
        if not isinstance(value, Mapping) or (
            not set(schema["required"]) <= value.keys()
            or value.keys() - schema["properties"].keys()
        ):
            _reject("INVALID_ARGUMENTS", "missing or unsupported fire-effect query fields")
        return cls(**dict(value))

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor_id": self.actor_id, "weapon_id": self.weapon_id,
            "target_id": self.target_id, "target_hex": self.target_hex,
            "attacker_model": self.attacker_model, "target_model": self.target_model,
            "target_profile": None if self.target_profile is None else dict(self.target_profile),
            "fire_mode": self.fire_mode,
            "guider_id": self.guider_id,
            "indirect_profile": None if self.indirect_profile is None else dict(self.indirect_profile),
        }


def _validate_target_profile(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {"domain", "blood", "armor_class", *_PROFILE_BOOLEANS}
    if not isinstance(value, Mapping) or set(value) != required:
        _reject("INVALID_TARGET_PROFILE", f"target_profile requires exactly {sorted(required)}")
    if not isinstance(value["domain"], str) or value["domain"] not in {"PERSONNEL", "VEHICLE"}:
        _reject("INVALID_TARGET_PROFILE", "only PERSONNEL and VEHICLE target profiles are supported")
    if type(value["blood"]) is not int or not 1 <= value["blood"] <= MAX_QUERY_INTEGER:
        _reject("INVALID_TARGET_PROFILE", "blood must be a positive safe integer")
    if not isinstance(value["armor_class"], str) or value["armor_class"] not in _ARMORS:
        _reject("INVALID_TARGET_PROFILE", "armor_class must be explicitly mapped")
    if any(type(value[name]) is not bool for name in _PROFILE_BOOLEANS):
        _reject("INVALID_TARGET_PROFILE", "target posture fields must be exact booleans")
    return dict(value)


def projected_fire_inputs(
    side: str,
    observation: Mapping[str, Any],
    source_frame: Mapping[str, Any],
    inventory: Sequence[Mapping[str, Any]],
    query: FireEffectQuery,
) -> tuple[OperatorState, OperatorState | None, Mapping[str, Any]]:
    """Build isolated combat inputs without any WorldState or enemy templates."""
    if observation.get("side") != side:
        _reject("PROJECTION_SIDE_MISMATCH", "observation belongs to another side")
    own = _record(observation.get("friendly_operators"), "operator_id", query.actor_id)
    if own is None or own.get("side") != side:
        _reject("ACTOR_NOT_CONTROLLED", "actor is absent from this side's friendly projection")
    source = _record(source_frame.get("operators"), "obj_id", query.actor_id)
    if source is None:
        _reject("ACTOR_PROJECTION_INCOMPLETE", "owned source projection is missing")
    if query.attacker_model is not None and query.attacker_model != own.get("subtype"):
        _reject("ATTACKER_MODEL_MISMATCH", "attacker_model does not match the controlled actor")
    weapons = source.get("carry_weapon_ids")
    if not isinstance(weapons, (list, tuple)) or query.weapon_id not in weapons:
        _reject("WEAPON_NOT_CARRIED", "weapon is not carried by this actor")
    own_inventory = _record(inventory, "operator_id", query.actor_id)
    resource = _record(
        None if own_inventory is None else own_inventory.get("weapons"),
        "weapon_id", query.weapon_id,
    )
    if own_inventory is None or resource is None:
        _reject("WEAPON_RESOURCE_UNAVAILABLE", "frozen own weapon resource is missing")
    attacker = _owned_operator(own, source, own_inventory)
    contact = None
    target_hex = query.target_hex
    model = query.target_model
    if query.target_id is not None:
        # Never look up an ID in a WorldState or the scenario's enemy templates.
        contact = _record(observation.get("visible_enemies"), "operator_id", query.target_id)
        if contact is None:
            _reject("TARGET_NOT_VISIBLE", "target is absent from the visible side projection")
        target_source = _record(source_frame.get("operators"), "obj_id", query.target_id)
        if target_source is None:
            _reject("TARGET_PROJECTION_INCOMPLETE", "visible target position is missing")
        target_hex = target_source.get("cur_hex")
        if model is not None and model != contact.get("subtype"):
            _reject("TARGET_MODEL_MISMATCH", "target_model does not match the visible contact")
        model = contact.get("subtype")
    if type(target_hex) is not int or target_hex < 0:
        _reject("TARGET_HEX_INVALID", "target needs a declared map hex")
    if query.target_profile is None:
        return attacker, None, resource
    profile = query.target_profile
    domain = profile["domain"]
    if contact is not None and domain != contact.get("operator_type"):
        _reject("TARGET_DOMAIN_MISMATCH", "profile domain does not match the visible contact")
    codes = source_identity_codes(model)
    if codes is not None and SourceMappings().domain(codes[0]).native_effective_value != domain:
        _reject("TARGET_MODEL_MISMATCH", "profile domain does not match the declared model")
    # A hypothetical record is deliberately unrelated to any real enemy ID.
    target = OperatorState(
        operator_id=query.target_id if query.target_id is not None else 0,
        side="hypothetical-enemy", operator_type=domain, subtype=model,
        x=float(target_hex % 100), y=float(target_hex // 100), altitude=0,
        heading=0, health=float(profile["blood"]), max_health=float(profile["blood"]),
        speed=0, max_speed=0, move_range=0, sensor_range=0,
        state="SUPPRESSED" if profile["suppressed"] else "STOPPED",
        alive=True, weapon_ids=[], blood=profile["blood"], max_blood=profile["blood"],
        current_hex=target_hex, deployed=True, stopped=not profile["moving"],
        raw_attributes={"armor_class": profile["armor_class"], "concealed": profile["concealed"]},
    )
    return attacker, target, resource


def _owned_operator(
    own: Mapping[str, Any], source: Mapping[str, Any], inventory: Mapping[str, Any],
) -> OperatorState:
    required = ("blood", "max_blood", "stopped", "weapon_cooldown_remaining_ms",
                "suppression_remaining_ms", "move_to_stop_remaining_ms")
    if any(own.get(name) is None for name in required):
        _reject("ACTOR_PROJECTION_INCOMPLETE", "owned combat state is incomplete")
    if any(type(inventory.get(name)) is not bool for name in ("weapon_locked", "weapon_unfolded")):
        _reject("ACTOR_PROJECTION_INCOMPLETE", "owned weapon posture is missing")
    locked_id = inventory.get("locked_weapon_id")
    if locked_id is not None and (type(locked_id) is not int or locked_id < 0):
        _reject("ACTOR_PROJECTION_INCOMPLETE", "owned weapon lock ID is invalid")
    return OperatorState(
        operator_id=own["operator_id"], side=own["side"],
        operator_type=own["operator_type"], subtype=own.get("subtype"),
        x=own["position"][0], y=own["position"][1], altitude=own["altitude"],
        heading=own["heading"], health=own["health"], max_health=own["max_health"],
        speed=own["speed"], max_speed=0, move_range=0, sensor_range=0,
        state=own["state"], alive=own["alive"], deployed=own["deployed"],
        weapon_ids=list(source["carry_weapon_ids"]), blood=own["blood"], max_blood=own["max_blood"],
        current_hex=source["cur_hex"], stopped=own["stopped"], movement_mode=own["movement_mode"],
        transition_kind=own.get("transition_kind"),
        move_to_stop_remaining_ms=own["move_to_stop_remaining_ms"],
        weapon_locked=inventory["weapon_locked"], weapon_unfolded=inventory["weapon_unfolded"],
        locked_weapon_id=inventory.get("locked_weapon_id"),
        weapon_cooldown_remaining_ms=own["weapon_cooldown_remaining_ms"],
        suppression_remaining_ms=own["suppression_remaining_ms"],
        indirect_fire_planned_cooldown_remaining_ms=own["indirect_fire_planned_cooldown_remaining_ms"],
        lifecycle_profile_id=own.get("lifecycle_profile_id"),
        lifecycle_remaining_ms=own.get("lifecycle_remaining_ms"),
        raw_attributes={"ammunition": dict(inventory["ammunition_pools"])},
    )


def projected_guider(
    side: str, observation: Mapping[str, Any], source_frame: Mapping[str, Any],
    inventory: Sequence[Mapping[str, Any]], guider_id: int,
) -> OperatorState:
    """Reconstruct an owned guidance observer from the same frozen view."""
    own = _record(observation.get("friendly_operators"), "operator_id", guider_id)
    if own is None or own.get("side") != side:
        _reject("GUIDER_NOT_CONTROLLED", "guider is absent from the friendly projection")
    source = _record(source_frame.get("operators"), "obj_id", guider_id)
    resources = _record(inventory, "operator_id", guider_id)
    if source is None or resources is None:
        _reject("GUIDER_PROJECTION_INCOMPLETE", "owned guider projection is incomplete")
    return _owned_operator(own, source, resources)


def direct_fire_probabilities(
    tables: AdjudicationTableSet, request: DirectFireRequest,
) -> dict[str, Any]:
    """Enumerate the existing staged resolver exactly under independent fair d6.

    The tables depend on dice sums, not individual order.  Weighting the 11
    possible 2d6 sums therefore needs at most 121 resolutions instead of 1296.
    No live dice source, target mutation, ammunition change or event publication
    occurs.  This does not attempt to predict a seeded PRNG's next result.
    """
    outcomes: Counter = Counter()
    two_d6 = [(total, 6 - abs(7 - total)) for total in range(2, 13)]
    correction_dice = 2 if request.target_domain is UnitDomain.VEHICLE else 1
    corrections = two_d6 if correction_dice == 2 else [(face, 1) for face in range(1, 7)]
    denominator = 6 ** (2 + correction_dice)
    for (base, base_weight), (correction, correction_weight) in product(two_d6, corrections):
        dice = _two_dice(base) + (
            _two_dice(correction) if correction_dice == 2 else (correction,)
        )
        result = DirectFireAdjudicator(tables, FixedDiceRoller(dice)).stage(request)
        if result.error is not None or result.outcome is None:
            return {
                "status": "UNRESOLVED", "reason_code": result.error or result.legality.reason_code,
                "can_execute_under_assumptions": result.legality.legal,
                "damage_probability": None, "expected_damage": None,
                "damage_distribution": [], "outcome_distribution": [],
            }
        outcome = result.outcome
        key = (outcome.vehicle_losses, outcome.squad_losses,
               outcome.suppression_applied, outcome.destroyed, outcome.no_effect)
        outcomes[key] += base_weight * correction_weight
    losses: Counter = Counter()
    records = []
    expected = Fraction()
    damaging = suppressing = destroying = 0
    for (vehicles, squads, suppression, destroyed, no_effect), weight in sorted(outcomes.items()):
        loss = vehicles + squads
        losses[loss] += weight
        expected += Fraction(loss * weight, denominator)
        damaging += weight if loss else 0
        suppressing += weight if suppression else 0
        destroying += weight if destroyed else 0
        records.append({
            "vehicle_losses": vehicles, "squad_losses": squads,
            "suppression_applied": suppression, "destroyed": destroyed,
            "no_effect": no_effect, **_probability(weight, denominator),
        })
    return {
        "status": "RESOLVED", "reason_code": "EXACT_FAIR_D6_ENUMERATION",
        "can_execute_under_assumptions": True,
        "damage_probability": float(Fraction(damaging, denominator)),
        "suppression_probability": float(Fraction(suppressing, denominator)),
        "destruction_probability": float(Fraction(destroying, denominator)),
        "expected_damage": float(expected), "damage_unit": "blood_strength_loss",
        "damage_distribution": [
            {"losses": loss, **_probability(weight, denominator)}
            for loss, weight in sorted(losses.items())
        ],
        "outcome_distribution": records,
        "probability_basis": "independent_fair_d6",
    }


def query_result_base(
    query: FireEffectQuery, observation: Mapping[str, Any],
    valid_actions: Sequence[Mapping[str, Any]], tables: AdjudicationTableSet | None,
) -> dict[str, Any]:
    can_execute = None
    if query.target_id is not None:
        if query.fire_mode in {"DIRECT", "GUIDED"}:
            can_execute = any(_matches_fire_action(record, query) for record in valid_actions)
    return {
        "schema_version": "native-fire-effect-query/1.0",
        "query": query.to_dict(),
        "query_kind": "VISIBLE_TARGET_WITH_ASSUMPTIONS" if query.target_id is not None else "HYPOTHETICAL_TARGET",
        "assumptions": None if query.target_profile is None else dict(query.target_profile),
        "can_execute": can_execute,
        "execution_evidence": "frozen legal actions" if query.target_id is not None else "hypothetical target has no executable identity",
        "hit_probability": None,
        "hit_probability_reason": "NO_SEPARATE_HIT_TEST: direct-fire rules resolve loss and suppression tables",
        "distribution_condition": "one fire attempt with all stated profile assumptions",
        "outcome_scope": "PRIMARY_TARGET_ONLY",
        "source": {
            "table_set_hash": None if tables is None else tables.content_hash,
            "observation_hash": observation.get("observation_hash"),
            "input_hash": canonical_hash(query.to_dict()),
        },
    }


def _matches_fire_action(record: Mapping[str, Any], query: FireEffectQuery) -> bool:
    action = record.get("action")
    if not isinstance(action, Mapping) or action.get("target_id") != query.target_id:
        return False
    parameters = action.get("parameters", {})
    if parameters.get("weapon_id") != query.weapon_id:
        return False
    if query.fire_mode == "DIRECT":
        return action.get("actor_id") == query.actor_id and action.get("action_type") == "SHOOT"
    return (
        action.get("action_type") == "FIRE"
        and action.get("actor_id") == query.guider_id
        and parameters.get("fire_mode") == "GUIDED"
        and parameters.get("guider_id") == query.guider_id
        and parameters.get("shooter_id") == query.actor_id
    )


def _record(values: Any, key: str, identity: int) -> Mapping[str, Any] | None:
    if not isinstance(values, (tuple, list)):
        return None
    return next((value for value in values if isinstance(value, Mapping) and value.get(key) == identity), None)


def _two_dice(total: int) -> tuple[int, int]:
    first = min(6, total - 1)
    return first, total - first


def _probability(numerator: int, denominator: int) -> dict[str, Any]:
    value = Fraction(numerator, denominator)
    return {"probability": float(value), "probability_fraction": f"{value.numerator}/{value.denominator}"}


def _reject(code: str, message: str) -> None:
    raise FireEffectQueryError(code, message)
