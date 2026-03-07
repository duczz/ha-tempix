"""
Tempix – Calendar Mixin.

Event lookup, tag parsing, comfort-activation check, and the
dashboard schedule-period display for calendar-mode rooms.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, UTC
from typing import Any

from homeassistant.const import STATE_ON, STATE_OFF, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.util import dt as dt_util

from custom_components.tempix.const import (
    SCHEDULING_MODE_CALENDAR,
    DEFAULT_MIN_TEMP,
    DEFAULT_MAX_TEMP,
)

_LOGGER = logging.getLogger(__name__)


class CalendarMixin:
    """Calendar integration: event lookup, tag parsing, comfort activation."""

    # ── Calendar Integration (v1.4.0) ────────────────────────────────────────

    def _get_active_calendar_event(self, force_check: bool = False, active_only: bool = True) -> dict[str, Any] | None:
        """Find the prioritized active calendar event matching filters."""
        if not force_check and self.config.scheduling_mode != SCHEDULING_MODE_CALENDAR:
            return None

        calendars = self.config.calendar
        if not calendars:
            return None

        if self._calendar_events:
            combined_events: list[dict] = []
            for cal_id in calendars:
                events = self._calendar_events.get(cal_id, [])
                for ev in events:
                    ev["calendar_id"] = cal_id
                    combined_events.append(ev)

            best_event = self._process_event_list(combined_events, active_only=active_only)
            if best_event:
                return best_event

        state_events: list[dict] = []
        for cal_id in calendars:
            state = self._get_state(cal_id)
            if not state or state.state in [STATE_UNAVAILABLE, STATE_UNKNOWN]:
                continue

            summary = (
                state.attributes.get("message") or
                state.attributes.get("summary") or
                (state.state if state.state not in [STATE_ON, STATE_OFF] else "") or
                "Termin"
            )
            state_events.append({
                "summary": summary,
                "location": state.attributes.get("location", ""),
                "description": state.attributes.get("description", ""),
                "start_time": state.attributes.get("start_time") or state.attributes.get("dtstart"),
                "end_time": state.attributes.get("end_time") or state.attributes.get("dtend"),
                "calendar_id": cal_id,
                "is_active": state.state != STATE_OFF,
            })

        return self._process_event_list(state_events, active_only=active_only, use_at_state=True)

    def _get_delegated_event(self, cal_id: str, day_name: str) -> dict[str, Any] | None:
        """Find the best event matching the weekday on a specific calendar."""
        if not self._calendar_events or cal_id not in self._calendar_events:
            return None

        events = self._calendar_events.get(cal_id, [])
        if not events:
            return None

        delegated_events = []
        for ev in events:
            start_dt = self._parse_dt(ev.get("start_time") or ev.get("start"))
            if start_dt and start_dt.strftime("%a") == day_name:
                delegated_events.append(ev)

        if delegated_events:
            return self._process_event_list(delegated_events, active_only=False)
        return None

    def _process_event_list(self, events: list[dict], active_only: bool = True, use_at_state: bool = False) -> dict | None:
        """Find the best matching event from a list (filter + priority)."""
        event_filters = self.config.calendar_event
        room_filter = self.config.calendar_room
        now = datetime.now(UTC)

        filters: list[str] = []
        if event_filters:
            filters = [f.strip().lower() for f in re.split(r"[,;]", event_filters) if f.strip()]

        best_event = None
        best_prio = 999
        best_tier = 9
        best_duration_prio = 9
        best_cal_idx = 99
        best_time_dist = 0.0

        for ev in events:
            if "start" in ev and "start_time" not in ev:
                ev["start_time"] = ev["start"]
            if "end" in ev and "end_time" not in ev:
                ev["end_time"] = ev["end"]

            summary = ev.get("summary", "Termin")
            location = (ev.get("location") or "").lower()
            start_dt = self._parse_dt(ev.get("start_time"))
            end_dt = self._parse_dt(ev.get("end_time"))

            is_active = False
            if use_at_state:
                is_active = ev.get("is_active", False)
            else:
                if start_dt and start_dt <= now:
                    if not end_dt or end_dt > now:
                        is_active = True

            if active_only and not is_active:
                continue

            # Location filter
            if room_filter:
                room_filters = [rf.strip().lower() for rf in re.split(r"[,;]", room_filter) if rf.strip()]
                event_locations = [loc.strip().lower() for loc in re.split(r"[,;]", location) if loc.strip()]
                if not event_locations:
                    event_locations = [""]

                match_found = any(rf in el for rf in room_filters for el in event_locations)
                if not match_found:
                    continue

            found_prio = 999
            if filters:
                for i, f in enumerate(filters):
                    if f in summary.lower():
                        found_prio = i
                        break
                if found_prio == 999:
                    continue

            is_all_day = start_dt and end_dt and start_dt.hour == 0 and start_dt.minute == 0 and end_dt.hour == 0 and end_dt.minute == 0
            current_tier = 0 if is_active else 1
            duration_prio = 1 if is_all_day else 0

            time_dist = 0.0
            if not is_active:
                if start_dt and start_dt > now:
                    time_dist = (start_dt - now).total_seconds()
                elif end_dt and end_dt <= now:
                    time_dist = (now - end_dt).total_seconds() + 1_000_000_000.0

            calendars = self.config.calendar
            cal_id = ev.get("calendar_id")
            cal_idx = calendars.index(cal_id) if cal_id in calendars else 99

            is_better = False
            if best_event is None:
                is_better = True
            else:
                current_score = (current_tier, time_dist, found_prio, duration_prio, cal_idx)
                best_score = (best_tier, best_time_dist, best_prio, best_duration_prio, best_cal_idx)
                if current_score < best_score:
                    is_better = True

            if is_better:
                best_prio = found_prio
                best_event = ev
                best_tier = current_tier
                best_time_dist = time_dist
                best_duration_prio = duration_prio
                best_cal_idx = cal_idx

        return best_event

    # ── dashboard schedule period ────────────────────────────────────────────

    def get_active_schedule_period(self) -> str:
        """Dashboard: return start–end time of current period."""
        mode = self.config.scheduling_mode

        if mode == SCHEDULING_MODE_CALENDAR:
            event = self._get_active_calendar_event(force_check=True, active_only=False)
            if event:
                tags = self.get_calendar_tags(active_only=False)
                forced_day = tags.get("use_day")
                delegated_event = tags.get("_delegated_event")
                suffix = f" ({forced_day})" if forced_day else ""

                target_event = delegated_event if delegated_event and forced_day else event

                start_dt = self._parse_dt(target_event.get("start_time") or target_event.get("start"))
                end_dt = self._parse_dt(target_event.get("end_time") or target_event.get("end"))

                is_all_day = target_event.get("all_day", False) or (
                    start_dt and end_dt and start_dt.hour == 0 and start_dt.minute == 0 and end_dt.hour == 0 and end_dt.minute == 0
                )

                if tags.get("time"):
                    return f"{tags['time']}{suffix}"

                if not is_all_day and start_dt and end_dt:
                    tz = dt_util.get_time_zone(self.hass.config.time_zone)
                    return f"{start_dt.astimezone(tz).strftime('%H:%M')} - {end_dt.astimezone(tz).strftime('%H:%M')}{suffix}"

                if forced_day:
                    tz = dt_util.get_time_zone(self.hass.config.time_zone)
                    now_time = datetime.now(tz).strftime("%H:%M")
                    entry = self.get_active_adjustment()

                    adjustments = self.config.adjustments
                    day_entries = [e for e in adjustments if forced_day in str(e.get("days", ""))]
                    day_entries.sort(key=lambda x: x.get("time", ""))

                    if entry:
                        start = entry.get("time", "00:00")
                        next_time = "23:59"
                        for e in day_entries:
                            if e.get("time", "") > start:
                                next_time = e["time"]
                                break
                        return f"{start} - {next_time}{suffix}"
                    elif day_entries:
                        for e in day_entries:
                            if e.get("time", "") > now_time:
                                return f"Sparbetrieb bis {e['time']}{suffix}"
                        return f"Sparbetrieb bis 23:59{suffix}"
                    else:
                        return f"00:00 - 23:59{suffix}"

                if tags.get("use_scheduler"):
                    sched_id = self.get_active_scheduler()
                    if sched_id:
                        state = self._get_state(sched_id)
                        if state:
                            prefix = "Heizen bis" if state.state == STATE_ON else "Sparbetrieb bis"
                            tz = dt_util.get_time_zone(self.hass.config.time_zone)
                            for attr in ["next_event", "next_trigger", "next_transition", "next_occurrence"]:
                                next_ev = state.attributes.get(attr)
                                dt = self._parse_dt(next_ev)
                                if dt:
                                    end_str = dt.astimezone(tz).strftime('%H:%M')
                                    return f"{prefix} {end_str} ({state.attributes.get('friendly_name')}){suffix}"

                if is_all_day:
                    return "Ganztägig"

                return "Keine Zeitspanne"
            return "Keine Zeitspanne"

        # Helper Mode
        sched_id = self.get_active_scheduler()
        if sched_id:
            state = self._get_state(sched_id)
            if state:
                prefix = "Heizen bis" if state.state == STATE_ON else "Sparbetrieb bis"
                for attr in ["next_event", "next_trigger", "next_transition", "next_occurrence"]:
                    next_ev = state.attributes.get(attr)
                    dt = self._parse_dt(next_ev)
                    if dt:
                        tz = dt_util.get_time_zone(self.hass.config.time_zone)
                        return f"{prefix} {dt.astimezone(tz).strftime('%H:%M')}"

        return "Keine Zeitspanne"

    # ── calendar overrides / tags ─────────────────────────────────────────────

    def get_calendar_overrides(self, active_only: bool = True) -> dict[str, Any]:
        """Extract comfort, eco and hvac from calendar event description."""
        return self.get_calendar_tags(active_only=active_only)

    def get_calendar_tags(self, event: dict | None = None, active_only: bool = True, depth: int = 0) -> dict[str, Any]:
        """Centralized parsing of all allowed tags in calendar descriptions.

        Supports recursive inheritance if ``use_day`` or ``use_scheduler`` is present.
        """
        if depth > 2:
            return {}

        if not event:
            event = self._get_active_calendar_event(active_only=active_only)
            if not event and not active_only:
                event = self._get_active_calendar_event(active_only=False)

        if not event:
            return {}

        description = (event.get("description") or "").strip()
        tags: dict[str, Any] = {}

        if description:
            comfort_match = re.search(r"comfort:\s*([\d.]+)", description, re.IGNORECASE)
            if comfort_match:
                try:
                    tags["comfort"] = max(DEFAULT_MIN_TEMP, min(DEFAULT_MAX_TEMP, float(comfort_match.group(1))))
                except ValueError:
                    pass

            eco_match = re.search(r"eco:\s*([\d.]+)", description, re.IGNORECASE)
            if eco_match:
                try:
                    tags["eco"] = max(DEFAULT_MIN_TEMP, min(DEFAULT_MAX_TEMP, float(eco_match.group(1))))
                except ValueError:
                    pass

            hvac_match = re.search(r"hvac:\s*(\w+)", description, re.IGNORECASE)
            if hvac_match:
                tags["hvac"] = hvac_match.group(1).lower()

            time_match = re.search(r"time:\s*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", description, re.IGNORECASE)
            if time_match:
                tags["time"] = f"{time_match.group(1)} - {time_match.group(2)}"

            day_match = re.search(r"use_day:\s*(\w+)(?:\s*@([\w.]+))?", description, re.IGNORECASE)
            if day_match:
                day_str = day_match.group(1).title()
                day_map = {
                    "Monday": "Mon", "Tuesday": "Tue", "Wednesday": "Wed",
                    "Thursday": "Thu", "Friday": "Fri", "Saturday": "Sat", "Sunday": "Sun",
                }
                tags["use_day"] = day_map.get(day_str, day_str[:3])
                if day_match.group(2):
                    tags["delegate_calendar"] = day_match.group(2).strip()

            sched_match = re.search(r"use_scheduler:\s*([^@\n]+)(?:\s*@([\w.]+))?", description, re.IGNORECASE)
            if sched_match:
                tags["use_scheduler"] = sched_match.group(1).strip()
                if sched_match.group(2):
                    tags["delegate_calendar"] = sched_match.group(2).strip()

        # Handle Inheritance
        delegate_cal = tags.get("delegate_calendar")
        forced_day = tags.get("use_day")
        if delegate_cal and forced_day:
            delegated_event = self._get_delegated_event(delegate_cal, forced_day)
            if delegated_event:
                parent_tags = self.get_calendar_tags(event=delegated_event, active_only=False, depth=depth + 1)
                for k, v in parent_tags.items():
                    if k not in tags:
                        tags[k] = v
                tags["_delegated_event"] = delegated_event

        return tags

    # ── calendar comfort active ──────────────────────────────────────────────

    def is_calendar_comfort_active(self) -> bool | None:
        """Check if any calendar event is active or starting soon (preheat)."""
        if self.config.scheduling_mode != SCHEDULING_MODE_CALENDAR:
            return False

        calendars = self.config.calendar
        for cid in calendars:
            state = self._get_state(cid)
            if not state or state.state in [STATE_UNAVAILABLE, STATE_UNKNOWN]:
                return None

        event = self._get_active_calendar_event(active_only=False)
        if not event:
            return False

        tags = self.get_calendar_tags(active_only=False)
        time_tag = tags.get("time")

        delegated_event = tags.get("_delegated_event")
        forced_day = tags.get("use_day")

        start_time = self._parse_dt(event.get("start_time") or event.get("start"))
        end_time = self._parse_dt(event.get("end_time") or event.get("end"))

        if delegated_event and forced_day:
            d_start = self._parse_dt(delegated_event.get("start_time") or delegated_event.get("start"))
            d_end = self._parse_dt(delegated_event.get("end_time") or delegated_event.get("end"))

            if d_start and d_end:
                now_local = dt_util.now()
                start_time = now_local.replace(hour=d_start.hour, minute=d_start.minute, second=0, microsecond=0)
                end_time = now_local.replace(hour=d_end.hour, minute=d_end.minute, second=0, microsecond=0)

                if end_time < start_time:
                    if now_local >= start_time:
                        end_time += timedelta(days=1)
                    else:
                        start_time -= timedelta(days=1)

                start_time = start_time.astimezone(UTC)
                end_time = end_time.astimezone(UTC)

        daily_start_found, daily_end_found = self._get_daily_time_window_dt(time_tag if time_tag else "")

        max_dur = self.config.max_optimum_start
        max_mins = max_dur.total_seconds() / 60.0

        preheat_minutes = 0.0
        if self.config.optimum_start:
            room_temp = self._resolve_room_temp()
            target_comfort = self.resolve_comfort_temperature()
            if room_temp is not None and target_comfort is not None and room_temp < target_comfort:
                heat_up_rate = self._get_effective_heating_rate()
                preheat_minutes = min(max_mins, ((target_comfort - room_temp) / heat_up_rate) * 60)

        now = datetime.now(UTC)

        if start_time and start_time <= now:
            if not end_time or end_time > now:
                if daily_start_found and daily_end_found:
                    tz = dt_util.get_time_zone(self.hass.config.time_zone)
                    local_now = datetime.now(tz)

                    if daily_start_found <= local_now < daily_end_found:
                        return True

                    if preheat_minutes > 0:
                        dt_preheat_start = daily_start_found - timedelta(minutes=preheat_minutes)
                        if dt_preheat_start <= local_now < daily_start_found:
                            return True

                    return False
                return True
            else:
                return False

        if preheat_minutes > 0 and start_time:
            preheat_start = start_time - timedelta(minutes=preheat_minutes)
            if preheat_start <= now < start_time:
                return True

        return False

    def _get_daily_time_window_dt(self, description: str) -> tuple[datetime | None, datetime | None]:
        """Parse ``time: HH:MM - HH:MM`` from description and return as today's datetimes."""
        time_match = re.search(r"time:\s*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", description, re.IGNORECASE)
        if not time_match:
            return None, None

        try:
            tz = dt_util.get_time_zone(self.hass.config.time_zone)
            local_now = datetime.now(tz)

            h1, m1 = map(int, time_match.group(1).split(":"))
            h2, m2 = map(int, time_match.group(2).split(":"))

            start = local_now.replace(hour=h1, minute=m1, second=0, microsecond=0)
            end = local_now.replace(hour=h2, minute=m2, second=0, microsecond=0)

            if end < start:
                if local_now >= start:
                    end += timedelta(days=1)
                else:
                    start -= timedelta(days=1)
            return start, end
        except (ValueError, IndexError):
            return None, None
