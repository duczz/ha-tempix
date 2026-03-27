"""
Tempix – Schedule Mixin.

Scheduler selection, adjustments, core comfort decision logic,
and optimum start. Calendar integration lives in engine_calendar.py.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, UTC

from homeassistant.const import STATE_ON, STATE_OFF

from custom_components.tempix.const import (
    SCHEDULING_MODE_CALENDAR,
    INVALID_STATES,
    HeatingState,
)

_LOGGER = logging.getLogger(__name__)


class ScheduleMixin:
    """Scheduler selection, adjustments, and comfort decision."""

    # ── scheduler ────────────────────────────────────────────────────────────

    def get_active_scheduler(self) -> str | None:
        """Return the currently active scheduler entity ID."""
        schedulers = self.config.schedulers
        count = len(schedulers)
        if count == 0:
            return None

        selector_id = self.config.scheduler_selector

        # Calendar override (v1.5.8)
        tags = self.get_calendar_tags()
        forced_sched = tags.get("use_scheduler")
        if forced_sched:
            if forced_sched in schedulers:
                return forced_sched
            for s_id in schedulers:
                s_state = self._get_state(s_id)
                if s_state:
                    fname = s_state.attributes.get("friendly_name", "")
                    if forced_sched.lower() in fname.lower():
                        return s_id

        if count == 1 or not selector_id:
            return schedulers[0]

        sel_val = self._state_value(selector_id)
        if sel_val is None:
            return schedulers[0]

        # Numeric index
        try:
            idx = int(float(sel_val))
            idx = max(1, min(idx, count))
            return schedulers[idx - 1]
        except (ValueError, TypeError):
            pass

        if sel_val in [STATE_ON, STATE_OFF]:
            return schedulers[0] if sel_val == STATE_OFF else (schedulers[1] if count > 1 else schedulers[0])

        if sel_val in schedulers:
            return sel_val

        # Name match
        for s_id in schedulers:
            s_state = self._get_state(s_id)
            if s_state:
                fname = s_state.attributes.get("friendly_name", "")
                if fname.lower() == sel_val.lower():
                    return s_id

        for s_id in schedulers:
            s_state = self._get_state(s_id)
            if s_state:
                fname = s_state.attributes.get("friendly_name", "")
                if sel_val.lower() in fname.lower():
                    return s_id

        return None

    def is_scheduler_active(self) -> bool | None:
        """Return scheduler state. ``None`` if uncertain."""
        sched = self.get_active_scheduler()
        if sched is None:
            return False
        state_obj = self._get_state(sched)
        if not state_obj or state_obj.state in INVALID_STATES:
            return None
        return state_obj.state == STATE_ON

    def is_scheduler_defined(self) -> bool:
        """Return ``True`` if a scheduler or calendar is configured."""
        if self.config.scheduling_mode == SCHEDULING_MODE_CALENDAR:
            return bool(self.config.calendar)
        return self.get_active_scheduler() is not None

    # ── adjustments ──────────────────────────────────────────────────────────

    def get_active_adjustment(self) -> dict | None:
        """Return the best matching adjustment entry for the current time."""
        adjustments = self.config.adjustments
        if not adjustments:
            return None

        now = datetime.now(UTC)
        scheduler_name: str | None = None
        active_sched = self.get_active_scheduler()
        if active_sched:
            s = self._get_state(active_sched)
            if s:
                name = s.attributes.get("friendly_name")
                if isinstance(name, str):
                    scheduler_name = name

        tags = self.get_calendar_tags()
        forced_day = tags.get("use_day")

        for day_offset in (0, -1):
            ts = now + timedelta(days=day_offset)
            current_day = forced_day if (day_offset == 0 and forced_day) else ts.strftime("%a")
            current_time = ts.strftime("%H:%M") if day_offset == 0 else "23:59"

            valid = [e for e in adjustments if "time" in e and e["time"] <= current_time]

            candidates = []
            for entry in valid:
                days = entry.get("days")
                sched = entry.get("scheduler")
                if days is not None and current_day not in str(days):
                    continue
                if sched is not None and isinstance(scheduler_name, str) and sched not in scheduler_name:
                    continue
                candidates.append(entry)

            if candidates:
                return sorted(candidates, key=lambda e: e["time"], reverse=True)[0]

        return None

    def get_adjustment_comfort(self, entry: dict | None) -> float | None:
        """Extract comfort temp from adjustment entry."""
        if not entry or "comfort" not in entry:
            return None
        v = entry["comfort"]
        try:
            return float(v)
        except (ValueError, TypeError):
            return self._float_state(v)

    def get_adjustment_eco(self, entry: dict | None) -> float | None:
        """Extract eco temp from adjustment entry."""
        if not entry or "eco" not in entry:
            return None
        v = entry["eco"]
        try:
            return float(v)
        except (ValueError, TypeError):
            return self._float_state(v)

    def get_adjustment_mode(self, entry: dict | None) -> str:
        """Extract mode override from adjustment."""
        if not entry or "mode" not in entry:
            return "auto"
        return entry["mode"]

    def get_adjustment_calibration(self, entry: dict | None) -> bool:
        """Extract calibration toggle from adjustment."""
        if not entry or "calibration" not in entry:
            return True
        return entry["calibration"] == "on"

    # ── optimum start ────────────────────────────────────────────────────────

    def is_optimum_start_active(self) -> bool:
        """Return ``True`` if optimum-start is currently pre-heating."""
        return getattr(self, "_optimum_start_active", False)

    # ── core comfort decision ────────────────────────────────────────────────

    def should_set_comfort(self, entry_mode: str = "auto") -> bool | None:
        """Return True (comfort), False (eco), or None (uncertain).

        Priority chain – highest wins, first match returns:
          1. force_comfort switch active           → True
          2. entry_mode == "eco"                   → False
          3. entry_mode == "comfort"               → True
          4. party mode active                     → True
          5. force_eco switch active               → False
          6. away mode active                      → True  (away schedule overrides)
          7. calendar event active                 → comfort_state = True
          8. scheduler active (non-calendar mode)  → comfort_state = True
          9. presence sensor active                → comfort_state = True
         10. optimum start window                  → comfort_state = True
         11. persons home + force_comfort time     → True
         12. persons home                          → comfort_state
         13. no persons/proximity defined          → is_anybody_home_or_proximity()

        To add a new condition: determine its priority relative to the chain above,
        insert at the correct position, and update this docstring accordingly.
        """
        scheduling_mode = self.config.scheduling_mode
        self._optimum_start_active = False
        if self.is_force_comfort_temp():
            return True
        if entry_mode == "eco":
            return False
        if entry_mode == "comfort":
            return True

        is_party, _ = self.check_party_mode()
        if is_party:
            return True
        if self.is_force_eco_temp():
            return False
        if self.is_away():
            return True

        sched_defined = self.is_scheduler_defined()
        pres_defined = self.is_presence_sensor_defined()

        comfort_state = False
        sched_uncertain = False
        pres_uncertain = False

        cached_home_status = None
        if self.is_person_defined() or self.is_proximity_defined():
            cached_home_status = self.is_anybody_home_or_proximity()

        # Calendar Integration
        if self.is_calendar_comfort_active():
            comfort_state = True

        # Scheduling / Presence
        if not comfort_state:
            if scheduling_mode != SCHEDULING_MODE_CALENDAR:
                if sched_defined:
                    sched_active = self.is_scheduler_active()
                    if sched_active is None:
                        return None
                    elif sched_active:
                        comfort_state = True

            if not comfort_state and pres_defined:
                pres_active = self.is_presence_active()
                if pres_active is None:
                    return None
                elif pres_active:
                    comfort_state = True

            if not comfort_state and scheduling_mode != SCHEDULING_MODE_CALENDAR:
                if not sched_defined and not pres_defined:
                    return cached_home_status

        # Optimum Start
        if self.config.optimum_start and not comfort_state:
            next_start = self.get_next_schedule_transition()
            if next_start:
                room_temp = self._resolve_room_temp()
                target_comfort = self.resolve_comfort_temperature()
                if room_temp is not None and target_comfort is not None and (target_comfort - room_temp) * self._factor > 0:
                    heat_up_rate = max(0.1, self.config.learned_heating_rate)
                    diff = abs(target_comfort - room_temp)
                    preheat_minutes = (diff / heat_up_rate) * 60

                    max_duration = self.config.max_optimum_start
                    max_minutes = max_duration.total_seconds() / 60
                    preheat_minutes = min(max_minutes, preheat_minutes)

                    now = datetime.now(UTC)
                    if now + timedelta(minutes=preheat_minutes) >= next_start:
                        label = "Pre-cooling" if self._factor == -1 else "Pre-heating"
                        self.debug_log(
                            f"Smart Preheating: {label} {preheat_minutes:.1f}min early for {next_start}. "
                            f"(Rate: {heat_up_rate}°C/h, Diff: {diff:.1f}°C)"
                        )
                        self._optimum_start_active = True
                        comfort_state = True

        # Person / Proximity Check
        if self.is_person_defined() or self.is_proximity_defined():
            if cached_home_status is None:
                return None
            if not cached_home_status:
                return False

            if self.config.persons_force_comfort:
                start_time_str = self.config.persons_force_comfort_start
                end_time_str = self.config.persons_force_comfort_end

                try:
                    now_time = datetime.now().time()
                    start_time = datetime.strptime(start_time_str, "%H:%M:%S").time()
                    end_time = datetime.strptime(end_time_str, "%H:%M:%S").time()

                    is_in_range = False
                    if start_time <= end_time:
                        is_in_range = start_time <= now_time <= end_time
                    else:
                        is_in_range = now_time >= start_time or now_time <= end_time

                    if is_in_range:
                        return True
                except (ValueError, TypeError):
                    return True

            if not comfort_state and (sched_uncertain or pres_uncertain):
                return None

            return comfort_state

        if not comfort_state and (sched_uncertain or pres_uncertain):
            return None
        return comfort_state

    # ── HVAC mode ────────────────────────────────────────────────────────────

    def calculate_hvac_mode(self, _set_comfort: bool | None = None) -> str | None:
        """Full HVAC-mode chain. Returns ``None`` if data is uncertain."""
        overrides = self.get_calendar_overrides()
        if "hvac" in overrides:
            return overrides["hvac"]

        if self.is_frost_protection():
            return self.config.hvac_mode_comfort

        idle_temp = self.config.idle_temperature
        if not self.is_automation_active():
            return "off" if idle_temp == 0 else self.config.hvac_mode_comfort

        window_open_status = self.is_window_open()
        if window_open_status is None:
            return None

        window_temp = self.resolve_window_open_temperature()
        if window_open_status and window_temp == 0 and not (self.is_force_comfort_temp() or self.is_liming_time()):
            return self.config.hvac_mode_comfort

        adj = self.get_active_adjustment()
        entry_mode = self.get_adjustment_mode(adj)
        if entry_mode == "off":
            return "off"

        if self.config.off_if_nobody_home:
            if self.is_person_defined() or self.is_proximity_defined():
                home_status = self.is_anybody_home_or_proximity()
                if home_status is None:
                    return None
                if not home_status:
                    set_comfort = _set_comfort if _set_comfort is not None else self.should_set_comfort(entry_mode)
                    if set_comfort is None:
                        return None
                    if not set_comfort:
                        return "off"

        set_comfort = _set_comfort if _set_comfort is not None else self.should_set_comfort(entry_mode)
        if set_comfort is None:
            return None

        return self.config.hvac_mode_comfort if set_comfort else self.config.hvac_mode_eco

    # ── reset data ───────────────────────────────────────────────────────────

    def calculate_reset_data(
        self,
        _is_change_trigger: bool = False,
        _is_physical_or_ui: bool = False,
        _change_temperature: float | None = None,
    ) -> list[dict]:
        """Compute per-entity temperature resets.

        Legacy: Previously wrote back to external helper entities.
        Since comfort/eco are now internal number entities managed via
        config_entries, no external reset is needed.
        """
        return []

    def determine_heating_state(self) -> HeatingState:
        """Determine the current heating state as an explicit enum value.

        Mirrors the priority chain of ``calculate_target_temperature`` and
        ``should_set_comfort`` without duplicating any logic. All existing
        methods remain unchanged – this method is purely additive.

        Priority (highest wins):
            FROST_PROTECTION > INACTIVE > WINDOW_OPEN > LIMING > PARTY >
            FORCE_COMFORT > FORCE_ECO > ADJUSTMENT > SMART_PREHEATING >
            AWAY > COMFORT/ECO > PAUSED (uncertain)
        """
        if self.config.manual_override_pause:
            return HeatingState.MANUAL_OVERRIDE

        if self.is_frost_protection():
            return HeatingState.FROST_PROTECTION

        if not self.is_automation_active():
            return HeatingState.INACTIVE

        window_open = self.is_window_open()
        if window_open is None:
            return HeatingState.PAUSED
        if window_open:
            return HeatingState.WINDOW_OPEN

        if self.is_liming_time():
            return HeatingState.LIMING

        is_party, _ = self.check_party_mode()
        if is_party:
            return HeatingState.PARTY

        if self.is_force_comfort_temp():
            return HeatingState.FORCE_COMFORT
        if self.is_force_eco_temp():
            return HeatingState.FORCE_ECO

        adj = self.get_active_adjustment()
        mode = self.get_adjustment_mode(adj)
        if mode != "auto":
            return HeatingState.ADJUSTMENT

        if self.is_optimum_start_active():
            return HeatingState.SMART_PREHEATING

        set_comfort = self.should_set_comfort(mode)
        if set_comfort is None:
            return HeatingState.PAUSED

        if set_comfort and self.is_away():
            return HeatingState.AWAY

        return HeatingState.COMFORT if set_comfort else HeatingState.ECO

    # ── schedule transition ──────────────────────────────────────────────────

    def get_next_schedule_transition(self) -> datetime | None:
        """Find the next time the active scheduler or calendar will turn ON."""
        mode = self.config.scheduling_mode

        entities: list[str] = []
        if mode == SCHEDULING_MODE_CALENDAR:
            entities = self.config.calendar
        else:
            sched_id = self.get_active_scheduler()
            if sched_id:
                entities = [sched_id]

        if not entities:
            return None

        next_transitions: list[datetime] = []

        for eid in entities:
            state = self._get_state(eid)
            if not state:
                continue

            for attr in ["start_time", "next_event", "next_trigger", "next_occurrence", "next_transition"]:
                val = state.attributes.get(attr)
                if val:
                    try:
                        if isinstance(val, str):
                            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                        else:
                            dt = val
                        if isinstance(dt, datetime):
                            dt = self._ensure_utc(dt)
                            if dt > datetime.now(UTC):
                                next_transitions.append(dt)
                    except (ValueError, TypeError):
                        pass

        return min(next_transitions) if next_transitions else None

    def get_next_duration_event(self) -> datetime | None:
        """Earliest future timestamp when a duration-based state confirmation fires."""
        now = datetime.now(UTC)
        events: list[datetime] = []

        # Persons
        persons = self.config.persons
        if persons:
            entering_duration = self.config.people_entering_duration
            leaving_duration = self.config.people_leaving_duration
            for p in persons:
                state_obj = self._get_state(p)
                if state_obj and state_obj.state not in INVALID_STATES:
                    last_changed = self._ensure_utc(state_obj.last_changed)
                    if not last_changed:
                        continue

                    from homeassistant.const import STATE_HOME
                    if state_obj.state == STATE_HOME:
                        if entering_duration > timedelta(0):
                            events.append(last_changed + entering_duration)
                    else:
                        if leaving_duration > timedelta(0):
                            events.append(last_changed + leaving_duration)

        # Presence
        presence_sensor = self.config.presence_sensor
        if presence_sensor:
            state_obj = self._get_state(presence_sensor)
            if state_obj and state_obj.state not in INVALID_STATES:
                sensor_on = state_obj.state == STATE_ON
                delta = self.config.presence_reaction_on if sensor_on else self.config.presence_reaction_off
                if delta > timedelta(0):
                    last_changed = self._ensure_utc(state_obj.last_changed)
                    if last_changed:
                        events.append(last_changed + delta)

        # Windows
        window_sensors = self.config.window_sensors
        if window_sensors:
            open_delta = self.config.window_reaction_open
            close_delta = self.config.window_reaction_close

            for sid in window_sensors:
                state_obj = self._get_state(sid)
                if state_obj and state_obj.state not in INVALID_STATES:
                    last_changed = self._ensure_utc(state_obj.last_changed)
                    if not last_changed:
                        continue

                    if state_obj.state in [STATE_ON, "open", "tilted"]:
                        if open_delta > timedelta(0):
                            events.append(last_changed + open_delta)
                    elif state_obj.state in [STATE_OFF, "closed"]:
                        if close_delta > timedelta(0):
                            events.append(last_changed + close_delta)

        # Optimum Start
        if self.config.optimum_start:
            next_start = self.get_next_schedule_transition()
            if next_start:
                room_temp = self._resolve_room_temp()
                target_comfort = self.resolve_comfort_temperature()
                if room_temp is not None and target_comfort is not None and (target_comfort - room_temp) * self._factor > 0:
                    heat_up_rate = self._get_effective_heating_rate()
                    preheat_minutes = (abs(target_comfort - room_temp) / heat_up_rate) * 60

                    max_dur = self.config.max_optimum_start
                    max_mins = max_dur.total_seconds() / 60.0
                    preheat_minutes = min(max_mins, preheat_minutes)
                    events.append(next_start - timedelta(minutes=preheat_minutes))

        future_events = [e for e in events if e > now]
        return min(future_events) if future_events else None

    def _get_effective_heating_rate(self) -> float:
        """Heating rate adjusted by outside temperature (T1 normalization)."""
        base_rate = self.config.learned_heating_rate
        outside_temp = self._resolve_outside_temp()

        if outside_temp is None:
            return base_rate

        ref_temp = 5.0
        sensitivity = 0.04
        factor = 1.0 - (ref_temp - outside_temp) * sensitivity
        factor = max(0.3, min(2.0, factor))

        return base_rate * factor
