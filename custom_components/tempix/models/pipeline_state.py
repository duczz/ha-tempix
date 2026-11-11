"""PipelineState — the immutable carrier through the reducer phases.

Every policy receives a PipelineState and returns a NEW instance
(Copy-on-Write) — in-place mutation is impossible by
construction (``frozen=True`` + tuple entries). Policies that intervene
append their ``ConstraintTrace`` via :meth:`evolve`.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from custom_components.tempix.models.decision_trace import ConstraintTrace
from custom_components.tempix.models.priorities import GoalClass
from custom_components.tempix.models.target_state import TargetState


@dataclass(frozen=True)
class PipelineState:
    """TargetState + accumulated traces at one point in the pipeline."""

    target: TargetState
    entries: tuple[ConstraintTrace, ...] = field(default=())
    # Which goal won the selection — set once by the engine, carried
    # through unchanged (status classification + goal-aware reducers
    # like the sunshine modifier need it).
    goal_class: GoalClass | None = None
    # Whether the winning goal chose the comfort branch (SCHEDULE/FORCE/
    # ADJUSTMENT set it; goals without a comfort/eco notion leave it None). V1: the
    # sunshine offset applies only to the schedule-comfort branch.
    comfort_active: bool | None = None

    def evolve(self, target: TargetState, trace: ConstraintTrace) -> PipelineState:
        """Return a new state with the given target and appended trace.

        ``goal_class`` / ``comfort_active`` are deliberately preserved —
        reducers change the target, never the goal attribution.
        """
        return replace(self, target=target, entries=self.entries + (trace,))
