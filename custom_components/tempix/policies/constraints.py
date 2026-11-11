"""Safety constraints — clamp and inhibit reducers.

Hierarchy: CLAMP_SOFT (20) < CLAMP_SAFETY (30) <
INHIBIT_WINDOW (40) < INHIBIT_SAFETY (50). Every constraint guards via
``is_applicable`` (None-Safety) and only intervenes when it would
actually change the target — traces stay noise-free.
"""
from __future__ import annotations

from custom_components.tempix.models.decision_trace import ConstraintTrace
from custom_components.tempix.models.pipeline_state import PipelineState
from custom_components.tempix.models.snapshot import TempixSnapshot
from custom_components.tempix.models.target_state import HVAC_OFF, TargetState
from custom_components.tempix.policies.base_policy import (
    ConstraintPriority,
    GoalClass,
    SafetyConstraint,
)

DEFAULT_MAX_TEMP = 30.0


class MaxTempConstraint(SafetyConstraint):
    """CLAMP_SOFT: room targets never exceed the configured maximum."""

    priority = ConstraintPriority.CLAMP_SOFT

    def __init__(self, max_temp: float = DEFAULT_MAX_TEMP) -> None:
        self._max_temp = max_temp

    def is_applicable(self, snapshot: TempixSnapshot, state: PipelineState) -> bool:
        temp = state.target.target_temperature
        return (
            state.target.is_temperature_driven
            and temp is not None
            and temp > self._max_temp
        )

    def apply(self, snapshot: TempixSnapshot, state: PipelineState) -> PipelineState:
        before = state.target.target_temperature
        return state.evolve(
            TargetState(hvac_mode=state.target.hvac_mode, target_temperature=self._max_temp),
            ConstraintTrace(
                policy=type(self).__name__,
                input_value=before,
                output_value=self._max_temp,
                reason=f"max temp clamp ({self._max_temp} °C)",
            ),
        )


class FrostConstraint(SafetyConstraint):
    """CLAMP_SAFETY: frost floor — overrides the TARGET, never the mode.

    Heating-only (v1.6.7 lesson): in cooling mode the frost
    floor must never apply, otherwise an AC would actively cool "for
    frost protection". Two facets:
    - temperature-driven heating targets are lifted to the frost floor
      (V1: ``max(frost_temp, …)`` for window/vacation temps);
    - an OFF target gets the frost SETPOINT injected while the mode
      stays OFF — defense against TRV firmware bugs — but only when the
      room is actually at risk (``room_temperature < frost_temp``).

    Deliberate deviation: V1's *active* frost state
    (``is_frost_protection()`` as highest-priority takeover incl. mode
    change) is NOT reproduced — the frost floor overrides the target, never
    the mode. Golden-Master fixtures contain no active-frost takeover.

    Resolved: that V1 rule was never a frost check at all
    but an ABSENCE rule (everyone away longer than ``frost_protection_duration``
    → drop to the frost temp). It was removed from V1 together with its config
    field, so this class was the ONLY frost mechanism until FrostGoal.
    Reintroducing time-graded absence would be a possible future change
    — as its own goal, not as a facet of frost protection.

    Cross-reference: the one case this class structurally
    cannot rescue — the master gate closed AND SystemStateGoal's idle-zero
    OFF target — is now handled one layer up by FrostGoal (GoalClass.FROST
    = 90, policies/goals.py), which changes the mode itself. That goal's
    target lands exactly on frost_temp, so this class's own is_applicable
    (strict ``<``) never re-fires on it — no double-application.
    """

    priority = ConstraintPriority.CLAMP_SAFETY

    def __init__(self, frost_enabled: bool, frost_temp: float, is_cooling: bool = False) -> None:
        self._frost_enabled = frost_enabled
        self._frost_temp = frost_temp
        self._is_cooling = is_cooling

    def is_applicable(self, snapshot: TempixSnapshot, state: PipelineState) -> bool:
        if not self._frost_enabled or self._is_cooling:
            return False
        temp = state.target.target_temperature
        if state.target.is_temperature_driven:
            return temp is not None and temp < self._frost_temp
        if state.target.is_off:
            # Inject the frost setpoint into an OFF device only when the
            # room is actually below the frost threshold.
            # Reads room_temperature_resolved, NOT room_temperature. The
            # latter is V1-identical by design (parity) and is
            # unconditionally None whenever no
            # external sensor is configured — the common case, and exactly
            # where this facet needs to see the device's own fallback
            # reading to fire at all. Only this one read changed; every
            # other consumer of room state keeps the parity-locked value.
            room = snapshot.room_temperature_resolved
            return (
                room is not None
                and room < self._frost_temp
                and temp != self._frost_temp
            )
        return False  # non-temperature modes (dry/fan_only): no floor

    def apply(self, snapshot: TempixSnapshot, state: PipelineState) -> PipelineState:
        before = state.target.target_temperature
        return state.evolve(
            TargetState(
                hvac_mode=state.target.hvac_mode,  # mode is never touched
                target_temperature=self._frost_temp,
            ),
            ConstraintTrace(
                policy=type(self).__name__,
                input_value=before,
                output_value=self._frost_temp,
                reason="frost floor (heating-only)",
            ),
        )


class WindowConstraint(SafetyConstraint):
    """INHIBIT_WINDOW: open window overrides the target (window wins).

    V1-parity semantics (pinned by golden-master scenarios):

    - ``window_open_temp > 0``: the target is SET to the window temp
      (V1 replaced, not clamped — required because a suppressed override
      must not shine through). Heating additionally floors at
      ``frost_temp`` (V1: ``max(frost_min, window_temp)``); cooling uses
      the plain window temp (frost is heating-only). Mode stays
      temperature-driven; non-temperature targets (off/dry) are left
      alone.
    - ``window_open_temp == 0`` ("turn the device off", v1.6.7):
      heating with frost enabled → comfort-mode at frost temp (V1);
      otherwise → OFF. Cooling → unconditionally OFF.

    Exceptions:
    - LIMING run active → not applicable (valve-protection overrides the
      window rule; same exception as the v1.6.7 hvac guard).
    - Deliberate deviation: V1's force-comfort exception in the
      ``== 0`` guard is NOT reproduced — it produced a 0.0-target with
      comfort mode (the original clamp-up bug pattern). The window
      beats even the stronger manual override, so it beats
      force-comfort too → OFF.
    """

    priority = ConstraintPriority.INHIBIT_WINDOW

    def __init__(
        self,
        window_open_temp: float,
        frost_enabled: bool,
        frost_temp: float,
        hvac_mode_comfort: str,
        is_cooling: bool = False,
    ) -> None:
        self._window_open_temp = window_open_temp
        self._frost_enabled = frost_enabled
        self._frost_temp = frost_temp
        self._hvac_mode_comfort = hvac_mode_comfort
        self._is_cooling = is_cooling

    def _result_target(self, state: PipelineState) -> TargetState:
        if self._window_open_temp > 0:
            if self._is_cooling:
                temp = self._window_open_temp
            else:
                temp = (
                    max(self._frost_temp, self._window_open_temp)
                    if self._frost_enabled
                    else self._window_open_temp
                )
            return TargetState(hvac_mode=state.target.hvac_mode, target_temperature=temp)
        # window_open_temp == 0 → device off (v1.6.7)…
        if not self._is_cooling and self._frost_enabled:
            # …unless the heating frost floor applies: keep actively
            # driving to the frost minimum (V1: comfort mode + frost_min).
            return TargetState(
                hvac_mode=self._hvac_mode_comfort,
                target_temperature=self._frost_temp,
            )
        return TargetState(hvac_mode=HVAC_OFF)

    def is_applicable(self, snapshot: TempixSnapshot, state: PipelineState) -> bool:
        # window_open_resolved carries the builder's own
        # resolution (open wins; below the reachability quorum it holds the
        # last known state); ``snapshot.window_open``
        # is V1-bridged for parity and goes plain None on the same sensor
        # ambiguity (V1 has no memory there), which used to fall through to
        # "no inhibit" — silently discarding the mitigation this field
        # exists for.
        if snapshot.window_open_resolved is not True:
            return False
        if snapshot.liming_active:  # valve-protection exception
            return False
        # Idle wins over window (V1: automation-off returns BEFORE the
        # window branch; pinned by a golden-master scenario).
        # Without this, an idle_temperature > 0 would be REPLACED by the
        # window temp — heating MORE against an open window than idle asks.
        if state.goal_class is GoalClass.SYSTEM:
            return False
        if self._window_open_temp > 0 and not state.target.is_temperature_driven:
            return False  # off/dry stays untouched by the window clamp
        return self._result_target(state) != state.target

    def apply(self, snapshot: TempixSnapshot, state: PipelineState) -> PipelineState:
        result = self._result_target(state)
        return state.evolve(
            result,
            ConstraintTrace(
                policy=type(self).__name__,
                input_value=state.target.target_temperature,
                output_value=result.target_temperature,
                reason=(
                    "window open → device off"
                    if result.is_off
                    else "window open → window temperature"
                ),
            ),
        )
