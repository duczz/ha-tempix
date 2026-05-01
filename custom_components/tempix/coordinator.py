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
from homeassistant.util import dt as dt_util
from homeassistant.helpers import entity_registry as er_helper
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_call_later,
    async_track_time_interval,
)

from custom_components.tempix.const import (
    CONF_LEARNED_HEATING_RATE,
    CONF_PARTY_MODE_SWITCH,
    CONF_GUEST_MODE_SWITCH,
    CALIBRATION_MODE_OFF,
    AGGRESSIVE_MODE_CALIBRATION,
    SCHEDULING_MODE_CALENDAR,
    HeatingState,
    DEFAULT_VACATION_TEMP,
)
from custom_components.tempix.config_model import TempixConfig
from custom_components.tempix.engine import TempixEngine
from custom_components.tempix.coordinator_scene import SceneManager
from custom_components.tempix.coordinator_appliers import (
    CalibrationApplier,
    ValvePositioner,
    async_apply_trv_change,
    safe_service_call,
)
from custom_components.tempix.coordinator_learning import HeatingRateLearner
from homeassistant.helpers.storage import Store

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
        self._last_update: dict | None = None
        self._trigger_timer: Any = None
        self._reeval_timer: Any = None
        self._update_lock = asyncio.Lock()
        self._updates_enabled = False
        self._ready_time: datetime | None = None
        self._uncertainty_start_time: datetime | None = None

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

        # Circuit breaker: tracks consecutive failures per TRV entity
        # { entity_id: {"failures": int, "retry_after": datetime | None} }
        self._cb_state: dict[str, dict] = {}
        self._cb_store = Store(hass, 1, f"tempix.cb_state.{entry_id}")

        # ── helper objects ────────────────────────────────────────────────
        self._scene_manager = SceneManager(
            hass,
            config.trvs,
            config.action_delay.total_seconds(),
            config.name,
            entry_id,
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
        if not self._validate_option(key, value, duration_mins):
            _LOGGER.error("%s: Invalid option value for %s: %s (duration=%s)", self.config.name, key, value, duration_mins)
            return

        # Cancel existing timer for this key if any
        if key in self._option_timers:
            self._option_timers[key]()
            del self._option_timers[key]

        # Update the option in the config entry
        entry_id = self.entry_id
        entry = self.hass.config_entries.async_get_entry(entry_id)
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

    # ── circuit breaker persistence ──────────────────────────────────────────

    async def _cb_load(self) -> None:
        try:
            stored = await self._cb_store.async_load()
            if stored:
                for eid, state in stored.items():
                    retry_raw = state.get("retry_after")
                    self._cb_state[eid] = {
                        "failures": state.get("failures", 0),
                        "retry_after": datetime.fromisoformat(retry_raw) if retry_raw else None,
                    }
        except Exception as exc:
            _LOGGER.warning("%s: Failed to load circuit breaker state: %s", self.config.name, exc)

    async def _cb_save(self) -> None:
        try:
            serialized = {
                eid: {
                    "failures": state["failures"],
                    "retry_after": state["retry_after"].isoformat() if state["retry_after"] else None,
                }
                for eid, state in self._cb_state.items()
            }
            await self._cb_store.async_save(serialized)
        except Exception as exc:
            _LOGGER.warning("%s: Failed to save circuit breaker state: %s", self.config.name, exc)

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def async_setup(self) -> None:
        """Register listeners matching every blueprint trigger."""
        self.debug_log(f"Starting coordinator setup (ID: {self.entry_id[:6]})")
        await self._scene_manager.async_load()
        await self._rate_learner.async_load()
        await self._cb_load()
        # M-C: Discard any window scene saved before this boot. TRVs may have been
        # manually changed during the downtime; restoring stale states would undo that.
        # If the window is still open after restart, Tempix will set eco immediately anyway.
        self._scene_manager.clear("window")

        if self.hass.state == CoreState.running:
            self.debug_log("HA already running, starting coordinator immediately")
            await self._start_coordinator()
        else:
            self.debug_log("Waiting for Home Assistant to start...")
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

        # ── Periodic Data Fetchers (decoupled from update loop) ──
        interval = timedelta(minutes=self.config.calendar_scan_interval)
        needs_initial_update = True

        if self.config.scheduling_mode == SCHEDULING_MODE_CALENDAR or self.config.holiday_calendar:
            self._listeners.append(async_track_time_interval(
                self.hass, self._async_fetch_and_update_calendar, interval
            ))
            # Attempt an immediate fetch. Cloud calendars (e.g. Google) that haven't
            # finished their server sync will fail silently (debug-level) and retry
            # on the next state-change or periodic timer.
            # Note: each wrapper calls async_update after its own fetch. Rooms with both
            # calendar + schedulers will therefore run two updates on startup — this is
            # intentional: if the calendar fetch fails (integration not yet ready), the
            # schedule update still runs independently with its own data.
            await self._async_fetch_and_update_calendar()
            needs_initial_update = False

        if self.config.scheduling_mode != SCHEDULING_MODE_CALENDAR and self.config.schedulers:
            self._listeners.append(async_track_time_interval(
                self.hass, self._async_fetch_and_update_schedule, interval
            ))
            await self._async_fetch_and_update_schedule()
            needs_initial_update = False

        if needs_initial_update:
            try:
                await self.async_update()
            except asyncio.CancelledError:
                _LOGGER.warning(
                    "%s: Initial update cancelled (likely integration reload before HA was ready). "
                    "Will retry on next state change.",
                    self.config.name,
                )

    async def _async_fetch_calendar_events(self) -> None:
        """Fetch agenda for all configured calendars for deep scanning."""
        calendars = list(self.config.calendar)
        if self.config.holiday_calendar and self.config.holiday_calendar not in calendars:
            calendars.append(self.config.holiday_calendar)
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
                params = {
                    "entity_id": cal_id,
                    "start_date_time": start.isoformat(),
                    "end_date_time": end.isoformat(),
                }
                response = await safe_service_call(
                    self.hass, self.config.name,
                    "calendar", "get_events",
                    params,
                    return_response=True,
                    ha_error_log_level=logging.DEBUG,
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

    async def _async_fetch_schedule_slots(self) -> None:
        """Fetch timeslots for all configured schedulers via schedule.get_schedule."""
        schedulers = self.config.schedulers
        if not schedulers:
            return

        all_slots: dict[str, list[dict[str, Any]]] = {}

        for sched_id in schedulers:
            if not self.hass.states.get(sched_id):
                _LOGGER.debug("Scheduler entity %s not available, skipping fetch", sched_id)
                continue
            try:
                self.debug_log(f"Fetching schedule slots for {sched_id}")
                response = await safe_service_call(
                    self.hass, self.config.name,
                    "schedule", "get_schedule",
                    {"entity_id": sched_id},
                    return_response=True,
                )
                if response:
                    # Response: {entity_id: {weekday: [{'from': time, 'to': time}]}}
                    data = response.get(sched_id, {})
                    all_slots[sched_id] = data
                    self.debug_log(f"Fetched schedule for {sched_id}: {list(data.keys())}")
            except Exception as exc:
                _LOGGER.warning("Error fetching schedule slots for %s: %s", sched_id, exc)

        self.engine.set_schedule_slots(all_slots)

    async def _async_fetch_and_update_calendar(self, _now: datetime | None = None) -> None:
        """Fetch calendar events and trigger a recalculation."""
        if not self._updates_enabled:
            return
        try:
            await self._async_fetch_calendar_events()
        except Exception as exc:
            _LOGGER.warning("%s: Calendar fetch failed, using last cached data: %s", self.config.name, exc)
        await self.async_update()

    async def _async_fetch_and_update_schedule(self, _now: datetime | None = None) -> None:
        """Fetch schedule slots and trigger a recalculation."""
        if not self._updates_enabled:
            return
        try:
            await self._async_fetch_schedule_slots()
        except Exception as exc:
            _LOGGER.warning("%s: Schedule slots fetch failed, using last cached data: %s", self.config.name, exc)
        await self.async_update()

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
        ):
            add_if_entity(val)

        # Calendars (v1.4.0)
        add_if_entity(self.config.calendar)

        # Proximity: config holds a device_id — track all its entities
        if self.config.proximity_entity:
            ent_reg = er_helper.async_get(self.hass)
            for entry in er_helper.async_entries_for_device(ent_reg, self.config.proximity_entity):
                tracked.append(entry.entity_id)

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
        self._listeners.append(async_track_time_interval(
            self.hass, self._on_heartbeat, timedelta(minutes=1)
        ))

    @callback
    def _on_state_change(self, event: Event) -> None:
        """Handle any tracked entity change."""
        if not self._updates_enabled:
            return

        entity_id = event.data.get("entity_id", "")
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")

        # Calendar state change → trigger immediate re-fetch + recalculation.
        # Skip if the calendar is still unavailable/syncing to avoid fetch spam on startup.
        if entity_id in self.config.calendar or entity_id == self.config.holiday_calendar:
            if new_state and new_state.state not in ("unavailable", "unknown"):
                self._create_tracked_task(self._async_fetch_and_update_calendar())
            return

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
        self.debug_log("Heartbeat fired")
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

        if self._trigger_timer:
            self._trigger_timer()
            self._trigger_timer = None

        if self._reeval_timer:
            self._reeval_timer()
            self._reeval_timer = None

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
            try:
                success = await self._do_update()

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

    def _validate_option(self, key: str, value: Any, duration_mins: int | None = None) -> bool:
        """Runtime validation for config options set via service calls."""
        if key == CONF_LEARNED_HEATING_RATE:
            return isinstance(value, (int, float)) and 0 < value <= 10.0
        if key in (CONF_PARTY_MODE_SWITCH, CONF_GUEST_MODE_SWITCH):
            if not isinstance(value, bool):
                return False
            if duration_mins is not None and (not isinstance(duration_mins, (int, float)) or duration_mins <= 0):
                return False
            return True
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
        self.engine.set_startup_time(self._ready_time)

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

        # 1 concurrent write per 3 TRVs — limits FRITZ!DECT radio contention
        semaphore = asyncio.Semaphore(max(len(self.config.trvs) // 3 + 1, 1))

        async def _apply_with_semaphore(change: dict[str, Any]) -> None:
            async with semaphore:
                eid = change["entity_id"]
                cb = self._cb_state.setdefault(eid, {"failures": 0, "retry_after": None})

                # Circuit breaker: skip if in backoff period
                if cb["retry_after"] and datetime.now(UTC) < cb["retry_after"]:
                    self.debug_log(f"Circuit breaker: skipping {eid} (retry after {cb['retry_after'].strftime('%H:%M')})")
                    return

                try:
                    await async_apply_trv_change(self.hass, self.config.name, change, secs)
                    # Success → reset circuit breaker
                    if cb["failures"] > 0:
                        _LOGGER.info("%s: %s recovered — circuit breaker reset", self.config.name, eid)
                    cb["failures"] = 0
                    cb["retry_after"] = None
                    await self._cb_save()
                except Exception as exc:
                    cb["failures"] += 1
                    # Backoff: 15 → 30 → 60 → 120min (cap at 120)
                    backoff_min = min(15 * (2 ** (cb["failures"] - 1)), 120)
                    cb["retry_after"] = datetime.now(UTC) + timedelta(minutes=backoff_min)
                    _LOGGER.warning(
                        "%s: %s failed (%d) — pausing for %dmin: %s",
                        self.config.name, eid, cb["failures"], backoff_min, exc,
                    )
                    await self._cb_save()

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

        # ── 7. Dynamic Re-evaluation (Phase 50) ──────────────────────────
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

        self._last_update = {
            "timestamp": datetime.now(UTC).isoformat(),
            "hvac_mode": hvac_mode,
            "target_temp": target_temp,
            "reason": self.current_reason,
            "changes_count": len(changes),
        }

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
                    parts.append("Outside < Threshold" if self.engine.is_cooling else "Outside > Threshold")
                return " | ".join(parts) if parts else "Inactive"

            case HeatingState.FROST_PROTECTION:
                return "Frost Protection"

            case HeatingState.WINDOW_OPEN:
                return "Window Open"

            case HeatingState.LIMING:
                return "Liming Protection"

            case HeatingState.VACATION:
                _, vt = self.engine.is_vacation_mode()
                temp = vt if vt is not None else DEFAULT_VACATION_TEMP
                return f"Vacation Mode ({temp}°C)"

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
                    parts = []
                    offset = self.config.away_offset
                    if offset != 0:
                        parts.append(f"🚶 -{offset}°C")
                    cal_tags = self.engine.get_calendar_tags()
                    if "comfort" in cal_tags:
                        parts.append(f"📅 {cal_tags['comfort']}°C")
                    if self.engine.is_sunshine_offset_active():
                        w_offset = self.engine.get_sunshine_offset()
                        if w_offset > 0:
                            parts.append(f"☀️ -{w_offset}°C")
                    suffix = f" ({', '.join(parts)})" if parts else ""
                    return f"Comfort{suffix}"
                return "Eco"

            case HeatingState.COMFORT:
                parts = []
                if self.engine.is_holiday_today():
                    parts.append("Holiday")
                cal_tags = self.engine.get_calendar_tags()
                if "comfort" in cal_tags:
                    parts.append(f"📅 {cal_tags['comfort']}°C")
                if self.engine.is_sunshine_offset_active():
                    w_offset = self.engine.get_sunshine_offset()
                    if w_offset > 0:
                        parts.append(f"☀️ -{w_offset}°C")
                suffix = f" ({', '.join(parts)})" if parts else ""
                return f"Comfort{suffix}"

            case HeatingState.ECO:
                parts = []
                if self.engine.is_holiday_today():
                    parts.append("Holiday")
                cal_tags = self.engine.get_calendar_tags()
                if "eco" in cal_tags:
                    parts.append(f"📅 {cal_tags['eco']}°C")
                t = self.engine.check_outside_threshold()
                if t is False and self.engine.is_season_mode():
                    parts.append("Outside < Threshold" if self.engine.is_cooling else "Outside > Threshold")
                suffix = f" ({', '.join(parts)})" if parts else ""
                return f"Eco{suffix}"

            case _:
                return "Unknown"

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
