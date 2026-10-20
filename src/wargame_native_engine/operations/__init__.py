"""Product operations: side commands, proposals and application Facade.

Gateway uses the proposal registry internally, so the higher-level facade
exports are resolved lazily to keep the dependency direction acyclic.
"""

from typing import Any

from wargame_native_engine.operations.proposals import (
    ActionProposal,
    ActionProposalRegistry,
    OperationError,
)

__all__ = [
    "ActionProposal",
    "ActionProposalRegistry",
    "MatchHandle",
    "OperationError",
    "GroupOperationPreview",
    "ExecutionQueryError",
    "ExecutionStatus",
    "OperationExecutionAvailability",
    "OperationExecutionPage",
    "OperationExecutionRecord",
    "ProductExecutionResult",
    "SeatDecisionExecutionResult",
    "SeatDecisionPreview",
    "ProductOperationService",
    "ScenarioMatchFactory",
    "SideController",
    "StagedAction",
    "UnitCommandMenu",
    "UnitOperationPreview",
    "WargameFacade",
]


def __getattr__(name: str) -> Any:
    if name in {
        "ExecutionQueryError", "ExecutionStatus", "OperationExecutionPage",
        "OperationExecutionAvailability", "OperationExecutionRecord",
    }:
        from wargame_native_engine.operations.execution_lifecycle import (
            ExecutionQueryError,
            ExecutionStatus,
            OperationExecutionAvailability,
            OperationExecutionPage,
            OperationExecutionRecord,
        )

        return {
            "ExecutionQueryError": ExecutionQueryError,
            "ExecutionStatus": ExecutionStatus,
            "OperationExecutionAvailability": OperationExecutionAvailability,
            "OperationExecutionPage": OperationExecutionPage,
            "OperationExecutionRecord": OperationExecutionRecord,
        }[name]
    if name == "UnitCommandMenu":
        from wargame_native_engine.operations.commands import UnitCommandMenu

        return UnitCommandMenu
    if name in {"SideController", "StagedAction"}:
        from wargame_native_engine.operations.controller import (
            SideController,
            StagedAction,
        )

        return {"SideController": SideController, "StagedAction": StagedAction}[name]
    if name in {"MatchHandle", "ScenarioMatchFactory", "WargameFacade"}:
        from wargame_native_engine.operations.facade import (
            MatchHandle,
            ScenarioMatchFactory,
            WargameFacade,
        )

        return {
            "MatchHandle": MatchHandle,
            "ScenarioMatchFactory": ScenarioMatchFactory,
            "WargameFacade": WargameFacade,
        }[name]
    if name in {
        "GroupOperationPreview", "ProductExecutionResult",
        "ProductOperationService", "SeatDecisionExecutionResult",
        "SeatDecisionPreview", "UnitOperationPreview",
    }:
        from wargame_native_engine.operations.product import (
            GroupOperationPreview,
            ProductExecutionResult,
            ProductOperationService,
            SeatDecisionExecutionResult,
            SeatDecisionPreview,
            UnitOperationPreview,
        )

        return {
            "GroupOperationPreview": GroupOperationPreview,
            "ProductExecutionResult": ProductExecutionResult,
            "ProductOperationService": ProductOperationService,
            "SeatDecisionExecutionResult": SeatDecisionExecutionResult,
            "SeatDecisionPreview": SeatDecisionPreview,
            "UnitOperationPreview": UnitOperationPreview,
        }[name]
    raise AttributeError(name)
