"""Health enums — own module so per-sensor/per-device models
can use them without importing the full snapshot (avoids import cycles).
``models/snapshot.py`` re-exports both names (canonical location).
"""
from __future__ import annotations

from enum import Enum


class SnapshotValidity(Enum):
    """Aggregated trust level of a snapshot."""

    VALID = "valid"
    # A fallback was applied (e.g. room temp from the TRV's own sensor) —
    # usable, but must not legitimize systematic misheating forever.
    DEGRADED = "degraded"
    # Hard hardware safeguards apply; the engine must not trust this.
    UNSAFE = "unsafe"


class HealthStatus(Enum):
    """Per-sensor health as classified by the SnapshotBuilder."""

    AVAILABLE = "available"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    # Entity was never snapshotted — accessing it is a bug (the V2
    # anti-live-fallback rule surfaces such reads as UNKNOWN).
    UNKNOWN = "unknown"
