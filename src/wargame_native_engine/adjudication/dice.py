from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from wargame_native_engine.core.rng import NativeRNG


@dataclass(frozen=True, slots=True)
class DiceRecord:
    episode_seed: int
    tick: int
    action_id: str
    roll_index: int
    individual_dice: tuple[int, ...]
    raw_total: int

    def to_dict(self) -> dict[str, object]:
        return {"episode_seed": self.episode_seed, "tick": self.tick,
                "action_id": self.action_id, "roll_index": self.roll_index,
                "individual_dice": list(self.individual_dice),
                "raw_total": self.raw_total}


class DiceSource(Protocol):
    def roll_d6(self, *, tick: int, action_id: str) -> DiceRecord: ...
    def roll_2d6(self, *, tick: int, action_id: str) -> DiceRecord: ...


class DiceRoller:
    def __init__(self, rng: NativeRNG) -> None:
        self._rng = rng
        self._roll_index = 0

    @property
    def episode_seed(self) -> int:
        return self._rng.seed

    def roll_d6(self, *, tick: int, action_id: str) -> DiceRecord:
        return self._roll(1, tick=tick, action_id=action_id)

    def roll_2d6(self, *, tick: int, action_id: str) -> DiceRecord:
        return self._roll(2, tick=tick, action_id=action_id)

    def _roll(self, count: int, *, tick: int, action_id: str) -> DiceRecord:
        values = tuple(self._rng.randbelow(6) + 1 for _ in range(count))
        result = DiceRecord(self._rng.seed, tick, action_id,
                            self._roll_index, values, sum(values))
        self._roll_index += 1
        return result


class FixedDiceRoller:
    def __init__(self, values: Sequence[int], *, episode_seed: int = 0) -> None:
        if any(int(value) not in range(1, 7) for value in values):
            raise ValueError("fixed dice values must be within 1..6")
        self._values = tuple(int(value) for value in values)
        self._cursor = 0
        self._roll_index = 0
        self.episode_seed = int(episode_seed)

    def roll_d6(self, *, tick: int, action_id: str) -> DiceRecord:
        return self._roll(1, tick=tick, action_id=action_id)

    def roll_2d6(self, *, tick: int, action_id: str) -> DiceRecord:
        return self._roll(2, tick=tick, action_id=action_id)

    def _roll(self, count: int, *, tick: int, action_id: str) -> DiceRecord:
        end = self._cursor + count
        if end > len(self._values):
            raise RuntimeError("fixed dice sequence exhausted")
        values = self._values[self._cursor:end]
        self._cursor = end
        result = DiceRecord(self.episode_seed, tick, action_id,
                            self._roll_index, values, sum(values))
        self._roll_index += 1
        return result
