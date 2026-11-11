"""TargetState — the one inseparable engine result.

HVAC mode and target temperature are ALWAYS computed together. V1
calculated them in two separate chains that could contradict each other —
the production bug (window open, ``window_open_temp == 0``:
temp chain said 0.0/off, mode chain said comfort, the dispatcher clamped
the 0-target up to the device minimum and the AC cooled to 16 °C against
an open window). This model makes that class of bug unrepresentable.

HVAC modes are plain ``str`` (HA's ``HVACMode`` is a str-enum; models
must stay HA-free — validation happens at the boundary).
"""
from __future__ import annotations

from dataclasses import dataclass

HVAC_OFF = "off"

# Modes that steer towards a setpoint; dry/fan_only/off carry no target
# temperature (None-Safety: constraints must guard via is_applicable).
TEMPERATURE_DRIVEN_HVAC_MODES = frozenset({"heat", "cool", "heat_cool", "auto"})

# Active modes WITHOUT a setpoint. Deliberately excludes "off" — that one has
# its own handling nearly everywhere (own status, own valve rule, own diff
# guard), so lumping it in here would silently widen those rules.
TEMPERATURELESS_HVAC_MODES = frozenset({"dry", "fan_only"})


@dataclass(frozen=True)
class TargetState:
    """Immutable room-level target: mode + temperature as ONE result."""

    hvac_mode: str
    # None for non-temperature modes (off/dry/fan_only) — never 0.0 as a
    # stand-in for "off": a 0-setpoint gets clamped up by device minimums.
    target_temperature: float | None = None

    @property
    def is_temperature_driven(self) -> bool:
        return self.hvac_mode in TEMPERATURE_DRIVEN_HVAC_MODES

    @property
    def is_off(self) -> bool:
        return self.hvac_mode == HVAC_OFF
