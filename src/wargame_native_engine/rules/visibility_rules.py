from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VisibilityRules:
    inclusive_boundary: bool = True
