"""Binding priority order for the V2 policy pipeline.

``GoalClass`` is derived from the V1 priority chains — this derivation is
the contract for every Goal Policy built afterwards.

Sources of the derivation (V1):
- ``engine_temperature.calculate_target_temperature()`` (value chain)
- ``engine_schedule.calculate_hvac_mode()`` (mode chain)
- ``engine_schedule.determine_heating_state()`` (status chain)

Derived goal order (strongest first), cross-checked against all three:

    SYSTEM > MANUAL_OVERRIDE > LIMING > VACATION > PARTY
           > FORCE > ADJUSTMENT > SCHEDULE

Deliberate V2 deviations from V1 (documented, not silent):

1. **SYSTEM is highest.**
   V1 checked the temporary override and liming BEFORE ``automation_active``
   in the value chains — an old override could keep heating a switched-off
   system. V2: the master switch always wins. A Golden-Master scenario
   pins the V1-compatible part (idle wins over window); the
   override-vs-system case is pinned by a domain-truth test (V1 had no
   snapshotable override path in the bridge).

2. **Window is NOT in this enum.** V1 mixed the window into the goal
   chains; V2 handles it as ``INHIBIT_WINDOW`` constraint (40). Its
   ``is_applicable`` excludes LIMING (valve-protection run overrides the
   window rule — same exception the v1.6.7 fix anchored in the
   ``window_open_temp == 0`` guard). Pinned by a Golden-Master scenario.

3. **Frost has a narrow goal-level presence.**
   ``FrostGoal`` (``GoalClass.FROST = 90``, above SYSTEM) takes over
   ``hvac_mode`` in exactly ONE sub-case: the master gate is closed
   (``not automation_active``) AND the idle target would be OFF
   (``idle_temperature <= 0``) AND the room is below the frost floor —
   the one case ``CLAMP_SAFETY`` cannot rescue on its own, because it may
   never change the mode. Every other frost scenario
   (temperature-driven targets everywhere, OFF targets from any OTHER
   source — adjustment-off, away-off, window==0) still goes through the
   unchanged ``FrostConstraint`` (30) exactly as before; this goal does
   NOT duplicate that mechanism. ``FrostGoal`` has no V1 lineage (V1's
   own active frost takeover was removed, see the ``FrostConstraint``
   docstring in ``policies/constraints.py``) — it is intentionally absent
   from the "Derived goal order" line above, which is a V1-derivation
   summary.

4. **Pause (``manual_mode``) never enters the pipeline**
   (passthrough + generation invalidation) — hence no GoalClass.

5. **Guest is NOT a goal**: it is a presence input of
   the ``SchedulePolicy``.

Also NOT goals: Away (transformation inside SchedulePolicy), Smart
Preconditioning (lookahead inside SchedulePolicy), Sunshine (Modifier),
calibration/valve/hysteresis (HardwareDispatcher device command planning).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from custom_components.tempix.models.pipeline_state import PipelineState
from custom_components.tempix.models.priorities import ConstraintPriority, GoalClass
from custom_components.tempix.models.snapshot import TempixSnapshot
from custom_components.tempix.models.target_state import TargetState

__all__ = [
    "ConstraintPriority",
    "GoalClass",
    "GoalPolicy",
    "ModifierPolicy",
    "SafetyConstraint",
]


# GoalClass / ConstraintPriority are DEFINED in models/priorities.py
# (PipelineState carries the winning GoalClass — keeping them in models
# avoids a models↔policies import cycle) and re-exported here as the
# canonical import location.


# ── policy base classes ───────────────────────────────────────────────────────
#
# All policies are pure: they read ONLY the snapshot, never
# HA state, never wall-clock time. Reducer policies return a NEW
# PipelineState via ``state.evolve(...)`` (Copy-on-Write).


class GoalPolicy(ABC):
    """Provides the base TargetState. Selection: highest applicable
    ``goal_class`` wins — never registration order."""

    #: Must be declared by every subclass (enforced at class creation).
    goal_class: GoalClass

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        if not isinstance(getattr(cls, "goal_class", None), GoalClass):
            raise TypeError(f"{cls.__name__} must declare a GoalClass 'goal_class'")

    @abstractmethod
    def is_applicable(self, snapshot: TempixSnapshot) -> bool:
        """Whether this goal claims the room in the given snapshot."""

    @abstractmethod
    def initial_target(self, snapshot: TempixSnapshot) -> TargetState:
        """The base target this goal wants (mode + temperature as ONE)."""

    def is_comfort_branch(self, snapshot: TempixSnapshot) -> bool | None:
        """Whether this goal chose the comfort branch (feeds
        ``PipelineState.comfort_active``). None = goal has no
        comfort/eco notion (e.g. SYSTEM idle, override)."""
        return None

    def pins_hvac_mode(self, snapshot: TempixSnapshot) -> bool:
        """Whether the mode of ``initial_target`` is this goal's own decision.

        False = the goal only claims the temperature; the engine then takes
        the mode from the next-highest applicable goal (temp-only
        override). Every goal except that case decides its mode.
        """
        return True

    def next_boundary(self, snapshot: TempixSnapshot) -> datetime | None:
        """Earliest moment THIS policy would decide differently.

        The engine aggregates ``min()`` over all policies into
        ``DecisionResult.next_evaluation_at``. None = no time boundary.
        """
        return None


class _ReducerPolicy(ABC):
    """Shared reducer contract for modifiers and constraints."""

    #: Reducer phase; pipeline sorts strictly by this value.
    priority: ConstraintPriority

    @abstractmethod
    def is_applicable(self, snapshot: TempixSnapshot, state: PipelineState) -> bool:
        """Mandatory guard (None-Safety): e.g. the frost clamp applies only
        to temperature-driven heating — never crash on target=None."""

    @abstractmethod
    def apply(self, snapshot: TempixSnapshot, state: PipelineState) -> PipelineState:
        """Return the evolved state (with appended ConstraintTrace)."""

    def next_boundary(self, snapshot: TempixSnapshot) -> datetime | None:
        return None


class ModifierPolicy(_ReducerPolicy):
    """Reducer of pipeline phase 1: transformational, additive/commuting only
    (single MODIFIER priority level — order among modifiers must not matter)."""

    priority = ConstraintPriority.MODIFIER


class SafetyConstraint(_ReducerPolicy):
    """Reducer of pipeline phases 2..4: clamps and inhibits."""

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        priority = getattr(cls, "priority", None)
        if not isinstance(priority, ConstraintPriority):
            raise TypeError(
                f"{cls.__name__} must declare a ConstraintPriority 'priority'"
            )
        if priority is ConstraintPriority.MODIFIER:
            raise TypeError(
                f"{cls.__name__}: MODIFIER priority belongs to ModifierPolicy"
            )
