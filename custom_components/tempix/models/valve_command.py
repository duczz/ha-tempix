"""ValveCommand — one planned valve opening.

A command type SEPARATE from HardwareCommand (temperature/mode only): valve
positioning writes a percentage on a ``number`` entity. Planned per cycle,
diffed against V1's ValvePositioner and sent by the dispatcher's
``dispatch_valves``. The number entity is resolved
from ``trv_id`` at send time (V1's ValvePositioner finds it via the entity
registry), so planning keys by the TRV.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ValveCommand:
    """Desired valve opening for one TRV (0..valve_max %)."""

    trv_id: str
    position: int
