"""Parallel Validation Mode — rollout protection.

Runs the V2 pipeline each cycle. Post-flip V2's result drives the hardware
(sent by the coordinator via ``_drive_v2_if_active``); V1 runs as observer
and the result is diffed against it as a ``DecisionDiff``.

Hard rules:
- Both engines read the same per-cycle state snapshot (no timing artifacts
  from separate ``hass.states`` reads); V2 resolves most domain fields
  natively (``domain_resolvers``), a remainder is bridged from V1
  (``V2Bridge``).
- Pre-flip (``v1``): a fresh throwaway dry-run dispatcher per cycle, seeded
  with V1's previous generic offsets for deadband parity. Post-flip: the
  long-lived dispatcher plans the commands it then sends.
- Compared dimensions: ``target_temperature``, ``hvac_mode``, planned
  per-device commands, ``calibration_offset`` (generic), ``valve_position``
  and ``calibration_value`` (entity-based calibration).
- The entire run is exception-safe at the call site: a V2 failure must
  NEVER break the coordinator cycle; post-flip it is tracked as an
  active-engine failure.

Expected diffs are the deliberate V2 deviations (a domain rule beats the
Golden Master).
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from custom_components.tempix.calibration import plan_calibration
from custom_components.tempix.clock import EngineClock, SystemClock
from custom_components.tempix.const import (
    ACTIVE_ENGINE,
    AGGRESSIVE_MODE_CALIBRATION,
    CALIBRATION_MODE_NATIVE,
)
from custom_components.tempix.decision_engine import DecisionEngine
from custom_components.tempix.domain_resolvers import (
    resolve_active_adjustment,
    resolve_active_scheduler,
    resolve_adjustment_calibration,
    resolve_adjustment_comfort,
    resolve_adjustment_eco,
    resolve_adjustment_mode,
    resolve_anybody_home_or_proximity,
    resolve_calendar_comfort_active,
    resolve_calendar_hvac_mode,
    resolve_calendar_tags,
    resolve_effective_conditioning_rate,
    resolve_holiday_comfort_via_calendar,
    resolve_holiday_comfort_via_scheduler,
    resolve_holiday_target_day,
    resolve_holiday_today,
    resolve_is_season_mode,
    resolve_missing_entity_references,
    resolve_next_schedule_transition,
    resolve_optimum_start_target,
    resolve_party_mode,
    resolve_presence_active,
    resolve_schedule_comfort,
    resolve_scheduler_active,
    resolve_scheduler_defined,
    resolve_sunshine_offset,
    resolve_vacation_mode,
)
from custom_components.tempix.hardware_dispatcher import HardwareDispatcher, PlannerConfig
from custom_components.tempix.models.capability_map import (
    DeviceCapability,
    DeviceCapabilityMap,
)
from custom_components.tempix.models.command_ledger import LedgerView
from custom_components.tempix.models.decision_diff import DecisionDiff, FieldDiff
from custom_components.tempix.models.decision_result import DecisionResult
from custom_components.tempix.models.override_state import ActiveOverride
from custom_components.tempix.models.snapshot import OverrideDetectionResult
from custom_components.tempix.models.system_context import SystemContext
from custom_components.tempix.models.trigger_event import TriggerEvent
from custom_components.tempix.models.target_state import (
    HVAC_OFF,
    TEMPERATURELESS_HVAC_MODES,
)
from custom_components.tempix.policies.constraints import FrostConstraint, WindowConstraint
from custom_components.tempix.policies.goals import (
    AdjustmentGoal,
    ForceGoal,
    FrostGoal,
    LimingGoal,
    ManualOverridePolicy,
    PartyGoal,
    SchedulePolicy,
    SystemStateGoal,
    VacationGoal,
)
from custom_components.tempix.policies.modifiers import SunshineModifier
from custom_components.tempix.snapshot_builder import (
    SnapshotBuilder,
    parse_float,
    update_window_memory,
)
from custom_components.tempix.v2_bridge import EngineSnapshotStates, V2Bridge

if TYPE_CHECKING:
    from custom_components.tempix.coordinator import TempixCoordinator

_LOGGER = logging.getLogger(__name__)

# Comparison tolerance for temperatures/offsets (float artifacts).
_COMPARE_TOLERANCE = 0.05


class ParallelValidator:
    """Runs one V2 evaluation per V1 cycle and compares the results."""

    def __init__(
        self, coordinator: TempixCoordinator, clock: EngineClock | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._clock = clock if clock is not None else SystemClock()
        # Rohwerte des Kalender-`hvac:`-Tags, die schon gemeldet wurden — die
        # Warnung soll einmal pro Wert kommen, nicht in jedem Zyklus. Pro
        # Zyklus entsteht ein neuer Validator, darum liegt das Set am
        # Coordinator (AU-6); ohne dessen Set gilt es nur pro Instanz.
        shared = getattr(coordinator, "_warned_hvac_tags", None)
        self._warned_hvac_tags: set[str] = shared if isinstance(shared, set) else set()

    # ── public entry point ────────────────────────────────────────────────

    def run(
        self,
        *,
        v1_target: float | None,
        v1_hvac: str | None,
        v1_state_value: str,
        v1_changes: list[dict],
        v1_offsets_prev: dict[str, float] | None,
        v1_offsets_new: dict[str, float] | None,
        trigger_event: TriggerEvent | None = None,
        ledger: LedgerView | None = None,
    ) -> tuple[DecisionResult, DecisionDiff, OverrideDetectionResult | None]:
        """Evaluate V2 on V1's inputs and diff both results.

        ``trigger_event`` + ``ledger`` run the override detection against
        the REAL event path. The result rides on
        the snapshot's ``detected_override_event`` — which NO policy consumes,
        so passing them cannot change the V2 decision and parity is untouched.
        It is returned as a SHADOW signal (logged/diagnosed separately, kept
        out of the parity DecisionDiff).

        ``v1_target``/``v1_hvac`` may be ``None`` — post-flip V2 is
        called even when V1's chain is uncertain (V1 returns None) so V2 can
        drive independently (V2 never blindly freezes). V2 still
        produces a full ``result``; there is simply nothing to diff against, so
        an EMPTY diff is returned."""
        coord = self._coordinator
        engine = V2Bridge(coord.engine)
        window_temp = engine.resolve_window_open_temperature()

        snapshot, adj = self.publish_snapshot(trigger_event, ledger)
        v2_engine = DecisionEngine(
            self._assemble_goals(), self._assemble_reducers(window_temp),
        )
        result = v2_engine.calculate(snapshot)

        # Device Command Planning. Pre-flip: a THROWAWAY dispatcher seeded with
        # V1's previous offsets (deadband parity for the diff), dry-run, never
        # touching a live ledger. Post-flip: plan over the
        # LONG-LIVED dispatcher — it owns the ledger/CB and will send these
        # EXACT commands next (plan-once: the calibration deadband advances once
        # here), so the parity diff reflects what actually reaches the hardware.
        # V2 owns its own offset history then → no V1-offset seeding.
        #
        # Diff-semantics inversion: this is WHY the post-flip diff
        # is not blind. Once V2 drives, ``_compare`` weighs V1's shadow ``changes``
        # (computed but no longer applied) against ``result.hardware_commands`` —
        # and those are the LONG-LIVED dispatcher's plan, i.e. the commands V2
        # actually sends this cycle, not a V1-seeded throwaway that would always
        # match V1 and hide real divergence.
        if ACTIVE_ENGINE == "v2":
            dispatcher = coord._v2_dispatcher
            dispatcher.configure(
                self._capability_map(), self._planner_config(window_temp))
        else:
            dispatcher = HardwareDispatcher(
                EngineSnapshotStates(engine), self._clock, self._capability_map(),
                planner=self._planner_config(window_temp), dry_run=True,
            )
            dispatcher._last_generic_offsets.update(v1_offsets_prev or {})
        commands = dispatcher.plan(result.target_state, snapshot)
        # Valve positions as their own command type — diffed against V1's
        # ValvePositioner.
        v2_valves = dispatcher.plan_valves(result.target_state, snapshot)
        # Entity-based calibration as its own command type. Planned
        # standalone (not over the dispatcher) — the registry discovery needs the
        # REAL hass, which the throwaway/shadow dispatcher's states-view lacks;
        # calibration has no per-dispatcher planning state (unlike the generic
        # offset deadband), so the two-dispatcher split doesn't apply. Diffed
        # against V1's calculate_calibration; sent post-flip by dispatch_calibrations.
        v2_cals = plan_calibration(
            hass=coord.hass,
            get_state=engine.get_state,
            trvs=tuple(coord.config.trvs),
            room_temp=snapshot.room_temperature,
            target_temp=result.target_state.target_temperature,
            enabled=resolve_adjustment_calibration(adj),
            planner=self._planner_config(window_temp),
            now=self._clock.now(),
            # Dedupe-Set nur post-Flip durchreichen (wie `_last_generic_offsets`
            # oben ein bewusster Zugriff auf den Dispatcher-State): pre-Flip ist
            # `dispatcher` die Wegwerf-Instanz, deren Set jeden Zyklus leer
            # startet — die Warnung wuerde im Minutentakt wiederkehren, und V1s
            # CalibrationApplier gibt sie ohnehin schon aus.
            warned=(dispatcher._calib_no_entity_warned
                    if ACTIVE_ENGINE == "v2" else None),
        )
        result = replace(
            result, hardware_commands=commands,
            valve_commands=v2_valves, calibration_commands=v2_cals,
        )

        if v1_target is None or v1_hvac is None:
            # V1's target/hvac chain was uncertain (returned None).
            # There is nothing to diff against, but V2 produced a full decision
            # and — post-flip — drives off ``result`` (the caller gates the diff
            # processing on an empty diff). Empty diff, not a torrent of phantom
            # None-vs-value FieldDiffs.
            diff = DecisionDiff(
                snapshot_id=result.snapshot_id,
                timestamp=self._coordinator_now(),
                winner_old=v1_state_value,
                winner_new=result.goal_class.name if result.goal_class else "?",
                diffs=(),
            )
        else:
            diff = self._compare(
                result,
                v2_offsets=dispatcher._last_generic_offsets,
                v2_valves=v2_valves,
                v1_target=v1_target,
                v1_hvac=v1_hvac,
                v1_state_value=v1_state_value,
                v1_changes=v1_changes,
                v1_offsets=v1_offsets_new or {},
            )
        return result, diff, snapshot.detected_override_event

    # ── V1 → snapshot bridge (identical inputs) ───────────────────────────

    def publish_snapshot(
        self,
        trigger_event: TriggerEvent | None = None,
        ledger: LedgerView | None = None,
    ):
        """Build this cycle's snapshot and publish it for the entities.

        Sensors, binary sensors, the climate card and the status attributes
        read ``coordinator._v2_last_snapshot`` instead of calling the engine
        live. Also runs on its own in a pause (``manual_mode``), where
        ``run()`` is skipped: without that, every one of those displays
        froze for the whole pause (room temperature, "persons home", window
        …), measured live 2026-11-10. No decision, nothing is sent.
        """
        coord = self._coordinator
        snapshot, adj = self._build_snapshot(trigger_event, ledger)
        coord._v2_last_snapshot = snapshot
        # Remember this cycle's valid window states for the next cycle's
        # reboot-safe close-delay (unavailable readings are skipped). Kept
        # current through a pause too, so the first cycle after it compares
        # against the real window state, not the one from before the pause.
        update_window_memory(
            coord._v2_prev_window_valid, snapshot,
            tuple(coord.config.window_sensors or ()),
        )
        return snapshot, adj

    def _build_snapshot(
        self,
        trigger_event: TriggerEvent | None = None,
        ledger: LedgerView | None = None,
    ):
        coord = self._coordinator
        engine = V2Bridge(coord.engine)
        config = coord.config

        # Adjustments-Matching braucht Ortszeit, nicht UTC — Nutzer tragen
        # `time`/`days` als Wanduhrzeit ein (gleiche Lehre wie eine
        # fruehere Regression in engine_schedule.py). `self._clock` liefert
        # bewusst UTC; `dt_util.as_local` ist die 1:1-Entsprechung
        # zu V1s `dt_util.now()`. `is_presence_sensor_active` dagegen braucht
        # UTC direkt (V1: `datetime.now(timezone.utc)`, nicht `dt_util.now()`).
        utc_now = self._clock.now()
        local_now = dt_util.as_local(utc_now)
        calendar_tags = resolve_calendar_tags(
            config, engine.calendar_events, engine.get_state, engine.parse_dt,
            utc_now, local_now,
        )
        active_sched = resolve_active_scheduler(
            config, calendar_tags,
            get_state=engine.get_state, state_value=engine.state_value,
        )
        scheduler_name: str | None = None
        if active_sched:
            sched_state = engine.get_state(active_sched)
            if sched_state:
                name = sched_state.attributes.get("friendly_name")
                if isinstance(name, str):
                    scheduler_name = name

        adj = resolve_active_adjustment(
            config.adjustments, now=local_now,
            calendar_tags=calendar_tags, scheduler_name=scheduler_name,
        )
        entry_mode = resolve_adjustment_mode(adj)
        is_vacation, vacation_temp = resolve_vacation_mode(config, calendar_tags)
        calendar_hvac_mode, rejected_hvac_tag = resolve_calendar_hvac_mode(calendar_tags)
        if rejected_hvac_tag is not None and rejected_hvac_tag not in self._warned_hvac_tags:
            self._warned_hvac_tags.add(rejected_hvac_tag)
            _LOGGER.warning(
                "%s: Kalender-Tag 'hvac: %s' ist kein bekannter Modus "
                "(heat, cool, heat_cool, auto, off, dry, fan_only) — der Tag wird ignoriert",
                config.name, rejected_hvac_tag,
            )
        is_party, party_temp = resolve_party_mode(config)
        away = bool(engine.is_away())
        force_comfort = bool(engine.is_force_comfort_temp())
        force_eco = bool(engine.is_force_eco_temp())
        anybody_home = engine.is_anybody_home()
        comfort_temperature = engine.resolve_comfort_temperature()
        room_temperature = engine.resolve_room_temperature()

        # Holiday-Zweig von `is_scheduler_active`: beide Hälften jetzt nativ.
        holiday_today = resolve_holiday_today(
            config, engine.calendar_events, engine.get_state, engine.parse_dt, local_now,
        )
        holiday_comfort_override: bool | None = None
        holiday_target_day = resolve_holiday_target_day(config)
        if holiday_today and holiday_target_day:
            if config.calendar:
                holiday_comfort_override = resolve_holiday_comfort_via_calendar(
                    config, engine.calendar_events, engine.parse_dt, utc_now, local_now,
                )
            else:
                holiday_comfort_override = resolve_holiday_comfort_via_scheduler(
                    engine.schedule_slots, active_sched, holiday_target_day, local_now,
                )

        missing_entity_references = resolve_missing_entity_references(
            config, engine.entity_is_missing,
        )

        is_sunshine_active, sunshine_offset = resolve_sunshine_offset(config, engine.get_state)

        season_mode = resolve_is_season_mode(config, engine.state_value)

        next_schedule_transition = resolve_next_schedule_transition(
            config, engine.calendar_events, engine.get_state, engine.parse_dt,
            active_sched, utc_now, local_now,
            schedule_slots=engine.schedule_slots, holiday_today=holiday_today,
        )
        optimum_start_target = resolve_optimum_start_target(
            config, engine.calendar_events, engine.get_state, engine.parse_dt,
            next_schedule_transition, utc_now, local_now,
        )

        scheduler_defined = resolve_scheduler_defined(config, active_sched)
        scheduler_active = resolve_scheduler_active(
            active_sched, engine.get_state, holiday_comfort_override,
        )
        presence_sensor_defined = bool(config.presence_sensor)
        presence_active = resolve_presence_active(
            config, engine.state_value, engine.get_state, utc_now,
        )

        # `is_anybody_home_or_proximity`s Kombinationslogik ist nativ portiert
        # (`resolve_anybody_home_or_proximity`); die beiden Proximity-Leaf-
        # Werte bleiben V1-gebrückt (Entity-Registry-Auflösung, außerhalb
        # dieses Ports) — `anybody_home` ist ohnehin
        # gebrückt.
        person_or_proximity_defined = (
            len(config.persons) > 0 or bool(config.guest_mode) or bool(config.proximity_entity)
        )
        # proximity_arrived als benannte Variable statt inline, damit sie
        # zusätzlich an builder.build() durchgereicht werden kann
        # (binary_sensor.py liest sie jetzt vom Snapshot statt live).
        proximity_arrived = engine.check_proximity_arrived()
        cached_home_status: bool | None = None
        if person_or_proximity_defined:
            cached_home_status = resolve_anybody_home_or_proximity(
                anybody_home, proximity_arrived, engine.check_proximity_towards(),
            )

        conditioning_rate = resolve_effective_conditioning_rate(
            config.learned_climate_rate, engine.resolve_outside_temperature(), engine.is_cooling,
        )

        # `entity_is_missing()` (Entity-Registry) bleibt V1-gebrückt — hier nur
        # als vorberechnete "lebende Kalender"-Liste, kein Methodenaufruf.
        live_calendars = [
            cid for cid in (config.calendar or ()) if not engine.entity_is_missing(cid)
        ]
        calendar_comfort_active = bool(resolve_calendar_comfort_active(
            config, engine.calendar_events, engine.get_state, engine.parse_dt,
            live_calendars, utc_now, local_now,
            room_temperature, comfort_temperature, conditioning_rate,
            is_cooling=engine.is_cooling,
        ))

        comfort, smart_preconditioning_fired = resolve_schedule_comfort(
            config,
            entry_mode=entry_mode,
            party_active=bool(is_party),
            force_eco=force_eco,
            away=away,
            now=utc_now,
            calendar_comfort_active=calendar_comfort_active,
            scheduler_defined=scheduler_defined,
            scheduler_active=scheduler_active,
            presence_sensor_defined=presence_sensor_defined,
            presence_active=presence_active,
            person_or_proximity_defined=person_or_proximity_defined,
            cached_home_status=cached_home_status,
            optimum_start_target=optimum_start_target,
            room_temperature=room_temperature,
            comfort_temperature=comfort_temperature,
            is_cooling=engine.is_cooling,
            conditioning_rate=conditioning_rate,
        )

        builder = SnapshotBuilder(
            EngineSnapshotStates(engine), self._clock,
            device_entities=tuple(config.trvs),
            temp_sensor=config.temp_sensor or None,
            window_sensors=tuple(config.window_sensors or ()),
            # Ohne diese beiden Zeilen lief der
            # Builder mit Reaktionszeit 0 (Default), und `window_open_resolved`
            # — der Wert, den `WindowConstraint` liest — folgte dem
            # Sensor sofort. V2 sendete das Fenster-Kommando nach dem Debounce
            # (2 s) statt nach `window_reaction_open/close`; V1 hielt die
            # Verzoegerung korrekt ein (DecisionDiff, binary_sensor blieb off).
            window_reaction_open=config.window_reaction_open,
            window_reaction_close=config.window_reaction_close,
        )
        # Warum ist `schedule_comfort` unbekannt? Der Builder sieht nur das
        # `None`. Die Kette dahinter kennt nur die Engine — und ohne diese
        # Zeile meldet das `room_degraded`-Issue jede PRAESENZ-Unsicherheit als
        # "schedule state unknown" und schickt den Nutzer zum Kalender, während
        # ein Proximity-Device oder eine Person-Entity die Ursache war.
        # `get_uncertainty_reasons()` ist seiteneffektfrei (im Gegensatz zu
        # `is_anybody_home()`, das `_last_home_status` schreibt).
        comfort_unknown_reason = None
        if comfort is None:
            culprits = engine.get_uncertainty_reasons()
            if culprits:
                comfort_unknown_reason = (
                    f"presence/schedule input unresolvable "
                    f"({', '.join(culprits[:3])}) → eco-safe fallback applied"
                )

        snapshot = builder.build(
            "parallel_validation",
            comfort_unknown_reason=comfort_unknown_reason,
            # Real override detection. Shadow only:
            # detected_override_event has no policy consumer, decision unchanged.
            trigger_event=trigger_event,
            ledger=ledger if ledger is not None else LedgerView(),
            # Reboot-safe close-delay memory — feeds `window_open_resolved`,
            # which WindowConstraint and the dispatcher read.
            previous_window_states=coord._v2_prev_window_valid,
            window_close_marks=coord._v2_window_close_marks,
            window_open_phase_confirmed=coord._v2_window_open_phase_confirmed,
            # V1-resolved values override the builder's own resolution —
            # both engines must see identical inputs.
            automation_active=bool(engine.is_automation_active()),
            window_open=engine.is_window_open(),
            anybody_home=anybody_home,
            presence_active=presence_active,
            proximity_arrived=proximity_arrived,
            away=away,
            vacation_active=bool(is_vacation),
            vacation_temperature=vacation_temp,
            party_active=bool(is_party),
            party_temperature=party_temp,
            force_comfort=force_comfort,
            force_eco=force_eco,
            liming_active=bool(engine.is_liming_time()),
            schedule_comfort=comfort,
            next_schedule_transition=next_schedule_transition,
            adjustment_mode=entry_mode or "auto",
            adjustment_comfort=resolve_adjustment_comfort(adj, engine.float_state),
            adjustment_eco=resolve_adjustment_eco(adj, engine.float_state),
            comfort_temperature=comfort_temperature,
            eco_temperature=engine.resolve_eco_temperature(),
            sunshine_active=is_sunshine_active,
            sunshine_offset=sunshine_offset,
            season_mode=season_mode,
            calendar_tags=calendar_tags,
            calendar_hvac_mode=calendar_hvac_mode,
            holiday_today=bool(holiday_today),
            preconditioning_active=smart_preconditioning_fired,
            room_temperature=room_temperature,
            active_override=self._mirror_override(away),
            missing_entity_references=missing_entity_references,
        )
        return snapshot, adj

    def _mirror_override(self, away: bool) -> ActiveOverride | None:
        """Mirror V1's TemporaryManualOverride 1:1 — V1 owns the lifecycle
        (slot-change clear, 24h limit), so the mirror must never expire on
        its own: context matches the current state, TTL stays None."""
        v1_override = self._coordinator._temporary_manual_override
        if v1_override is None:
            return None
        return ActiveOverride(
            override_id="v1-mirror",
            hvac_mode=v1_override.hvac_mode,
            target_temperature=v1_override.target_temperature,
            created_at=v1_override.timestamp,
            expires_at=None,
            context=SystemContext(
                schedule_id=None, away=away, created_at=v1_override.timestamp,
            ),
        )

    # ── pipeline assembly (per cycle: resolved values are dynamic) ───────

    def _assemble_goals(self):
        config = self._coordinator.config
        engine = V2Bridge(self._coordinator.engine)
        comfort_mode = config.hvac_mode_comfort
        eco_mode = config.hvac_mode_eco
        return [
            SchedulePolicy(
                comfort_mode, eco_mode,
                away_behavior=config.away_behavior,
                away_offset=config.away_offset,
                is_cooling=engine.is_cooling,
            ),
            AdjustmentGoal(comfort_mode, eco_mode),
            ForceGoal(comfort_mode, eco_mode),
            PartyGoal(comfort_mode),
            VacationGoal(comfort_mode),
            LimingGoal(comfort_mode),
            ManualOverridePolicy(comfort_mode),
            SystemStateGoal(
                idle_temperature=config.idle_temperature,
                hvac_mode_comfort=comfort_mode,
            ),
            FrostGoal(
                config.frost_protection_enabled,
                config.frost_protection_temp,
                config.idle_temperature,
                comfort_mode,
                is_cooling=engine.is_cooling,
            ),
        ]

    def _assemble_reducers(self, window_temp: float):
        config = self._coordinator.config
        engine = V2Bridge(self._coordinator.engine)
        # NOTE: no room-level MaxTempConstraint here — V1 has no room-level
        # max clamp (only per-device min/max in planning); adding it would
        # produce permanent false diffs. Activation comes with V2-native config.
        return [
            SunshineModifier(config.sunshine_offset_value),
            FrostConstraint(
                config.frost_protection_enabled,
                config.frost_protection_temp,
                engine.is_cooling,
            ),
            WindowConstraint(
                window_temp,
                config.frost_protection_enabled,
                config.frost_protection_temp,
                config.hvac_mode_comfort,
                engine.is_cooling,
            ),
        ]

    def _planner_config(self, window_temp: float) -> PlannerConfig:
        config = self._coordinator.config
        engine = V2Bridge(self._coordinator.engine)
        return PlannerConfig(
            aggressive_mode=config.aggressive_mode_selector,
            aggressive_range=config.aggressive_range,
            aggressive_offset=config.aggressive_offset,
            calibration_mode=config.calibration_mode,
            calibration_delta=config.calibration_delta,
            generic_calibration_limit=config.generic_calibration_limit,
            calibration_step_size=config.calibration_step_size,
            calibration_keyword=config.calibration_keyword,
            calibration_timeout=config.calibration_timeout,
            min_instead_of_off=config.min_instead_of_off,
            off_if_above_room_temp=config.off_if_above_room_temp,
            hysteresis=config.hysteresis,
            is_cooling=engine.is_cooling,
            window_open_temp=window_temp,
            valve_mode=config.valve_mode,
            valve_diff=config.valve_diff,
            valve_max=config.valve_max,
            valve_min=config.valve_min,
            valve_step=config.valve_step,
            valve_keyword=config.valve_keyword,
            frost_enabled=config.frost_protection_enabled,
            frost_temp=config.frost_protection_temp,
        )

    def _capability_map(self) -> DeviceCapabilityMap:
        coord = self._coordinator
        snapshot_states = EngineSnapshotStates(V2Bridge(coord.engine))
        devices = []
        for trv_id in coord.config.trvs:
            state = snapshot_states.get(trv_id)
            attrs = state.attributes if state is not None else {}
            modes = attrs.get("hvac_modes") or ()
            raw_min_temp = parse_float(attrs.get("min_temp"))
            raw_max_temp = parse_float(attrs.get("max_temp"))
            raw_step = parse_float(attrs.get("target_temp_step"))
            devices.append(DeviceCapability(
                entity_id=trv_id,
                hvac_modes=frozenset(str(m).lower() for m in modes),
                min_temp=raw_min_temp if raw_min_temp is not None else 5.0,
                max_temp=raw_max_temp if raw_max_temp is not None else 30.0,
                target_temp_step=raw_step if raw_step is not None else 0.5,
            ))
        return DeviceCapabilityMap(devices=tuple(devices))

    # ── comparison ────────────────────────────────────────────────────────

    def _compare(
        self,
        result: DecisionResult,
        *,
        v2_offsets: dict[str, float],
        v2_valves: tuple = (),
        v1_target: float,
        v1_hvac: str,
        v1_state_value: str,
        v1_changes: list[dict],
        v1_offsets: dict[str, float],
    ) -> DecisionDiff:
        diffs: list[FieldDiff] = []
        v2 = result.target_state

        if v1_hvac != v2.hvac_mode:
            diffs.append(FieldDiff("hvac_mode", v1_hvac, v2.hvac_mode))

        both_off = v1_hvac == HVAC_OFF and v2.hvac_mode == HVAC_OFF
        # Bei temperaturlosen Modi (dry / fan_only) gibt V2
        # bewusst KEINEN Sollwert aus — das Geraet hat in diesen Modi keinen.
        # V1 kennt die Modi nirgends und rechnet stumpf die regulaere Kette
        # weiter, liefert also eine Zahl. Die zu vergleichen waere Dauerrauschen
        # ueber einen Wert, den die Hardware ignoriert. Der Modus-Vergleich eine
        # Zeile darueber bleibt scharf — eine echte Modus-Abweichung faellt also
        # weiterhin auf.
        v2_modeless = v2.hvac_mode in TEMPERATURELESS_HVAC_MODES
        if (not both_off and not v2_modeless
                and not self._temp_equal(v1_target, v2.target_temperature)):
            diffs.append(FieldDiff(
                "target_temperature", v1_target, v2.target_temperature))

        # per-device planned commands (V1 changes vs V2 planned commands)
        v1_map = {
            c["entity_id"]: (c.get("hvac_mode"), c.get("temperature"))
            for c in v1_changes
        }
        v2_map = {
            c.entity_id: (c.hvac_mode, c.target_temperature)
            for c in result.hardware_commands
        }
        for entity_id in sorted(set(v1_map) | set(v2_map)):
            old = v1_map.get(entity_id)
            new = v2_map.get(entity_id)
            if old is None or new is None:
                diffs.append(FieldDiff(f"{entity_id}.command", old, new))
                continue
            if old[0] != new[0]:
                diffs.append(FieldDiff(f"{entity_id}.hvac_mode", old[0], new[0]))
            # Geraete-Ebene: dieselbe Begruendung wie oben —
            # V2 sendet in dry/fan_only nur den Modus, V1 haengt eine
            # bedeutungslose Temperatur an (dieselbe Klasse wie die
            # Comet-DECT-Lektion bei "off").
            if new[0] in TEMPERATURELESS_HVAC_MODES:
                continue
            if not self._temp_equal(old[1], new[1]):
                diffs.append(FieldDiff(f"{entity_id}.temperature", old[1], new[1]))

        # calibration offsets. Only meaningful when the room decision actually
        # drives a temperature: V1 recomputes a generic offset even when the
        # room is off (its target is always a number), whereas V2's TargetState
        # carries None → plan() skips the generic block and keeps the seeded
        # value. Comparing those produces phantom offset diffs with no hardware
        # relevance, so gate the comparison on an active, temperature-driven
        # decision.
        if v2.is_temperature_driven and v2.target_temperature is not None:
            for entity_id in sorted(set(v1_offsets) | set(v2_offsets)):
                old_off = v1_offsets.get(entity_id)
                new_off = v2_offsets.get(entity_id)
                if not self._temp_equal(old_off, new_off):
                    diffs.append(FieldDiff(
                        f"{entity_id}.calibration_offset", old_off, new_off))

        # valve position. Only when
        # valve positioning is active — V1's ValvePositioner is gated the same
        # way (coordinator: `if valve_mode != "off"`), so both emit nothing
        # otherwise. V1 uses its own target; comparing the actually-driven
        # positions is intended.
        config = self._coordinator.config
        if config.valve_mode != "off":
            engine = V2Bridge(self._coordinator.engine)
            v2_pos_by_trv = {c.trv_id: c.position for c in v2_valves}
            for trv_id in config.trvs:
                v1_pos = (
                    engine.calculate_valve_position(trv_id, v1_target)
                    if v1_target is not None else None
                )
                v2_pos = v2_pos_by_trv.get(trv_id)
                if v1_pos != v2_pos:
                    diffs.append(FieldDiff(
                        f"{trv_id}.valve_position", v1_pos, v2_pos))

        # entity-based calibration value. Only the "native" /
        # aggressive-calibration path writes a calibration entity — generic
        # bakes into the target temp (compared via .calibration_offset above),
        # off does nothing. V1 recomputes per TRV via calculate_calibration;
        # V2's planned commands ride on the result. Both throttle-free here
        # (the throttle is a send-side concern), so the values line up 1:1.
        if (config.calibration_mode == CALIBRATION_MODE_NATIVE
                or config.aggressive_mode_selector == AGGRESSIVE_MODE_CALIBRATION):
            engine = V2Bridge(self._coordinator.engine)
            v2_val_by_trv = {c.trv_id: c.value for c in result.calibration_commands}
            for trv_id in config.trvs:
                v1_res = engine.calculate_calibration(trv_id)
                v1_val = v1_res.get("value") if v1_res else None
                v2_val = v2_val_by_trv.get(trv_id)
                if not self._temp_equal(v1_val, v2_val):
                    diffs.append(FieldDiff(
                        f"{trv_id}.calibration_value", v1_val, v2_val))

        return DecisionDiff(
            snapshot_id=result.snapshot_id,
            timestamp=self._coordinator_now(),
            winner_old=v1_state_value,
            winner_new=result.goal_class.name if result.goal_class else "?",
            diffs=tuple(diffs),
        )

    @staticmethod
    def _temp_equal(a: Any, b: Any) -> bool:
        if a is None or b is None:
            return a is None and b is None
        try:
            return abs(float(a) - float(b)) <= _COMPARE_TOLERANCE
        except (TypeError, ValueError):
            return a == b

    def _coordinator_now(self):
        return self._clock.now()
