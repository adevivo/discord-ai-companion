from collections import Counter
from itertools import product

import pytest

from companion.game.dice import Roll, roll_2d6


@pytest.mark.parametrize(
    "dice,outcome,text",
    [
        ((6, 6), "success", "success (12)"),
        ((4, 6), "success", "success (10)"),
        ((3, 6), "partial", "partial success (9)"),
        ((1, 6), "partial", "partial success (7)"),
        ((3, 3), "failure", "failure (6)"),
        ((1, 1), "failure", "failure (2)"),
    ],
)
def test_outcome_tiers(dice, outcome, text):
    roll = Roll(dice)
    assert roll.outcome == outcome
    assert roll.describe() == text


def test_tier_probabilities_match_2d6():
    counts = Counter(Roll(d).outcome for d in product(range(1, 7), repeat=2))
    assert counts == {"success": 6, "partial": 15, "failure": 15}


def test_roll_uses_injected_randint():
    values = iter([2, 5])
    roll = roll_2d6(lambda a, b: next(values))
    assert roll.dice == (2, 5)


def test_real_rolls_are_in_range():
    for _ in range(200):
        assert all(1 <= d <= 6 for d in roll_2d6().dice)
