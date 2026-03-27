"""
Tempix – Coordinator.

Manages state listeners, timers, and applies changes to TRVs.
Maps every blueprint trigger to an HA event listener.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from datetime import timedelta, datetime, UTC

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant, Event, callback
from homeassistant.helpers import entity_registry as er_helper
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_call_later,
    async_track_time_interval,
)

from custom_components.tempix.const import (
    DOMAIN,
    CONF_LEARNED_HEATING_RATE,
    CALIBRATION_MODE_OFF,
    AGGRESSIVE_MODE_CALIBRATION,
    SCHEDULING_MODE_CALENDAR,
    HeatingState,
)
from custom_components.tempix.config_model import TempixConfig
from custom_components.tempix.engine import TempixEngine
from custom_components.tempix.coordinator_scene import SceneManager
from custom_components.tempix.coordinator_appliers import (
    CalibrationApplier,
    ValvePositioner,
    safe_service_call,
)
from custom_components.tempix.coordinator_learning import HeatingRateLearner

_LOGGER = logging.getLogger(__name__)

# Diagnostic-only attributes – state changes in these are ignored (M-5)
_DIAG_ATTRS: frozenset[str] = frozenset({
    "battery_level", "linkquality", "rssi", "battery", "volt", "pressure"
})

class TempixCoordinator:
    """State management and TRV control – mirrors blueprint actions."""

    def __init__(
        self, hass: HomeAssistant, config: TempixConfig, engine: TempixEngine, entry_id: str,
    ) -> None:
        self.hass = hass
        self.config = config
        self.engine = engine
        self.entry_id = entry_id

        self._ha_started_listener: Any = None
        self._listeners: list = []
        self._timers: list = []
        self._trigger_timer: Any = None
        self._heartbeat_timer: Any = None
        self._reeval_timer: Any = None
        self._calendar_timer: Any = None
        self._update_lock = asyncio.Lock()
        self._updates_enabled = False
        self._ready_time: datetime | None = None
        self._last_update: dict | None = None
        self._last_update_time: datetime | None = None
        self._uncertainty_start_time: datetime | None = None
        self._last_calendar_fetch: datetime | None = None

        # Public state for sensors
        self.current_hvac: str = "off"
        self.current_temperature: float | None = None
        self.current_reason: str = ""
        self.current_state: HeatingState = HeatingState.PAUSED
        self.last_changes: list[dict] = []
        self.last_calibrations: dict[str, float] = {}
        self.last_generic_offsets: dict[str, float] = {}

        self._entity_callbacks: list = []
        self._option_timers: dict[str, Any] = {}
        self._background_tasks: set[asyncio.Task] = set()

        # ── helper objects ────────────────────────────────────────────────
        self._scene_manager = SceneManager(
            hass,
            config.trvs,
            config.action_delay.total_seconds(),
            config.name,
        )
        self._calib_applier = CalibrationApplier(hass, config, engine, config.name)
        self._valve_positioner = ValvePositioner(hass, config, engine, config.name)
        self._rate_learner = HeatingRateLearner(hass, config, engine, entry_id)
        self._prev_party: bool = False

    def debug_log(self, msg: str) -> None:
        """Log debug message with coordinator prefix."""
        if self.config.debug_mode:
            _LOGGER.info("TPX Coord [%s]: %s", self.config.name, msg)
        else:
            _LOGGER.debug("TPX Coord [%s]: %s", self.config.name, msg)

    def _create_tracked_task(self, coro) -> asyncio.Task:
        """Create a fire-and-forget task and track it to prevent leaks on unload."""
        task = self.hass.async_create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def async_set_temporary_option(
        self, key: str, value: bool, duration_mins: int | None = None
    ) -> None:
        """Set an option temporarily or permanently and handle timers."""
        # P1 Fix (F-FM-1): Input validation
        if not self._validate_option(key, value):
            _LOGGER.error("%s: Invalid option value for %s: %s", self.config.name, key, value)
            return

        # Cancel existing timer for this key if any
        if key in self._option_timers:
            self._option_timers[key]()
            del self._option_timers[key]

        # Update the option in the config entry
        entry = self.hass.config_entries.async_get_entry(
            next(
                entry_id
                for entry_id, data in self.hass.data[DOMAIN].items()
                if data.get("coordinator") == self
            )
        )
        if not entry:
            return

        new_options = dict(entry.options)
        new_options[key] = value
        self.hass.config_entries.async_update_entry(entry, options=new_options)

        # If we enable it with a duration, start a timer to disable it
        if value and duration_mins and duration_mins > 0:
            self.debug_log(f"Setting temporary option {key}={value} for {duration_mins} mins")

            # Cancel existing timer if one is running for this option
            if key in self._option_timers and self._option_timers[key]:
                self._option_timers[key]()

            @callback
            def _timer_finished(_now):
                self.debug_log(f"Temporary option {key} expired. Resetting to False.")
                self._option_timers.pop(key, None)
                self._create_tracked_task(
                    self.async_set_temporary_option(key, False)
                )

            self._option_timers[key] = async_call_later(
                self.hass, duration_mins * 60, _timer_finished
            )
        else:
            self.debug_log(f"Setting option {key}={value} (no timer)")

    def async_add_listener(self, callback_func):
        """Register an entity callback for state updates."""
        self._entity_callbacks.append(callback_func)

        def remove_listener():
            if callback_func in self._entity_callbacks:
                self._entity_callbacks.remove(callback_func)

        return remove_listener

    def _call_listeners(self):
        """Notify all registered listeners (entities) of a change."""
        for callback_func in self._entity_callbacks:
            try:
                callback_func()
            except Exception as exc:
                _LOGGER.error("Error in entity callback: %s", exc, exc_info=True)

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def async_setup(self) -> None:
        """Register listeners matching every blueprint trigger."""
        self.debug_log(f"Starting coordinator setup (ID: {self.entry_id[:6]})")
        await self._scene_manager.async_load()
        # M-C: Discard any window scene saved before this boot. TRVs may have been
        # manually changed during the downtime; restoring stale states would undo that.
        # If the window is still open after restart, Tempix will set eco immediately anyway.
        self._scene_manager.clear("window")

        if self.hass.state == CoreState.running:
            self.debug_log("HA already running, starting coordinator immediately")
            await self._start_coordinator()
        else:
            self.debug_log("WAiting for Home Assistant to start...")
            # P3 Fix (F-LOOP-1): Capture bus listener unsubscriber
            self._ha_started_listener = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self._on_ha_started
            )

    async def _on_ha_started(self, _event: Event) -> None:
        self._ha_started_listener = None
        await self._start_coordinator()

    async def _start_coordinator(self) -> None:
        """Start the coordinator without delays."""
        if self._updates_enabled:
            self.debug_log("Coordinator already started, skipping redundant start.")
            return

        self._register_listeners()
        self._updates_enabled = True
        self._ready_time = datetime.now(UTC)
        self.debug_log(f"Coordinator ready at {self._ready_time}")

        # ── Calendar Periodic Scan (v1.4.0) ──
        if self.config.scheduling_mode == SCHEDULING_MODE_CALENDAR:
            self._calendar_timer = async_track_time_interval(
                self.hass, self.async_update, timedelta(minutes=self.config.calendar_scan_interval)
            )

        await self.async_update()

    async def _async_fetch_calendar_events(self) -> None:
        """Fetch agenda for all configured calendars for deep scanning."""
        calendars = self.config.calendar
        if not calendars:
            return

        now = datetime.now(UTC)
        start = now - timedelta(days=1)
        end = now + timedelta(days=7)

        all_events: dict[str, list[dict[str, Any]]] = {}

        for cal_id in calendars:
            if not self.hass.states.get(cal_id):
                _LOGGER.debug("Calendar entity %s not available, skipping fetch", cal_id)
                continue
            try:
                self.debug_log(f"Fetching calendar events for {cal_id}")
                response = await safe_service_call(
                    self.hass, self.config.name,
                    "calendar", "get_events",
                    {
                        "entity_id": cal_id,
                        "start_date_time": start.isoformat(),
                        "end_date_time": end.isoformat(),
                    },
                    return_response=True,
                )

                if response and cal_id in response:
                    events = response[cal_id].get("events", [])
                    all_events[cal_id] = events
                    self.debug_log(f"Fetched {len(events)} events for {cal_id}")
                elif response and "events" in response:
                    events = response.get("events", [])
                    all_events[cal_id] = events
            except Exception as exc:
                _LOGGER.error("Error fetching calendar events for %s: %s", cal_id, exc, exc_info=True)

        self.engine.set_calendar_events(all_events)
        self._last_calendar_fetch = now

    def _register_listeners(self) -> None:
        """Track entities for state changes = blueprint triggers."""
        tracked: list[str] = []

        def add_if_entity(eid: Any) -> None:
            if isinstance(eid, str) and "." in eid:
                tracked.append(eid)
            elif isinstance(eid, list):
                for item in eid:
                    add_if_entity(item)

        # TRVs
        add_if_entity(self.config.trvs)

        # Temperature sensor
        add_if_entity(self.config.temp_sensor)

        # Schedulers
        add_if_entity(self.config.schedulers)
        add_if_entity(self.config.scheduler_selector)

        # Persons
        add_if_entity(self.config.persons)

        # Single entities or lists
        for val in (
            self.config.guest_mode,
            self.config.presence_sensor,
            self.config.scheduler_presence,
            self.config.season_mode_entity,
            self.config.outside_temp_sensor,
            self.config.aggressive_mode_selector,
        ):
            add_if_entity(val)

        # Calendars (v1.4.0)
        add_if_entity(self.config.calendar)

        # Proximity Device Entities
        prox_device_id = self.config.proximity_entity
        if prox_device_id:
            try:
                ent_reg = er_helper.async_get(self.hass)
                for entry in er_helper.async_entries_for_device(ent_reg, prox_device_id):
                    tracked.append(entry.entity_id)
            except Exception as exc:
                self.debug_log(f"Failed to track proximity device entities: {exc}")

        # Windows
        add_if_entity(self.config.window_sensors)

        # Deduplicate and filter non-string values (e.g. booleans)
        tracked = list(set(eid for eid in tracked if isinstance(eid, str)))

        if tracked:
            unsub = async_track_state_change_event(
                self.hass, tracked, self._on_state_change
            )
            self._listeners.append(unsub)

        self.debug_log(f"tracking {len(tracked)} entities")

        # Periodic heartbeat (every 1 min)
        if self._heartbeat_timer:
            self._heartbeat_timer()

        self._heartbeat_timer = async_call_later(
            self.hass, 60, self._on_heartbeat
        )

    @callback
    def _on_state_change(self, event: Event) -> None:
        """Handle any tracked entity change."""
        if not self._updates_enabled:
            return

        entity_id = event.data.get("entity_id", "")
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")

        # Ignore identical state
        if old_state and new_state and old_state.state == new_state.state:
            # Whitelist for essential attribute changes
            # 1. TRV physical/UI temperature changes
            if entity_id in self.config.trvs:
                old_temp = old_state.attributes.get("temperature")
                new_temp = new_state.attributes.get("temperature")
                if old_temp != new_temp:
                    self._create_tracked_task(
                        self._handle_trv_temp_change(entity_id, old_temp, new_temp)
                    )
                    return

            # 2. Weather/Temperature sensor attribute changes
            changed_attrs = set(new_state.attributes.keys()) - set(old_state.attributes.keys())
            for attr in new_state.attributes:
                if old_state.attributes.get(attr) != new_state.attributes.get(attr):
                    changed_attrs.add(attr)

            # If ONLY diagnostic attributes changed, ignore.
            if changed_attrs and changed_attrs.issubset(_DIAG_ATTRS):
                return

            # If no important attribute changed and state is same, ignore.
            if not changed_attrs:
                return

        delta = self.config.action_delay

        # Cancel previous trigger if still pending (Debouncing)
        if self._trigger_timer:
            self._trigger_timer()

        self._trigger_timer = async_call_later(
            self.hass, delta.total_seconds(), self._delayed_update
        )

    @callback
    def _on_heartbeat(self, _now) -> None:
        """Periodic heartbeat to ensure state is in sync."""
        self._create_tracked_task(self.async_update())

    @callback
    def _delayed_update(self, _now) -> None:
        self._create_tracked_task(self.async_update())

    async def _handle_trv_temp_change(
        self, _entity_id: str, _old_temp, new_temp,
    ) -> None:
        """Handle UI or physical temperature change on a TRV."""
        ui_change = self.config.ui_change
        physical_change = self.config.physical_change

        if not ui_change and not physical_change:
            return

        if new_temp is not None:
            reset_data = self.engine.calculate_reset_data(
                is_physical_or_ui=True,
                change_temperature=float(new_temp),
            )
            for entry in reset_data:
                try:
                    await safe_service_call(
                        self.hass, self.config.name,
                        "input_number", "set_value",
                        {"entity_id": entry["entity"], "value": entry["temp"]},
                    )
                except Exception as exc:
                    _LOGGER.warning("%s: failed to sync temp: %s", self.config.name, exc)

    async def async_unload(self) -> None:
        """Clean up listeners and timers."""
        self.debug_log("Unloading coordinator - cleaning up listeners and timers")
        if self._ha_started_listener:
            self._ha_started_listener()
            self._ha_started_listener = None

        for unsub in self._listeners:
            unsub()
        self._listeners.clear()

        # Cancel all active timers
        if self._trigger_timer:
            self._trigger_timer()
            self._trigger_timer = None

        if self._heartbeat_timer:
            self._heartbeat_timer()
            self._heartbeat_timer = None

        if self._reeval_timer:
            self._reeval_timer()
            self._reeval_timer = None

        if self._calendar_timer:
            self._calendar_timer()
            self._calendar_timer = None

        # Cancel all option-specific timers
        for cancel_timer in self._option_timers.values():
            if cancel_timer:
                cancel_timer()
        self._option_timers.clear()

        # Cancel tracked background tasks (H-2)
        for task in list(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()

    # ── main update loop ─────────────────────────────────────────────────────

    async def async_update(self, _now: datetime | None = None) -> None:
        """Full recalculate + apply.  Blueprint: main action block."""
        if not self._updates_enabled:
            return

        async with self._update_lock:
            # ── Calendar Agenda Fetch (v1.6.0) ──
            if self.config.scheduling_mode == SCHEDULING_MODE_CALENDAR:
                now_dt = datetime.now(UTC)
                if (not self._last_calendar_fetch or
                    (now_dt - self._last_calendar_fetch).total_seconds() > (self.config.calendar_scan_interval * 60 - 30)):
                    try:
                        await self._async_fetch_calendar_events()
                    except Exception as exc:
                        _LOGGER.warning(
                            "%s: Calendar fetch failed, using last cached data: %s",
                            self.config.name, exc,
                        )

            # R3: Trigger-Throttling (Audit 1.1)
            now = datetime.now(UTC)
            if self._last_update_time and (now - self._last_update_time).total_seconds() < 2:
                if self.config.log_level == "debug":
                    _LOGGER.debug("%s: Skipping redundant update (cooldown active)", self.config.name)
                return

            if self._heartbeat_timer:
                self._heartbeat_timer()

            self._heartbeat_timer = async_call_later(
                self.hass, 60, self._on_heartbeat
            )

            try:
                success = await self._do_update()
                self._last_update_time = datetime.now(UTC)

                if success:
                    self._uncertainty_start_time = None
                else:
                    # P2 Fix (F-SS-1): schedule a faster retry (30s) on uncertainty
                    if not self._uncertainty_start_time:
                        self._uncertainty_start_time = datetime.now(UTC)

                    elapsed = (datetime.now(UTC) - self._uncertainty_start_time).total_seconds()

                    if elapsed < 300:  # 5 minutes
                        if self._reeval_timer:
                            self._reeval_timer()
                        self.debug_log(
                            f"Update paused due to uncertainty (elapsed: {elapsed:.0f}s). "
                            "Scheduling retry in 30s."
                        )
                        self._reeval_timer = async_call_later(
                            self.hass, 30, self._delayed_update
                        )
                    else:
                        self.debug_log("Uncertainty timeout (5min) reached. Falling back to default heartbeat.")

            except Exception as exc:
                _LOGGER.error("%s: update error: %s", self.config.name, exc, exc_info=True)

    async def async_request_refresh(self) -> None:
        """Alias for async_update to support DataUpdateCoordinator-like interface."""
        await self.async_update()

    def _validate_option(self, key: str, value: Any) -> bool:
        """P1 Fix (F-FM-1): Runtime validation for config options."""
        if key == CONF_LEARNED_HEATING_RATE:
            if not isinstance(value, (int, float)):
                return False
            if value <= 0:
                return False
        return True

    async def _do_update(self) -> bool:
        name = self.config.name
        log = self.config.log_level

        # ── 0. Create State Snapshot (P2 2.1) ──────────────────────────
        snapshot = {}
        for eid in self._get_snapshot_entities():
            state = self.hass.states.get(eid)
            if state:
                snapshot[eid] = state
        self.engine.set_state_snapshot(snapshot)
        self.engine._startup_time = self._ready_time

        # ── 1. Engine calculations ───────────────────────────────────────
        adj = self.engine.get_active_adjustment()
        entry_mode = self.engine.get_adjustment_mode(adj)

        set_comfort = self.engine.should_set_comfort(entry_mode)

        active = self.engine.is_automation_active()

        hvac_mode = self.engine.calculate_hvac_mode(_set_comfort=set_comfort)
        target_temp = self.engine.calculate_target_temperature(_set_comfort=set_comfort)

        changes, gen_offsets = self.engine.calculate_changes(
            self.last_generic_offsets,
            _target_temp=target_temp,
            _hvac_mode=hvac_mode,
        )

        self.current_hvac = hvac_mode
        self.current_temperature = target_temp
        self.current_state = self.engine.determine_heating_state()
        self.current_reason = self._build_reason(set_comfort)

        self.last_changes = changes
        self.last_generic_offsets = gen_offsets

        if log == "debug" or self.config.debug_mode:
            self.debug_log(
                f"active={active} hvac={hvac_mode} target={target_temp:.1f} changes={len(changes)} reason={self.current_reason}"
            )

        # Early return if uncertain
        if target_temp is None or hvac_mode is None:
            return False

        # ── Manual Override / Pause (hands-off) ──────────────────────────
        if self.config.manual_override_pause:
            self._call_listeners()
            return True

        # ── 1.2 Heating Rate Learning (v1.5.0) ───────────────────────────
        if self.config.optimum_start:
            await self._rate_learner.update(target_temp, hvac_mode)

        # ── 1.1 Notify UI early (Phase 47) ───────────────────────────────
        self._call_listeners()

        # ── 2. Window scene management ───────────────────────────────────
        window_open = self.engine.is_window_open()
        legacy_window = self.config.window_legacy_restore

        async def _scene_service_caller(domain, service, service_data):
            return await safe_service_call(self.hass, name, domain, service, service_data)

        is_party, _ = self.engine.check_party_mode()

        # M-E: Party ended while window still open → scene was saved with party temps,
        # discard it so closing the window won't restore stale party temperatures.
        if self._prev_party and not is_party and window_open and self._scene_manager.has_scene("window"):
            self._scene_manager.clear("window")
            self.debug_log("Party ended while window open – window scene discarded")
        self._prev_party = is_party

        if window_open and legacy_window and not self._scene_manager.has_scene("window"):
            await self._scene_manager.save("window")
        elif not window_open and self._scene_manager.has_scene("window"):
            await self._scene_manager.restore("window", _scene_service_caller)

        # ── 4. Apply changes (Throttled Parallel - v1.5.1) ───────────────
        secs = self.config.action_delay.total_seconds()

        semaphore = asyncio.Semaphore(3)

        async def _apply_with_semaphore(change: dict[str, Any]) -> None:
            async with semaphore:
                await self._async_apply_trv_change(change, secs)

        if changes:
            tasks = [
                self.hass.async_create_task(_apply_with_semaphore(change))
                for change in changes
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, BaseException):
                    _LOGGER.error(
                        "%s: TRV change failed for %s: %s",
                        self.config.name,
                        changes[i].get("entity_id", "unknown"),
                        result,
                    )

        # ── 5. Calibration ───────────────────────────────────────────────
        aggressive_calib_active = self.config.aggressive_mode_selector == AGGRESSIVE_MODE_CALIBRATION

        if self.config.calibration_mode != CALIBRATION_MODE_OFF or aggressive_calib_active:
            # R2: Verify window state again right before calibration
            if self.engine.is_window_open():
                if self.config.log_level == "debug":
                    _LOGGER.debug("%s: Calibration skipped - Window opened during update cycle", name)
                return True
            new_cals = await self._calib_applier.apply()
            self.last_calibrations.update(new_cals)

        # ── 6. Valve positioning ─────────────────────────────────────────
        if self.config.valve_mode != "off":
            await self._valve_positioner.apply(target_temp)

        # ── 7. Fire event for external automations ───────────────────────
        self.hass.bus.async_fire(f"{DOMAIN}_update", {
            "name": name,
            "hvac_mode": hvac_mode,
            "temperature": target_temp,
            "reason": self.current_reason,
        })

        # ── 8. Dynamic Re-evaluation (Phase 50) ──────────────────────────
        if self._reeval_timer:
            self._reeval_timer()
            self._reeval_timer = None

        next_event = self.engine.get_next_duration_event()
        if next_event:
            now = datetime.now(UTC)
            delay = (next_event - now).total_seconds()
            if delay > 0:
                self.debug_log(f"Scheduling re-evaluation in {delay + 1:.1f}s")
                self._reeval_timer = async_call_later(
                    self.hass, delay + 1, self._delayed_update
                )

        return True

    def _build_reason(self, set_comfort: bool | None = None) -> str:
        """Build a human-readable reason string from the current HeatingState.

        Args:
            set_comfort: Pre-computed comfort decision from _do_update().
                         Passed through for AWAY / weather details that need it.
                         Avoids re-calling should_set_comfort() which has
                         side-effects on _optimum_start_active (CQS fix).
        """
        match self.current_state:
            case HeatingState.MANUAL_OVERRIDE:
                return "Manual Override (Paused)"

            case HeatingState.PAUSED:
                reasons = self.engine.get_uncertainty_reasons()
                if reasons:
                    short_reasons = [r.split(".")[-1] for r in reasons[:2] if isinstance(r, str)]
                    suffix = f" ({', '.join(short_reasons)})" if short_reasons else ""
                    return f"Paused{suffix}"
                return "Paused"

            case HeatingState.INACTIVE:
                parts = []
                if not self.engine.is_season_mode():
                    parts.append("Off")
                t = self.engine.check_outside_threshold()
                if t is False and self.engine.is_season_mode():
                    if (not self.engine.is_scheduler_defined()
                            or self.engine.is_scheduler_active() is not False
                            or self.engine.is_calendar_comfort_active() is not False):
                        parts.append("Outside > Threshold")
                return " | ".join(parts) if parts else "Inactive"

            case HeatingState.FROST_PROTECTION:
                return "Frost Protection"

            case HeatingState.WINDOW_OPEN:
                return "Window Open"

            case HeatingState.LIMING:
                return "Liming Protection"

            case HeatingState.PARTY:
                _, pt = self.engine.check_party_mode()
                return f"Party Mode ({pt}°)" if pt else "Party Mode"

            case HeatingState.FORCE_COMFORT:
                return "Force Comfort"

            case HeatingState.FORCE_ECO:
                return "Force Eco"

            case HeatingState.ADJUSTMENT:
                adj = self.engine.get_active_adjustment()
                mode = self.engine.get_adjustment_mode(adj)
                return f"Adjustment ({mode})"

            case HeatingState.SMART_PREHEATING:
                return "Smart Preheating"

            case HeatingState.AWAY:
                if set_comfort:
                    offset = self.config.away_offset
                    if offset != 0:
                        return f"Away (-{offset}°C)"
                    return "Away"
                return "Eco"

            case HeatingState.COMFORT:
                if self.engine.is_weather_anticipation_active():
                    w_offset = self.engine.get_weather_offset()
                    if w_offset > 0:
                        return f"Comfort (Weather -{w_offset}°C)"
                return "Comfort"

            case HeatingState.ECO:
                return "Eco"

            case _:
                return "Unknown"

    async def _async_apply_trv_change(self, change: dict, secs: float) -> None:
        """Apply a single TRV change asynchronously."""
        eid = change["entity_id"]
        new_mode = change.get("hvac_mode")
        new_temp = change.get("temperature")
        state = self.hass.states.get(eid)
        cur_mode = state.state if state else None
        cur_temp = state.attributes.get("temperature") if state else None
        name = self.config.name

        try:
            # ── Combined mode and temperature update ─────────────────────
            if (new_mode and new_mode != cur_mode and
                new_temp is not None and (new_mode != "off" or new_temp > 0)):

                if cur_temp is None or abs(float(cur_temp) - new_temp) >= 0.1:
                    await safe_service_call(
                        self.hass, name,
                        "climate", "set_temperature",
                        {"entity_id": eid, "temperature": new_temp, "hvac_mode": new_mode},
                    )
                    return

            # ── Separate updates if combination wasn't possible ──────────
            if new_mode and new_mode != cur_mode:
                await safe_service_call(
                    self.hass, name,
                    "climate", "set_hvac_mode",
                    {"entity_id": eid, "hvac_mode": new_mode},
                )
                if secs > 0:
                    await asyncio.sleep(secs)

            if new_temp is not None and (new_mode != "off" or new_temp > 0):
                if cur_temp is None or abs(float(cur_temp) - new_temp) >= 0.1:
                    await safe_service_call(
                        self.hass, name,
                        "climate", "set_temperature",
                        {"entity_id": eid, "temperature": new_temp},
                    )
        except Exception as exc:
            _LOGGER.warning("%s: failed to apply %s: %s", name, eid, exc)

    def _get_snapshot_entities(self) -> set[str]:
        """Collect all entity IDs from config that should be snapshotted."""
        entities = set()
        config = self.config

        def add(val: Any) -> None:
            """Add entity IDs from a str, list[str], or None value."""
            if not val:
                return
            if isinstance(val, str):
                entities.add(val)
            elif isinstance(val, list):
                for v in val:
                    if isinstance(v, str) and v:
                        entities.add(v)

        add(config.temp_sensor)
        add(config.outside_temp_sensor)
        add(config.weather_entity)
        add(config.scheduler_selector)
        add(config.scheduler_presence)
        add(config.season_mode_entity)
        add(config.proximity_entity)
        add(config.trvs)
        add(config.schedulers)
        add(config.persons)
        add(config.guest_mode)
        add(config.presence_sensor)
        add(config.window_sensors)
        add(config.calendar)

        return entities
