"""
Tempix – Protection Mixin.

Season mode, outside-temperature threshold, automation-active gate,
window detection, frost protection, and liming protection.
"""
from __future__ import annotations

from datetime import datetime, timedelta, UTC
from typing import Any

from homeassistant.const import STATE_ON, STATE_OFF, STATE_HOME

from custom_components.tempix.const import (
    INVALID_STATES,
)


class ProtectionMixin:
    """Season mode, window detection, frost & liming protection."""

    # ── season mode ──────────────────────────────────────────────────────────

    def is_season_mode(self) -> bool:
        """Return ``True`` if season is active (or no entity configured)."""
        entity_id = self.config.season_mode_entity
        if not entity_id:
            return True
        s = self._state_value(entity_id)
        return s == STATE_ON if s is not None else True

    # ── outside temperature threshold ────────────────────────────────────────

    def check_outside_threshold(self) -> bool | None:
        """Return ``True`` = heating ON, ``False`` = OFF, ``None`` = not configured.

        Uses hysteresis to prevent rapid switching when the outside temperature
        hovers near the threshold.  For heating (_factor=1):
          - Turn OFF only when temp >= threshold + hysteresis
          - Turn ON  only when temp <  threshold - hysteresis
          - In the dead zone: keep previous state
        """
        sensor_id = self.config.outside_temp_sensor
        if not sensor_id:
            return None

        threshold = self.config.outside_temp_threshold
        hysteresis = self.config.outside_temp_hysteresis
        fallback = self.config.outside_temp_fallback

        temp = self._temp_state(sensor_id)
        if temp is None:
            return fallback

        t = float(temp)
        if self._last_outside_ok is None:
            # First evaluation — no previous state, use plain threshold
            outside_ok = (t - threshold) * self._factor < 0
        elif self._last_outside_ok:
            # Currently heating — only turn OFF if clearly above threshold
            outside_ok = (t - (threshold + hysteresis)) * self._factor < 0
        else:
            # Currently off — only turn ON if clearly below threshold
            outside_ok = (t - (threshold - hysteresis)) * self._factor < 0

        self._last_outside_ok = outside_ok

        use_room = self.config.room_temp_threshold_enabled
        if use_room:
            room_threshold = self.config.room_temp_threshold
            room_temp = self._resolve_room_temp()
            if room_temp is not None:
                room_ok = (room_temp - room_threshold) * self._factor < 0
                return outside_ok and room_ok

        return outside_ok

    # ── automation active ────────────────────────────────────────────────────

    def is_automation_active(self) -> bool:
        """Gate: season_mode AND outside_temp AND manual_switch."""
        if not self.config.automation_active:
            return False
        season = self.is_season_mode()
        outside = self.check_outside_threshold()
        if outside is None:
            return season
        return season and outside

    # ── window detection ─────────────────────────────────────────────────────

    def is_window_open(self) -> bool | None:
        """Window state with reaction-time awareness. ``None`` if sensors are unknown."""
        sensors = self.config.window_sensors
        if not sensors:
            return False

        now = datetime.now(UTC)
        open_delta = self.config.window_reaction_open
        close_delta = self.config.window_reaction_close

        on_time = now - open_delta
        off_time = now - close_delta

        has_open = False
        closed_but_recent = False

        invalid_count = 0

        for sid in sensors:
            state = self._get_state(sid)
            if not state or state.state in INVALID_STATES:
                self.debug_log(f"Window sensor {sid} is in invalid state: {state.state if state else 'None'}")
                invalid_count += 1
                continue

            try:
                last_changed = self._ensure_utc(state.last_changed)
            except Exception:
                last_changed = state.last_changed

            if state.state in [STATE_ON, "open", "tilted"]:
                if last_changed <= on_time:
                    has_open = True
            elif state.state in [STATE_OFF, "closed"]:
                is_reboot_timestamp = False
                grace_dur = self.config.sensor_retention
                if self._startup_time and last_changed:
                    diff = abs((last_changed - self._startup_time).total_seconds())
                    if diff < grace_dur.total_seconds():
                        is_reboot_timestamp = True
                        self.debug_log(f"Reboot detected for window {sid} (diff: {diff:.1f}s). Ignoring close delay.")

                if not is_reboot_timestamp and last_changed >= off_time:
                    closed_but_recent = True
                    self.debug_log(f"Window {sid} recently closed (delay active until {last_changed + close_delta})")

        # A confirmed-open sensor always wins
        if has_open:
            return True

        # Majority vote: require at least half the sensors to be reachable
        valid_count = len(sensors) - invalid_count
        if invalid_count > 0 and valid_count * 2 < len(sensors):
            self.debug_log(f"Window: {invalid_count}/{len(sensors)} sensors unavailable – uncertain")
            return None
        if invalid_count > 0:
            self.debug_log(f"Window: {invalid_count}/{len(sensors)} sensors unavailable – majority vote used")

        return closed_but_recent

    # ── frost protection ─────────────────────────────────────────────────────

    def is_frost_protection(self) -> bool:
        """Return ``True`` if frost protection conditions are met."""
        if not self.config.frost_protection_enabled:
            return False
        delta = self.config.frost_protection_duration
        if delta.total_seconds() == 0:
            return False

        now = datetime.now(UTC)
        threshold = now - delta

        presence = self.config.presence_sensor
        guest = self.config.guest_mode
        persons = self.config.persons

        relevant: list[tuple[Any, str]] = []
        if presence:
            relevant.append((presence, STATE_ON))
        if guest:
            relevant.append((guest, STATE_ON))
        for p in persons:
            relevant.append((p, STATE_HOME))

        if not relevant:
            return False

        for eid, active_state in relevant:
            state = self._get_state(eid)
            if not state:
                continue
            if state.state == active_state:
                return False
            try:
                last_changed = self._ensure_utc(state.last_changed)
            except Exception:
                last_changed = state.last_changed
            if last_changed > threshold:
                return False

        return True

    # ── liming protection ────────────────────────────────────────────────────

    def is_liming_time(self) -> bool:
        """Return ``True`` if liming protection is currently active."""
        if not self.config.liming_protection:
            return False

        season_entity = self.config.season_mode_entity
        liming_in_season = self.config.liming_in_season
        if season_entity:
            if not (self.is_season_mode() or liming_in_season):
                return False

        now = datetime.now(UTC)
        target_day = self.config.liming_day

        days_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
        target_wd = days_map.get(target_day, 0)

        if now.weekday() != target_wd:
            return False

        time_str = self.config.liming_time
        try:
            parts = time_str.split(":")
            h, m = int(parts[0]), int(parts[1])
            duration = self.config.liming_duration
            start = now.replace(hour=h, minute=m, second=0, microsecond=0)
            end = start + timedelta(minutes=duration)
            return start <= now <= end
        except Exception:
            return False
