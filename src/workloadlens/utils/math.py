from __future__ import annotations

from collections import Counter
from typing import Dict


def counter_shares(counter: Counter) -> Dict[str, float]:
    """
    Convert a Counter of counts into a dict of shares (fractions summing to 1).
    Returns an empty dict if the counter is empty or sums to zero.
    """
    if not counter:
        return {}
    total = sum(counter.values())
    if not total:
        return {}
    return {key: value / total for key, value in counter.items()}
