from __future__ import annotations

from dataclasses import dataclass

from wargame_native_engine.core.world import WorldState
from wargame_native_engine.rules.terminal_rules import TerminalRules


@dataclass(frozen=True, slots=True)
class TerminalDecision:
    terminal: bool
    truncated: bool
    winner: str | None
    reason: str | None


def evaluate_terminal(world: WorldState, rules: TerminalRules,
                      side_order: tuple[str, ...]) -> TerminalDecision:
    if rules.elimination:
        living = {side: any(item.alive for item in world.operators.values()
                            if item.side == side) for side in side_order}
        alive_sides = [side for side in side_order if living[side]]
        if not alive_sides:
            return TerminalDecision(True, False, None, "mutual_elimination")
        if len(alive_sides) == 1:
            return TerminalDecision(True, False, alive_sides[0], "side_eliminated")
    if rules.objective_score_to_win is not None:
        winners = [side for side in side_order
                   if world.sides[side].objective_score >= rules.objective_score_to_win]
        if winners:
            return TerminalDecision(True, False,
                                    winners[0] if len(winners) == 1 else None,
                                    "objective_score" if len(winners) == 1
                                    else "objective_score_draw")
    if world.tick >= rules.max_ticks:
        return TerminalDecision(False, True, None, "max_ticks")
    return TerminalDecision(False, False, None, None)
