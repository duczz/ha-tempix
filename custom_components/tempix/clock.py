"""Explicit time model for the V2 engine.

The engine never calls ``datetime.now()`` itself — time is a dependency.
The SnapshotBuilder freezes the evaluation timestamp into the
``TempixSnapshot``; policies read only that. ``FixedClock`` is the test
implementation that makes every time-dependent path deterministic.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class EngineClock(Protocol):
    """Time source for SnapshotBuilder / engine components."""

    def now(self) -> datetime:
        """Return the current time as an aware UTC datetime."""
        ...


class SystemClock:
    """Production clock — wall time in UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """Deterministic test clock. Only advances explicitly."""

    def __init__(self, fixed: datetime) -> None:
        if fixed.tzinfo is None:
            raise ValueError("FixedClock requires an aware (UTC) datetime")
        self._now = fixed

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("FixedClock requires an aware (UTC) datetime")
        self._now = moment
