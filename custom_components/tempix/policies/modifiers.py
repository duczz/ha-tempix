"""Modifier policies — phase-1 reducers.

Single MODIFIER priority level: all modifiers must be additive/
commuting; order among them must not matter.
"""
from __future__ import annotations

from custom_components.tempix.models.decision_trace import ConstraintTrace
from custom_components.tempix.models.pipeline_state import PipelineState
from custom_components.tempix.models.priorities import GoalClass
from custom_components.tempix.models.snapshot import TempixSnapshot
from custom_components.tempix.models.target_state import TargetState
from custom_components.tempix.policies.base_policy import ModifierPolicy


class SunshineModifier(ModifierPolicy):
    """Sunshine offset (V1: ``calculate_target_temperature`` tail).

    Applies ONLY to the schedule-comfort branch (goals like party/vacation/
    override returned before the sunshine block) — and after the away
    transformation (V1 gated on ``set_comfort``, not on the final value).

    Solar-gain compensation → REDUCE the target in BOTH modes: heating needs
    less (the sun helps warm the room), cooling needs more (a lower cool
    setpoint = more cooling to counter the solar heat). Direction is
    therefore mode-independent; ``is_cooling`` is not needed here (fixed:
    the old ``+ offset`` for cooling let the room drift warmer
    exactly when the sun was heating it, contradicting the documented
    "reduced when sunny" behaviour).
    """

    def __init__(self, offset: float) -> None:
        self._offset = offset

    def is_applicable(self, snapshot: TempixSnapshot, state: PipelineState) -> bool:
        return (
            snapshot.sunshine_active
            and self._offset != 0
            and state.goal_class is GoalClass.SCHEDULE
            and state.comfort_active is True
            and state.target.is_temperature_driven
            and state.target.target_temperature is not None
        )

    def apply(self, snapshot: TempixSnapshot, state: PipelineState) -> PipelineState:
        before = state.target.target_temperature
        assert before is not None  # guarded by is_applicable
        after = before - self._offset
        return state.evolve(
            TargetState(hvac_mode=state.target.hvac_mode, target_temperature=after),
            ConstraintTrace(
                policy=type(self).__name__,
                input_value=before,
                output_value=after,
                reason="sunshine offset (comfort branch)",
            ),
        )
