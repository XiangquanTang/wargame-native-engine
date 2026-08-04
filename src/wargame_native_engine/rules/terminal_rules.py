from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TerminalRules:
    max_ticks: int = 500
    elimination: bool = True
    objective_score_to_win: int | None = None

    def __post_init__(self) -> None:
        if self.max_ticks < 1:
            raise ValueError("max_ticks must be positive")
        if self.objective_score_to_win is not None and self.objective_score_to_win < 1:
            raise ValueError("objective_score_to_win must be positive")
