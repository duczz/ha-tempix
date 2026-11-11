"""HardwareCommand — the bare desired state for one device.

Hardware-related fields ONLY: no policy reasons, no traces, no room
state. The dispatcher translates this into HA service calls and tracks
it in the CommandLedger. Valve positioning (``number.set_value``) has
its own command type (``ValveCommand``) — it is not squeezed in here.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HardwareCommand:
    """Desired state for a single climate entity."""

    entity_id: str
    hvac_mode: str | None = None
    # None = don't touch the setpoint (e.g. pure mode change or
    # non-temperature modes like dry/fan_only).
    target_temperature: float | None = None

    @property
    def is_noop(self) -> bool:
        return self.hvac_mode is None and self.target_temperature is None
