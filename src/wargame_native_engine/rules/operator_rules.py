from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OperatorRules:
    profiles: tuple[tuple[str, tuple[tuple[str, float], ...]], ...]

    def profile(self, operator_type: str) -> dict[str, float]:
        return dict(dict(self.profiles).get(operator_type, ()))
