from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TerrainRules:
    profiles: tuple[tuple[str, tuple[tuple[str, object], ...]], ...]

    def profile(self, terrain_type: str) -> dict[str, object]:
        return dict(dict(self.profiles).get(terrain_type, ()))
