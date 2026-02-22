from __future__ import annotations

import random


def hit(probability: float) -> bool:
    clipped = max(0.0, min(1.0, float(probability)))
    return random.random() < clipped

