"""DeviceState — snapshotted state of one managed climate device.

Dynamic counterpart of ``DeviceCapability`` (static). Needed by the
override detection (previous/current device values) and the dispatcher's
Device Command Planning. Dry normalization happens at build
time: a device reporting a non-temperature mode carries
``target_temperature = None`` — always.
"""
from __future__ import annotations

from dataclasses import dataclass

from custom_components.tempix.models.health import HealthStatus


@dataclass(frozen=True)
class DeviceState:
    """Immutable per-device state as read at snapshot time."""

    entity_id: str
    hvac_mode: str | None
    target_temperature: float | None
    current_temperature: float | None
    health: HealthStatus = HealthStatus.AVAILABLE
