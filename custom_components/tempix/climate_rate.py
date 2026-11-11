"""Shared T1 outside-temperature normalization for the climate rate.

Single source of truth for the T1 constants — they were duplicated in
``engine_schedule._get_effective_conditioning_rate`` (multiplies the factor)
and ``coordinator_learning`` (divides by it to normalize a measured rate)
and could drift silently (same failure class as the V1/V2 constant copies).
HA-free on purpose so the V2 resolver (domain_resolvers) can import it.

Model: at the reference outside temperature (5 °C) a room conditions at its
learned base rate. Warmer outside → heating gets easier (factor > 1);
colder → harder (factor < 1). Clamped so a sensor glitch can never produce
an absurd rate.

This model is heating-only and was applied unchanged to
cooling rooms too — monotonically INCREASING in outside_temp means warmer
outside was treated as "easier", when a cooling room physically works
HARDER as it gets warmer outside. Both callers at the time
(coordinator_learning, engine_schedule) inherited the inverted sign;
domain_resolvers, added later, passes ``is_cooling`` from the start. No real
cooling characteristic has ever been measured for this codebase, so `is_cooling`
disables the model entirely (factor 1.0) rather than guessing a mirrored
one that would be equally unfounded.
"""
from __future__ import annotations

T1_REFERENCE_OUTSIDE_TEMP = 5.0
T1_SENSITIVITY = 0.04
T1_FACTOR_MIN = 0.3
T1_FACTOR_MAX = 2.0


def t1_normalization_factor(outside_temp: float, is_cooling: bool = False) -> float:
    """Speed-up/slow-down factor vs. the 5 °C reference outside temperature.

    Heating only — cooling rooms get the neutral factor until a real
    cooling characteristic is measured.
    """
    if is_cooling:
        return 1.0
    factor = 1.0 - (T1_REFERENCE_OUTSIDE_TEMP - outside_temp) * T1_SENSITIVITY
    return max(T1_FACTOR_MIN, min(T1_FACTOR_MAX, factor))
