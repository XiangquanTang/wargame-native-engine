"""Versioned Native simplified indirect-fire authority facts.

This module intentionally has no Engine, WorldState, Action, Replay, or Viewer
dependency.  Executable authority is still separate from Action exposure.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
from collections.abc import Iterable
from typing import Any, Mapping

from wargame_native_engine.io.serialization import canonical_hash


INDIRECT_FIRE_AUTHORITY_SCHEMA_VERSION = "native-indirect-fire-authority/1.1"
INDIRECT_FIRE_EXECUTION_POLICY_SCHEMA_VERSION = (
    "native-indirect-fire-execution-policy/1.0"
)
INDIRECT_FIRE_EXECUTION_POLICY_ID = "native-indirect-fire-execution-policy"
INDIRECT_FIRE_EXECUTION_POLICY_VERSION = "1.0.0"
INDIRECT_FIRE_AUTHORITY_STATUS = "EXECUTABLE_AUTHORITY_READY"
INDIRECT_FIRE_UNKNOWN_POLICY = "PRESERVE_UNKNOWN_REJECT_EXECUTION"
INDIRECT_FIRE_MODE = "INDIRECT"
INDIRECT_FIRE_AMMUNITION_MAPPING_SCHEMA_VERSION = (
    "native-indirect-fire-ammunition-mapping/1.0"
)
INDIRECT_FIRE_OBSERVATION_POLICY_SCHEMA_VERSION = (
    "native-indirect-fire-observation-policy/1.0"
)
INDIRECT_FIRE_DAMAGE_POLICY_SCHEMA_VERSION = (
    "native-indirect-fire-damage-policy/1.0"
)


class ArtilleryClass(str, Enum):
    LIGHT = "LIGHT"
    MEDIUM = "MEDIUM"
    HEAVY = "HEAVY"


class IndirectTargetDomain(str, Enum):
    GROUND_UNIT = "GROUND_UNIT"
    VEHICLE = "VEHICLE"
    PERSONNEL = "PERSONNEL"
    BUILDING = "BUILDING"
    AREA = "AREA"
    UNKNOWN = "UNKNOWN"


class IndirectRangePolicy(str, Enum):
    """Spatial admission policy; it is not a numeric or infinite range."""

    SCENARIO_DEFINED_GLOBAL_HEX = "SCENARIO_DEFINED_GLOBAL_HEX"


class IndirectFireMissionState(str, Enum):
    PLANNED = "PLANNED"
    FLYING = "FLYING"
    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"


class UnresolvedAuthority(str, Enum):
    UNRESOLVED = "UNRESOLVED"


class ScatterDirectionPolicy(str, Enum):
    UNKNOWN = "UNKNOWN"
    HEX_NEIGHBOR_6WAY_RANDOM = "HEX_NEIGHBOR_6WAY_RANDOM"


class IndirectFireAmmoConsumeTiming(str, Enum):
    MISSION_CREATE = "MISSION_CREATE"


class IndirectFireAmmoRefundPolicy(str, Enum):
    NO_REFUND = "NO_REFUND"


class IndirectScatterDirectionMode(str, Enum):
    HEX_NEIGHBOR_6WAY_RANDOM = "HEX_NEIGHBOR_6WAY_RANDOM"


class IndirectScatterDistancePolicy(str, Enum):
    TWO_D6_TABLE = "TWO_D6_TABLE"


class IndirectFireCancellationReason(str, Enum):
    USER_CANCELLED = "USER_CANCELLED"
    SHOOTER_DESTROYED = "SHOOTER_DESTROYED"
    MISSION_INVALIDATED = "MISSION_INVALIDATED"


class IndirectAffectedHexPolicy(str, Enum):
    IMPACT_HEX_ONLY = "IMPACT_HEX_ONLY"


class IndirectMultiTargetOrder(str, Enum):
    OPERATOR_ID_ASCENDING = "OPERATOR_ID_ASCENDING"


@dataclass(frozen=True, slots=True)
class IndirectFireProfile:
    profile_id: str
    weapon_id: int
    artillery_class: ArtilleryClass
    supported_fire_mode: str
    target_domain: IndirectTargetDomain
    range_policy: IndirectRangePolicy
    minimum_range: int | None
    maximum_range: int | None
    flight_time_seconds: int
    active_duration_seconds: int
    planned_cooldown_seconds: int
    scatter_model_id: str
    effect_model_id: str
    resource_policy_id: str
    mission_policy_id: str

    def __post_init__(self) -> None:
        _nonempty(self.profile_id, "profile_id")
        _nonnegative_int(self.weapon_id, "weapon_id")
        if not isinstance(self.artillery_class, ArtilleryClass):
            raise TypeError("artillery_class must be ArtilleryClass")
        if self.supported_fire_mode != INDIRECT_FIRE_MODE:
            raise ValueError("supported_fire_mode must be INDIRECT")
        if not isinstance(self.target_domain, IndirectTargetDomain):
            raise TypeError("target_domain must be IndirectTargetDomain")
        if not isinstance(self.range_policy, IndirectRangePolicy):
            raise TypeError("range_policy must be IndirectRangePolicy")
        _optional_nonnegative_int(self.minimum_range, "minimum_range")
        _optional_nonnegative_int(self.maximum_range, "maximum_range")
        if self.range_policy is IndirectRangePolicy.SCENARIO_DEFINED_GLOBAL_HEX:
            if self.minimum_range is not None or self.maximum_range is not None:
                raise ValueError(
                    "global-hex policy cannot be represented as numeric range"
                )
            if self.target_domain is not IndirectTargetDomain.AREA:
                raise ValueError("global-hex policy requires AREA target domain")
        if (
            self.minimum_range is not None
            and self.maximum_range is not None
            and self.minimum_range > self.maximum_range
        ):
            raise ValueError("minimum_range cannot exceed maximum_range")
        for name in (
            "flight_time_seconds",
            "active_duration_seconds",
            "planned_cooldown_seconds",
        ):
            _nonnegative_int(getattr(self, name), name)
        for name in (
            "scatter_model_id",
            "effect_model_id",
            "resource_policy_id",
            "mission_policy_id",
        ):
            _nonempty(getattr(self, name), name)

    @property
    def unresolved_fields(self) -> tuple[str, ...]:
        values: list[str] = []
        if self.range_policy is not IndirectRangePolicy.SCENARIO_DEFINED_GLOBAL_HEX:
            if self.minimum_range is None:
                values.append("minimum_range")
            if self.maximum_range is None:
                values.append("maximum_range")
        if self.target_domain is IndirectTargetDomain.UNKNOWN:
            values.append("target_domain")
        return tuple(values)


@dataclass(frozen=True, slots=True)
class IndirectFireResourcePolicy:
    policy_id: str
    ammo_cost: int | UnresolvedAuthority
    cooldown_policy: str
    cancel_policy: str

    def __post_init__(self) -> None:
        _nonempty(self.policy_id, "policy_id")
        if type(self.ammo_cost) is int:
            _nonnegative_int(self.ammo_cost, "ammo_cost")
        elif self.ammo_cost is not UnresolvedAuthority.UNRESOLVED:
            raise TypeError("ammo_cost must be an exact int or UNRESOLVED")
        _nonempty(self.cooldown_policy, "cooldown_policy")
        _nonempty(self.cancel_policy, "cancel_policy")


@dataclass(frozen=True, slots=True)
class IndirectFireMissionPolicy:
    policy_id: str
    states: tuple[IndirectFireMissionState, ...]
    flight_duration_seconds: int
    active_duration_seconds: int
    can_cancel_during_flight: bool

    def __post_init__(self) -> None:
        _nonempty(self.policy_id, "policy_id")
        expected = tuple(IndirectFireMissionState)
        if self.states != expected:
            raise ValueError("indirect-fire mission states must be complete and ordered")
        _nonnegative_int(self.flight_duration_seconds, "flight_duration_seconds")
        _nonnegative_int(self.active_duration_seconds, "active_duration_seconds")
        if type(self.can_cancel_during_flight) is not bool:
            raise TypeError("can_cancel_during_flight must be an exact bool")


@dataclass(frozen=True, slots=True)
class IndirectScatterProfile:
    scatter_model_id: str
    dice_expression: str
    table_reference: str
    direction_policy: ScatterDirectionPolicy
    distance_policy: str

    def __post_init__(self) -> None:
        _nonempty(self.scatter_model_id, "scatter_model_id")
        if self.dice_expression != "2d6":
            raise ValueError("indirect scatter dice_expression must be 2d6")
        _nonempty(self.table_reference, "table_reference")
        if self.direction_policy is not (
            ScatterDirectionPolicy.HEX_NEIGHBOR_6WAY_RANDOM
        ):
            raise ValueError("scatter direction must use six-way Native RNG")
        _nonempty(self.distance_policy, "distance_policy")


@dataclass(frozen=True, slots=True)
class IndirectImpactProfile:
    profile_id: str
    affected_area: str
    resolution_scope: str
    friendly_fire: bool
    one_hit_per_unit: bool
    planned_hex_field: str
    impact_hex_field: str

    def __post_init__(self) -> None:
        _nonempty(self.profile_id, "profile_id")
        if self.affected_area != "RESOLVED_HEX_ONLY":
            raise ValueError("affected_area must be RESOLVED_HEX_ONLY")
        if self.resolution_scope != "INITIAL_OCCUPANTS_AND_ACTIVE_HEX_ENTRANTS":
            raise ValueError("unsupported indirect-fire resolution_scope")
        if self.friendly_fire is not True:
            raise ValueError("indirect fire must preserve friendly-fire authority")
        if self.one_hit_per_unit is not True:
            raise ValueError("indirect fire must preserve one-hit-per-unit authority")
        if self.planned_hex_field != "planned_hex":
            raise ValueError("planned hex evidence field must be planned_hex")
        if self.impact_hex_field != "impact_hex":
            raise ValueError("impact hex evidence field must be impact_hex")


@dataclass(frozen=True, slots=True)
class IndirectFireAmmoPolicy:
    consume_timing: IndirectFireAmmoConsumeTiming
    refund_policy: IndirectFireAmmoRefundPolicy

    def __post_init__(self) -> None:
        if self.consume_timing is not IndirectFireAmmoConsumeTiming.MISSION_CREATE:
            raise ValueError("indirect ammunition must be consumed at mission create")
        if self.refund_policy is not IndirectFireAmmoRefundPolicy.NO_REFUND:
            raise ValueError("cancelled indirect missions must not refund ammunition")


@dataclass(frozen=True, slots=True)
class IndirectScatterDirectionPolicy:
    direction_policy: IndirectScatterDirectionMode
    direction_draw_policy: str
    distance_policy: IndirectScatterDistancePolicy
    distance_table_reference: str
    planned_hex_field: str
    impact_hex_field: str

    def __post_init__(self) -> None:
        if self.direction_policy is not (
            IndirectScatterDirectionMode.HEX_NEIGHBOR_6WAY_RANDOM
        ):
            raise ValueError("scatter must select one of six neighboring directions")
        if self.direction_draw_policy != "UNIFORM_NATIVE_RNG_D6":
            raise ValueError("scatter direction must use deterministic NativeRNG d6")
        if self.distance_policy is not IndirectScatterDistancePolicy.TWO_D6_TABLE:
            raise ValueError("scatter distance must use the 2d6 table")
        _nonempty(self.distance_table_reference, "distance_table_reference")
        if self.planned_hex_field != "planned_hex":
            raise ValueError("scatter input field must remain planned_hex")
        if self.impact_hex_field != "impact_hex":
            raise ValueError("scatter output field must remain impact_hex")


@dataclass(frozen=True, slots=True)
class IndirectFireCancellationPolicy:
    allowed_phases: tuple[IndirectFireMissionState, ...]
    reasons: tuple[IndirectFireCancellationReason, ...]
    refund_policy: IndirectFireAmmoRefundPolicy

    def __post_init__(self) -> None:
        if self.allowed_phases != (IndirectFireMissionState.FLYING,):
            raise ValueError("indirect cancellation is allowed only while FLYING")
        if self.reasons != tuple(IndirectFireCancellationReason):
            raise ValueError("indirect cancellation reasons must be complete and ordered")
        if self.refund_policy is not IndirectFireAmmoRefundPolicy.NO_REFUND:
            raise ValueError("indirect cancellation must not refund ammunition")


@dataclass(frozen=True, slots=True)
class IndirectFireImpactPolicy:
    affected_hex_policy: IndirectAffectedHexPolicy
    one_hit_per_unit: bool
    multi_target_order: IndirectMultiTargetOrder

    def __post_init__(self) -> None:
        if self.affected_hex_policy is not IndirectAffectedHexPolicy.IMPACT_HEX_ONLY:
            raise ValueError("indirect impact affects only impact_hex")
        if self.one_hit_per_unit is not True:
            raise ValueError("indirect impact must adjudicate each unit once")
        if self.multi_target_order is not IndirectMultiTargetOrder.OPERATOR_ID_ASCENDING:
            raise ValueError("indirect targets must be ordered by operator_id")


@dataclass(frozen=True, slots=True)
class IndirectFireExecutionPolicy:
    policy_id: str
    policy_version: str
    ammo_policy: IndirectFireAmmoPolicy
    scatter_policy: IndirectScatterDirectionPolicy
    cancellation_policy: IndirectFireCancellationPolicy
    impact_policy: IndirectFireImpactPolicy
    content_hash: str

    def __post_init__(self) -> None:
        _nonempty(self.policy_id, "policy_id")
        _nonempty(self.policy_version, "policy_version")
        if self.cancellation_policy.refund_policy is not self.ammo_policy.refund_policy:
            raise ValueError("ammo and cancellation refund policies must agree")
        if (
            len(self.content_hash) != 64
            or any(value not in "0123456789abcdef" for value in self.content_hash)
        ):
            raise ValueError("content_hash must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class IndirectFireAmmunitionMapping:
    weapon_id: int
    ammunition_key: str
    ammo_cost_per_mission: int
    source_mapping: str

    def __post_init__(self) -> None:
        _nonnegative_int(self.weapon_id, "weapon_id")
        _nonempty(self.ammunition_key, "ammunition_key")
        _positive_int(self.ammo_cost_per_mission, "ammo_cost_per_mission")
        _relative_text(self.source_mapping, "source_mapping")


@dataclass(frozen=True, slots=True)
class IndirectFireAmmunitionAuthority:
    mapping_id: str
    version: str
    authority_status: str
    authority_basis: str
    mappings: tuple[IndirectFireAmmunitionMapping, ...]
    content_hash: str

    def __post_init__(self) -> None:
        for name in ("mapping_id", "version", "authority_basis"):
            _nonempty(getattr(self, name), name)
        if self.authority_status != INDIRECT_FIRE_AUTHORITY_STATUS:
            raise ValueError("indirect ammunition authority is not executable")
        if tuple(sorted(self.mappings, key=lambda item: item.weapon_id)) != self.mappings:
            raise ValueError("ammunition mappings must use weapon_id order")
        _unique((item.weapon_id for item in self.mappings), "ammunition weapon_id")
        _sha256(self.content_hash, "content_hash")

    def mapping_for_weapon(
        self,
        weapon_id: int,
    ) -> IndirectFireAmmunitionMapping | None:
        if type(weapon_id) is not int or weapon_id < 0:
            return None
        return next(
            (item for item in self.mappings if item.weapon_id == weapon_id),
            None,
        )


@dataclass(frozen=True, slots=True)
class IndirectFireObservationAuthority:
    policy_id: str
    version: str
    authority_status: str
    impact_observation_policy: str
    requires_observer: bool
    correction_level_policy: str
    dispersion_table_id: str
    authority_basis: str
    content_hash: str

    def __post_init__(self) -> None:
        for name in (
            "policy_id",
            "version",
            "impact_observation_policy",
            "correction_level_policy",
            "dispersion_table_id",
            "authority_basis",
        ):
            _nonempty(getattr(self, name), name)
        if self.authority_status != INDIRECT_FIRE_AUTHORITY_STATUS:
            raise ValueError("indirect observation authority is not executable")
        if self.impact_observation_policy != "UNOBSERVED":
            raise ValueError("Native simplified v1 supports only UNOBSERVED impact")
        if self.requires_observer is not False:
            raise ValueError("UNOBSERVED impact cannot require an observer")
        if self.correction_level_policy != "FIXED_UNOBSERVED":
            raise ValueError("unsupported indirect observation correction policy")
        if self.dispersion_table_id != "indirect_fire.unobserved_dispersion":
            raise ValueError("UNOBSERVED authority requires the unobserved table")
        _sha256(self.content_hash, "content_hash")


@dataclass(frozen=True, slots=True)
class IndirectFireDamagePolicy:
    policy_id: str
    version: str
    authority_status: str
    unarmored_target_modifier: int
    unarmored_application: str
    unarmored_authority_basis: str
    unknown_armor_policy: str
    numeric_correction_application: str
    numeric_zero_policy: str
    symbolic_correction_application: str
    symbolic_ordered_results: tuple[str, ...]
    symbolic_zero_policy: str
    symbolic_authority_basis: str
    content_hash: str

    def __post_init__(self) -> None:
        for name in (
            "policy_id",
            "version",
            "unarmored_application",
            "unarmored_authority_basis",
            "unknown_armor_policy",
            "numeric_correction_application",
            "numeric_zero_policy",
            "symbolic_correction_application",
            "symbolic_zero_policy",
            "symbolic_authority_basis",
        ):
            _nonempty(getattr(self, name), name)
        if self.authority_status != INDIRECT_FIRE_AUTHORITY_STATUS:
            raise ValueError("indirect damage authority is not executable")
        if type(self.unarmored_target_modifier) is not int:
            raise TypeError("unarmored_target_modifier must be an exact int")
        if self.unarmored_application != "EXPLICIT_VALUE":
            raise ValueError("unarmored modifier must be an explicit value")
        if self.unknown_armor_policy != "REJECT":
            raise ValueError("unknown armor must fail closed")
        if (
            self.numeric_correction_application
            != "ADD_AND_CLAMP_TO_TARGET_STRENGTH"
            or self.numeric_zero_policy != "PRESERVE_BASE_RESULT"
        ):
            raise ValueError("unsupported numeric correction authority")
        if (
            self.symbolic_correction_application
            != "SHIFT_ORDERED_LADDER_AND_CLAMP"
            or self.symbolic_zero_policy != "PRESERVE_BASE_RESULT"
        ):
            raise ValueError("unsupported symbolic correction authority")
        expected = (
            "NO_EFFECT", "SUPPRESS", "LOSS_1", "LOSS_2", "LOSS_3", "LOSS_4",
        )
        if self.symbolic_ordered_results != expected:
            raise ValueError("indirect symbolic result ladder mismatch")
        _sha256(self.content_hash, "content_hash")


@dataclass(frozen=True, slots=True)
class IndirectFireAuthorityDescriptor:
    profile_id: str
    authority_version: str
    execution_policy_hash: str
    ammo_mapping_hash: str
    observation_policy_hash: str
    damage_policy_hash: str

    def __post_init__(self) -> None:
        _nonempty(self.profile_id, "profile_id")
        _nonempty(self.authority_version, "authority_version")
        for name in (
            "execution_policy_hash",
            "ammo_mapping_hash",
            "observation_policy_hash",
            "damage_policy_hash",
        ):
            _sha256(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class IndirectFireAuthoritySource:
    machine_table: str
    table_set_id: str
    table_revision: str
    table_content_sha256: str
    guide: str
    context: str

    def __post_init__(self) -> None:
        for name in (
            "machine_table",
            "table_set_id",
            "table_revision",
            "guide",
            "context",
        ):
            _nonempty(getattr(self, name), name)
        if (
            len(self.table_content_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.table_content_sha256)
        ):
            raise ValueError("table_content_sha256 must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class IndirectFireAuthority:
    config_id: str
    config_version: str
    source: IndirectFireAuthoritySource
    profiles: tuple[IndirectFireProfile, ...]
    resource_policies: tuple[IndirectFireResourcePolicy, ...]
    mission_policies: tuple[IndirectFireMissionPolicy, ...]
    scatter_profiles: tuple[IndirectScatterProfile, ...]
    impact_profiles: tuple[IndirectImpactProfile, ...]
    unresolved_authority: tuple[str, ...]
    content_hash: str

    def profile(self, profile_id: str) -> IndirectFireProfile | None:
        return next(
            (item for item in self.profiles if item.profile_id == profile_id),
            None,
        )

    def profile_for_weapon(self, weapon_id: int) -> IndirectFireProfile | None:
        if type(weapon_id) is not int or weapon_id < 0:
            return None
        return next(
            (item for item in self.profiles if item.weapon_id == weapon_id),
            None,
        )

    def resource_policy(
        self,
        policy_id: str,
    ) -> IndirectFireResourcePolicy | None:
        return next(
            (item for item in self.resource_policies if item.policy_id == policy_id),
            None,
        )

    def mission_policy(
        self,
        policy_id: str,
    ) -> IndirectFireMissionPolicy | None:
        return next(
            (item for item in self.mission_policies if item.policy_id == policy_id),
            None,
        )

    def scatter_profile(
        self,
        model_id: str,
    ) -> IndirectScatterProfile | None:
        return next(
            (item for item in self.scatter_profiles if item.scatter_model_id == model_id),
            None,
        )

    def impact_profile(
        self,
        profile_id: str,
    ) -> IndirectImpactProfile | None:
        return next(
            (item for item in self.impact_profiles if item.profile_id == profile_id),
            None,
        )

    def execution_blockers(self, weapon_id: int) -> tuple[str, ...]:
        profile = self.profile_for_weapon(weapon_id)
        if profile is None:
            return ("profile",)
        resource = self.resource_policy(profile.resource_policy_id)
        values = list(profile.unresolved_fields)
        if resource is None or resource.ammo_cost is UnresolvedAuthority.UNRESOLVED:
            values.append("ammo_cost")
        scatter = self.scatter_profile(profile.scatter_model_id)
        if scatter is None or scatter.direction_policy is ScatterDirectionPolicy.UNKNOWN:
            values.append("scatter_direction")
        return tuple(sorted(set(values)))


def default_indirect_fire_profiles_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "config/adjudication/indirect_fire_profiles_v1.json"
    )


def default_indirect_fire_execution_policy_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "config/adjudication/indirect_fire_execution_policy_v1.json"
    )


def default_indirect_fire_ammunition_mapping_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "config/adjudication/indirect_fire_ammunition_mapping_v1.json"
    )


def default_indirect_fire_observation_policy_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "config/adjudication/indirect_fire_observation_policy_v1.json"
    )


def default_indirect_fire_damage_policy_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "config/adjudication/indirect_fire_damage_policy_v1.json"
    )


def load_indirect_fire_ammunition_authority(
    path: str | Path | None = None,
    *,
    repository_root: str | Path | None = None,
) -> IndirectFireAmmunitionAuthority:
    source_path = (
        default_indirect_fire_ammunition_mapping_path()
        if path is None
        else Path(path)
    )
    root = _read_json_object(source_path, "indirect-fire ammunition authority")
    item = _exact_mapping(root, "indirect-fire ammunition authority", {
        "schema_version", "mapping_id", "version", "authority_status",
        "authority_basis", "mappings",
    })
    if item["schema_version"] != INDIRECT_FIRE_AMMUNITION_MAPPING_SCHEMA_VERSION:
        raise ValueError("unsupported indirect-fire ammunition mapping schema")
    raw_mappings = item["mappings"]
    if not isinstance(raw_mappings, list):
        raise ValueError("indirect-fire ammunition mappings must be an array")
    root_path = (
        Path(repository_root).resolve()
        if repository_root is not None
        else Path(__file__).resolve().parents[3]
    )
    mappings: list[IndirectFireAmmunitionMapping] = []
    for raw in raw_mappings:
        value = _exact_mapping(raw, "ammunition mapping", {
            "weapon_id", "ammunition_key", "ammo_cost_per_mission",
            "source_mapping",
        })
        mapping = IndirectFireAmmunitionMapping(
            weapon_id=_nonnegative_int(value["weapon_id"], "weapon_id"),
            ammunition_key=_nonempty(value["ammunition_key"], "ammunition_key"),
            ammo_cost_per_mission=_positive_int(
                value["ammo_cost_per_mission"], "ammo_cost_per_mission"
            ),
            source_mapping=_relative_text(value["source_mapping"], "source_mapping"),
        )
        evidence_path = (root_path / mapping.source_mapping).resolve()
        if (
            not evidence_path.is_relative_to(root_path)
            or not evidence_path.is_file()
        ):
            raise ValueError("indirect-fire ammunition source mapping is unavailable")
        mappings.append(mapping)
    return IndirectFireAmmunitionAuthority(
        mapping_id=_nonempty(item["mapping_id"], "mapping_id"),
        version=_nonempty(item["version"], "version"),
        authority_status=_nonempty(item["authority_status"], "authority_status"),
        authority_basis=_nonempty(item["authority_basis"], "authority_basis"),
        mappings=tuple(sorted(mappings, key=lambda value: value.weapon_id)),
        content_hash=canonical_hash(root),
    )


def load_indirect_fire_observation_authority(
    path: str | Path | None = None,
) -> IndirectFireObservationAuthority:
    source_path = (
        default_indirect_fire_observation_policy_path()
        if path is None
        else Path(path)
    )
    root = _read_json_object(source_path, "indirect-fire observation authority")
    item = _exact_mapping(root, "indirect-fire observation authority", {
        "schema_version", "policy_id", "version", "authority_status",
        "impact_observation_policy", "requires_observer",
        "correction_level_policy", "dispersion_table_id", "authority_basis",
    })
    if item["schema_version"] != INDIRECT_FIRE_OBSERVATION_POLICY_SCHEMA_VERSION:
        raise ValueError("unsupported indirect-fire observation policy schema")
    return IndirectFireObservationAuthority(
        policy_id=_nonempty(item["policy_id"], "policy_id"),
        version=_nonempty(item["version"], "version"),
        authority_status=_nonempty(item["authority_status"], "authority_status"),
        impact_observation_policy=_nonempty(
            item["impact_observation_policy"], "impact_observation_policy"
        ),
        requires_observer=_exact_bool(
            item["requires_observer"], "requires_observer"
        ),
        correction_level_policy=_nonempty(
            item["correction_level_policy"], "correction_level_policy"
        ),
        dispersion_table_id=_nonempty(
            item["dispersion_table_id"], "dispersion_table_id"
        ),
        authority_basis=_nonempty(item["authority_basis"], "authority_basis"),
        content_hash=canonical_hash(root),
    )


def load_indirect_fire_damage_policy(
    path: str | Path | None = None,
) -> IndirectFireDamagePolicy:
    source_path = (
        default_indirect_fire_damage_policy_path()
        if path is None
        else Path(path)
    )
    root = _read_json_object(source_path, "indirect-fire damage policy")
    item = _exact_mapping(root, "indirect-fire damage policy", {
        "schema_version", "policy_id", "version", "authority_status",
        "unarmored_target_modifier", "unknown_armor_policy",
        "numeric_result_correction", "symbolic_result_correction",
    })
    if item["schema_version"] != INDIRECT_FIRE_DAMAGE_POLICY_SCHEMA_VERSION:
        raise ValueError("unsupported indirect-fire damage policy schema")
    unarmored = _exact_mapping(
        item["unarmored_target_modifier"],
        "unarmored_target_modifier",
        {"value", "application", "authority_basis"},
    )
    numeric = _exact_mapping(
        item["numeric_result_correction"],
        "numeric_result_correction",
        {"application", "zero_policy"},
    )
    symbolic = _exact_mapping(
        item["symbolic_result_correction"],
        "symbolic_result_correction",
        {"application", "ordered_results", "zero_policy", "authority_basis"},
    )
    raw_results = symbolic["ordered_results"]
    if not isinstance(raw_results, list):
        raise ValueError("symbolic ordered_results must be an array")
    return IndirectFireDamagePolicy(
        policy_id=_nonempty(item["policy_id"], "policy_id"),
        version=_nonempty(item["version"], "version"),
        authority_status=_nonempty(item["authority_status"], "authority_status"),
        unarmored_target_modifier=_exact_int(
            unarmored["value"], "unarmored_target_modifier.value"
        ),
        unarmored_application=_nonempty(
            unarmored["application"], "unarmored_target_modifier.application"
        ),
        unarmored_authority_basis=_nonempty(
            unarmored["authority_basis"], "unarmored_target_modifier.authority_basis"
        ),
        unknown_armor_policy=_nonempty(
            item["unknown_armor_policy"], "unknown_armor_policy"
        ),
        numeric_correction_application=_nonempty(
            numeric["application"], "numeric_result_correction.application"
        ),
        numeric_zero_policy=_nonempty(
            numeric["zero_policy"], "numeric_result_correction.zero_policy"
        ),
        symbolic_correction_application=_nonempty(
            symbolic["application"], "symbolic_result_correction.application"
        ),
        symbolic_ordered_results=tuple(
            _nonempty(value, "symbolic ordered result") for value in raw_results
        ),
        symbolic_zero_policy=_nonempty(
            symbolic["zero_policy"], "symbolic_result_correction.zero_policy"
        ),
        symbolic_authority_basis=_nonempty(
            symbolic["authority_basis"], "symbolic_result_correction.authority_basis"
        ),
        content_hash=canonical_hash(root),
    )


def build_indirect_fire_authority_descriptor(
    profile: IndirectFireProfile,
    authority: IndirectFireAuthority,
    execution_policy: IndirectFireExecutionPolicy,
    ammunition: IndirectFireAmmunitionAuthority,
    observation: IndirectFireObservationAuthority,
    damage: IndirectFireDamagePolicy,
) -> IndirectFireAuthorityDescriptor:
    if authority.profile(profile.profile_id) != profile:
        raise ValueError("indirect-fire profile is not part of current authority")
    mapping = ammunition.mapping_for_weapon(profile.weapon_id)
    if mapping is None:
        raise ValueError("indirect-fire ammunition mapping is unavailable")
    resource = authority.resource_policy(profile.resource_policy_id)
    if resource is None or resource.ammo_cost != mapping.ammo_cost_per_mission:
        raise ValueError("indirect-fire ammunition authorities disagree")
    return IndirectFireAuthorityDescriptor(
        profile_id=profile.profile_id,
        authority_version=INDIRECT_FIRE_AUTHORITY_SCHEMA_VERSION,
        execution_policy_hash=execution_policy.content_hash,
        ammo_mapping_hash=ammunition.content_hash,
        observation_policy_hash=observation.content_hash,
        damage_policy_hash=damage.content_hash,
    )


def load_indirect_fire_replay_identity() -> dict[str, str]:
    """Load the exact, versioned authority set bound to INDIRECT actions."""

    authority = load_indirect_fire_authority()
    execution = load_indirect_fire_execution_policy()
    ammunition = load_indirect_fire_ammunition_authority()
    observation = load_indirect_fire_observation_authority()
    damage = load_indirect_fire_damage_policy()
    return {
        "indirect_fire_lifecycle_version": (
            "native-indirect-fire-active-area/1.0"
        ),
        "indirect_fire_authority_version": (
            INDIRECT_FIRE_AUTHORITY_SCHEMA_VERSION
        ),
        "indirect_fire_authority_hash": authority.content_hash,
        "execution_policy_hash": execution.content_hash,
        "ammo_mapping_hash": ammunition.content_hash,
        "observation_policy_hash": observation.content_hash,
        "damage_policy_hash": damage.content_hash,
    }


def load_indirect_fire_execution_policy(
    path: str | Path | None = None,
) -> IndirectFireExecutionPolicy:
    """Load the frozen executable mission-semantics authority."""

    source_path = (
        default_indirect_fire_execution_policy_path()
        if path is None
        else Path(path)
    )
    try:
        root = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"invalid indirect-fire execution policy {source_path}: {exc}"
        ) from exc
    item = _exact_mapping(
        root,
        "indirect-fire execution policy",
        {
            "schema_version",
            "policy_id",
            "policy_version",
            "authority_status",
            "ammo_policy",
            "scatter_policy",
            "cancellation_policy",
            "impact_policy",
        },
    )
    if item["schema_version"] != INDIRECT_FIRE_EXECUTION_POLICY_SCHEMA_VERSION:
        raise ValueError("unsupported indirect-fire execution policy schema")
    if (
        item["policy_id"] != INDIRECT_FIRE_EXECUTION_POLICY_ID
        or item["policy_version"] != INDIRECT_FIRE_EXECUTION_POLICY_VERSION
    ):
        raise ValueError("unsupported indirect-fire execution policy identity")
    if item["authority_status"] != INDIRECT_FIRE_AUTHORITY_STATUS:
        raise ValueError("indirect-fire execution policy is not authority-ready")

    ammo = _exact_mapping(
        item["ammo_policy"],
        "ammo_policy",
        {"consume_timing", "refund_policy"},
    )
    scatter = _exact_mapping(
        item["scatter_policy"],
        "scatter_policy",
        {
            "direction_policy",
            "direction_draw_policy",
            "distance_policy",
            "distance_table_reference",
            "planned_hex_field",
            "impact_hex_field",
        },
    )
    cancellation = _exact_mapping(
        item["cancellation_policy"],
        "cancellation_policy",
        {"allowed_phases", "reasons", "refund_policy"},
    )
    impact = _exact_mapping(
        item["impact_policy"],
        "impact_policy",
        {"affected_hex_policy", "one_hit_per_unit", "multi_target_order"},
    )
    raw_phases = cancellation["allowed_phases"]
    raw_reasons = cancellation["reasons"]
    if not isinstance(raw_phases, list) or not isinstance(raw_reasons, list):
        raise ValueError("cancellation phases and reasons must be arrays")
    return IndirectFireExecutionPolicy(
        policy_id=_nonempty(item["policy_id"], "policy_id"),
        policy_version=_nonempty(item["policy_version"], "policy_version"),
        ammo_policy=IndirectFireAmmoPolicy(
            consume_timing=IndirectFireAmmoConsumeTiming(
                str(ammo["consume_timing"])
            ),
            refund_policy=IndirectFireAmmoRefundPolicy(
                str(ammo["refund_policy"])
            ),
        ),
        scatter_policy=IndirectScatterDirectionPolicy(
            direction_policy=IndirectScatterDirectionMode(
                str(scatter["direction_policy"])
            ),
            direction_draw_policy=str(scatter["direction_draw_policy"]),
            distance_policy=IndirectScatterDistancePolicy(
                str(scatter["distance_policy"])
            ),
            distance_table_reference=_nonempty(
                scatter["distance_table_reference"],
                "distance_table_reference",
            ),
            planned_hex_field=str(scatter["planned_hex_field"]),
            impact_hex_field=str(scatter["impact_hex_field"]),
        ),
        cancellation_policy=IndirectFireCancellationPolicy(
            allowed_phases=tuple(
                IndirectFireMissionState(str(value)) for value in raw_phases
            ),
            reasons=tuple(
                IndirectFireCancellationReason(str(value)) for value in raw_reasons
            ),
            refund_policy=IndirectFireAmmoRefundPolicy(
                str(cancellation["refund_policy"])
            ),
        ),
        impact_policy=IndirectFireImpactPolicy(
            affected_hex_policy=IndirectAffectedHexPolicy(
                str(impact["affected_hex_policy"])
            ),
            one_hit_per_unit=_exact_bool(
                impact["one_hit_per_unit"], "one_hit_per_unit"
            ),
            multi_target_order=IndirectMultiTargetOrder(
                str(impact["multi_target_order"])
            ),
        ),
        content_hash=canonical_hash(root),
    )


def load_indirect_fire_authority(
    path: str | Path | None = None,
    *,
    repository_root: str | Path | None = None,
) -> IndirectFireAuthority:
    source_path = default_indirect_fire_profiles_path() if path is None else Path(path)
    try:
        root = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"invalid indirect-fire authority configuration {source_path}: {exc}"
        ) from exc
    if not isinstance(root, Mapping):
        raise ValueError("indirect-fire authority root must be an object")
    required = {
        "schema_version",
        "config_id",
        "config_version",
        "authority_status",
        "unknown_policy",
        "source",
        "profiles",
        "resource_policies",
        "mission_policies",
        "scatter_profiles",
        "impact_profiles",
        "unresolved_authority",
    }
    if set(root) != required:
        raise ValueError("indirect-fire authority fields mismatch")
    if root["schema_version"] != INDIRECT_FIRE_AUTHORITY_SCHEMA_VERSION:
        raise ValueError("unsupported indirect-fire authority schema")
    if root["authority_status"] != INDIRECT_FIRE_AUTHORITY_STATUS:
        raise ValueError("indirect-fire authority is not authority-ready")
    if root["unknown_policy"] != INDIRECT_FIRE_UNKNOWN_POLICY:
        raise ValueError("indirect-fire unknown policy must reject execution")

    root_path = (
        Path(repository_root).resolve()
        if repository_root is not None
        else Path(__file__).resolve().parents[3]
    )
    authority_source = _load_authority_source(root["source"], root_path)
    profiles = _load_profiles(root["profiles"])
    resource_policies = _load_resource_policies(root["resource_policies"])
    mission_policies = _load_mission_policies(root["mission_policies"])
    scatter_profiles = _load_scatter_profiles(root["scatter_profiles"])
    impact_profiles = _load_impact_profiles(root["impact_profiles"])
    unresolved = _sorted_unique_strings(
        root["unresolved_authority"], "unresolved_authority"
    )

    for name, values in (
        ("profiles", profiles),
        ("resource_policies", resource_policies),
        ("mission_policies", mission_policies),
        ("scatter_profiles", scatter_profiles),
        ("impact_profiles", impact_profiles),
    ):
        if not values:
            raise ValueError(f"indirect-fire {name} must not be empty")

    _unique((item.profile_id for item in profiles), "profile_id")
    _unique((item.weapon_id for item in profiles), "weapon_id")
    _unique((item.policy_id for item in resource_policies), "resource policy")
    _unique((item.policy_id for item in mission_policies), "mission policy")
    _unique((item.scatter_model_id for item in scatter_profiles), "scatter model")
    _unique((item.profile_id for item in impact_profiles), "impact profile")

    resource_ids = {item.policy_id for item in resource_policies}
    mission_ids = {item.policy_id for item in mission_policies}
    scatter_ids = {item.scatter_model_id for item in scatter_profiles}
    impact_ids = {item.profile_id for item in impact_profiles}
    for profile in profiles:
        if profile.resource_policy_id not in resource_ids:
            raise ValueError(f"unknown resource policy {profile.resource_policy_id!r}")
        if profile.mission_policy_id not in mission_ids:
            raise ValueError(f"unknown mission policy {profile.mission_policy_id!r}")
        if profile.scatter_model_id not in scatter_ids:
            raise ValueError(f"unknown scatter profile {profile.scatter_model_id!r}")
        if profile.effect_model_id not in impact_ids:
            raise ValueError(f"unknown impact profile {profile.effect_model_id!r}")

    return IndirectFireAuthority(
        config_id=_nonempty(root["config_id"], "config_id"),
        config_version=_nonempty(root["config_version"], "config_version"),
        source=authority_source,
        profiles=profiles,
        resource_policies=resource_policies,
        mission_policies=mission_policies,
        scatter_profiles=scatter_profiles,
        impact_profiles=impact_profiles,
        unresolved_authority=unresolved,
        content_hash=canonical_hash(root),
    )


def _load_authority_source(
    value: object,
    repository_root: Path,
) -> IndirectFireAuthoritySource:
    item = _exact_mapping(
        value,
        "source",
        {
            "machine_table",
            "table_set_id",
            "table_revision",
            "table_content_sha256",
            "guide",
            "context",
        },
    )
    result = IndirectFireAuthoritySource(
        machine_table=_relative_text(item["machine_table"], "machine_table"),
        table_set_id=_nonempty(item["table_set_id"], "table_set_id"),
        table_revision=_nonempty(item["table_revision"], "table_revision"),
        table_content_sha256=_nonempty(
            item["table_content_sha256"], "table_content_sha256"
        ),
        guide=_relative_text(item["guide"], "guide"),
        context=_relative_text(item["context"], "context"),
    )
    for relative in (result.machine_table, result.guide, result.context):
        target = (repository_root / relative).resolve()
        if not target.is_relative_to(repository_root) or not target.is_file():
            raise ValueError(f"indirect-fire authority source is unavailable: {relative}")
    table = json.loads((repository_root / result.machine_table).read_text(encoding="utf-8"))
    metadata = _mapping(table.get("metadata"), "machine table metadata")
    if metadata.get("table_set_id") != result.table_set_id:
        raise ValueError("indirect-fire table_set_id mismatch")
    if metadata.get("revision") != result.table_revision:
        raise ValueError("indirect-fire table revision mismatch")
    if metadata.get("content_sha256") != result.table_content_sha256:
        raise ValueError("indirect-fire table content hash mismatch")
    return result


def _load_profiles(value: object) -> tuple[IndirectFireProfile, ...]:
    profiles: list[IndirectFireProfile] = []
    fields = {
        "profile_id", "weapon_id", "artillery_class", "supported_fire_mode",
        "target_domain", "range_policy", "minimum_range", "maximum_range",
        "flight_time_seconds", "active_duration_seconds",
        "planned_cooldown_seconds", "scatter_model_id", "effect_model_id",
        "resource_policy_id", "mission_policy_id",
    }
    for key, raw in sorted(_mapping(value, "profiles").items()):
        item = _exact_mapping(raw, str(key), fields)
        profile_id = _nonempty(item["profile_id"], "profile_id")
        if profile_id != key:
            raise ValueError("indirect-fire profile key/id mismatch")
        profiles.append(IndirectFireProfile(
            profile_id=profile_id,
            weapon_id=_nonnegative_int(item["weapon_id"], "weapon_id"),
            artillery_class=ArtilleryClass(str(item["artillery_class"])),
            supported_fire_mode=str(item["supported_fire_mode"]),
            target_domain=IndirectTargetDomain(str(item["target_domain"])),
            range_policy=IndirectRangePolicy(str(item["range_policy"])),
            minimum_range=_optional_nonnegative_int(item["minimum_range"], "minimum_range"),
            maximum_range=_optional_nonnegative_int(item["maximum_range"], "maximum_range"),
            flight_time_seconds=_nonnegative_int(item["flight_time_seconds"], "flight_time_seconds"),
            active_duration_seconds=_nonnegative_int(item["active_duration_seconds"], "active_duration_seconds"),
            planned_cooldown_seconds=_nonnegative_int(item["planned_cooldown_seconds"], "planned_cooldown_seconds"),
            scatter_model_id=_nonempty(item["scatter_model_id"], "scatter_model_id"),
            effect_model_id=_nonempty(item["effect_model_id"], "effect_model_id"),
            resource_policy_id=_nonempty(item["resource_policy_id"], "resource_policy_id"),
            mission_policy_id=_nonempty(item["mission_policy_id"], "mission_policy_id"),
        ))
    return tuple(sorted(
        profiles,
        key=lambda item: (item.weapon_id, item.profile_id),
    ))


def _load_resource_policies(
    value: object,
) -> tuple[IndirectFireResourcePolicy, ...]:
    values: list[IndirectFireResourcePolicy] = []
    fields = {"policy_id", "ammo_cost", "cooldown_policy", "cancel_policy"}
    for key, raw in sorted(_mapping(value, "resource_policies").items()):
        item = _exact_mapping(raw, str(key), fields)
        policy_id = _nonempty(item["policy_id"], "policy_id")
        if policy_id != key:
            raise ValueError("resource policy key/id mismatch")
        raw_cost = item["ammo_cost"]
        cost = (
            UnresolvedAuthority.UNRESOLVED
            if raw_cost == UnresolvedAuthority.UNRESOLVED.value
            else _nonnegative_int(raw_cost, "ammo_cost")
        )
        values.append(IndirectFireResourcePolicy(
            policy_id=policy_id,
            ammo_cost=cost,
            cooldown_policy=_nonempty(item["cooldown_policy"], "cooldown_policy"),
            cancel_policy=_nonempty(item["cancel_policy"], "cancel_policy"),
        ))
    return tuple(values)


def _load_mission_policies(
    value: object,
) -> tuple[IndirectFireMissionPolicy, ...]:
    values: list[IndirectFireMissionPolicy] = []
    fields = {
        "policy_id", "states", "flight_duration_seconds",
        "active_duration_seconds", "can_cancel_during_flight",
    }
    for key, raw in sorted(_mapping(value, "mission_policies").items()):
        item = _exact_mapping(raw, str(key), fields)
        policy_id = _nonempty(item["policy_id"], "policy_id")
        if policy_id != key:
            raise ValueError("mission policy key/id mismatch")
        raw_states = item["states"]
        if not isinstance(raw_states, list):
            raise ValueError("mission policy states must be an array")
        values.append(IndirectFireMissionPolicy(
            policy_id=policy_id,
            states=tuple(IndirectFireMissionState(str(state)) for state in raw_states),
            flight_duration_seconds=_nonnegative_int(item["flight_duration_seconds"], "flight_duration_seconds"),
            active_duration_seconds=_nonnegative_int(item["active_duration_seconds"], "active_duration_seconds"),
            can_cancel_during_flight=_exact_bool(item["can_cancel_during_flight"], "can_cancel_during_flight"),
        ))
    return tuple(values)


def _load_scatter_profiles(
    value: object,
) -> tuple[IndirectScatterProfile, ...]:
    values: list[IndirectScatterProfile] = []
    fields = {
        "scatter_model_id", "dice_expression", "table_reference",
        "direction_policy", "distance_policy",
    }
    for key, raw in sorted(_mapping(value, "scatter_profiles").items()):
        item = _exact_mapping(raw, str(key), fields)
        model_id = _nonempty(item["scatter_model_id"], "scatter_model_id")
        if model_id != key:
            raise ValueError("scatter profile key/id mismatch")
        values.append(IndirectScatterProfile(
            scatter_model_id=model_id,
            dice_expression=str(item["dice_expression"]),
            table_reference=_nonempty(item["table_reference"], "table_reference"),
            direction_policy=ScatterDirectionPolicy(str(item["direction_policy"])),
            distance_policy=_nonempty(item["distance_policy"], "distance_policy"),
        ))
    return tuple(values)


def _load_impact_profiles(
    value: object,
) -> tuple[IndirectImpactProfile, ...]:
    values: list[IndirectImpactProfile] = []
    fields = {
        "profile_id", "affected_area", "resolution_scope",
        "friendly_fire", "one_hit_per_unit", "planned_hex_field",
        "impact_hex_field",
    }
    for key, raw in sorted(_mapping(value, "impact_profiles").items()):
        item = _exact_mapping(raw, str(key), fields)
        profile_id = _nonempty(item["profile_id"], "profile_id")
        if profile_id != key:
            raise ValueError("impact profile key/id mismatch")
        values.append(IndirectImpactProfile(
            profile_id=profile_id,
            affected_area=str(item["affected_area"]),
            resolution_scope=str(item["resolution_scope"]),
            friendly_fire=_exact_bool(item["friendly_fire"], "friendly_fire"),
            one_hit_per_unit=_exact_bool(item["one_hit_per_unit"], "one_hit_per_unit"),
            planned_hex_field=_nonempty(
                item["planned_hex_field"], "planned_hex_field"
            ),
            impact_hex_field=_nonempty(
                item["impact_hex_field"], "impact_hex_field"
            ),
        ))
    return tuple(values)


def _exact_mapping(
    value: object,
    name: str,
    fields: set[str],
) -> Mapping[str, Any]:
    item = _mapping(value, name)
    if set(item) != fields:
        raise ValueError(f"{name} fields mismatch")
    return item


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _read_json_object(path: Path, name: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {name} configuration {path}: {exc}") from exc
    return _mapping(value, name)


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _relative_text(value: object, name: str) -> str:
    text = _nonempty(value, name)
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{name} must be a safe repository-relative path")
    return path.as_posix()


def _nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative exact int")
    return value


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive exact int")
    return value


def _exact_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an exact int")
    return value


def _sha256(value: object, name: str) -> str:
    text = _nonempty(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{name} must be lowercase SHA-256")
    return text


def _optional_nonnegative_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value, name)


def _exact_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be an exact bool")
    return value


def _sorted_unique_strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    normalized = tuple(_nonempty(item, name) for item in value)
    if normalized != tuple(sorted(set(normalized))):
        raise ValueError(f"{name} must be unique and deterministically sorted")
    return normalized


def _unique(values: Iterable[object], name: str) -> None:
    materialized = tuple(values)
    if len(materialized) != len(set(materialized)):
        raise ValueError(f"{name} values must be unique")


__all__ = [
    "ArtilleryClass",
    "INDIRECT_FIRE_AMMUNITION_MAPPING_SCHEMA_VERSION",
    "INDIRECT_FIRE_DAMAGE_POLICY_SCHEMA_VERSION",
    "INDIRECT_FIRE_EXECUTION_POLICY_ID",
    "INDIRECT_FIRE_EXECUTION_POLICY_SCHEMA_VERSION",
    "INDIRECT_FIRE_EXECUTION_POLICY_VERSION",
    "INDIRECT_FIRE_AUTHORITY_SCHEMA_VERSION",
    "INDIRECT_FIRE_OBSERVATION_POLICY_SCHEMA_VERSION",
    "IndirectAffectedHexPolicy",
    "IndirectFireAmmoConsumeTiming",
    "IndirectFireAmmoPolicy",
    "IndirectFireAmmoRefundPolicy",
    "IndirectFireAmmunitionAuthority",
    "IndirectFireAmmunitionMapping",
    "IndirectFireAuthority",
    "IndirectFireAuthorityDescriptor",
    "IndirectFireAuthoritySource",
    "IndirectFireCancellationPolicy",
    "IndirectFireCancellationReason",
    "IndirectFireDamagePolicy",
    "IndirectFireExecutionPolicy",
    "IndirectFireImpactPolicy",
    "IndirectFireMissionPolicy",
    "IndirectFireMissionState",
    "IndirectFireObservationAuthority",
    "IndirectFireProfile",
    "IndirectFireResourcePolicy",
    "IndirectImpactProfile",
    "IndirectRangePolicy",
    "IndirectMultiTargetOrder",
    "IndirectScatterDirectionMode",
    "IndirectScatterDirectionPolicy",
    "IndirectScatterDistancePolicy",
    "IndirectScatterProfile",
    "IndirectTargetDomain",
    "ScatterDirectionPolicy",
    "UnresolvedAuthority",
    "build_indirect_fire_authority_descriptor",
    "default_indirect_fire_ammunition_mapping_path",
    "default_indirect_fire_damage_policy_path",
    "default_indirect_fire_profiles_path",
    "default_indirect_fire_execution_policy_path",
    "default_indirect_fire_observation_policy_path",
    "load_indirect_fire_ammunition_authority",
    "load_indirect_fire_authority",
    "load_indirect_fire_damage_policy",
    "load_indirect_fire_execution_policy",
    "load_indirect_fire_observation_authority",
    "load_indirect_fire_replay_identity",
]
