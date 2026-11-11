"""GoalClass / ConstraintPriority — the binding pipeline order.

Lives in ``models/`` so pure models (``PipelineState`` carries the
winning ``GoalClass``) can use it without importing the policies
package. ``policies.base_policy`` re-exports both names — import from
there when working on policies (canonical location).

The full V1 derivation and the documented deviations are
documented in ``policies/base_policy.py``.
"""
from __future__ import annotations

from enum import IntEnum


class GoalClass(IntEnum):
    """Priority of a Goal Policy — highest applicable value wins selection.

    Values are spaced by 10 so future goals can be slotted in without
    renumbering. Never compare goals by registration order.
    """

    # Base target from scheduler/calendar, incl. holiday resolution,
    # away transformation, guest presence and smart preconditioning.
    SCHEDULE = 10
    # JSON adjustment with mode != "auto" (forces comfort/eco/off).
    # Adjustment *values* (comfort/eco temps) are SCHEDULE inputs instead.
    ADJUSTMENT = 20
    # Force-Comfort / Force-Eco switches (mutual exclusion stays on the
    # entity level). V1: checked after Party, before Adjustment-mode.
    FORCE = 30
    PARTY = 40
    # V1: vacation wins over party (determine_heating_state order).
    VACATION = 50
    # Valve-protection run; force-comfort-like, beats the window rule via
    # the INHIBIT_WINDOW is_applicable exception (NOT via goal order).
    LIMING = 60
    # Temporary manual override — strongest *user* intent in normal
    # operation. The open window suppresses it at constraint level.
    MANUAL_OVERRIDE = 70
    # Season mode / automation master switch / outside-temperature gate →
    # idle/off target. Deliberately above MANUAL_OVERRIDE (deviation 1,
    # see policies/base_policy.py).
    SYSTEM = 80
    # Frost takeover — narrow: the master gate is closed
    # (SYSTEM would return an idle-zero OFF target) AND the room is
    # actually below the frost floor. Above SYSTEM so it can flip the OFF
    # target's MODE itself — FrostConstraint (CLAMP_SAFETY, 30) is
    # forbidden from ever touching the mode and cannot rescue
    # this one case on its own. V2-only capability, no V1 chain to derive
    # this position from (see policies/goals.py::FrostGoal); still slotted
    # at +10 like every other goal so nothing above it needs renumbering
    # later.
    FROST = 90


class ConstraintPriority(IntEnum):
    """Fixed 4-phase reducer order — values are normative."""

    MODIFIER = 10        # transformational (e.g. sunshine offset)
    CLAMP_SOFT = 20      # soft bounds (e.g. max temp 30 °C)
    CLAMP_SAFETY = 30    # frost floor: overrides target, not mode; heating-only
    INHIBIT_WINDOW = 40  # window open: sets mode OFF/clamps to window temp
    INHIBIT_SAFETY = 50  # reserved: absolute safety inhibit (no constraint uses it yet)
