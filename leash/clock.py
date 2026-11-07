"""A clock you can move by hand, so tests and demos can fast-forward time."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional


class FakeClock:
    def __init__(self, start: Optional[datetime] = None, tick: float = 0.0):
        self.now = start or datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
        self._tick = timedelta(seconds=tick)  # moves forward by itself after every look (0 = frozen)

    def __call__(self) -> datetime:
        current = self.now
        self.now += self._tick
        return current

    def advance(self, **kwargs: float) -> datetime:
        self.now += timedelta(**kwargs)
        return self.now
