"""CalibrationCommand — one planned entity-based calibration write.

A command type SEPARATE from HardwareCommand (climate mode/temp) and
ValveCommand (valve %): entity-based calibration writes an offset/value on a
``number`` entity or a Tado offset via ``tado.set_climate_temperature_offset``.
This is the V2 port of V1's ``CalibrationApplier`` (calibration_mode = "native"
or aggressive-calibration) — the FRITZ generic path bakes the offset into the
target temperature in ``plan()`` instead and produces NO calibration command.

Planned + diffed against V1's ``calculate_calibration`` (keyed by ``trv_id``);
the actual send (``number.set_value`` / Tado offset + optional external-sensor
select toggle) is the dispatcher's ``dispatch_calibrations``.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CalibrationCommand:
    """Desired calibration write for one TRV."""

    # Source TRV — keys the parity diff and the send-side throttle.
    trv_id: str
    # Entity the service call writes to: the calibration ``number`` entity
    # (non-Tado) or the TRV itself (Tado offset service).
    target_entity: str
    # The value to write (number value / room temp, or Tado offset °C).
    value: float
    is_tado: bool = False
    # External-sensor select/switch to flip to "external" before writing the
    # calibration (non-Tado only); None when no such entity exists.
    select_entity: str | None = None
    # Context for the diff/log (V1 parity: offset vs. absolute-temperature
    # calibration entities compute differently).
    old_value: float | None = None
    is_offset: bool = False
