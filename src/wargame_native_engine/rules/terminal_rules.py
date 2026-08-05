from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TerminalRules:
    max_ticks: int
    elimination: bool
    objective_score_to_win: int | None
    mission_score_enabled: bool = False
    mutual_elimination_result: str = "draw"
    tie_result: str = "draw"
    evaluation_order: tuple[str, ...] = (
        "elimination", "objective", "max_ticks")

    def __post_init__(self) -> None:
        if self.max_ticks < 1:
            raise ValueError("max_ticks must be positive")
        if self.objective_score_to_win is not None and self.objective_score_to_win < 1:
            raise ValueError("objective_score_to_win must be positive")
        if self.mutual_elimination_result != "draw":
            raise ValueError("only transparent draw mutual elimination is supported")
        if self.evaluation_order != ("elimination", "objective", "max_ticks"):
            raise ValueError("unsupported terminal evaluation order")
