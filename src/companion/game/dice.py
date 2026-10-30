"""2d6 rolls with three outcome tiers: 10+ success, 7-9 partial, 6- failure."""

import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

Outcome = Literal["success", "partial", "failure"]

_SYSTEM_RANDOM = random.SystemRandom()

_LABELS: dict[Outcome, str] = {
    "success": "success",
    "partial": "partial success",
    "failure": "failure",
}


@dataclass(frozen=True)
class Roll:
    dice: tuple[int, int]

    @property
    def total(self) -> int:
        return sum(self.dice)

    @property
    def outcome(self) -> Outcome:
        if self.total >= 10:
            return "success"
        if self.total >= 7:
            return "partial"
        return "failure"

    def describe(self) -> str:
        return f"{_LABELS[self.outcome]} ({self.total})"


def roll_2d6(randint: Callable[[int, int], int] = _SYSTEM_RANDOM.randint) -> Roll:
    return Roll((randint(1, 6), randint(1, 6)))
