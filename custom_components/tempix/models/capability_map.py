"""DeviceCapabilityMap — static device capabilities.

Rebuilt from the devices' HA attributes every cycle and immutable per
cycle (a TRV can come online with different modes). The dispatcher
consults it for mode fallbacks (a device that cannot ``dry`` gets ``off``
instead) and the Device Command Planning stage for per-device
clamping/rounding. Dynamic values (current mode,
current temp, calibration offset) do NOT belong here — they live in the
snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_STALENESS_TIMEOUT = 300.0  # seconds until a CONFIRMED ledger entry is STALE


@dataclass(frozen=True)
class DeviceCapability:
    """Static capabilities of one climate device."""

    entity_id: str
    hvac_modes: frozenset[str] = frozenset()
    min_temp: float = 5.0
    max_temp: float = 30.0
    target_temp_step: float = 0.5
    # Seconds after which a confirmed hardware state counts as STALE
    # (LedgerEntryState).
    staleness_timeout: float = DEFAULT_STALENESS_TIMEOUT

    def supports(self, hvac_mode: str) -> bool:
        return hvac_mode in self.hvac_modes


@dataclass(frozen=True)
class DeviceCapabilityMap:
    """Immutable room-level lookup of all managed devices."""

    devices: tuple[DeviceCapability, ...] = field(default=())

    def for_entity(self, entity_id: str) -> DeviceCapability | None:
        for device in self.devices:
            if device.entity_id == entity_id:
                return device
        return None

    def supports(self, entity_id: str, hvac_mode: str) -> bool:
        device = self.for_entity(entity_id)
        return device is not None and device.supports(hvac_mode)

    @property
    def entity_ids(self) -> tuple[str, ...]:
        return tuple(device.entity_id for device in self.devices)
