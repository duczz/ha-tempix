"""Goal policies — who provides the base TargetState.

Each goal is pure: config values arrive as primitives at construction
(config is reload-static; the pipeline is rebuilt on config reload),
dynamic state comes exclusively from the snapshot. V1-parity notes and
deliberate deviations are documented per class.

Selection contract (engine): filter ``is_applicable``, pick the highest
``goal_class`` — SCHEDULE is the always-applicable fallback.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.tempix.models.snapshot import TempixSnapshot
from custom_components.tempix.models.system_context import MAX_AGE_HOURS
from custom_components.tempix.models.target_state import (
    HVAC_OFF,
    TEMPERATURE_DRIVEN_HVAC_MODES,
    TargetState,
)
from custom_components.tempix.policies.base_policy import GoalClass, GoalPolicy

# V1: engine_temperature falls back to 21.0 when comfort is unresolved
# during a liming run, and const.DEFAULT_VACATION_TEMP is 15.0.
LIMING_FALLBACK_TEMP = 21.0
DEFAULT_VACATION_TEMP = 15.0
# Matches const.DEFAULT_PARTY_TEMP — the value the Number
# entity displays when party_temperature is unset. Party used to fall back to
# comfort_temperature instead, so the shown default and the engine's actual
# target silently disagreed by up to 5 degC.
DEFAULT_PARTY_TEMP = 18.0


def _effective_comfort(snapshot: TempixSnapshot) -> float | None:
    """Comfort setpoint with the JSON adjustment override applied (V1:
    ``adjustment_comfort or comfort``). Single source for all goals."""
    if snapshot.adjustment_comfort is not None:
        return snapshot.adjustment_comfort
    return snapshot.comfort_temperature


def _effective_eco(snapshot: TempixSnapshot) -> float | None:
    """Eco setpoint with the JSON adjustment override applied."""
    if snapshot.adjustment_eco is not None:
        return snapshot.adjustment_eco
    return snapshot.eco_temperature


def _with_calendar_hvac_mode(
    snapshot: TempixSnapshot, target: TargetState,
) -> TargetState:
    """Replace the mode of ``target`` by the calendar ``hvac:`` tag.

    The tag only decides the MODE; the temperature stays the one the goal
    computed for its branch. Modes without a setpoint (off/dry/fan_only, see
    ``TargetState``) carry none — the tag would otherwise hand a comfort value
    to a mode that has no use for it. No tag → ``target`` unchanged.
    """
    mode = snapshot.calendar_hvac_mode
    if mode is None:
        return target
    temperature = (
        target.target_temperature if mode in TEMPERATURE_DRIVEN_HVAC_MODES else None
    )
    return TargetState(hvac_mode=mode, target_temperature=temperature)


class SystemStateGoal(GoalPolicy):
    """Master gate: automation off / season off / outside-temp gate → idle.

    V1: ``calculate_target_temperature`` returned ``idle_temperature``
    (0 → 0.0) and the hvac chain returned "off" for idle 0, comfort-mode
    otherwise. Deliberate V2 deviation: this goal outranks MANUAL_OVERRIDE
    (V1 checked the override first — a stale override could keep heating
    a switched-off system).
    """

    goal_class = GoalClass.SYSTEM

    def __init__(self, idle_temperature: float, hvac_mode_comfort: str) -> None:
        self._idle_temperature = idle_temperature
        self._hvac_mode_comfort = hvac_mode_comfort

    def is_applicable(self, snapshot: TempixSnapshot) -> bool:
        return not snapshot.automation_active

    def initial_target(self, snapshot: TempixSnapshot) -> TargetState:
        if self._idle_temperature <= 0:
            return TargetState(hvac_mode=HVAC_OFF)
        return TargetState(
            hvac_mode=self._hvac_mode_comfort,
            target_temperature=self._idle_temperature,
        )


class FrostGoal(GoalPolicy):
    """Frost takeover — V2-only, no V1 equivalent.

    ``SystemStateGoal`` (SYSTEM = 80) returns ``TargetState(hvac_mode=OFF)``
    whenever the master gate is closed AND ``idle_temperature <= 0``.
    ``FrostConstraint`` (CLAMP_SAFETY, 30, in ``policies/constraints.py``) can
    inject a frost setpoint into that OFF target, but by design
    ("overrides target, not mode") it never changes the mode — so the
    injected setpoint never reaches the device (``hardware_dispatcher.py``
    drops any temperature whenever ``valve_mode == HVAC_OFF``). Net effect
    without this goal: nothing turns heating on when the room is
    dangerously cold and the gate is shut.

    Deliberately narrow — must NOT fire for anything ``FrostConstraint``
    already handles correctly:

    - ``idle_temperature > 0``: SYSTEM already returns a temperature-driven
      target, which ``FrostConstraint`` correctly floors (a deliberate
      V2 deviation). Firing here too would DOWNGRADE
      the target from ``idle_temperature`` to ``frost_temp`` whenever
      ``idle_temperature > frost_temp`` — a regression, not a fix.
    - Any OFF target from a source OTHER than SYSTEM's idle-zero branch
      (adjustment-off, away-off, window==0): unaffected, still governed by
      ``FrostConstraint``'s OFF facet.

    An open window still wins (same precedent as Force-Comfort):
    ``WindowConstraint.is_applicable`` excludes only ``GoalClass.SYSTEM``
    ("idle wins over window"), not ``GoalClass.FROST`` — no change to
    ``WindowConstraint`` was needed or made.
    """

    goal_class = GoalClass.FROST

    def __init__(
        self,
        frost_enabled: bool,
        frost_temp: float,
        idle_temperature: float,
        hvac_mode_comfort: str,
        is_cooling: bool = False,
    ) -> None:
        self._frost_enabled = frost_enabled
        self._frost_temp = frost_temp
        self._idle_temperature = idle_temperature
        self._hvac_mode_comfort = hvac_mode_comfort
        self._is_cooling = is_cooling

    def is_applicable(self, snapshot: TempixSnapshot) -> bool:
        if not self._frost_enabled or self._is_cooling:
            return False  # heating-only, same guard as FrostConstraint
        if snapshot.automation_active or self._idle_temperature > 0:
            return False  # SYSTEM's idle-zero branch is the only gap this fills
        room = snapshot.room_temperature_resolved
        return room is not None and room < self._frost_temp

    def initial_target(self, snapshot: TempixSnapshot) -> TargetState:
        return TargetState(
            hvac_mode=self._hvac_mode_comfort,
            target_temperature=self._frost_temp,
        )


class ManualOverridePolicy(GoalPolicy):
    """Temporary manual override with explicit expiry.

    The TTL is explicit (``expires_at`` from room-filtered transitions)
    — no fingerprint heuristics. A context change (away flip, new
    schedule phase) invalidates the override. Write-back isolation: this
    policy never mutates state; the engine only NAMES the expired override
    via :meth:`expired_override_id`.

    **Reality check — while V1 owns the override, most of the above is INERT.**
    The only producer of ``snapshot.active_override`` is
    ``ParallelValidator._mirror_override`` (one call site, verified), and it
    mirrors V1's override 1:1 on purpose, because **V1 owns the lifecycle**
    (slot-change clear via the schedule fingerprint, 24h limit in
    ``_do_update``). Three consequences, all by construction:

    * ``ctx.away`` is frozen from the SAME variable the snapshot's ``away``
      comes from, every cycle → the away comparison can never be True.
    * ``ctx.schedule_id`` is hard-coded ``None`` → the phase comparison can
      never be True.
    * ``expires_at`` is ``None`` → only the hard 24h ``SystemContext`` net
      is live.

    In production this policy therefore expires an override at 24h and never
    for a context change. **Do NOT "fix" that by activating the
    comparisons.** V2 would declare the override invalid while V1 still
    holds it, V1 would re-mirror it on the next cycle, and post-flip the
    room's target would oscillate between the override value and the
    schedule value. The comparisons go live once V2 gets its own
    override state and the V1 mirror disappears — that is what they are for.

    ``expired_override_id`` has NO consumer either: it rides on the
    ``DecisionResult`` and is shown in diagnostics; nothing deletes
    anything. The "state manager" the design refers to does not exist yet.
    """

    goal_class = GoalClass.MANUAL_OVERRIDE

    def __init__(self, hvac_mode_comfort: str) -> None:
        self._hvac_mode_comfort = hvac_mode_comfort

    def _is_context_invalid(self, snapshot: TempixSnapshot) -> bool:
        """While V1 owns the override this ALWAYS returns False in
        production — the V1 mirror re-freezes the context every cycle. See the class docstring before
        touching it; making it fire would fight V1's lifecycle."""
        override = snapshot.active_override
        if override is None:
            return False
        ctx = override.context
        if ctx.away != snapshot.away:
            return True
        # Only compare when BOTH sides know their phase — a missing id
        # (boot, first fetch pending) must not kill a valid override.
        if (
            ctx.schedule_id is not None
            and snapshot.schedule_phase_id is not None
            and ctx.schedule_id != snapshot.schedule_phase_id
        ):
            return True
        return False

    def expired_override_id(self, snapshot: TempixSnapshot) -> str | None:
        """Id of the override that must be cleared — None if still valid."""
        override = snapshot.active_override
        if override is None:
            return None
        if override.is_expired(snapshot.timestamp) or self._is_context_invalid(snapshot):
            return override.override_id
        return None

    def is_applicable(self, snapshot: TempixSnapshot) -> bool:
        return (
            snapshot.active_override is not None
            and self.expired_override_id(snapshot) is None
        )

    def pins_hvac_mode(self, snapshot: TempixSnapshot) -> bool:
        """A temp-only override (card / HA slider without a mode) leaves the
        mode to the automation — V1 parity: ``calculate_hvac_mode`` only
        short-circuits on a SET override mode. By design, moving the slider
        while the automation has the room off (e.g. ``hvac_mode_eco = off``)
        keeps it off; the temperature is
        kept and applies once the user switches the mode on."""
        override = snapshot.active_override
        return override is None or override.hvac_mode is not None

    def initial_target(self, snapshot: TempixSnapshot) -> TargetState:
        override = snapshot.active_override
        assert override is not None  # guarded by is_applicable
        # Mode-only overrides (e.g. dry) keep temperature None
        # (None-Safety). For temp-only overrides the comfort mode is only a
        # placeholder: pins_hvac_mode() is False, the engine replaces it
        # with the mode of the next applicable goal. NOT "physically
        # identical" — with hvac_mode_eco = off it switched the device on.
        hvac_mode = override.hvac_mode or self._hvac_mode_comfort
        return TargetState(
            hvac_mode=hvac_mode,
            target_temperature=override.target_temperature,
        )

    def next_boundary(self, snapshot: TempixSnapshot) -> datetime | None:
        override = snapshot.active_override
        if override is None:
            return None
        if override.expires_at is not None:
            return override.expires_at
        # No explicit TTL known → the hard 24h safety net is the boundary.
        return override.created_at + timedelta(hours=MAX_AGE_HOURS)


class LimingGoal(GoalPolicy):
    """Valve-protection run — force-comfort-like (V1: ``is_liming_time``).

    Beats the window rule via the INHIBIT_WINDOW ``is_applicable``
    exception, NOT via goal order (pinned by a golden-master scenario).
    """

    goal_class = GoalClass.LIMING

    def __init__(self, hvac_mode_comfort: str) -> None:
        self._hvac_mode_comfort = hvac_mode_comfort

    def is_applicable(self, snapshot: TempixSnapshot) -> bool:
        return snapshot.liming_active

    def initial_target(self, snapshot: TempixSnapshot) -> TargetState:
        comfort = snapshot.comfort_temperature
        return TargetState(
            hvac_mode=self._hvac_mode_comfort,
            target_temperature=comfort if comfort is not None else LIMING_FALLBACK_TEMP,
        )

    def next_boundary(self, snapshot: TempixSnapshot) -> datetime | None:
        return snapshot.liming_ends_at


class VacationGoal(GoalPolicy):
    """Vacation mode (V1: before party in ``determine_heating_state``).

    The frost floor (V1: ``max(frost_temp, vacation_temp)``) is applied
    by the CLAMP_SAFETY constraint — not duplicated here.
    """

    goal_class = GoalClass.VACATION

    def __init__(self, hvac_mode_comfort: str) -> None:
        self._hvac_mode_comfort = hvac_mode_comfort

    def is_applicable(self, snapshot: TempixSnapshot) -> bool:
        return snapshot.vacation_active

    def initial_target(self, snapshot: TempixSnapshot) -> TargetState:
        temp = snapshot.vacation_temperature
        target = TargetState(
            hvac_mode=self._hvac_mode_comfort,
            target_temperature=temp if temp is not None else DEFAULT_VACATION_TEMP,
        )
        # `vacation:` and `hvac:` in the SAME calendar event — the tag is
        # the more specific statement, so it decides the mode. A vacation that
        # does not come from the calendar stays untouched by the tag.
        if snapshot.calendar_tags.get("vacation"):
            return _with_calendar_hvac_mode(snapshot, target)
        return target


class PartyGoal(GoalPolicy):
    """Party mode (DEFAULT_PARTY_TEMP fallback, matching
    the Number entity's displayed default — not comfort_temperature. The two
    used to disagree silently whenever party_temperature was unset."""

    goal_class = GoalClass.PARTY

    def __init__(self, hvac_mode_comfort: str) -> None:
        self._hvac_mode_comfort = hvac_mode_comfort

    def is_applicable(self, snapshot: TempixSnapshot) -> bool:
        return snapshot.party_active

    def initial_target(self, snapshot: TempixSnapshot) -> TargetState:
        temp = snapshot.party_temperature
        if temp is None:
            temp = DEFAULT_PARTY_TEMP
        return TargetState(hvac_mode=self._hvac_mode_comfort, target_temperature=temp)

    def next_boundary(self, snapshot: TempixSnapshot) -> datetime | None:
        return snapshot.party_ends_at


class ForceGoal(GoalPolicy):
    """Force-Comfort / Force-Eco switches (V1: comfort checked first).

    Uses the ADJUSTMENT-effective temperatures like V1 (`eff_comfort =
    adjustment_comfort or comfort`) — adjustment VALUES apply even under
    force, only the adjustment MODE is outranked.
    """

    goal_class = GoalClass.FORCE

    def __init__(self, hvac_mode_comfort: str, hvac_mode_eco: str) -> None:
        self._hvac_mode_comfort = hvac_mode_comfort
        self._hvac_mode_eco = hvac_mode_eco

    def is_applicable(self, snapshot: TempixSnapshot) -> bool:
        return snapshot.force_comfort or snapshot.force_eco

    def _comfort_selected(self, snapshot: TempixSnapshot) -> bool:
        return snapshot.force_comfort  # V1: force_comfort wins over force_eco

    def is_comfort_branch(self, snapshot: TempixSnapshot) -> bool | None:
        return self._comfort_selected(snapshot)

    def initial_target(self, snapshot: TempixSnapshot) -> TargetState:
        if self._comfort_selected(snapshot):
            return TargetState(
                hvac_mode=self._hvac_mode_comfort,
                target_temperature=_effective_comfort(snapshot),
            )
        return TargetState(
            hvac_mode=self._hvac_mode_eco,
            target_temperature=_effective_eco(snapshot),
        )


class AdjustmentGoal(GoalPolicy):
    """JSON adjustment with mode != "auto" (forces comfort/eco/off).

    In "auto" mode this goal is NOT applicable — the adjustment values
    then act as SCHEDULE inputs.
    """

    goal_class = GoalClass.ADJUSTMENT

    def __init__(self, hvac_mode_comfort: str, hvac_mode_eco: str) -> None:
        self._hvac_mode_comfort = hvac_mode_comfort
        self._hvac_mode_eco = hvac_mode_eco

    def is_applicable(self, snapshot: TempixSnapshot) -> bool:
        return snapshot.adjustment_mode in ("comfort", "eco", "off")

    def is_comfort_branch(self, snapshot: TempixSnapshot) -> bool | None:
        if snapshot.adjustment_mode == "comfort":
            return True
        if snapshot.adjustment_mode == "eco":
            return False
        return None  # "off" has no comfort/eco notion

    def initial_target(self, snapshot: TempixSnapshot) -> TargetState:
        mode = snapshot.adjustment_mode
        if mode == "off":
            return TargetState(hvac_mode=HVAC_OFF)  # V1 hvac chain: entry "off" → off
        if mode == "comfort":
            return TargetState(
                hvac_mode=self._hvac_mode_comfort,
                target_temperature=_effective_comfort(snapshot),
            )
        return TargetState(
            hvac_mode=self._hvac_mode_eco,
            target_temperature=_effective_eco(snapshot),
        )


class SchedulePolicy(GoalPolicy):
    """Base goal: scheduler/calendar phase + away transformation +
    smart preconditioning. Always applicable (fallback).

    V1-parity notes:
    - ``schedule_comfort`` arrives resolved (holiday/use_day/guest folded
      in by the builder). ``None`` (uncertain) → safe eco default; the
      builder mirrors the uncertainty in ``SnapshotValidity`` (V1
      returned a blocking "uncertainty" instead — deliberate deviation,
      dispatch gating happens via validity in V2).
    - Away transforms the COMFORT branch only (V1: ``if set_comfort and
      is_away()``): "eco" → eco temp (mode stays comfort-mode, V1 hvac
      chain), "offset" → comfort ∓ offset (cooling-aware), "off" → OFF.
      V1 hvac chain applied away-"off" regardless of the branch — kept.
    - Preconditioning pulls the comfort phase forward; the builder
      computes the lookahead (learned rate + next transition), the
      policy consumes the flag.
    """

    goal_class = GoalClass.SCHEDULE

    def __init__(
        self,
        hvac_mode_comfort: str,
        hvac_mode_eco: str,
        away_behavior: str = "ignore",
        away_offset: float = 0.0,
        is_cooling: bool = False,
    ) -> None:
        self._hvac_mode_comfort = hvac_mode_comfort
        self._hvac_mode_eco = hvac_mode_eco
        self._away_behavior = away_behavior
        self._away_offset = away_offset
        self._is_cooling = is_cooling

    def is_applicable(self, snapshot: TempixSnapshot) -> bool:
        return True

    def _set_comfort(self, snapshot: TempixSnapshot) -> bool:
        set_comfort = snapshot.schedule_comfort is True  # None → eco-safe
        if not set_comfort and snapshot.preconditioning_active:
            set_comfort = True
        return set_comfort

    def is_comfort_branch(self, snapshot: TempixSnapshot) -> bool | None:
        return self._set_comfort(snapshot)

    def initial_target(self, snapshot: TempixSnapshot) -> TargetState:
        # V1 hvac chain: away_behavior "off" turns the device off
        # regardless of the comfort/eco branch — and, deliberately unlike V1,
        # also ahead of the calendar `hvac:` tag.
        if snapshot.away and self._away_behavior == "off":
            return TargetState(hvac_mode=HVAC_OFF)
        return _with_calendar_hvac_mode(snapshot, self._base_target(snapshot))

    def _base_target(self, snapshot: TempixSnapshot) -> TargetState:
        eff_comfort = _effective_comfort(snapshot)
        eff_eco = _effective_eco(snapshot)

        set_comfort = self._set_comfort(snapshot)
        if not set_comfort:
            return TargetState(hvac_mode=self._hvac_mode_eco, target_temperature=eff_eco)

        target = eff_comfort
        if snapshot.away:
            if self._away_behavior == "eco":
                target = eff_eco
            elif self._away_behavior == "offset" and target is not None:
                # Energy saving: heating → colder, cooling → warmer (V1).
                if self._is_cooling:
                    target = target + self._away_offset
                else:
                    target = target - self._away_offset
        return TargetState(hvac_mode=self._hvac_mode_comfort, target_temperature=target)

    def next_boundary(self, snapshot: TempixSnapshot) -> datetime | None:
        return snapshot.next_schedule_transition
