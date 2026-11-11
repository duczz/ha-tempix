"""TriggerEvent — the event delta that triggered an evaluation.

Required input for the override detection: its partial-propagation
criterion compares against the PREVIOUS device value, and the
ui/physical source classification needs the HA event context. Without
this delta neither is implementable — ``trigger_source: str`` alone is
not enough.

Heartbeat/timer cycles have no TriggerEvent (``trigger_event = None`` on
the snapshot) → override detection does not run. Primitives only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TriggerEvent:
    """State-change delta of the triggering TRV event."""

    entity_id: str
    old_hvac: str | None
    old_temp: float | None
    new_hvac: str | None
    new_temp: float | None
    # UTC timestamp of the event (TRV timestamps are truth).
    timestamp: datetime
    # True if the HA event context carries a user_id/parent_id (UI action).
    is_user_context: bool
