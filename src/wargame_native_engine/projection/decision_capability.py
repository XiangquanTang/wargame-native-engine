"""Pure adapters for frozen Engine action and capability evidence.

This module consumes detached Engine-produced records.  It never reads a
WorldState, interprets lifecycle state, calls visibility, enumerates targets,
or decides action legality.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
import json
import math
import re
from types import MappingProxyType
from typing import Iterable, Sequence

from wargame_native_engine.core.action import (
    ActionType,
    EngineAction,
    ValidAction,
)
from wargame_native_engine.core.boarding import (
    BoardCapability,
    BoardingCapabilityAssessment,
    BoardingCapabilityStatus,
    BoardingTransitionType,
    UnloadCapability,
)
from wargame_native_engine.core.concealment import ConcealmentCapability
from wargame_native_engine.core.fire_capability import (
    GuidedFireCapability,
    IndirectFireCapability,
    IndirectFireCapabilityStatus,
)
from wargame_native_engine.io.serialization import (
    canonical_hash,
    canonical_json,
    json_clone,
)
from wargame_native_engine.projection.decision_semantic import (
    DECISION_ACTION_CATALOG_SCHEMA_VERSION,
    DECISION_CAPABILITY_SNAPSHOT_SCHEMA_VERSION,
    ActionCatalogReference,
    AuthorityMetadata,
    CapabilityAvailabilityStatus,
    CapabilityBlockerCode,
    CapabilityEvidenceKind,
    CapabilityFamilyStatus,
    DecisionCapabilityFamily,
    DecisionCapabilitySnapshot,
    DecisionProjectionContractError,
    DecisionSide,
    DecisionSnapshotIdentity,
    OperatorCapabilitySummary,
    SideDecisionContext,
    SideDecisionSemanticProjection,
    action_catalog_reference_hash,
    build_decision_semantic_projection,
)
from wargame_native_engine.projection.operator_perspective import (
    ObservedEnemyOperatorLifecyclePayload,
    OwnOperatorLifecyclePayload,
)


_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_VALID_ACTION_FIELDS = {"action", "priority", "source"}
_ENGINE_ACTION_FIELDS = {
    "side",
    "actor_id",
    "action_type",
    "target_id",
    "target_position",
    "parameters",
}

_ACTION_FAMILY = {
    ActionType.DEPLOY: DecisionCapabilityFamily.DEPLOY,
    ActionType.END_DEPLOYMENT: DecisionCapabilityFamily.END_DEPLOYMENT,
    ActionType.MOVE: DecisionCapabilityFamily.MOVE,
    ActionType.CHANGE_STATE: DecisionCapabilityFamily.CHANGE_STATE,
    ActionType.ATTACK: DecisionCapabilityFamily.DIRECT_FIRE,
    ActionType.SHOOT: DecisionCapabilityFamily.DIRECT_FIRE,
    ActionType.STOP_MOVE: DecisionCapabilityFamily.STOP_MOVE,
    ActionType.BOARD: DecisionCapabilityFamily.BOARD,
    ActionType.UNLOAD: DecisionCapabilityFamily.UNLOAD,
    ActionType.CAPTURE: DecisionCapabilityFamily.CAPTURE,
    ActionType.ENTER_CONCEALMENT: DecisionCapabilityFamily.ENTER_CONCEALMENT,
    ActionType.WEAPON_LOCK: DecisionCapabilityFamily.WEAPON_LOCK,
    ActionType.WEAPON_UNFOLD: DecisionCapabilityFamily.WEAPON_UNFOLD,
    ActionType.WAIT: DecisionCapabilityFamily.WAIT,
}
_FIRE_MODE_FAMILY = {
    "GUIDED": DecisionCapabilityFamily.GUIDED_FIRE,
    "INDIRECT": DecisionCapabilityFamily.INDIRECT_FIRE,
}
_CONCEALMENT_NOT_APPLICABLE = frozenset({
    "AIR_UNIT_NOT_SUPPORTED",
    "UNIT_DOMAIN_NOT_SUPPORTED",
})


@dataclass(frozen=True, slots=True)
class ExplicitCapabilityEvidence:
    side: DecisionSide
    decision_window_index: int
    frame_index: int
    sim_time_ms: int
    operator_reference: int
    family: DecisionCapabilityFamily
    status: CapabilityAvailabilityStatus
    producer_namespace: str
    blocker_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.side, DecisionSide):
            raise TypeError("side must be DecisionSide")
        for name in (
            "decision_window_index",
            "frame_index",
            "sim_time_ms",
            "operator_reference",
        ):
            _nonnegative_int(getattr(self, name), name)
        if not isinstance(self.family, DecisionCapabilityFamily):
            raise TypeError("family must be DecisionCapabilityFamily")
        if self.status not in {
            CapabilityAvailabilityStatus.AVAILABLE,
            CapabilityAvailabilityStatus.BLOCKED,
            CapabilityAvailabilityStatus.NOT_APPLICABLE,
            CapabilityAvailabilityStatus.MISSING,
        }:
            raise DecisionProjectionContractError(
                "explicit capability evidence cannot assert UNKNOWN"
            )
        _token(self.producer_namespace, "producer_namespace")
        blockers = tuple(sorted(set(self.blocker_codes)))
        if blockers != tuple(self.blocker_codes):
            raise DecisionProjectionContractError(
                "explicit blocker codes must be sorted and unique"
            )
        for value in blockers:
            _token(value, "blocker code")
        if self.status is CapabilityAvailabilityStatus.BLOCKED:
            if not blockers:
                raise DecisionProjectionContractError(
                    "explicit BLOCKED evidence requires blocker codes"
                )
        elif blockers:
            raise DecisionProjectionContractError(
                "only explicit BLOCKED evidence may contain blocker codes"
            )


@dataclass(frozen=True, slots=True)
class ActionCatalogEntry:
    action_id: str
    actor_reference: int | None
    capability_family: DecisionCapabilityFamily
    action_type: str
    target_reference: int | None
    target_position: tuple[float, float] | None
    canonical_parameters_json: str
    priority: int
    producer_namespace: str
    operation_hash: str
    semantic_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _text(self.action_id, "action_id")
        if self.actor_reference is not None:
            _nonnegative_int(self.actor_reference, "actor_reference")
        if not isinstance(self.capability_family, DecisionCapabilityFamily):
            raise TypeError("capability_family must be DecisionCapabilityFamily")
        _token(self.action_type, "action_type")
        if self.target_reference is not None:
            _nonnegative_int(self.target_reference, "target_reference")
        if self.target_position is not None:
            if (
                not isinstance(self.target_position, tuple)
                or len(self.target_position) != 2
            ):
                raise TypeError("target_position must be a two-item tuple")
            for value in self.target_position:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                ):
                    raise TypeError("target_position must contain finite numbers")
        try:
            parameters = json.loads(self.canonical_parameters_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DecisionProjectionContractError(
                "canonical_parameters_json must contain canonical JSON"
            ) from exc
        if not isinstance(parameters, dict):
            raise DecisionProjectionContractError(
                "action parameters must be a JSON object"
            )
        if canonical_json(parameters) != self.canonical_parameters_json:
            raise DecisionProjectionContractError(
                "action parameters JSON is not canonical"
            )
        if type(self.priority) is not int:
            raise TypeError("priority must be an exact int")
        _token(self.producer_namespace, "producer_namespace")
        _sha256(self.operation_hash, "operation_hash")
        object.__setattr__(
            self,
            "semantic_hash",
            canonical_hash(_action_entry_payload(self)),
        )

    def parameters(self) -> dict[str, object]:
        value = json.loads(self.canonical_parameters_json)
        if not isinstance(value, dict):  # pragma: no cover - constructor proved it
            raise RuntimeError("catalog parameters unexpectedly changed shape")
        return value

    def to_dict(self) -> dict[str, object]:
        return {
            **_action_entry_payload(self),
            "semantic_hash": self.semantic_hash,
        }


@dataclass(frozen=True, slots=True)
class ActionCatalogSnapshot:
    schema_version: str
    side: DecisionSide
    decision_window_index: int
    frame_index: int
    sim_time_ms: int
    entries: tuple[ActionCatalogEntry, ...]
    catalog_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != DECISION_ACTION_CATALOG_SCHEMA_VERSION:
            raise DecisionProjectionContractError(
                "unsupported action catalog snapshot schema"
            )
        if not isinstance(self.side, DecisionSide):
            raise TypeError("side must be DecisionSide")
        for name in ("decision_window_index", "frame_index", "sim_time_ms"):
            _nonnegative_int(getattr(self, name), name)
        entries = tuple(self.entries)
        if any(type(value) is not ActionCatalogEntry for value in entries):
            raise TypeError("entries must contain exact ActionCatalogEntry values")
        action_ids = tuple(item.action_id for item in entries)
        if len(set(action_ids)) != len(action_ids):
            raise DecisionProjectionContractError("duplicate action_id in catalog")
        operations = tuple(item.operation_hash for item in entries)
        if len(set(operations)) != len(operations):
            raise DecisionProjectionContractError(
                "duplicate concrete semantic action in catalog"
            )
        object.__setattr__(self, "entries", entries)
        object.__setattr__(
            self,
            "catalog_hash",
            action_catalog_reference_hash(
                schema_version=self.schema_version,
                side=self.side,
                decision_window_index=self.decision_window_index,
                frame_index=self.frame_index,
                sim_time_ms=self.sim_time_ms,
                action_ids=action_ids,
                action_entry_hashes=tuple(
                    item.semantic_hash for item in entries
                ),
                actor_references=tuple(sorted({
                    item.actor_reference
                    for item in entries
                    if item.actor_reference is not None
                })),
            ),
        )

    @property
    def action_count(self) -> int:
        return len(self.entries)

    @property
    def action_ids(self) -> tuple[str, ...]:
        return tuple(item.action_id for item in self.entries)

    def reference(self) -> ActionCatalogReference:
        actors = tuple(sorted({
            item.actor_reference
            for item in self.entries
            if item.actor_reference is not None
        }))
        return ActionCatalogReference(
            schema_version=self.schema_version,
            side=self.side,
            decision_window_index=self.decision_window_index,
            frame_index=self.frame_index,
            sim_time_ms=self.sim_time_ms,
            action_count=len(self.entries),
            action_ids=self.action_ids,
            action_entry_hashes=tuple(
                item.semantic_hash for item in self.entries
            ),
            actor_references=actors,
            catalog_hash=self.catalog_hash,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "side": self.side.value,
            "decision_window_index": self.decision_window_index,
            "frame_index": self.frame_index,
            "sim_time_ms": self.sim_time_ms,
            "action_count": len(self.entries),
            "entries": [item.to_dict() for item in self.entries],
            "catalog_hash": self.catalog_hash,
        }


_QUERY_ANY = object()


@dataclass(frozen=True, slots=True)
class ActionCatalogIndex:
    """Immutable, order-preserving queries over one frozen action catalog."""

    catalog: ActionCatalogSnapshot
    _parameters: tuple[Mapping[str, object], ...] = field(
        init=False, repr=False, compare=False,
    )
    _by_actor: Mapping[int | None, tuple[int, ...]] = field(
        init=False, repr=False, compare=False,
    )
    _by_family: Mapping[DecisionCapabilityFamily, tuple[int, ...]] = field(
        init=False, repr=False, compare=False,
    )
    _by_action_type: Mapping[str, tuple[int, ...]] = field(
        init=False, repr=False, compare=False,
    )
    _by_target: Mapping[int | None, tuple[int, ...]] = field(
        init=False, repr=False, compare=False,
    )

    def __post_init__(self) -> None:
        if type(self.catalog) is not ActionCatalogSnapshot:
            raise TypeError("catalog must be an exact ActionCatalogSnapshot")
        parameters: list[Mapping[str, object]] = []
        actors: dict[int | None, list[int]] = {}
        families: dict[DecisionCapabilityFamily, list[int]] = {}
        action_types: dict[str, list[int]] = {}
        targets: dict[int | None, list[int]] = {}
        for position, entry in enumerate(self.catalog.entries):
            decoded = json.loads(entry.canonical_parameters_json)
            parameters.append(_freeze_json_mapping(decoded))
            actors.setdefault(entry.actor_reference, []).append(position)
            families.setdefault(entry.capability_family, []).append(position)
            action_types.setdefault(entry.action_type, []).append(position)
            targets.setdefault(entry.target_reference, []).append(position)
        object.__setattr__(self, "_parameters", tuple(parameters))
        object.__setattr__(self, "_by_actor", _freeze_position_index(actors))
        object.__setattr__(self, "_by_family", _freeze_position_index(families))
        object.__setattr__(
            self, "_by_action_type", _freeze_position_index(action_types),
        )
        object.__setattr__(self, "_by_target", _freeze_position_index(targets))

    def query(
        self,
        *,
        actor_reference: int | None | object = _QUERY_ANY,
        capability_family: DecisionCapabilityFamily | None = None,
        action_type: str | None = None,
        target_reference: int | None | object = _QUERY_ANY,
        target_position: tuple[float, float] | None | object = _QUERY_ANY,
        parameters: Mapping[str, object] | None = None,
    ) -> tuple[ActionCatalogEntry, ...]:
        """Return matching entries in their original catalog order.

        ``parameters`` is an explicit subset match. Omitting actor or target
        means any value, while passing ``None`` selects catalog-wide actions
        that intentionally have no actor or target.
        """

        candidate_sets: list[tuple[int, ...]] = []
        if actor_reference is not _QUERY_ANY:
            _optional_reference(actor_reference, "actor_reference")
            candidate_sets.append(self._by_actor.get(actor_reference, ()))
        if capability_family is not None:
            if not isinstance(capability_family, DecisionCapabilityFamily):
                raise TypeError("capability_family must be DecisionCapabilityFamily")
            candidate_sets.append(self._by_family.get(capability_family, ()))
        if action_type is not None:
            _token(action_type, "action_type")
            candidate_sets.append(self._by_action_type.get(action_type, ()))
        if target_reference is not _QUERY_ANY:
            _optional_reference(target_reference, "target_reference")
            candidate_sets.append(self._by_target.get(target_reference, ()))
        if target_position is not _QUERY_ANY:
            _optional_target_position(target_position)
        expected_parameters = (
            None if parameters is None else _normalized_query_parameters(parameters)
        )
        positions = (
            range(len(self.catalog.entries))
            if not candidate_sets
            else min(candidate_sets, key=len)
        )
        result: list[ActionCatalogEntry] = []
        for position in positions:
            entry = self.catalog.entries[position]
            if (
                actor_reference is not _QUERY_ANY
                and entry.actor_reference != actor_reference
            ):
                continue
            if (
                capability_family is not None
                and entry.capability_family is not capability_family
            ):
                continue
            if action_type is not None and entry.action_type != action_type:
                continue
            if (
                target_reference is not _QUERY_ANY
                and entry.target_reference != target_reference
            ):
                continue
            if (
                target_position is not _QUERY_ANY
                and entry.target_position != target_position
            ):
                continue
            if expected_parameters is not None and not _mapping_contains(
                self._parameters[position], expected_parameters,
            ):
                continue
            result.append(entry)
        return tuple(result)


@dataclass(frozen=True, slots=True)
class ActionCatalogRecordAdapter:
    """Bind typed catalog entries to detached legacy records without loss."""

    index: ActionCatalogIndex | None
    _record_json: tuple[str, ...] = field(repr=False)
    _legacy_descriptors: tuple[
        tuple[
            int | None,
            DecisionCapabilityFamily | None,
            str,
            int | None,
            tuple[float, float] | None,
            Mapping[str, object],
        ] | None,
        ...,
    ] = field(repr=False, compare=False)
    _position_by_action_id: Mapping[str, int] = field(
        repr=False, compare=False,
    )

    def __post_init__(self) -> None:
        if self.index is not None and type(self.index) is not ActionCatalogIndex:
            raise TypeError("index must be an exact ActionCatalogIndex or None")
        object.__setattr__(self, "_record_json", tuple(self._record_json))
        descriptors = tuple(self._legacy_descriptors)
        for value in descriptors:
            if value is not None and not isinstance(value[5], MappingProxyType):
                raise TypeError("legacy descriptor parameters must be read-only")
        object.__setattr__(self, "_legacy_descriptors", descriptors)
        object.__setattr__(
            self,
            "_position_by_action_id",
            MappingProxyType(dict(self._position_by_action_id)),
        )

    @classmethod
    def from_records(
        cls,
        catalog: ActionCatalogSnapshot | None,
        legacy_records: Sequence[Mapping[str, object]],
    ) -> ActionCatalogRecordAdapter:
        if isinstance(legacy_records, (str, bytes)) or not isinstance(
            legacy_records, Sequence,
        ):
            raise TypeError("legacy_records must be a sequence")
        raw_records = tuple(legacy_records)
        if any(not isinstance(value, Mapping) for value in raw_records):
            raise TypeError("legacy_records must contain mappings")
        record_json = tuple(canonical_json(value) for value in raw_records)
        if catalog is None:
            return cls(
                None,
                record_json,
                tuple(_legacy_descriptor(value) for value in raw_records),
                MappingProxyType({}),
            )
        index = ActionCatalogIndex(catalog)
        if len(raw_records) != catalog.action_count:
            raise DecisionProjectionContractError(
                "legacy action/catalog count mismatch"
            )
        for position, (raw, entry) in enumerate(
            zip(raw_records, catalog.entries)
        ):
            if raw.get("action_id") != entry.action_id:
                raise DecisionProjectionContractError(
                    f"legacy action identity mismatch at index {position}"
                )
            action = raw.get("action")
            if not isinstance(action, Mapping):
                raise DecisionProjectionContractError(
                    f"legacy action record missing at index {position}"
                )
            if canonical_hash(action) != entry.operation_hash:
                raise DecisionProjectionContractError(
                    f"legacy operation mismatch at index {position}"
                )
        return cls(
            index,
            record_json,
            (),
            MappingProxyType({
                entry.action_id: position
                for position, entry in enumerate(catalog.entries)
            }),
        )

    def query_records(
        self,
        *,
        actor_reference: int | None | object = _QUERY_ANY,
        capability_family: DecisionCapabilityFamily | None = None,
        action_type: str | None = None,
        target_reference: int | None | object = _QUERY_ANY,
        target_position: tuple[float, float] | None | object = _QUERY_ANY,
        parameters: Mapping[str, object] | None = None,
    ) -> tuple[Mapping[str, object], ...]:
        if self.index is not None:
            entries = self.index.query(
                actor_reference=actor_reference,
                capability_family=capability_family,
                action_type=action_type,
                target_reference=target_reference,
                target_position=target_position,
                parameters=parameters,
            )
            positions = tuple(
                self._position_by_action_id[entry.action_id] for entry in entries
            )
        else:
            positions = self._legacy_positions(
                actor_reference=actor_reference,
                capability_family=capability_family,
                action_type=action_type,
                target_reference=target_reference,
                target_position=target_position,
                parameters=parameters,
            )
        return tuple(_decode_record(self._record_json[value]) for value in positions)

    def _legacy_positions(
        self,
        *,
        actor_reference: int | None | object,
        capability_family: DecisionCapabilityFamily | None,
        action_type: str | None,
        target_reference: int | None | object,
        target_position: tuple[float, float] | None | object,
        parameters: Mapping[str, object] | None,
    ) -> tuple[int, ...]:
        if actor_reference is not _QUERY_ANY:
            _optional_reference(actor_reference, "actor_reference")
        if capability_family is not None and not isinstance(
            capability_family, DecisionCapabilityFamily,
        ):
            raise TypeError("capability_family must be DecisionCapabilityFamily")
        if action_type is not None:
            _token(action_type, "action_type")
        if target_reference is not _QUERY_ANY:
            _optional_reference(target_reference, "target_reference")
        if target_position is not _QUERY_ANY:
            _optional_target_position(target_position)
        expected = (
            None if parameters is None else _normalized_query_parameters(parameters)
        )
        result = []
        for position, descriptor in enumerate(self._legacy_descriptors):
            if descriptor is None:
                continue
            actor, family, kind, target, position_value, supplied = descriptor
            if actor_reference is not _QUERY_ANY and actor != actor_reference:
                continue
            if capability_family is not None and family is not capability_family:
                continue
            if action_type is not None and kind != action_type:
                continue
            if target_reference is not _QUERY_ANY and target != target_reference:
                continue
            if target_position is not _QUERY_ANY and position_value != target_position:
                continue
            if expected is not None and not _mapping_contains(supplied, expected):
                continue
            result.append(position)
        return tuple(result)


def capability_family_for_action(
    action: EngineAction,
) -> DecisionCapabilityFamily:
    """Map one exact Engine action through the versioned v1 family table."""

    if type(action) is not EngineAction:
        raise TypeError("action must be an exact EngineAction")
    _validate_engine_action_schema()
    if action.action_type is ActionType.FIRE:
        mode = action.parameters.get("fire_mode")
        if not isinstance(mode, str):
            raise DecisionProjectionContractError(
                "FIRE valid action requires an explicit supported fire_mode"
            )
        family = _FIRE_MODE_FAMILY.get(mode.upper())
        if family is None:
            raise DecisionProjectionContractError(
                f"unsupported FIRE capability mode: {mode}"
            )
        return family
    family = _ACTION_FAMILY.get(action.action_type)
    if family is None:
        raise DecisionProjectionContractError(
            f"unmapped Engine action type: {action.action_type.value}"
        )
    return family


def build_action_catalog_snapshot(
    *,
    snapshot_identity: DecisionSnapshotIdentity,
    frozen_valid_actions: Sequence[ValidAction],
) -> ActionCatalogSnapshot:
    """Freeze exactly the supplied Engine order without generating actions."""

    if type(snapshot_identity) is not DecisionSnapshotIdentity:
        raise TypeError("snapshot_identity must be DecisionSnapshotIdentity")
    if isinstance(frozen_valid_actions, (str, bytes)) or not isinstance(
        frozen_valid_actions,
        Sequence,
    ):
        raise TypeError("frozen_valid_actions must be a sequence")
    _validate_valid_action_schema()
    entries: list[ActionCatalogEntry] = []
    for value in tuple(frozen_valid_actions):
        if type(value) is not ValidAction:
            raise TypeError("catalog input must contain exact ValidAction values")
        action = value.action
        if type(action) is not EngineAction:
            raise TypeError("ValidAction must contain an exact EngineAction")
        if action.side != snapshot_identity.side.value:
            raise DecisionProjectionContractError(
                "valid action side/snapshot side mismatch"
            )
        parameters = json_clone(action.parameters)
        if not isinstance(parameters, dict):
            raise DecisionProjectionContractError(
                "Engine action parameters must be a JSON object"
            )
        target_position = (
            None
            if action.target_position is None
            else (
                float(action.target_position[0]),
                float(action.target_position[1]),
            )
        )
        operation = {
            "side": action.side,
            "actor_id": action.actor_id,
            "action_type": action.action_type.value,
            "target_id": action.target_id,
            "target_position": (
                None if target_position is None else list(target_position)
            ),
            "parameters": parameters,
        }
        entries.append(ActionCatalogEntry(
            action_id=action.action_id,
            actor_reference=action.actor_id,
            capability_family=capability_family_for_action(action),
            action_type=action.action_type.value,
            target_reference=action.target_id,
            target_position=target_position,
            canonical_parameters_json=canonical_json(parameters),
            priority=value.priority,
            producer_namespace=value.source,
            operation_hash=canonical_hash(operation),
        ))
    return ActionCatalogSnapshot(
        schema_version=DECISION_ACTION_CATALOG_SCHEMA_VERSION,
        side=snapshot_identity.side,
        decision_window_index=snapshot_identity.decision_window_index,
        frame_index=snapshot_identity.frame_index,
        sim_time_ms=snapshot_identity.sim_time_ms,
        entries=tuple(entries),
    )


def build_decision_capability_snapshot(
    *,
    snapshot_identity: DecisionSnapshotIdentity,
    own_operator_references: Iterable[int],
    action_catalog: ActionCatalogSnapshot,
    explicit_capability_records: Iterable[ExplicitCapabilityEvidence] = (),
) -> DecisionCapabilitySnapshot:
    """Normalize supplied Engine evidence without reading lifecycle or World."""

    if type(snapshot_identity) is not DecisionSnapshotIdentity:
        raise TypeError("snapshot_identity must be DecisionSnapshotIdentity")
    if type(action_catalog) is not ActionCatalogSnapshot:
        raise TypeError("action_catalog must be an exact ActionCatalogSnapshot")
    _require_snapshot_match(snapshot_identity, action_catalog)
    own = _sorted_references(own_operator_references)
    records = tuple(explicit_capability_records)
    if any(type(value) is not ExplicitCapabilityEvidence for value in records):
        raise TypeError(
            "explicit_capability_records must contain exact evidence records"
        )
    explicit: dict[
        tuple[int, DecisionCapabilityFamily],
        ExplicitCapabilityEvidence,
    ] = {}
    for record in records:
        _require_snapshot_match(snapshot_identity, record)
        if record.operator_reference not in own:
            raise DecisionProjectionContractError(
                "capability evidence actor is not an OWN reference"
            )
        key = (record.operator_reference, record.family)
        if key in explicit:
            raise DecisionProjectionContractError(
                "duplicate or conflicting explicit capability evidence"
            )
        explicit[key] = record

    counts: dict[tuple[int, DecisionCapabilityFamily], int] = {}
    for entry in action_catalog.entries:
        actor = entry.actor_reference
        if actor is None:
            continue
        if actor not in own:
            raise DecisionProjectionContractError(
                "catalog action actor is not an OWN reference"
            )
        key = (actor, entry.capability_family)
        counts[key] = counts.get(key, 0) + 1

    summaries: list[OperatorCapabilitySummary] = []
    for actor in own:
        families = tuple(
            _merge_capability_evidence(
                actor,
                family,
                counts.get((actor, family), 0),
                explicit.get((actor, family)),
            )
            for family in DecisionCapabilityFamily
        )
        summaries.append(OperatorCapabilitySummary(actor, families))
    return DecisionCapabilitySnapshot(
        schema_version=DECISION_CAPABILITY_SNAPSHOT_SCHEMA_VERSION,
        side=snapshot_identity.side,
        decision_window_index=snapshot_identity.decision_window_index,
        frame_index=snapshot_identity.frame_index,
        sim_time_ms=snapshot_identity.sim_time_ms,
        operators=tuple(summaries),
    )


def build_decision_from_snapshots(
    *,
    authority_metadata: AuthorityMetadata,
    snapshot: DecisionSnapshotIdentity,
    side_context: SideDecisionContext,
    own_operators: Iterable[OwnOperatorLifecyclePayload],
    observed_enemies: Iterable[ObservedEnemyOperatorLifecyclePayload],
    capability_snapshot: DecisionCapabilitySnapshot,
    action_catalog_snapshot: ActionCatalogSnapshot,
) -> SideDecisionSemanticProjection:
    """Bind one real catalog snapshot to the existing pure Decision core."""

    if type(action_catalog_snapshot) is not ActionCatalogSnapshot:
        raise TypeError(
            "action_catalog_snapshot must be an exact ActionCatalogSnapshot"
        )
    _require_snapshot_match(snapshot, action_catalog_snapshot)
    _validate_catalog_capability_consistency(
        action_catalog_snapshot,
        capability_snapshot,
    )
    return build_decision_semantic_projection(
        authority_metadata=authority_metadata,
        snapshot=snapshot,
        side_context=side_context,
        own_operators=own_operators,
        observed_enemies=observed_enemies,
        capability_snapshot=capability_snapshot,
        action_catalog_reference=action_catalog_snapshot.reference(),
    )


def guided_capability_evidence(
    snapshot: DecisionSnapshotIdentity,
    values: Iterable[GuidedFireCapability],
) -> tuple[ExplicitCapabilityEvidence, ...]:
    actors: set[int] = set()
    for value in tuple(values):
        if type(value) is not GuidedFireCapability:
            raise TypeError("guided evidence requires GuidedFireCapability")
        actors.add(value.guider_id)
    return tuple(
        _producer_evidence(
            snapshot,
            actor,
            DecisionCapabilityFamily.GUIDED_FIRE,
            CapabilityAvailabilityStatus.AVAILABLE,
            "engine.guided_fire_capabilities",
        )
        for actor in sorted(actors)
    )


def board_capability_evidence(
    snapshot: DecisionSnapshotIdentity,
    values: Iterable[BoardCapability],
) -> tuple[ExplicitCapabilityEvidence, ...]:
    actors: set[int] = set()
    for value in tuple(values):
        if type(value) is not BoardCapability:
            raise TypeError("board evidence requires BoardCapability")
        actors.add(value.passenger_id)
    return tuple(
        _producer_evidence(
            snapshot,
            actor,
            DecisionCapabilityFamily.BOARD,
            CapabilityAvailabilityStatus.AVAILABLE,
            "engine.board_capabilities",
        )
        for actor in sorted(actors)
    )


def unload_capability_evidence(
    snapshot: DecisionSnapshotIdentity,
    values: Iterable[UnloadCapability],
) -> tuple[ExplicitCapabilityEvidence, ...]:
    actors: set[int] = set()
    for value in tuple(values):
        if type(value) is not UnloadCapability:
            raise TypeError("unload evidence requires UnloadCapability")
        actors.add(value.carrier_id)
    return tuple(
        _producer_evidence(
            snapshot,
            actor,
            DecisionCapabilityFamily.UNLOAD,
            CapabilityAvailabilityStatus.AVAILABLE,
            "engine.unload_capabilities",
        )
        for actor in sorted(actors)
    )


def boarding_capability_assessment_evidence(
    snapshot: DecisionSnapshotIdentity,
    values: Iterable[BoardingCapabilityAssessment],
) -> tuple[ExplicitCapabilityEvidence, ...]:
    result: list[ExplicitCapabilityEvidence] = []
    for value in tuple(values):
        if type(value) is not BoardingCapabilityAssessment:
            raise TypeError(
                "boarding assessment evidence requires BoardingCapabilityAssessment"
            )
        family = (
            DecisionCapabilityFamily.BOARD
            if value.operation_type is BoardingTransitionType.BOARD
            else DecisionCapabilityFamily.UNLOAD
        )
        status = {
            BoardingCapabilityStatus.AVAILABLE: (
                CapabilityAvailabilityStatus.AVAILABLE
            ),
            BoardingCapabilityStatus.BLOCKED: CapabilityAvailabilityStatus.BLOCKED,
            BoardingCapabilityStatus.NOT_APPLICABLE: (
                CapabilityAvailabilityStatus.BLOCKED
            ),
        }[value.status]
        result.append(_producer_evidence(
            snapshot,
            value.actor_id,
            family,
            status,
            "engine.boarding_capability_assessments",
            value.blockers,
        ))
    return tuple(sorted(
        result,
        key=lambda item: (item.operator_reference, item.family.value),
    ))


def concealment_capability_evidence(
    snapshot: DecisionSnapshotIdentity,
    values: Iterable[ConcealmentCapability],
) -> tuple[ExplicitCapabilityEvidence, ...]:
    result: list[ExplicitCapabilityEvidence] = []
    for value in tuple(values):
        if type(value) is not ConcealmentCapability:
            raise TypeError(
                "concealment evidence requires ConcealmentCapability"
            )
        if value.available:
            status = CapabilityAvailabilityStatus.AVAILABLE
            blockers: tuple[str, ...] = ()
        elif value.reason in _CONCEALMENT_NOT_APPLICABLE:
            status = CapabilityAvailabilityStatus.NOT_APPLICABLE
            blockers = ()
        else:
            status = CapabilityAvailabilityStatus.BLOCKED
            blockers = (value.reason,)
        result.append(_producer_evidence(
            snapshot,
            value.operator_id,
            DecisionCapabilityFamily.ENTER_CONCEALMENT,
            status,
            "engine.concealment_capabilities",
            blockers,
        ))
    return tuple(sorted(
        result,
        key=lambda item: (item.operator_reference, item.family.value),
    ))


def indirect_capability_evidence(
    snapshot: DecisionSnapshotIdentity,
    values: Iterable[IndirectFireCapability],
) -> tuple[ExplicitCapabilityEvidence, ...]:
    grouped: dict[int, list[IndirectFireCapability]] = {}
    for value in tuple(values):
        if type(value) is not IndirectFireCapability:
            raise TypeError("indirect evidence requires IndirectFireCapability")
        grouped.setdefault(value.shooter_id, []).append(value)
    result = []
    for actor, records in sorted(grouped.items()):
        available = any(
            item.status is IndirectFireCapabilityStatus.AVAILABLE
            for item in records
        )
        blockers = (
            ()
            if available
            else tuple(sorted({
                blocker
                for item in records
                for blocker in item.blockers
            }))
        )
        result.append(_producer_evidence(
            snapshot,
            actor,
            DecisionCapabilityFamily.INDIRECT_FIRE,
            (
                CapabilityAvailabilityStatus.AVAILABLE
                if available
                else CapabilityAvailabilityStatus.BLOCKED
            ),
            "engine.indirect_fire_capabilities",
            blockers,
        ))
    return tuple(result)


def _merge_capability_evidence(
    actor: int,
    family: DecisionCapabilityFamily,
    valid_count: int,
    explicit: ExplicitCapabilityEvidence | None,
) -> CapabilityFamilyStatus:
    if valid_count > 0:
        if explicit is not None and explicit.status is not (
            CapabilityAvailabilityStatus.AVAILABLE
        ):
            raise DecisionProjectionContractError(
                "valid action conflicts with explicit capability evidence: "
                f"operator={actor},family={family.value}"
            )
        return CapabilityFamilyStatus(
            family=family,
            status=CapabilityAvailabilityStatus.AVAILABLE,
            producer_namespace="engine.valid_actions",
            evidence_kind=CapabilityEvidenceKind.ENGINE_VALID_ACTION_INSTANCE,
            available_instance_count=valid_count,
        )
    if explicit is None:
        return CapabilityFamilyStatus(
            family=family,
            status=CapabilityAvailabilityStatus.UNKNOWN,
            producer_namespace="decision.capability_adapter",
            evidence_kind=CapabilityEvidenceKind.NO_CONCLUSIVE_ENGINE_EVIDENCE,
        )
    if explicit.status is CapabilityAvailabilityStatus.AVAILABLE:
        return CapabilityFamilyStatus(
            family=family,
            status=explicit.status,
            producer_namespace=explicit.producer_namespace,
            evidence_kind=CapabilityEvidenceKind.ENGINE_CAPABILITY_RECORD,
            available_instance_count=0,
        )
    if explicit.status is CapabilityAvailabilityStatus.BLOCKED:
        return CapabilityFamilyStatus(
            family=family,
            status=explicit.status,
            producer_namespace=explicit.producer_namespace,
            evidence_kind=CapabilityEvidenceKind.ENGINE_CAPABILITY_RECORD,
            blocker_codes=tuple(
                CapabilityBlockerCode(
                    explicit.producer_namespace,
                    blocker,
                )
                for blocker in explicit.blocker_codes
            ),
        )
    if explicit.status is CapabilityAvailabilityStatus.NOT_APPLICABLE:
        return CapabilityFamilyStatus(
            family=family,
            status=explicit.status,
            producer_namespace=explicit.producer_namespace,
            evidence_kind=(
                CapabilityEvidenceKind.EXPLICIT_APPLICABILITY_AUTHORITY
            ),
        )
    return CapabilityFamilyStatus(
        family=family,
        status=CapabilityAvailabilityStatus.MISSING,
        producer_namespace=explicit.producer_namespace,
        evidence_kind=CapabilityEvidenceKind.REQUIRED_EVIDENCE_MISSING,
    )


def _validate_catalog_capability_consistency(
    catalog: ActionCatalogSnapshot,
    capabilities: DecisionCapabilitySnapshot,
) -> None:
    if type(capabilities) is not DecisionCapabilitySnapshot:
        raise TypeError(
            "capability_snapshot must be an exact DecisionCapabilitySnapshot"
        )
    if (
        capabilities.side is not catalog.side
        or capabilities.decision_window_index != catalog.decision_window_index
        or capabilities.frame_index != catalog.frame_index
        or capabilities.sim_time_ms != catalog.sim_time_ms
    ):
        raise DecisionProjectionContractError(
            "capability/catalog snapshot identity mismatch"
        )
    expected: dict[tuple[int, DecisionCapabilityFamily], int] = {}
    for entry in catalog.entries:
        if entry.actor_reference is None:
            continue
        key = (entry.actor_reference, entry.capability_family)
        expected[key] = expected.get(key, 0) + 1
    actual = {
        (operator.operator_reference, family.family): family
        for operator in capabilities.operators
        for family in operator.families
    }
    for key, count in expected.items():
        status = actual.get(key)
        if (
            status is None
            or status.status is not CapabilityAvailabilityStatus.AVAILABLE
            or status.available_instance_count != count
        ):
            raise DecisionProjectionContractError(
                "catalog/capability available-instance mismatch"
            )


def _producer_evidence(
    snapshot: DecisionSnapshotIdentity,
    actor: int,
    family: DecisionCapabilityFamily,
    status: CapabilityAvailabilityStatus,
    producer: str,
    blockers: tuple[str, ...] = (),
) -> ExplicitCapabilityEvidence:
    return ExplicitCapabilityEvidence(
        side=snapshot.side,
        decision_window_index=snapshot.decision_window_index,
        frame_index=snapshot.frame_index,
        sim_time_ms=snapshot.sim_time_ms,
        operator_reference=actor,
        family=family,
        status=status,
        producer_namespace=producer,
        blocker_codes=tuple(sorted(set(blockers))),
    )


def _action_entry_payload(value: ActionCatalogEntry) -> dict[str, object]:
    return {
        "action_id": value.action_id,
        "actor_reference": value.actor_reference,
        "capability_family": value.capability_family.value,
        "action_type": value.action_type,
        "target_reference": value.target_reference,
        "target_position": (
            None if value.target_position is None else list(value.target_position)
        ),
        "parameters": value.parameters(),
        "priority": value.priority,
        "producer_namespace": value.producer_namespace,
        "operation_hash": value.operation_hash,
    }


def _require_snapshot_match(
    snapshot: DecisionSnapshotIdentity,
    value: object,
) -> None:
    if (
        getattr(value, "side", None) is not snapshot.side
        or getattr(value, "decision_window_index", None)
        != snapshot.decision_window_index
        or getattr(value, "frame_index", None) != snapshot.frame_index
        or getattr(value, "sim_time_ms", None) != snapshot.sim_time_ms
    ):
        raise DecisionProjectionContractError(
            "side/window/frame/time evidence mismatch"
        )


def _validate_valid_action_schema() -> None:
    if {item.name for item in fields(ValidAction)} != _VALID_ACTION_FIELDS:
        raise DecisionProjectionContractError(
            "ValidAction schema changed without catalog version bump"
        )


def _validate_engine_action_schema() -> None:
    if {item.name for item in fields(EngineAction)} != _ENGINE_ACTION_FIELDS:
        raise DecisionProjectionContractError(
            "EngineAction schema changed without catalog version bump"
        )


def _freeze_position_index(values: Mapping[object, list[int]]) -> Mapping:
    return MappingProxyType({key: tuple(items) for key, items in values.items()})


def _freeze_json_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("action parameters must be a mapping")
    return MappingProxyType({
        str(key): _freeze_json_value(item) for key, item in value.items()
    })


def _freeze_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _freeze_json_mapping(value)
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _normalized_query_parameters(
    value: Mapping[str, object],
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("parameters must be a mapping")
    decoded = json.loads(canonical_json(value))
    return _freeze_json_mapping(decoded)


def _mapping_contains(
    supplied: Mapping[str, object], expected: Mapping[str, object],
) -> bool:
    return all(
        key in supplied and supplied[key] == value
        for key, value in expected.items()
    )


def _optional_reference(value: object, name: str) -> None:
    if value is not None:
        _nonnegative_int(value, name)


def _optional_target_position(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, tuple) or len(value) != 2:
        raise TypeError("target_position must be a two-item tuple or None")
    for item in value:
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
        ):
            raise TypeError("target_position must contain finite numbers")


def _legacy_descriptor(
    value: Mapping[str, object],
) -> tuple[
    int | None,
    DecisionCapabilityFamily | None,
    str,
    int | None,
    tuple[float, float] | None,
    Mapping[str, object],
] | None:
    action = value.get("action")
    if not isinstance(action, Mapping):
        return None
    actor = action.get("actor_id")
    actor_reference = actor if type(actor) is int and actor >= 0 else None
    kind_value = action.get("action_type", "UNKNOWN")
    kind = str(kind_value).upper()
    target = action.get("target_id")
    target_reference = target if type(target) is int and target >= 0 else None
    raw_position = action.get("target_position")
    target_position = (
        (float(raw_position[0]), float(raw_position[1]))
        if isinstance(raw_position, (list, tuple))
        and len(raw_position) == 2
        and all(
            isinstance(item, (int, float)) and not isinstance(item, bool)
            for item in raw_position
        )
        else None
    )
    raw_parameters = action.get("parameters")
    parameters = (
        _normalized_query_parameters(raw_parameters)
        if isinstance(raw_parameters, Mapping)
        else MappingProxyType({})
    )
    family = _legacy_family(kind, parameters)
    return (
        actor_reference,
        family,
        kind,
        target_reference,
        target_position,
        parameters,
    )


def _legacy_family(
    action_type: str, parameters: Mapping[str, object],
) -> DecisionCapabilityFamily | None:
    try:
        typed_action = ActionType(action_type)
    except ValueError:
        return None
    if typed_action is ActionType.FIRE:
        mode = parameters.get("fire_mode")
        return _FIRE_MODE_FAMILY.get(str(mode).upper()) if mode is not None else None
    return _ACTION_FAMILY.get(typed_action)


def _decode_record(value: str) -> Mapping[str, object]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):  # pragma: no cover - constructor proved it
        raise RuntimeError("legacy action record unexpectedly changed shape")
    return decoded


def _sorted_references(values: Iterable[int]) -> tuple[int, ...]:
    result = tuple(values)
    for value in result:
        _nonnegative_int(value, "own_operator_reference")
    if len(set(result)) != len(result):
        raise DecisionProjectionContractError("duplicate OWN operator reference")
    return tuple(sorted(result))


def _nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise TypeError(f"{name} must be a non-negative exact int")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise TypeError(f"{name} must be a non-empty normalized string")
    return value


def _token(value: object, name: str) -> str:
    text = _text(value, name)
    if _TOKEN.fullmatch(text) is None:
        raise DecisionProjectionContractError(f"{name} is not a stable token")
    return text


def _sha256(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise DecisionProjectionContractError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return value


__all__ = [
    "ActionCatalogEntry",
    "ActionCatalogIndex",
    "ActionCatalogRecordAdapter",
    "ActionCatalogSnapshot",
    "ExplicitCapabilityEvidence",
    "board_capability_evidence",
    "boarding_capability_assessment_evidence",
    "build_action_catalog_snapshot",
    "build_decision_capability_snapshot",
    "build_decision_from_snapshots",
    "capability_family_for_action",
    "concealment_capability_evidence",
    "guided_capability_evidence",
    "indirect_capability_evidence",
    "unload_capability_evidence",
]
