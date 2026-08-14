"""Pre-game, side-isolated deployment plans and scenario finalization."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from wargame_native_engine.io.scenario import NativeScenario, scenario_from_dict
from wargame_native_engine.io.serialization import (
    canonical_hash,
    canonical_json,
    json_clone,
)


class SessionLifecycleState(str, Enum):
    CREATED = "Created"
    SEAT_BINDING = "SeatBinding"
    DEPLOYMENT = "Deployment"
    RUNNING = "Running"
    TERMINATED = "Terminated"
    PUBLISHED = "Published"
    CLOSED = "Closed"


class DeploymentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"{self.code}:{self.message}")


@dataclass(frozen=True, slots=True)
class DeploymentPlacement:
    operator_id: int
    position: tuple[float, float]


@dataclass(frozen=True, slots=True)
class DeploymentPlan:
    side: str
    placements: tuple[DeploymentPlacement, ...]


@dataclass(frozen=True, slots=True)
class DeploymentView:
    session_id: str
    side: str
    state: str
    own_plan: DeploymentPlan | None
    own_ready: bool
    opponent_ready: bool


@dataclass(frozen=True, slots=True)
class FinalizedDeployment:
    session_id: str
    source_scenario_hash: str
    deployment_hash: str
    final_scenario_hash: str
    scenario_bytes: bytes
    scenario: NativeScenario


class DeploymentSession:
    """Build one canonical scenario before any GameSession exists.

    The object has no WorldState or Engine reference.  It validates complete
    side plans against explicit scenario allow-lists, then emits one immutable
    NativeScenario suitable for constructing the normal Session chain.
    """

    def __init__(
        self,
        scenario: NativeScenario,
        *,
        session_id: str,
        red_controller_id: str,
        blue_controller_id: str,
    ) -> None:
        self.session_id = _nonempty(session_id, "session_id")
        red = _nonempty(red_controller_id, "red_controller_id")
        blue = _nonempty(blue_controller_id, "blue_controller_id")
        if red == blue:
            raise DeploymentError(
                "controller_binding_conflict", "RED and BLUE identities differ"
            )
        self._source = scenario_from_dict(scenario.to_dict())
        self.source_scenario_hash = self._source.scenario_hash
        self._bindings = MappingProxyType({"red": red, "blue": blue})
        self._plans: dict[str, DeploymentPlan] = {}
        self._finalized: FinalizedDeployment | None = None
        self.state = SessionLifecycleState.DEPLOYMENT

    def submit(
        self,
        controller_id: str,
        plan: DeploymentPlan,
    ) -> DeploymentView:
        if self.state is not SessionLifecycleState.DEPLOYMENT:
            raise DeploymentError(
                "deployment_finalized", "deployment can no longer be changed"
            )
        side = self._side(controller_id)
        if not isinstance(plan, DeploymentPlan):
            raise TypeError("plan must be DeploymentPlan")
        if plan.side != side:
            raise DeploymentError(
                "deployment_side_mismatch",
                f"{side.upper()} controller cannot submit {plan.side!r} plan",
            )
        if side in self._plans:
            raise DeploymentError(
                "deployment_already_submitted", f"{side.upper()} plan is frozen"
            )
        normalized = _validate_plan(self._source, plan)
        candidates = {**self._plans, side: normalized}
        finalized = None
        if set(candidates) == set(self._source.side_order):
            finalized = _finalize(
                self.session_id, self._source, candidates,
            )
        self._plans[side] = normalized
        if finalized is not None:
            self._finalized = finalized
            self.state = SessionLifecycleState.RUNNING
        return self.view(controller_id)

    def view(self, controller_id: str) -> DeploymentView:
        side = self._side(controller_id)
        opponent = "blue" if side == "red" else "red"
        own = self._plans.get(side)
        return DeploymentView(
            session_id=self.session_id,
            side=side,
            state=self.state.value,
            own_plan=None if own is None else _copy_plan(own),
            own_ready=own is not None,
            opponent_ready=opponent in self._plans,
        )

    @property
    def finalized(self) -> FinalizedDeployment:
        if self._finalized is None:
            raise DeploymentError(
                "deployment_incomplete", "both side plans are required"
            )
        value = self._finalized
        return FinalizedDeployment(
            value.session_id,
            value.source_scenario_hash,
            value.deployment_hash,
            value.final_scenario_hash,
            bytes(value.scenario_bytes),
            scenario_from_dict(value.scenario.to_dict()),
        )

    def _side(self, controller_id: str) -> str:
        matches = tuple(
            side for side, value in self._bindings.items()
            if value == controller_id
        )
        if len(matches) != 1:
            raise DeploymentError(
                "controller_not_bound",
                f"controller {controller_id!r} is not bound to this deployment",
            )
        return matches[0]


def _validate_plan(
    scenario: NativeScenario,
    plan: DeploymentPlan,
) -> DeploymentPlan:
    expected = tuple(sorted(
        item.operator_id for item in scenario.operators if item.side == plan.side
    ))
    ids = [placement.operator_id for placement in plan.placements]
    if len(ids) != len(set(ids)):
        raise DeploymentError(
            "duplicate_deployment_unit", "an operator occurs more than once"
        )
    if tuple(sorted(ids)) != expected:
        raise DeploymentError(
            "deployment_unit_set_mismatch",
            f"{plan.side.upper()} plan must contain exactly {expected!r}",
        )
    allowed = _allowed_positions(scenario, plan.side)
    normalized: list[DeploymentPlacement] = []
    for placement in plan.placements:
        position = _position(placement.position)
        if position not in allowed[placement.operator_id]:
            raise DeploymentError(
                "deployment_position_not_allowed",
                f"operator {placement.operator_id} position is not explicit",
            )
        if not (
            0 <= position[0] <= scenario.map.width
            and 0 <= position[1] <= scenario.map.height
        ):
            raise DeploymentError(
                "deployment_outside_map",
                f"operator {placement.operator_id} is outside the map",
            )
        if position in scenario.map.blocked_positions:
            raise DeploymentError(
                "deployment_terrain_blocked",
                f"operator {placement.operator_id} is on blocked terrain",
            )
        normalized.append(DeploymentPlacement(placement.operator_id, position))
    return DeploymentPlan(
        plan.side,
        tuple(sorted(normalized, key=lambda item: item.operator_id)),
    )


def _allowed_positions(
    scenario: NativeScenario,
    side: str,
) -> Mapping[int, frozenset[tuple[float, float]]]:
    result = {
        operator_id: {tuple(position)}
        for operator_id, position in scenario.deployment_positions[side].items()
    }
    native = scenario.metadata.get("native_deployment")
    configured = (
        native.get("allowed_positions")
        if isinstance(native, Mapping)
        else None
    )
    side_values = configured.get(side) if isinstance(configured, Mapping) else None
    if isinstance(side_values, Mapping):
        for raw_id, positions in side_values.items():
            operator_id = int(raw_id)
            if operator_id not in result:
                raise DeploymentError(
                    "deployment_allowlist_unknown_unit",
                    f"allow-list references operator {operator_id}",
                )
            if not isinstance(positions, Sequence) or isinstance(positions, (str, bytes)):
                raise DeploymentError(
                    "invalid_deployment_allowlist", "allowed_positions must be arrays"
                )
            result[operator_id].update(_position(value) for value in positions)
    return MappingProxyType({
        key: frozenset(values) for key, values in result.items()
    })


def _finalize(
    session_id: str,
    source: NativeScenario,
    plans: Mapping[str, DeploymentPlan],
) -> FinalizedDeployment:
    plan_record = {
        side: {
            str(item.operator_id): list(item.position)
            for item in plan.placements
        }
        for side, plan in sorted(plans.items())
    }
    deployment_hash = canonical_hash(plan_record)
    document = source.to_dict()
    document["deployment"]["positions"] = json_clone(plan_record)
    selected = {
        item.operator_id: item.position
        for plan in plans.values() for item in plan.placements
    }
    if len(set(selected.values())) != len(selected):
        raise DeploymentError(
            "deployment_position_conflict",
            "multiple operators cannot occupy one deployment position",
        )
    for operator in document["operators"]:
        operator_id = int(operator["operator_id"])
        position = selected[operator_id]
        original = source.deployment_positions[str(operator["side"])][operator_id]
        if operator.get("current_hex") is not None and position != original:
            raise DeploymentError(
                "deployment_hex_identity_unavailable",
                "hex deployment changes require an explicit position-to-hex contract",
            )
        operator["x"], operator["y"] = position
        operator["deployed"] = True
    document["initial_stage"] = "combat"
    metadata = json_clone(document.get("metadata", {}))
    metadata["deployment_provenance"] = {
        "source_scenario_hash": source.scenario_hash,
        "deployment_hash": deployment_hash,
        "builder_version": "native-pre-game-deployment/1.0",
    }
    document["metadata"] = metadata
    scenario = scenario_from_dict(document)
    payload = (canonical_json(scenario.to_dict()) + "\n").encode("utf-8")
    return FinalizedDeployment(
        session_id=session_id,
        source_scenario_hash=source.scenario_hash,
        deployment_hash=deployment_hash,
        final_scenario_hash=scenario.scenario_hash,
        scenario_bytes=payload,
        scenario=scenario,
    )


def _copy_plan(plan: DeploymentPlan) -> DeploymentPlan:
    return DeploymentPlan(
        plan.side,
        tuple(DeploymentPlacement(item.operator_id, tuple(item.position))
              for item in plan.placements),
    )


def _position(value: object) -> tuple[float, float]:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise DeploymentError("invalid_deployment_position", "position must be XY")
    values = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise DeploymentError(
                "invalid_deployment_position", "coordinates must be numeric"
            )
        values.append(float(item))
    return (values[0], values[1])


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeploymentError("invalid_identity", f"{field} must be non-empty")
    return value
