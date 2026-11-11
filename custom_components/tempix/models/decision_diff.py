"""DecisionDiff — structured V1/V2 comparison record.

Deviations between V1 (``old``, observer) and V2 (``new``, drives the
hardware) are logged as structured JSON (``old``, ``new``, ``delta``,
``winner_old``, ``winner_new``) — never as a generic CRITICAL string.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class FieldDiff:
    """One deviating dimension (room field or per-device value)."""

    field: str          # e.g. "target_temperature", "climate.trv_1.hvac_mode"
    old: Any            # V1 value (observer)
    new: Any            # V2 value (drives the hardware)


@dataclass(frozen=True)
class DecisionDiff:
    """All deviations of one evaluation cycle (empty = parity)."""

    snapshot_id: str
    timestamp: datetime
    winner_old: str     # V1 ClimateState value
    winner_new: str     # V2 winning GoalClass name
    diffs: tuple[FieldDiff, ...] = field(default=())

    @property
    def has_diffs(self) -> bool:
        return bool(self.diffs)

    @property
    def signature(self) -> tuple:
        """Stable identity of WHAT deviates (not when) — used to log a
        persistent deviation once at WARNING instead of flooding every
        cycle (repeats go to DEBUG)."""
        return (
            self.winner_old,
            self.winner_new,
            tuple(sorted((d.field, str(d.old), str(d.new)) for d in self.diffs)),
        )

    def to_log_dict(self) -> dict[str, Any]:
        """Structured payload for the JSON diff log."""
        return {
            "snapshot_id": self.snapshot_id,
            "timestamp": self.timestamp.isoformat(),
            "winner_old": self.winner_old,
            "winner_new": self.winner_new,
            "old": {d.field: d.old for d in self.diffs},
            "new": {d.field: d.new for d in self.diffs},
            "delta": [d.field for d in self.diffs],
        }
