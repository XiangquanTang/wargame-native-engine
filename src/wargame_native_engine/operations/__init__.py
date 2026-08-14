"""Product operation contracts over the existing Gateway and Engine."""

from wargame_native_engine.operations.proposals import (
    ActionProposal,
    ActionProposalRegistry,
    OperationError,
)

__all__ = [
    "ActionProposal",
    "ActionProposalRegistry",
    "OperationError",
]
