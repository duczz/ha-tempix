"""DecisionEngine — the pure reducer core + concurrency shell.

Pure part (:meth:`calculate`): TempixSnapshot → DecisionResult.
1. Goal selection: highest applicable ``GoalClass`` wins (never
   registration order).
2. Fixed 4-phase reducer pipeline: reducers sorted once by
   ``ConstraintPriority``; every application is Copy-on-Write.
3. Final validation: invariant violations downgrade ``validity`` to
   UNSAFE (hardware safeguards react) instead of raising in production.
4. ``next_evaluation_at``: min() over all policies' ``next_boundary()``,
   past boundaries filtered.
5. Write-back isolation: an expired override is only NAMED
   (``expired_override_id``) — the engine never deletes. **The named id
   currently has NO consumer**: it rides on the
   ``DecisionResult`` and reaches diagnostics, nothing acts on it. V1 owns
   the override lifecycle for now — see ``ManualOverridePolicy``.

Concurrency shell (:meth:`submit`): single-slot latest-state
mailbox per room — a newer snapshot simply overwrites the pending slot,
nothing queues up. The generation counter discards in-flight results
after :meth:`invalidate` (pause toggle & other config switches) so they
can never race into the dispatcher.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime

from custom_components.tempix.models.decision_result import DecisionResult
from custom_components.tempix.models.decision_trace import DecisionTrace
from custom_components.tempix.models.pipeline_state import PipelineState
from custom_components.tempix.models.snapshot import SnapshotValidity, TempixSnapshot
from custom_components.tempix.models.target_state import TargetState
from custom_components.tempix.policies.base_policy import GoalPolicy, _ReducerPolicy
from custom_components.tempix.rate_limiter import EvaluationRateLimiter

_LOGGER = logging.getLogger(__name__)

ResultCallback = Callable[[DecisionResult], Awaitable[None] | None]


class DecisionEngine:
    """Pure evaluation pipeline with a single-slot mailbox on top."""

    def __init__(
        self,
        goals: Sequence[GoalPolicy],
        reducers: Sequence[_ReducerPolicy] = (),
        *,
        rate_limiter: EvaluationRateLimiter | None = None,
        on_result: ResultCallback | None = None,
    ) -> None:
        if not goals:
            raise ValueError("DecisionEngine needs at least one GoalPolicy")
        self._goals = tuple(goals)
        # Sorted ONCE by priority — registration order is irrelevant.
        self._reducers = tuple(sorted(reducers, key=lambda r: r.priority))
        self._rate_limiter = rate_limiter
        self._on_result = on_result
        # Mailbox + generation model
        self._pending: TempixSnapshot | None = None
        self._running = False
        self._generation = 0

    # ── pure core ─────────────────────────────────────────────────────────

    def calculate(self, snapshot: TempixSnapshot) -> DecisionResult:
        """Pure reducer: snapshot in, decision out. No I/O, no clock."""
        applicable = self._applicable_goals(snapshot)
        goal = applicable[0]
        target = goal.initial_target(snapshot)
        if not goal.pins_hvac_mode(snapshot):
            target = self._merge_fallback_mode(snapshot, target, applicable[1:])
        if target.is_temperature_driven and target.target_temperature is None:
            target = self._merge_fallback_temperature(snapshot, target, applicable[1:])
        state = PipelineState(
            target=target,
            goal_class=goal.goal_class,
            comfort_active=goal.is_comfort_branch(snapshot),
        )

        for reducer in self._reducers:
            if reducer.is_applicable(snapshot, state):
                state = reducer.apply(snapshot, state)

        validity = self._validate(snapshot, state)

        return DecisionResult(
            target_state=state.target,
            goal_class=goal.goal_class,
            trace=DecisionTrace(entries=state.entries),
            snapshot_id=snapshot.snapshot_id,
            validity=validity,
            validity_reasons=snapshot.validity_reasons,
            expired_override_id=self._expired_override_id(snapshot),
            next_evaluation_at=self._next_evaluation_at(snapshot),
        )

    def _applicable_goals(self, snapshot: TempixSnapshot) -> list[GoalPolicy]:
        """Applicable goals, highest GoalClass first (winner = index 0)."""
        applicable = sorted(
            (g for g in self._goals if g.is_applicable(snapshot)),
            key=lambda g: g.goal_class,
            reverse=True,
        )
        if not applicable:
            # SchedulePolicy is the always-applicable fallback; reaching
            # this means the pipeline was assembled without it.
            raise RuntimeError("no applicable GoalPolicy — missing fallback goal")
        return applicable

    def _merge_fallback_temperature(
        self,
        snapshot: TempixSnapshot,
        target: TargetState,
        fallbacks: list[GoalPolicy],
    ) -> TargetState:
        """V1 parity for mode-only overrides on temperature-driven modes.

        A manual mode change without a temperature (real case:
        AC turned off→cool via the card; the temperature ack arrives cycles
        later — "mode-ack-vor-temp-ack") yields a winning goal whose target
        is e.g. ``cool``/None. V1 takes the mode from the override and lets
        the TEMPERATURE continue through the regular chain. Mirror that:
        keep the winner's mode, take the temperature from the next-highest
        applicable goal that has one (party beats schedule etc. — exactly
        V1's priority chain). If no fallback carries a temperature, the
        target stays None and the final validation degrades to UNSAFE as
        before — that safety net is unchanged.
        """
        for goal in fallbacks:
            fallback = goal.initial_target(snapshot)
            if fallback.target_temperature is not None:
                _LOGGER.debug(
                    "mode-only target '%s' (snapshot %s): temperature %.1f "
                    "merged from fallback goal %s",
                    target.hvac_mode, snapshot.snapshot_id,
                    fallback.target_temperature, goal.goal_class.name,
                )
                return TargetState(
                    hvac_mode=target.hvac_mode,
                    target_temperature=fallback.target_temperature,
                )
        return target

    def _merge_fallback_mode(
        self,
        snapshot: TempixSnapshot,
        target: TargetState,
        fallbacks: list[GoalPolicy],
    ) -> TargetState:
        """V1 parity for temp-only overrides — mirror of the temperature
        merge above.

        A temperature change without a mode (card / HA slider:
        ``climate.async_set_temperature`` passes ``new_hvac=None``) keeps
        the winner's TEMPERATURE and takes the MODE from the next-highest
        applicable goal — i.e. the mode V2 would run without the override.
        V1's ``calculate_hvac_mode`` does the same: it only short-circuits
        on a set override mode. SchedulePolicy is always applicable, so a
        fallback normally exists; without one the target stays unchanged.
        """
        for goal in fallbacks:
            if not goal.pins_hvac_mode(snapshot):
                continue
            fallback = goal.initial_target(snapshot)
            _LOGGER.debug(
                "temp-only target %s (snapshot %s): mode '%s' merged from "
                "fallback goal %s",
                target.target_temperature, snapshot.snapshot_id,
                fallback.hvac_mode, goal.goal_class.name,
            )
            return TargetState(
                hvac_mode=fallback.hvac_mode,
                target_temperature=target.target_temperature,
            )
        return target

    def _validate(self, snapshot: TempixSnapshot, state: PipelineState) -> SnapshotValidity:
        """Final reducer step: assert invariants, downgrade on violation."""
        target = state.target
        if target.is_temperature_driven and target.target_temperature is None:
            _LOGGER.warning(
                "invariant violated (snapshot %s): temperature-driven mode "
                "'%s' without target temperature — validity → UNSAFE",
                snapshot.snapshot_id, target.hvac_mode,
            )
            return SnapshotValidity.UNSAFE
        return snapshot.validity

    def _expired_override_id(self, snapshot: TempixSnapshot) -> str | None:
        for goal in self._goals:
            expired = getattr(goal, "expired_override_id", None)
            if callable(expired):
                override_id = expired(snapshot)
                if override_id is not None:
                    return override_id
        return None

    def _next_evaluation_at(self, snapshot: TempixSnapshot) -> datetime | None:
        """min() over every policy's next_boundary, past values filtered."""
        boundaries: list[datetime] = []
        for policy in (*self._goals, *self._reducers):
            boundary = policy.next_boundary(snapshot)
            if boundary is not None and boundary > snapshot.timestamp:
                boundaries.append(boundary)
        return min(boundaries, default=None)

    # ── concurrency shell ─────────────────────────────────────────────────

    def invalidate(self) -> None:
        """Discard all in-flight evaluations (pause toggle, config switch)."""
        self._generation += 1

    async def submit(self, snapshot: TempixSnapshot) -> DecisionResult | None:
        """Single-slot mailbox: newest snapshot wins, nothing queues.

        Returns the last delivered result when this call drained the
        mailbox, or ``None`` when another ``submit`` is already running
        (that runner picks the new snapshot up from the slot).
        """
        self._pending = snapshot
        if self._running:
            return None

        self._running = True
        last_result: DecisionResult | None = None
        try:
            while self._pending is not None:
                # Capture the generation BEFORE the rate-limiter await.
                # invalidate() (pause toggle) bumps _generation; if it fires
                # while we wait on the rate limiter, the post-await check
                # below must see the change. Capturing after the await would
                # read the already-bumped value and never discard.
                generation = self._generation
                if self._rate_limiter is not None:
                    await self._rate_limiter.acquire()
                # Take the slot AFTER the throttle wait — whatever is
                # newest by now is what gets evaluated (latest-state).
                pending, self._pending = self._pending, None
                result = self.calculate(pending)
                if generation != self._generation:
                    # Config toggled since we started this cycle → result is
                    # stale and must never reach the dispatcher.
                    continue
                if self._on_result is not None:
                    callback_result = self._on_result(result)
                    if callback_result is not None:
                        await callback_result
                last_result = result
        finally:
            self._running = False
        return last_result
