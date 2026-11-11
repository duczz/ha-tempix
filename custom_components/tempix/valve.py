"""Valve positioning — native V2 port of V1's calculate_valve_position.

Optional feature (``valve_mode != "off"``): Tempix writes the valve opening %
on a ``number`` entity instead of leaving it to the TRV. Ported as a PURE
function so the parallel validator can diff it against V1's engine method
and so the V1 ``ValvePositioner`` can be removed later.

Faithful 1:1 port of ``TempixEngine.calculate_valve_position``
(engine_calibration.py) — the room-temperature resolution stays with the
caller (V2 passes the already-resolved ``snapshot.room_temperature``).
Since ``valve_min`` the V1 method delegates its curve here, so both engines
share ONE formula and the parallel validator cannot drift on it.

``valve_min`` (minimum opening / closing point): many valves stay physically
shut below some opening (e.g. 18 %). Every opening ABOVE 0 is remapped from
``(0, valve_max]`` onto ``[valve_min, valve_max]`` (as Versatile Thermostat's
``minimum_opening_degrees``), so the user enters the closing point itself and
the smallest opening already lets water through. Closed stays closed: the
point at which the valve first opens (the ``step / 2`` rounding threshold) is
unchanged, only the opening it then gets is lifted. ``valve_min = 0`` is the
exact pre-``valve_min`` output.
"""
from __future__ import annotations

import math


def calculate_valve_position(
    *,
    mode: str,
    max_diff: float,
    valve_max: int,
    step: int,
    room_temp: float | None,
    target_temp: float | None,
    force_comfort: bool,
    valve_min: int = 0,
) -> int:
    """Valve opening percentage for positioning mode (0..``valve_max``)."""
    if mode == "off":
        return 100
    if room_temp is None or target_temp is None:
        return 100

    diff = target_temp - room_temp
    if diff >= max_diff:
        return valve_max
    if diff <= 0:
        return 0
    if max_diff <= 0:
        return valve_max

    opening_regular = (100 / max_diff) * diff
    if mode == "pessimistic":
        opening = math.sqrt(abs(opening_regular)) * 10
    elif mode == "optimistic":
        opening = (opening_regular ** 2) / 100
    else:
        opening = opening_regular

    opening_abs = (opening / 100) * valve_max
    if force_comfort:
        return valve_max
    rounded = int(((opening_abs + step / 2) // step) * step)
    if valve_min <= 0 or rounded == 0:
        # No minimum, or still below the opening threshold. Capped: rounding to
        # the step may overshoot a valve_max that is no multiple of it (VM-1:
        # valve_max 97, step 10 -> 100).
        return min(rounded, valve_max)

    # rounded > 0 implies valve_max > 0 (opening_abs scales with it).
    floor = min(valve_min, valve_max)
    span = opening_abs * (valve_max - floor) / valve_max
    return min(floor + int(((span + step / 2) // step) * step), valve_max)
