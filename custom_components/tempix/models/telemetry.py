"""Telemetry read-model and room health state.

Hard architecture rule:
``Telemetry`` is meant as a pure READ-model for sensors/diagnostics. It
must NEVER be used as an input for the DecisionEngine — the engine works
exclusively on the ``TempixSnapshot``.

``RoomHealth`` is set by the ``HardwareDispatcher``; ``Telemetry`` itself
is not instantiated yet.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class RoomHealth(Enum):
    """Aggregated health of a room's hardware.

    Set by the dispatcher based on ``DeviceDispatchResult``:
    - ``HEALTHY``  — no attempted dispatch FAILED.
    - ``DEGRADED`` — at least one device FAILED (battery empty,
      Zigbee timeout, …) while others still respond.
    - ``OFFLINE``  — no device in the room responds.
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OFFLINE = "offline"


@dataclass(frozen=True)
class Telemetry:
    """Immutable per-room telemetry snapshot for sensors and diagnostics.

    Intended as the coordinator's per-cycle read-model; not published yet.
    Every field is a primitive or another frozen model — no HA objects.
    """

    room_health: RoomHealth = RoomHealth.HEALTHY
    # UTC timestamp of the cycle that produced this telemetry.
    last_updated: datetime | None = None
    # Correlates telemetry with the snapshot/decision that produced it.
    snapshot_id: str | None = None
    # entity_ids whose last dispatch FAILED (feeds DEGRADED/OFFLINE).
    failed_devices: tuple[str, ...] = field(default=())
