"""
Tempix – Coordinator.

Manages state listeners, timers, and applies changes to TRVs.
Maps every blueprint trigger to an HA event listener.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
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
    CONF_LEARNED_CLIMATE_RATE,
    CONF_PARTY_MODE_SWITCH,
    CONF_GUEST_MODE_SWITCH,
    CALIBRATION_MODE_OFF,
    AGGRESSIVE_MODE_CALIBRATION,
    SCHEDULING_MODE_CALENDAR,
    ClimateState,
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
from custom_components.tempix.coordinator_learning import ClimateRateLearner
from homeassistant.helpers.storage import Store
from homeassistant.helpers.debounce import Debouncer

_LOGGER = logging.getLogger(__name__)

# Diagnostic-only attributes – state changes in these are ignored (M-5)
_DIAG_ATTRS: frozenset[str] = frozenset({
    "battery_level", "linkquality", "rssi", "battery", "volt", "pressure"
})

@dataclass
class CommandInfo:
    """Represents a command sent to a TRV to track expected echoes."""
    target_temperature: float | None
    hvac_mode: str | None
    timestamp: float

@dataclass
class TemporaryManualOverride:
    """Represents an active manual override."""
    active: bool
    source: str  # "ui" or "physical"
    timestamp: datetime
    schedule_revision: int
    target_temperature: float | None
    hvac_mode: str | None

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
        self._refresh_in_progress: bool = False
        self._refresh_pending: bool = False
        self._ready_time: datetime | None = None
        self._uncertainty_start_time: datetime | None = None

        # Public state for sensors
        self.current_hvac: str = "off"
        self.current_temperature: float | None = None
        self.current_reason: str = ""
        self.current_state: ClimateState = ClimateState.PAUSED
        self.last_changes: list[dict] = []
        self.last_calibrations: dict[str, float] = {}
        self.last_generic_offsets: dict[str, float] = {}

        # T-2: Temporary Override & Echo Tracking
        self._pending_commands: dict[str, deque[CommandInfo]] = {
            trv: deque(maxlen=5) for trv in config.trvs
        }
        self._first_state_seen: dict[str, bool] = {trv: False for trv in config.trvs}
        self._startup_ignore_until: dict[str, datetime] = {}
        self._override_propagation_until: datetime | None = None
        self._override_clear_suppress_until: datetime | None = None
        # Values of the last cleared override — clear-suppress only mutes echoes
        # that still carry these values, real user input passes through.
        self._cleared_override_temp: float | None = None
        self._cleared_override_hvac: str | None = None
        self._override_blocked_warned: bool = False
        self._temporary_manual_override: TemporaryManualOverride | None = None
        self._schedule_revision: int = 0
        self._schedule_transition_pending: bool = False
        self._last_schedule_fingerprint: str | None = None  # None = never computed

        self._entity_callbacks: list = []
        self._option_timers: dict[str, Any] = {}
        self._option_timer_expires: dict[str, datetime] = {}
        self._background_tasks: set[asyncio.Task] = set()

        # Circuit breaker: tracks consecutive failures per TRV entity
        # { entity_id: {"failures": int, "retry_after": datetime | None} }
        self._cb_state: dict[str, dict] = {}
        self._cb_store = Store(hass, 1, f"tempix.cb_state.{entry_id}")

        # Domain State persistence (Phase 0.1)
        self._initialized: bool = False
        self._domain_store = Store(hass, 1, f"tempix_domain_state_{entry_id}")
        self._domain_save_debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=2.0,
            immediate=False,
            function=self._async_save_domain_state,
        )

        # Hysteresis persistence
        self._hysteresis_dirty: bool = False
        self._store = Store(hass, 1, f"tempix_hysteresis_{entry_id}")
        self._save_debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=2.0,
            immediate=False,
            function=self._async_save_hysteresis_state,
        )
        self.engine.set_on_dirty(self._on_hysteresis_dirty)

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
        self._rate_learner = ClimateRateLearner(hass, config, engine, entry_id)
        self._prev_party: bool = False

    def debug_log(self, msg: str) -> None:
        """Log debug message with coordinator prefix."""
        if self.config.debug_mode:
            _LOGGER.info("TPX Coord [%s]: %s", self.config.name, msg)
        else:
            _LOGGER.debug("TPX Coord [%s]: %s", self.config.name, msg)

    def apply_config(self, config: TempixConfig) -> None:
        """Swap the typed config on the coordinator AND all helper objects.

        Dynamic (no-reload) option updates create a NEW TempixConfig instance.
        CalibrationApplier, ValvePositioner and ClimateRateLearner hold their
        own reference — without this swap they keep reading the stale config
        until the next full reload (and diverge from switch-toggle mutations).
        """
        self.config = config
        self._calib_applier._config = config
        self._valve_positioner._config = config
        self._rate_learner._config = config
        self._scene_manager._action_delay_secs = config.action_delay.total_seconds()
        self._scene_manager._name = config.name

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
        self._option_timer_expires.pop(key, None)

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
                self._option_timer_expires.pop(key, None)
                self._create_tracked_task(
                    self.async_set_temporary_option(key, False)
                )

            # Keep track of when it expires for persistence
            self._option_timer_expires[key] = datetime.now(UTC) + timedelta(minutes=duration_mins)
            self._option_timers[key] = async_call_later(
                self.hass, duration_mins * 60, _timer_finished
            )
        else:
            self.debug_log(f"Setting option {key}={value} (no timer)")

        # B7 / ADR-001: persist timer changes right away. Without this a
        # party/guest timer set after the last override event only reached
        # the store on graceful unload — and was lost on crash/power loss.
        self._create_tracked_task(self._domain_save_debouncer.async_call())

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

    # ── domain state persistence ──────────────────────────────────────────────

    async def _async_restore_domain_state(self) -> None:
        self._initialized = False
        try:
            restored = await self._domain_store.async_load()
            if not isinstance(restored, dict):
                return

            override_data = restored.get("override")
            if isinstance(override_data, dict) and "timestamp" in override_data and "expires_at" in override_data:
                ts = datetime.fromisoformat(override_data["timestamp"])
                expires_at = datetime.fromisoformat(override_data["expires_at"])
                now = datetime.now(UTC)
                
                if now < expires_at:
                    self._temporary_manual_override = TemporaryManualOverride(
                        active=override_data.get("active", True),
                        target_temperature=override_data.get("target_temperature"),
                        hvac_mode=override_data.get("hvac_mode"),
                        source=override_data.get("source", "physical"),
                        timestamp=ts,
                        schedule_revision=override_data.get("schedule_revision", self._schedule_revision)
                    )
                    self.debug_log(f"Restored manual override: {override_data.get('target_temperature')}°C, expires at {expires_at}")
                else:
                    self.debug_log("Ignored restored override because it has expired.")

            # Restore option timers (e.g. Party Mode Timer)
            option_timers_data = restored.get("option_timers")
            if isinstance(option_timers_data, dict):
                now = datetime.now(UTC)
                for key, expires_str in option_timers_data.items():
                    try:
                        expires_at = datetime.fromisoformat(expires_str)
                        if expires_at > now:
                            remaining_seconds = (expires_at - now).total_seconds()
                            self._option_timer_expires[key] = expires_at
                            
                            @callback
                            def _restored_timer_finished(_now, k=key):
                                self.debug_log(f"Restored temporary option {k} expired. Resetting to False.")
                                self._option_timers.pop(k, None)
                                self._option_timer_expires.pop(k, None)
                                self._create_tracked_task(self.async_set_temporary_option(k, False))
                                
                            self._option_timers[key] = async_call_later(self.hass, remaining_seconds, _restored_timer_finished)
                            self.debug_log(f"Restored timer for {key}, expires in {remaining_seconds}s")
                    except Exception as e:
                        self.debug_log(f"Failed to restore timer for {key}: {e}")

            restored_fp = restored.get("last_schedule_fingerprint")
            if isinstance(restored_fp, str):  # None-Guard + type-check against corrupted store
                self._last_schedule_fingerprint = restored_fp
        except Exception as exc:
            _LOGGER.warning("%s: Failed to restore Tempix domain state: %s", self.config.name, exc)
        finally:
            self._initialized = True

    async def _async_save_domain_state(self) -> None:
        state = {
            "schema_version": 1,
            "override": None,
            "option_timers": {k: v.isoformat() for k, v in self._option_timer_expires.items()},
            "last_schedule_fingerprint": getattr(self, "_last_schedule_fingerprint", None)
        }
        
        if self._temporary_manual_override and self._temporary_manual_override.active:
            override = self._temporary_manual_override
            state["override"] = {
                "active": override.active,
                "source": override.source,
                "timestamp": override.timestamp.isoformat(),
                "expires_at": (override.timestamp + timedelta(hours=24)).isoformat(), # V1 defaults to 24 hours
                "schedule_revision": override.schedule_revision,
                "target_temperature": override.target_temperature,
                "hvac_mode": override.hvac_mode
            }

        try:
            await self._domain_store.async_save(state)
        except Exception as exc:
            _LOGGER.warning("%s: Failed to save domain state: %s", self.config.name, exc)

    # ── hysteresis persistence ───────────────────────────────────────────────

    async def _async_restore_hysteresis(self) -> None:
        try:
            data = await self._store.async_load()
            if data and "outside_ok" in data:
                self.engine.restore_outside_state(data["outside_ok"])
        except Exception as exc:
            _LOGGER.warning("%s: Failed to load hysteresis state: %s", self.config.name, exc)

    def _on_hysteresis_dirty(self) -> None:
        """Called by engine when outside_ok changes."""
        if not self.hass.is_running:
            return
        self._hysteresis_dirty = True
        self._create_tracked_task(self._save_debouncer.async_call())

    async def _async_save_hysteresis_state(self) -> None:
        if not self._hysteresis_dirty:
            return
        self._hysteresis_dirty = False
        try:
            await self._store.async_save({"outside_ok": self.engine.outside_ok})
        except Exception as exc:
            _LOGGER.warning("%s: Failed to save hysteresis state: %s", self.config.name, exc)

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def async_setup(self) -> None:
        """Register listeners matching every blueprint trigger."""
        self.debug_log(f"Starting coordinator setup (ID: {self.entry_id[:6]})")
        await self._scene_manager.async_load()
        await self._rate_learner.async_load()
        await self._cb_load()
        await self._async_restore_hysteresis()
        await self._async_restore_domain_state()
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

        # Pre-populate _first_state_seen for TRVs that HA already knows about.
        # Without this, a TRV that was connected hours before Tempix starts processing
        # would still get its first state change suppressed as if it were a startup sync.
        for trv in self.config.trvs:
            state = self.hass.states.get(trv)
            if state and state.state not in ("unavailable", "unknown"):
                self._first_state_seen[trv] = True
                self.debug_log(f"Pre-populated _first_state_seen: {trv} (state={state.state})")

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
            self.config.weather_entity,
        ):
            add_if_entity(val)

        # Calendars (v1.4.0)
        add_if_entity(self.config.calendar)
        add_if_entity(self.config.holiday_calendar)

        # Proximity: config holds a device_id — track all its entities
        if self.config.proximity_entity:
            ent_reg = er_helper.async_get(self.hass)
            for entry in er_helper.async_entries_for_device(ent_reg, self.config.proximity_entity):
                tracked.append(entry.entity_id)

        # Windows
        add_if_entity(self.config.window_sensors)

        # Adjustments: comfort/eco may reference entity_ids (e.g. input_number.foo)
        for entry in self.config.adjustments or []:
            if isinstance(entry, dict):
                add_if_entity(entry.get("comfort"))
                add_if_entity(entry.get("eco"))

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

        # TRV changes (HVAC or Temperature) - Evaluate for manual override
        if entity_id in self.config.trvs and new_state:
            if new_state.state in ("unavailable", "unknown"):
                return
            old_temp = old_state.attributes.get("temperature") if old_state else None
            new_temp = new_state.attributes.get("temperature")
            old_hvac = old_state.state if old_state else None
            new_hvac = new_state.state

            if old_temp != new_temp or old_hvac != new_hvac:
                suppressed = self._evaluate_trv_event(
                    entity_id, new_temp, new_hvac, event.context,
                    old_temp=old_temp, old_hvac=old_hvac,
                )
                if suppressed:
                    # Event was suppressed (Startup, Echo, Propagation, Clear Suppress).
                    # Do NOT start the debounce timer — the engine must not run without
                    # an active override. activate_temporary_manual_override() (the
                    # non-suppress path) already calls async_update() itself (line ~838).
                    return

        # Ignore identical state
        if old_state and new_state and old_state.state == new_state.state:
            # Whitelist for essential attribute changes
            # 1. Weather/Temperature sensor attribute changes
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

    @callback
    def _evaluate_trv_event(
        self, entity_id: str, new_temp: float | None, new_hvac: str | None, context: Any,
        old_temp: float | None = None, old_hvac: str | None = None,
    ) -> bool:
        """Handle TRV state or attribute changes, checking for manual overrides.

        old_temp/old_hvac carry the previous TRV state and enable partial-echo
        detection (integrations that ack a combined command as two events).

        Returns:
            True  – the event was suppressed (Startup, Echo, Propagation, Clear Suppress).
                    The caller should NOT start the debounce timer.
            False – a temporary override was activated. activate_temporary_manual_override()
                    already triggers async_update() internally, so no extra debounce needed.
        """
        # 1. Manual Override Guard (Hands-off Modus)
        # Event layer: prevent creation of new shadow states
        if getattr(self.config, "manual_override", False):
            self.debug_log(f"Manual override is active. Ignoring TRV event from {entity_id}.")
            return True

        # 2. Startup Suppression (Per-Entity Initial-Sync)
        now = datetime.now(UTC)
        if not self._first_state_seen.get(entity_id, False):
            self._first_state_seen[entity_id] = True
            # Double-join stutter protection (5s)
            self._startup_ignore_until[entity_id] = now + timedelta(seconds=5)
            self.debug_log(f"Startup suppression: Ignoring FIRST state from {entity_id}")
            return True

        ignore_until = self._startup_ignore_until.get(entity_id)
        if ignore_until and now < ignore_until:
            self.debug_log(f"Startup suppression: Ignoring double-join state from {entity_id}")
            return True

        # Prune expired commands from queue (TTL = 180s)
        self._prune_expired_commands(entity_id, now)

        # 1.5 Clear Suppression – ignore TRV echoes after manual override clear.
        # Value-based (Bug-2-Klasse): nur Events unterdrücken, die noch die Werte
        # des GERADE GELÖSCHTEN Overrides tragen (verspätete TRV-Callbacks).
        # Echte neue User-Eingaben mit anderen Werten passieren sofort.
        if self._override_clear_suppress_until and now < self._override_clear_suppress_until:
            if self._matches_cleared_override(new_temp, new_hvac):
                self.debug_log(
                    f"Clear suppression: Ignoring TRV echo from {entity_id} "
                    f"(still carries cleared override values temp={new_temp}, hvac={new_hvac})."
                )
                return True
            self.debug_log(
                f"Non-matching event from {entity_id} during clear-suppress window "
                f"(new_temp={new_temp}, new_hvac={new_hvac}). "
                f"Treating as intentional user change."
            )

        # 2. Queue Matching (Echo Detection)
        if self._is_queue_match(entity_id, new_temp, new_hvac):
            self.debug_log(f"Echo detected from {entity_id}: temp={new_temp}, hvac={new_hvac}. Ignoring.")
            return True

        # 2.5 Partial Propagation Echo (Multi-Event-Acks)
        # Manche Integrationen (z.B. Zigbee2MQTT) bestätigen ein kombiniertes
        # Mode+Temp-Kommando als ZWEI getrennte State-Events: erst kippt der Mode
        # (Temp-Attribut hält noch den alten Wert), dann folgt die Temperatur.
        # Keines der Events matcht das Kommando vollständig — aber jedes matcht es
        # in genau der Dimension, die sich geändert hat, während die ANDERE
        # Dimension noch ihrem vorherigen Wert entspricht. Das ist ein Echo, kein
        # User-Eingriff; das vollständige Ack folgt mit dem nächsten Event und
        # räumt die Queue über den regulären Match auf.
        if self._is_partial_queue_match(entity_id, new_temp, new_hvac, old_temp, old_hvac):
            self.debug_log(
                f"Partial propagation echo from {entity_id}: temp={new_temp}, hvac={new_hvac} "
                f"(previous: temp={old_temp}, hvac={old_hvac}). Ignoring, awaiting full ack."
            )
            return True

        # 3. Smart Propagation Ignore
        if self._override_propagation_until and now < self._override_propagation_until:
            # Check if this event matches the currently broadcasting override.
            # If so, it's an expected propagation echo from a delayed TRV.
            if self._temporary_manual_override and self._temporary_manual_override.active:
                if self._matches_command(
                    new_temp, new_hvac,
                    self._temporary_manual_override.target_temperature,
                    self._temporary_manual_override.hvac_mode
                ):
                    self.debug_log(f"Propagation echo from {entity_id} matches broadcasting override. Ignoring.")
                    return True
                else:
                    # Bug 2 Fix: Kein return True hier.
                    # Ein Nicht-Match im Propagation-Fenster ist eine echte neue User-Eingabe
                    # (z.B. User ändert sofort Meinung: 25°C → 22°C innerhalb von 15s).
                    # Echo-Detection (_is_queue_match) hat Firmware-Bounces bereits vorher abgefangen.
                    # → Fall-through zu Step 4: activate_temporary_manual_override
                    self.debug_log(
                        f"Non-matching event from {entity_id} during propagation window "
                        f"(new_temp={new_temp}, new_hvac={new_hvac}). "
                        f"Treating as intentional user change, not a delayed bounce."
                    )

        # 4. Activate Temporary Override
        # activate_temporary_manual_override() calls async_update() internally — no
        # additional debounce timer is needed in _on_state_change for this path.
        source = "ui" if context and (context.user_id or context.parent_id) else "physical"
        self.activate_temporary_manual_override(new_temp, new_hvac, source, now)
        return False

    def _prune_expired_commands(self, entity_id: str, now: datetime) -> None:
        """Remove commands older than 180s from the pending queue."""
        queue = self._pending_commands.get(entity_id)
        if not queue:
            return
        # now.timestamp() is seconds since epoch
        current_ts = now.timestamp()
        while queue and (current_ts - queue[0].timestamp) > 180:
            expired = queue.popleft()
            self.debug_log(f"Pruned expired command for {entity_id}: temp={expired.target_temperature}, hvac={expired.hvac_mode}")

    def _temps_match(self, temp_a: float | None, temp_b: float | None) -> bool:
        """Temperature match within 0.5°C tolerance (floating point / TRV rounding)."""
        if temp_a is None and temp_b is None:
            return True
        if temp_a is None or temp_b is None:
            return False
        return abs(float(temp_a) - float(temp_b)) <= 0.5

    def _matches_command(
        self, temp_a: float | None, hvac_a: str | None,
        temp_b: float | None, hvac_b: str | None
    ) -> bool:
        """Check if temperature and hvac mode match."""
        # HVAC match
        if hvac_a != hvac_b:
            return False

        if hvac_a == "off" and hvac_b == "off":
            return True

        return self._temps_match(temp_a, temp_b)

    def _is_partial_queue_match(
        self, entity_id: str, new_temp: float | None, new_hvac: str | None,
        old_temp: float | None, old_hvac: str | None,
    ) -> bool:
        """Detect a partial ack of a pending command (multi-event propagation).

        Matches when exactly ONE dimension (hvac or temp) already equals a
        pending command while the OTHER dimension still holds its previous
        value. The queue is intentionally NOT advanced — the full ack that
        follows pops the entry via the regular _is_queue_match.
        Conservative: without old values (old_temp/old_hvac unknown) no
        partial match is claimed.
        """
        queue = self._pending_commands.get(entity_id)
        if not queue:
            return False

        for cmd in queue:
            hvac_matches = new_hvac == cmd.hvac_mode
            temp_matches = self._temps_match(new_temp, cmd.target_temperature)

            # Mode-Ack zuerst: hvac matcht das Kommando, Temp ist noch der alte Wert
            if hvac_matches and not temp_matches and old_temp is not None \
                    and self._temps_match(new_temp, old_temp):
                return True

            # Temp-Ack zuerst: Temp matcht das Kommando, hvac ist noch der alte Wert
            if temp_matches and not hvac_matches and old_hvac is not None \
                    and new_hvac == old_hvac:
                return True

        return False

    def _matches_cleared_override(self, new_temp: float | None, new_hvac: str | None) -> bool:
        """Check if an event still carries the values of the just-cleared override.

        Unset override dimensions (None) act as wildcard — the override never
        commanded them, so the TRV echo carries arbitrary previous values there.
        """
        ct = self._cleared_override_temp
        ch = self._cleared_override_hvac
        if ct is None and ch is None:
            return False
        temp_ok = ct is None or self._temps_match(new_temp, ct)
        hvac_ok = ch is None or new_hvac == ch
        return temp_ok and hvac_ok

    def _is_queue_match(self, entity_id: str, new_temp: float | None, new_hvac: str | None) -> bool:
        """Check if the event matches any pending command. If so, advance queue."""
        queue = self._pending_commands.get(entity_id)
        if not queue:
            return False

        # Find match
        match_index = -1
        for i, cmd in enumerate(queue):
            if self._matches_command(new_temp, new_hvac, cmd.target_temperature, cmd.hvac_mode):
                match_index = i
                break

        if match_index >= 0:
            # Advance queue: remove the matched item and all older items before it
            for _ in range(match_index + 1):
                queue.popleft()
            return True
            
        return False

    def track_sent_command(self, entity_id: str, target_temperature: float | None, hvac_mode: str | None) -> None:
        """Add a command to the pending queue when Tempix sends it."""
        queue = self._pending_commands.get(entity_id)
        if queue is not None:
            now_ts = datetime.now(UTC).timestamp()
            queue.append(CommandInfo(
                target_temperature=target_temperature,
                hvac_mode=hvac_mode,
                timestamp=now_ts
            ))
            self.debug_log(f"Tracked sent command for {entity_id}: temp={target_temperature}, hvac={hvac_mode}")

    def activate_temporary_manual_override(
        self, new_temp: float | None, new_hvac: str | None, source: str, now: datetime
    ) -> None:
        """Activate a temporary manual override."""
        if not getattr(self.config, "enable_temporary_manual_override", False):
            # Warn once per setup/reload — with the feature off (migration
            # default for existing users) EVERY physical dial turn lands here,
            # a WARNING each time would spam the log (see gotcha #19 pattern).
            if not self._override_blocked_warned:
                self._override_blocked_warned = True
                _LOGGER.warning(
                    "TPX [%s]: Manual TRV change ignored – 'Enable Temporary Manual Override' "
                    "is OFF in the options (source=%s). Further occurrences are logged at debug level.",
                    self.config.name, source,
                )
            else:
                self.debug_log(
                    f"activate_temporary_manual_override blocked – feature is OFF (source={source})"
                )
            return

        # Cancel any active clear-suppress window – explicit re-activation takes priority
        self._override_clear_suppress_until = None
        self._cleared_override_temp = None
        self._cleared_override_hvac = None
            
        # Merge with existing active override to prevent UI clobbering
        merged_temp = new_temp
        merged_hvac = new_hvac
        if self._temporary_manual_override and self._temporary_manual_override.active:
            if merged_temp is None:
                merged_temp = self._temporary_manual_override.target_temperature
            if merged_hvac is None:
                merged_hvac = self._temporary_manual_override.hvac_mode

        self.debug_log(f"Activating temporary override: temp={merged_temp}, hvac={merged_hvac}, source={source}")
        self._temporary_manual_override = TemporaryManualOverride(
            active=True,
            source=source,
            timestamp=now,
            schedule_revision=self._schedule_revision,
            target_temperature=merged_temp,
            hvac_mode=merged_hvac
        )
        
        # Start propagation phase for 15s
        self._override_propagation_until = now + timedelta(seconds=15)
        
        # We must trigger an update so the engine incorporates the override
        self._create_tracked_task(self.async_update())
        self._create_tracked_task(self._domain_save_debouncer.async_call())

    def clear_temporary_manual_override(self) -> None:
        """Deactivate the temporary manual override."""
        if self._temporary_manual_override and self._temporary_manual_override.active:
            self.debug_log("Clearing temporary override.")
            # Remember the cleared values: the suppress window below only mutes
            # TRV echoes that still carry THESE values — real new user input
            # (different values) must pass through immediately.
            self._cleared_override_temp = self._temporary_manual_override.target_temperature
            self._cleared_override_hvac = self._temporary_manual_override.hvac_mode
            self._temporary_manual_override = None
            # Suppress matching TRV echoes for 15s to prevent re-activation from
            # delayed TRV state-change callbacks that still carry the old
            # override temperature.
            self._override_clear_suppress_until = datetime.now(UTC) + timedelta(seconds=15)
            self._create_tracked_task(self.async_update())
            self._create_tracked_task(self._domain_save_debouncer.async_call())

    def _increment_schedule_revision(self) -> None:
        """The ONLY allowed mutation path for schedule revisions."""
        self._schedule_revision += 1
        self.debug_log(f"Schedule revision incremented to {self._schedule_revision}")
        
        # Temporary overrides die when the schedule changes logically
        self.clear_temporary_manual_override()

    async def async_unload(self) -> None:
        """Clean up listeners and timers."""
        self.debug_log("Unloading coordinator - cleaning up listeners and timers")
        if self._hysteresis_dirty:
            self._hysteresis_dirty = False
            try:
                await self._store.async_save({"outside_ok": self.engine.outside_ok})
            except Exception as exc:
                _LOGGER.warning("%s: Failed to flush hysteresis state on unload: %s", self.config.name, exc)
                
        # Flush domain state before shutting down debouncer to prevent data loss
        try:
            await self._async_save_domain_state()
        except Exception as exc:
            _LOGGER.warning("%s: Failed to flush domain state on unload: %s", self.config.name, exc)
            
        if self._save_debouncer:
            self._save_debouncer.async_shutdown()
        if self._domain_save_debouncer:
            self._domain_save_debouncer.async_shutdown()

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
        if not self._initialized:
            self.debug_log("Skipping update because domain state is still restoring.")
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
        """Coalesce concurrent refresh requests.

        If a refresh is already in progress, set a pending flag and return immediately.
        After the current refresh completes, run exactly one more pass to capture any
        changes made during the in-flight update. Prevents N×TRV-call cascades when
        multiple switches/services toggle simultaneously.
        """
        if self._refresh_in_progress:
            self._refresh_pending = True
            return
        self._refresh_in_progress = True
        try:
            while True:
                self._refresh_pending = False
                await self.async_update()
                if not self._refresh_pending:
                    break
        finally:
            self._refresh_in_progress = False

    def _validate_option(self, key: str, value: Any, duration_mins: int | None = None) -> bool:
        """Runtime validation for config options set via service calls."""
        if key == CONF_LEARNED_CLIMATE_RATE:
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

        override_temp = None
        override_hvac = None
        if self._temporary_manual_override:
            override_temp = self._temporary_manual_override.target_temperature
            override_hvac = self._temporary_manual_override.hvac_mode

        hvac_mode = self.engine.calculate_hvac_mode(_set_comfort=set_comfort, _manual_override_hvac=override_hvac)
        target_temp = self.engine.calculate_target_temperature(_set_comfort=set_comfort, _manual_override_temp=override_temp)

        # ── Schedule Fingerprinting (V1 Fix for Boot & Window Timers) ────────
        # Only semantic inputs: which scheduler is active + which adjustment block.
        # Excluded intentionally:
        #   sched_next – changes over time; Scheduler may be 'unavailable' on startup,
        #                causing a false-positive mismatch and deleting the override.
        #   cal_overrides – calendar events already have the highest priority in
        #                   calculate_hvac_mode / calculate_target_temperature,
        #                   so they override the output without needing to delete the override.
        active_sched = self.engine.get_active_scheduler() or ""
        adj_fp = self.engine.get_adjustment_fingerprint() or ""
        
        # V1 Fix für fehlende Slot-Wechsel: Wir brauchen den Zeitfortschritt (sched_next) wieder im Fingerprint.
        # Um den bekannten Boot-Bug (False-Positive wenn Scheduler kurz unavailable ist) zu verhindern,
        # übernehmen wir den alten Wert, falls sched_next aktuell None ist.
        sched_next = self.engine.get_next_schedule_transition()
        sched_next_str = str(sched_next) if sched_next else "None"
        
        last_fp_raw = getattr(self, "_last_schedule_fingerprint", None)
        # Wenn der Fingerprint bereits 3 Pipes hat, können wir den alten sched_next Wert sicher extrahieren (Index 2).
        if sched_next is None and last_fp_raw and len(last_fp_raw.split("|")) >= 3:
            # Fallback auf den alten Wert, um False-Positives beim Booten zu vermeiden
            sched_next_str = last_fp_raw.split("|")[2]

        away_fp = "away" if self.engine.is_away() else "home"
        current_fingerprint = f"{active_sched}|{adj_fp}|{sched_next_str}|{away_fp}"
        
        # Kernfix Bug 1: None und "" abfangen – kein False-Positive beim ersten Heartbeat nach Restore.
        # getattr-Default greift nur wenn Attribut FEHLT; None/"" werden explizit via `or` behandelt.
        last_fp = last_fp_raw or current_fingerprint
        
        if last_fp != current_fingerprint:
            self._schedule_transition_pending = True
            self.debug_log(f"Schedule fingerprint changed: {last_fp} -> {current_fingerprint}")
            
        self._last_schedule_fingerprint = current_fingerprint

        if self._temporary_manual_override:
            # Check 24h expiration (V1 Bugfix)
            now = datetime.now(UTC)
            expires_at = self._temporary_manual_override.timestamp + timedelta(hours=24)
            if now >= expires_at:
                self.debug_log("Temporary manual override expired (24h limit reached). Clearing.")
                self._schedule_transition_pending = True

        # ── Schedule-Transition: Override aufheben ──────────────────────────
        if self._schedule_transition_pending:
            self._schedule_transition_pending = False
            if self._temporary_manual_override:
                self.debug_log(
                    "Schedule transition detected. Clearing temporary manual override."
                )
                self._increment_schedule_revision()
                
                # If we cleared it, recalculate immediately so we don't apply the override on this tick
                hvac_mode = self.engine.calculate_hvac_mode(_set_comfort=set_comfort, _manual_override_hvac=None)
                target_temp = self.engine.calculate_target_temperature(_set_comfort=set_comfort, _manual_override_temp=None)

        changes, gen_offsets = self.engine.calculate_changes(
            self.last_generic_offsets,
            _target_temp=target_temp,
            _hvac_mode=hvac_mode,
        )

        self.current_hvac = hvac_mode
        self.current_temperature = target_temp
        self.current_state = self.engine.determine_heating_state()
        if self._temporary_manual_override and self.current_state not in (
            ClimateState.FROST_PROTECTION,
            ClimateState.WINDOW_OPEN,
        ):
            # Frost (chain priority) and open windows (A3) actually drive the
            # output while an override exists — showing "Temporary Manual
            # Override" would misreport why the room heats/doesn't heat.
            self.current_state = ClimateState.TEMPORARY_MANUAL_OVERRIDE
            
        self.current_reason = self._build_reason(set_comfort)

        self.last_changes = changes
        self.last_generic_offsets = gen_offsets

        if log == "debug" or self.config.debug_mode:
            # target_temp can still be None here (uncertainty) — no :.1f on None
            target_str = f"{target_temp:.1f}" if target_temp is not None else "None"
            self.debug_log(
                f"active={active} hvac={hvac_mode} target={target_str} changes={len(changes)} reason={self.current_reason}"
            )

        # Early return if uncertain
        if target_temp is None or hvac_mode is None:
            return False

        # 🟢🔴 Manual Override / Pause (hands-off) 🟢🔴
        if self.config.manual_override:
            # System layer: reconcile state
            # Lösche eventuell existierende temporäre Overrides, damit diese 
            # nach Beendigung der Pause nicht als "Shadow State" zurückkehren.
            self.clear_temporary_manual_override()
            self._call_listeners()
            return True

        # ── 1.2 Climate Rate Learning (v1.5.0) ───────────────────────────
        if self.config.smart_preconditioning:
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
        if self._prev_party and not is_party and self._scene_manager.has_scene("window"):
            self._scene_manager.clear("window")
            self.debug_log("Party ended – window scene discarded")
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
                    # T-2 Track sent command for Echo-Detection
                    self.track_sent_command(eid, change.get("temperature"), change.get("hvac_mode"))
                    await async_apply_trv_change(self.hass, self.config.name, change, secs)
                    # Success → reset circuit breaker. Only persist when the
                    # state actually changed — otherwise every successful TRV
                    # call would write the store to disk (SD-card wear).
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

                @callback
                def _schedule_transition_callback(_now):
                    """Triggers an update after a duration timer (e.g. window/presence) expires.
                    NOTE: We intentionally do NOT set _schedule_transition_pending = True here anymore,
                    because duration timers should NOT clear manual overrides."""
                    self._delayed_update(_now)

                self._reeval_timer = async_call_later(
                    self.hass, delay + 1, _schedule_transition_callback
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
        """Build a human-readable reason string from the current ClimateState.

        Args:
            set_comfort: Pre-computed comfort decision from _do_update().
                         Passed through for AWAY / weather details that need it.
                         Avoids re-calling should_set_comfort() which has
                         side-effects on _smart_preconditioning_active (CQS fix).
        """
        match self.current_state:
            case ClimateState.MANUAL_OVERRIDE:
                return "Manual Override"

            case ClimateState.TEMPORARY_MANUAL_OVERRIDE:
                return "Temporary Manual Override"

            case ClimateState.PAUSED:
                reasons = self.engine.get_uncertainty_reasons()
                if reasons:
                    short_reasons = [r.split(".")[-1] for r in reasons[:2] if isinstance(r, str)]
                    suffix = f" ({', '.join(short_reasons)})" if short_reasons else ""
                    return f"Paused{suffix}"
                return "Paused"

            case ClimateState.INACTIVE:
                parts = []
                if not self.engine.is_season_mode():
                    parts.append("Off")
                t = self.engine.check_outside_threshold()
                if t is False and self.engine.is_season_mode():
                    parts.append("Outside < Threshold" if self.engine.is_cooling else "Outside > Threshold")
                return " | ".join(parts) if parts else "Inactive"

            case ClimateState.FROST_PROTECTION:
                return "Frost Protection"

            case ClimateState.WINDOW_OPEN:
                return "Window Open"

            case ClimateState.LIMING:
                return "Liming Protection"

            case ClimateState.VACATION:
                _, vt = self.engine.is_vacation_mode()
                temp = vt if vt is not None else DEFAULT_VACATION_TEMP
                return f"Vacation Mode ({temp}°C)"

            case ClimateState.PARTY:
                _, pt = self.engine.check_party_mode()
                return f"Party Mode ({pt}°)" if pt else "Party Mode"

            case ClimateState.FORCE_COMFORT:
                return "Force Comfort"

            case ClimateState.FORCE_ECO:
                return "Force Eco"

            case ClimateState.ADJUSTMENT:
                adj = self.engine.get_active_adjustment()
                mode = self.engine.get_adjustment_mode(adj)
                return f"Adjustment ({mode})"

            case ClimateState.SMART_PRECONDITIONING:
                return "Smart Pre-Conditioning"

            case ClimateState.AWAY:
                if set_comfort:
                    parts = []
                    behavior = self.config.away_behavior
                    if behavior == "eco":
                        parts.append("🚶 Eco")
                    elif behavior == "off":
                        parts.append("🚶 Off")
                    else:
                        offset = self.config.away_offset
                        if offset != 0:
                            sign = "+" if self.engine.is_cooling else "-"
                            parts.append(f"🚶 {sign}{offset}°C")
                    
                    cal_tags = self.engine.get_calendar_tags()
                    if "comfort" in cal_tags:
                        parts.append(f"📅 {cal_tags['comfort']}°C")
                    if self.engine.is_sunshine_offset_active():
                        w_offset = self.engine.get_sunshine_offset()
                        if w_offset > 0:
                            w_sign = "+" if self.engine.is_cooling else "-"
                            parts.append(f"☀️ {w_sign}{w_offset}°C")
                    suffix = f" ({', '.join(parts)})" if parts else ""
                    return f"Comfort{suffix}"
                return "Eco"

            case ClimateState.COMFORT:
                parts = []
                if self.engine.is_holiday_today():
                    parts.append("Holiday")
                cal_tags = self.engine.get_calendar_tags()
                if "comfort" in cal_tags:
                    parts.append(f"📅 {cal_tags['comfort']}°C")
                if self.engine.is_sunshine_offset_active():
                    w_offset = self.engine.get_sunshine_offset()
                    if w_offset > 0:
                        w_sign = "+" if self.engine.is_cooling else "-"
                        parts.append(f"☀️ {w_sign}{w_offset}°C")
                suffix = f" ({', '.join(parts)})" if parts else ""
                return f"Comfort{suffix}"

            case ClimateState.ECO:
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
