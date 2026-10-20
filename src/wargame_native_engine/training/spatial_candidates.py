"""Bounded, deterministic spatial candidates for single-seat Decision-v2.

The provider is an orchestration component, not an adjudicator.  It consumes
only a detached side projection, side-owned capability records, and an
immutable public static-map record.  Every candidate is still created by the
existing Controller/Gateway/Engine planners.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Callable, Mapping, Sequence

from wargame_native_engine.core.fire_capability import (
    IndirectFireCapability,
    IndirectFireCapabilityStatus,
    indirect_fire_capability_sort_key,
)
from wargame_native_engine.gateway.models import (
    CommandGatewayError,
    ControllerView,
)
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.operations.proposals import ActionProposal
from wargame_native_engine.projection.decision_semantic import (
    DecisionCapabilityFamily,
)


SPATIAL_CANDIDATE_PROVIDER_SCHEMA_VERSION = (
    "native-spatial-candidate-provider-config/1.0"
)
SPATIAL_CANDIDATE_PROVIDER_ID = "native-bounded-static-map-spatial-candidates"
SPATIAL_CANDIDATE_PROVIDER_VERSION = "1.0.0"


class SpatialCandidateProviderError(RuntimeError):
    """Stable fail-closed error at the candidate preparation boundary."""


@dataclass(frozen=True, slots=True)
class SpatialCandidateProviderConfig:
    schema_version: str
    provider_id: str
    provider_version: str
    cell_set_rule: str
    cell_sort_order: str
    semantic_deduplication: str
    movement_modes: tuple[str, ...]
    max_map_cells: int
    max_planning_requests: int
    final_action_capacity: int
    content_hash: str
    shortlist_quotas: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SPATIAL_CANDIDATE_PROVIDER_SCHEMA_VERSION:
            raise SpatialCandidateProviderError("unsupported provider config schema")
        if self.provider_id != SPATIAL_CANDIDATE_PROVIDER_ID or self.provider_version not in {"1.0.0", "2.0.0"}:
            raise SpatialCandidateProviderError("unsupported provider identity")
        expected_rule = ("ALL_DECLARED_NATIVE_HEXES" if self.provider_version == "1.0.0"
                         else "PUBLIC_LOCAL_OBJECTIVE_VISIBLE_GRID_SHORTLIST")
        if self.cell_set_rule != expected_rule:
            raise SpatialCandidateProviderError("unsupported provider cell set rule")
        if self.cell_sort_order != "HEX_ID_ASC":
            raise SpatialCandidateProviderError("unsupported provider cell ordering")
        if self.semantic_deduplication != "ENGINE_ACTION_ID":
            raise SpatialCandidateProviderError("unsupported provider deduplication")
        if self.movement_modes != ("NORMAL",):
            raise SpatialCandidateProviderError("unsupported provider movement modes")
        for name in (
            "max_map_cells", "max_planning_requests", "final_action_capacity",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise SpatialCandidateProviderError(f"{name} must be positive")
        if self.provider_version == "2.0.0":
            if len(self.shortlist_quotas) != 4 or any(type(q) is not int or q <= 0 for q in self.shortlist_quotas):
                raise SpatialCandidateProviderError("four positive shortlist quotas required")
            if self.max_planning_requests > 128 or self.final_action_capacity > 256:
                raise SpatialCandidateProviderError("medium provider exceeds fixed request/action budget")
        elif self.shortlist_quotas:
            raise SpatialCandidateProviderError("v1 does not accept shortlist quotas")
        if self.content_hash != canonical_hash(self.semantic_payload()):
            raise SpatialCandidateProviderError("provider config hash mismatch")

    def semantic_payload(self) -> dict[str, object]:
        result = {
            "schema_version": self.schema_version,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "cell_set_rule": self.cell_set_rule,
            "cell_sort_order": self.cell_sort_order,
            "semantic_deduplication": self.semantic_deduplication,
            "movement_modes": list(self.movement_modes),
            "max_map_cells": self.max_map_cells,
            "max_planning_requests": self.max_planning_requests,
            "final_action_capacity": self.final_action_capacity,
        }

        if self.provider_version == "2.0.0":
            result["shortlist_quotas"] = list(self.shortlist_quotas)
        return result

    def to_dict(self) -> dict[str, object]:
        value = self.semantic_payload()
        value["config_hash"] = self.content_hash
        return value

    def identity_record(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "config_hash": self.content_hash,
            "cell_set_rule": self.cell_set_rule,
            "cell_sort_order": self.cell_sort_order,
            "semantic_deduplication": self.semantic_deduplication,
            "max_map_cells": self.max_map_cells,
            "max_planning_requests": self.max_planning_requests,
            "final_action_capacity": self.final_action_capacity,
        }


@dataclass(frozen=True, slots=True)
class SpatialCandidatePreparationReport:
    session_id: str
    side: str
    decision_window_index: int
    provider_config_hash: str
    public_map_hash: str
    visible_input_hash: str
    map_cell_count: int
    base_catalog_count: int
    planned_request_budget: int
    attempted_count: int
    accepted_count: int
    rejected_count: int
    deduplicated_count: int
    move_attempted_count: int
    move_accepted_count: int
    indirect_attempted_count: int
    indirect_accepted_count: int
    final_catalog_count: int
    planning_seconds: float
    selected_cells: tuple[tuple[str, tuple[int, ...]], ...] = ()
    excluded_spatial_pairs: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }


def build_spatial_candidate_provider_config(
    value: Mapping[str, object],
) -> SpatialCandidateProviderConfig:
    if not isinstance(value, Mapping):
        raise TypeError("provider config must be a mapping")
    expected = {
        "schema_version", "provider_id", "provider_version", "cell_set_rule",
        "cell_sort_order", "semantic_deduplication", "movement_modes",
        "max_map_cells", "max_planning_requests", "final_action_capacity",
    }
    source = dict(value)
    claimed_hash = source.pop("config_hash", None)
    if source.get("provider_version") == "2.0.0":
        expected.add("shortlist_quotas")
    if set(source) != expected:
        raise SpatialCandidateProviderError(
            "provider config fields are missing or unknown"
        )
    modes = source["movement_modes"]
    if isinstance(modes, (str, bytes)) or not isinstance(modes, Sequence):
        raise SpatialCandidateProviderError("movement_modes must be a sequence")
    temporary = object.__new__(SpatialCandidateProviderConfig)
    fields = {
        "schema_version": str(source["schema_version"]),
        "provider_id": str(source["provider_id"]),
        "provider_version": str(source["provider_version"]),
        "cell_set_rule": str(source["cell_set_rule"]),
        "cell_sort_order": str(source["cell_sort_order"]),
        "semantic_deduplication": str(source["semantic_deduplication"]),
        "movement_modes": tuple(str(item) for item in modes),
        "max_map_cells": source["max_map_cells"],
        "max_planning_requests": source["max_planning_requests"],
        "final_action_capacity": source["final_action_capacity"],
        "shortlist_quotas": tuple(source.get("shortlist_quotas", ())),
    }
    for name, item in fields.items():
        object.__setattr__(temporary, name, item)
    computed = canonical_hash(temporary.semantic_payload())
    if claimed_hash is not None and claimed_hash != computed:
        raise SpatialCandidateProviderError("provider config hash mismatch")
    return SpatialCandidateProviderConfig(**fields, content_hash=computed)


def load_spatial_candidate_provider_config(
    path: str | Path,
) -> SpatialCandidateProviderConfig:
    source = Path(path)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpatialCandidateProviderError(
            f"provider config cannot be loaded: {source}"
        ) from exc
    return build_spatial_candidate_provider_config(value)


class BoundedSpatialCandidateProvider:
    """Generate the complete configured small-map candidate set once."""

    def __init__(self, config: SpatialCandidateProviderConfig) -> None:
        if not isinstance(config, SpatialCandidateProviderConfig):
            raise TypeError("config must be SpatialCandidateProviderConfig")
        self.config = config

    def prepare(
        self,
        *,
        view: ControllerView,
        public_map: Mapping[str, object],
        indirect_capabilities: Sequence[IndirectFireCapability],
        action_capacity: int,
        plan_move: Callable[..., ActionProposal],
        plan_indirect_fire: Callable[..., ActionProposal],
    ) -> SpatialCandidatePreparationReport:
        started = time.perf_counter()
        if view.terminal or view.terminated or view.truncated:
            raise SpatialCandidateProviderError(
                "candidate preparation requires an active decision window"
            )
        catalog = view.action_catalog
        decision = view.decision_semantic
        if catalog is None or decision is None:
            raise SpatialCandidateProviderError("Decision projection is required")
        if type(action_capacity) is not int or action_capacity <= 0:
            raise SpatialCandidateProviderError("action capacity must be positive")
        if action_capacity != self.config.final_action_capacity:
            raise SpatialCandidateProviderError(
                "provider final action capacity does not match environment"
            )
        cells = _public_cells(public_map)
        if len(cells) > self.config.max_map_cells:
            raise SpatialCandidateProviderError(
                "public map cell capacity exceeded: "
                f"{len(cells)} > {self.config.max_map_cells}"
            )
        move_actors = tuple(sorted({
            entry.actor_reference
            for entry in catalog.entries
            if entry.capability_family is DecisionCapabilityFamily.MOVE
            and entry.actor_reference is not None
        }))
        if any(
            type(item) is not IndirectFireCapability
            for item in indirect_capabilities
        ):
            raise TypeError("indirect capabilities must be typed records")
        indirect = tuple(sorted(
            (
                item for item in indirect_capabilities
                if item.status is IndirectFireCapabilityStatus.AVAILABLE
            ),
            key=indirect_fire_capability_sort_key,
        ))
        move_cells = {actor: cells for actor in move_actors}
        indirect_cells = {item.shooter_id: cells for item in indirect}
        if self.config.provider_version == "2.0.0":
            move_cells = {actor: _shortlist(cells, decision, actor, self.config.shortlist_quotas)
                          for actor in move_actors}
            indirect_cells = {item.shooter_id: _shortlist(cells, decision, item.shooter_id, self.config.shortlist_quotas)
                              for item in indirect}
        request_budget = (sum(map(len, move_cells.values())) * len(self.config.movement_modes)
                          + sum(len(indirect_cells[item.shooter_id]) for item in indirect))
        if request_budget > self.config.max_planning_requests:
            raise SpatialCandidateProviderError(
                "planning request capacity exceeded: "
                f"{request_budget} > {self.config.max_planning_requests}"
            )
        base_count = catalog.action_count
        if base_count + request_budget > action_capacity:
            raise SpatialCandidateProviderError(
                "worst-case final catalog capacity exceeded before planning: "
                f"{base_count} + {request_budget} > {action_capacity}"
            )

        existing = {entry.action_id for entry in catalog.entries}
        accepted: set[str] = set()
        attempted = rejected = deduplicated = 0
        move_attempted = move_accepted = 0
        indirect_attempted = indirect_accepted = 0
        request_prefix = (
            f"spatial:{self.config.content_hash[:16]}:{view.session_id}:"
            f"{view.perspective.lower()}:{view.step_index}"
        )
        for actor_id in move_actors:
            for mode in self.config.movement_modes:
                for target_hex in move_cells[actor_id]:
                    attempted += 1
                    move_attempted += 1
                    proposal = plan_move(
                        actor_id,
                        target_hex,
                        request_id=(
                            f"{request_prefix}:move:{actor_id}:{mode}:{target_hex}"
                        ),
                        movement_mode=mode,
                    )
                    if not proposal.reachable or proposal.action_id is None:
                        rejected += 1
                        continue
                    if proposal.action_id in existing or proposal.action_id in accepted:
                        deduplicated += 1
                        continue
                    accepted.add(proposal.action_id)
                    move_accepted += 1
        for capability in indirect:
            for target_hex in indirect_cells[capability.shooter_id]:
                attempted += 1
                indirect_attempted += 1
                try:
                    proposal = plan_indirect_fire(
                        capability.shooter_id,
                        capability.weapon_id,
                        target_hex,
                        request_id=(
                            f"{request_prefix}:indirect:{capability.shooter_id}:"
                            f"{capability.weapon_id}:{target_hex}"
                        ),
                    )
                except CommandGatewayError as exc:
                    if exc.code != "indirect_fire_plan_rejected":
                        raise
                    rejected += 1
                    continue
                if proposal.action_id is None:
                    raise SpatialCandidateProviderError(
                        "accepted indirect proposal lacks an action identity"
                    )
                if proposal.action_id in existing or proposal.action_id in accepted:
                    deduplicated += 1
                    continue
                accepted.add(proposal.action_id)
                indirect_accepted += 1
        accepted_count = len(accepted)
        final_count = base_count + accepted_count
        if final_count > action_capacity:
            raise SpatialCandidateProviderError(
                "final catalog capacity exceeded after planning"
            )
        visible_input_hash = canonical_hash({
            "decision": decision.semantic_hash,
            "catalog": catalog.catalog_hash,
            "inventory": list(view.own_weapon_inventory),
            "public_map": dict(public_map),
            "provider": self.config.content_hash,
        })
        return SpatialCandidatePreparationReport(
            session_id=view.session_id,
            side=view.perspective.lower(),
            decision_window_index=view.step_index,
            provider_config_hash=self.config.content_hash,
            public_map_hash=str(public_map["map_hash"]),
            visible_input_hash=visible_input_hash,
            map_cell_count=len(cells),
            base_catalog_count=base_count,
            planned_request_budget=request_budget,
            attempted_count=attempted,
            accepted_count=accepted_count,
            rejected_count=rejected,
            deduplicated_count=deduplicated,
            move_attempted_count=move_attempted,
            move_accepted_count=move_accepted,
            indirect_attempted_count=indirect_attempted,
            indirect_accepted_count=indirect_accepted,
            final_catalog_count=final_count,
            planning_seconds=time.perf_counter() - started,
            selected_cells=tuple((f"move:{actor}", targets) for actor, targets in move_cells.items())
                + tuple((f"indirect:{item.shooter_id}:{item.weapon_id}", indirect_cells[item.shooter_id]) for item in indirect),
            excluded_spatial_pairs=len(cells) * (len(move_actors) * len(self.config.movement_modes) + len(indirect)) - request_budget,
        )


def _shortlist(cells, decision, actor_id, quotas):
    """v2: independent category quotas, union, then hex-ID sort.

    Native hex coordinates are row*100+column. Local ranking uses squared
    coordinate distance (not a path or a reachability claim); the public
    planner alone decides reachability. No category borrows another's quota.
    """
    own = next(item for item in decision.own_operators if item.operator_reference == actor_id)
    origin = own.deployment.current_hex
    if origin not in cells:
        raise SpatialCandidateProviderError("shortlist actor lacks public current hex")
    def distance(cell, x, y):
        return (cell % 100 - x) ** 2 + (cell // 100 - y) ** 2
    local = sorted((c for c in cells if c != origin), key=lambda c: (distance(c, origin % 100, origin // 100), c))[:quotas[0]]
    objectives = decision.side_context.objectives
    goals = sorted(cells, key=lambda c: (min((distance(c, o.x, o.y) for o in objectives), default=float("inf")), c))[:quotas[1]] if objectives else []
    visible = sorted({int(e.observation.position[1]) * 100 + int(e.observation.position[0])
                      for e in decision.observed_enemies} & set(cells))[:quotas[2]]
    grid = [cells[(i + 1) * (len(cells) - 1) // (quotas[3] + 1)] for i in range(quotas[3])]
    return tuple(sorted(set(local + goals + visible + grid)))


def _public_cells(public_map: Mapping[str, object]) -> tuple[int, ...]:
    if public_map.get("cell_set_rule") != "ALL_DECLARED_NATIVE_HEXES":
        raise SpatialCandidateProviderError("public map cell rule mismatch")
    raw = public_map.get("valid_hex_ids")
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise SpatialCandidateProviderError("public map lacks declared hex IDs")
    cells = tuple(raw)
    if not cells or any(type(item) is not int or item < 0 for item in cells):
        raise SpatialCandidateProviderError("public map hex IDs are invalid")
    if cells != tuple(sorted(set(cells))):
        raise SpatialCandidateProviderError("public map hex IDs are not sorted unique")
    if not isinstance(public_map.get("map_hash"), str):
        raise SpatialCandidateProviderError("public map identity is missing")
    return cells


__all__ = [
    "BoundedSpatialCandidateProvider",
    "SPATIAL_CANDIDATE_PROVIDER_ID",
    "SPATIAL_CANDIDATE_PROVIDER_SCHEMA_VERSION",
    "SPATIAL_CANDIDATE_PROVIDER_VERSION",
    "SpatialCandidatePreparationReport",
    "SpatialCandidateProviderConfig",
    "SpatialCandidateProviderError",
    "build_spatial_candidate_provider_config",
    "load_spatial_candidate_provider_config",
]
