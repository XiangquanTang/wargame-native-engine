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
    "ProductExecutionResult",
    "ProductOperationService",
    "SideController",
    "StagedAction",
    "UnitCommandMenu",
    "UnitOperationPreview",
    "WargameFacade",
]


def __getattr__(name: str) -> Any:
    if name == "UnitCommandMenu":
        from wargame_native_engine.operations.commands import UnitCommandMenu

        return UnitCommandMenu
    if name in {"SideController", "StagedAction"}:
        from wargame_native_engine.operations.controller import (
            SideController,
            StagedAction,
        )

        return {"SideController": SideController, "StagedAction": StagedAction}[name]
    if name in {"MatchHandle", "WargameFacade"}:
        from wargame_native_engine.operations.facade import MatchHandle, WargameFacade

        return {"MatchHandle": MatchHandle, "WargameFacade": WargameFacade}[name]
    if name in {
        "GroupOperationPreview", "ProductExecutionResult",
        "ProductOperationService", "UnitOperationPreview",
    }:
        from wargame_native_engine.operations.product import (
            GroupOperationPreview,
            ProductExecutionResult,
            ProductOperationService,
            UnitOperationPreview,
        )

        return {
            "GroupOperationPreview": GroupOperationPreview,
            "ProductExecutionResult": ProductExecutionResult,
            "ProductOperationService": ProductOperationService,
            "UnitOperationPreview": UnitOperationPreview,
        }[name]
    raise AttributeError(name)
