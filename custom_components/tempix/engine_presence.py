"""
Tempix – Presence Mixin.

Person tracking, guest mode, proximity detection, away logic,
party mode, and force comfort/eco.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.const import STATE_ON, STATE_HOME

_LOGGER = logging.getLogger(__name__)
from homeassistant.helpers import entity_registry as er_helper

from custom_components.tempix.const import (
    SCHEDULING_MODE_CALENDAR,
    INVALID_STATES,
)


class PresenceMixin:
    """Presence, guest mode, proximity, away logic, party mode."""

    # ── guest mode ───────────────────────────────────────────────────────────

    def is_guest_mode(self) -> bool | None:
        """Return guest-mode state. ``None`` if all external entities are uncertain."""
        if self.config.guest_mode_switch:
            return True

        val = self.config.guest_mode
        if not val:
            return False

        entities = val if isinstance(val, list) else [val]

        uncertain = False
        for entity_id in entities:
            state = self.hass.states.get(entity_id)
            if not state or state.state in INVALID_STATES:
                uncertain = True
                if entity_id not in self._guest_warned:
                    _LOGGER.warning(
                        "TPX [%s]: Guest mode entity %s is unavailable – "
                        "guest mode cannot be detected. Guests may be set to eco.",
                        self.config.name, entity_id,
                    )
                    self._guest_warned.add(entity_id)
                continue
            if state.state in [STATE_ON, STATE_HOME, "active"]:
                self._guest_warned.discard(entity_id)  # clear warning if entity recovers
                return True

        return None if uncertain else False

    # ── persons / home detection ─────────────────────────────────────────────

    def is_anybody_home(self) -> bool | None:
        """Person tracking with entering/leaving duration. ``None`` if uncertain."""
        now = datetime.now(timezone.utc)
        
        # Enforce immediate state trust during the configured startup grace period
        # or if the state change happened before integration setup (initial state).
        grace_dur = self.config.sensor_retention
        in_grace = False
        if self._startup_time:
            in_grace = abs((now - self._startup_time).total_seconds()) < grace_dur.total_seconds()

        is_guest = self.is_guest_mode()
        if is_guest is True:
            self._last_home_status = True
            return True
        if is_guest is None:
            if self._last_home_status is not None:
                return self._last_home_status
            return None

        persons = self.config.persons
        if not persons:
            result = True if self.is_minimal_config() else False
            self._last_home_status = result
            return result

        entering_duration = self.config.people_entering_duration
        leaving_duration = self.config.people_leaving_duration
        now = datetime.now(timezone.utc)

        uncertain = False
        anyone_home = False
        for p in persons:
            state_obj = self._get_state(p)
            if not state_obj or state_obj.state in INVALID_STATES:
                uncertain = True
                continue

            s = state_obj.state
            last_changed = self._ensure_utc(state_obj.last_changed) or now

            is_reboot = in_grace
            if not is_reboot and self._startup_time:
                # Trust initial states that were set before/at integration start
                if last_changed <= self._startup_time + timedelta(seconds=1):
                    is_reboot = True

            if is_reboot:
                self.debug_log(f"Initial state/Reboot detected for {p}. Ignoring delays.")

            if s == STATE_HOME:
                if entering_duration > timedelta(0):
                    if is_reboot or now - last_changed >= entering_duration:
                        anyone_home = True
                    continue
                anyone_home = True
            else:
                if leaving_duration > timedelta(0):
                    if not is_reboot and now - last_changed < leaving_duration:
                        anyone_home = True

        if anyone_home:
            self._last_home_status = True
            return True
        if uncertain:
            if in_grace:
                # First boot, no cached value → assume someone is home
                self.debug_log("Person entities uncertain (grace period), no cached status, assuming home")
                return True
            
            # Outside of grace period, uncertainty means we cannot make a safe decision
            return None

        self._last_home_status = False
        return False

    def is_person_defined(self) -> bool:
        """Return ``True`` if any person or guest-mode entity is configured."""
        return len(self.config.persons) > 0 or bool(self.config.guest_mode)

    # ── proximity ────────────────────────────────────────────────────────────

    def check_proximity_arrived(self) -> bool:
        """Return ``True`` if proximity sensor shows 'arrived'."""
        prox_id = self.config.proximity_entity
        if not prox_id:
            return False

        try:
            ent_reg = er_helper.async_get(self.hass)
            entries = er_helper.async_entries_for_device(ent_reg, prox_id)

            for entry in entries:
                state = self.hass.states.get(entry.entity_id)
                if not state:
                    continue
                if (state.attributes.get("device_class") == "enum" and
                        state.state == "arrived"):
                    return True
        except Exception:
            pass
        return False

    def check_proximity_towards(self) -> bool:
        """Return ``True`` if proximity shows 'towards' within distance limit."""
        prox_id = self.config.proximity_entity
        if not prox_id:
            return False
        distance_limit = self.config.proximity_distance

        try:
            ent_reg = er_helper.async_get(self.hass)
            entries = er_helper.async_entries_for_device(ent_reg, prox_id)

            towards_stems: set[str] = set()
            distance_stems: set[str] = set()

            for entry in entries:
                eid = entry.entity_id
                state = self.hass.states.get(eid)
                if not state:
                    continue

                device_class = state.attributes.get("device_class")
                stem = re.sub(r'_(?=[^_]*$).*', '', eid)

                is_towards = device_class == "enum" and state.state == "towards"
                is_towards = is_towards or (not device_class and "direction" in eid and state.state == "towards")

                is_dist = device_class == "distance"
                is_dist = is_dist or (not device_class and "distance" in eid)

                if is_towards:
                    towards_stems.add(stem)
                elif is_dist:
                    try:
                        d = int(float(state.state))
                        if d <= distance_limit:
                            distance_stems.add(stem)
                    except (ValueError, TypeError):
                        continue

            return len(towards_stems.intersection(distance_stems)) > 0
        except Exception:
            pass
        return False

    def is_proximity_defined(self) -> bool:
        """Return ``True`` if a proximity entity is configured."""
        return bool(self.config.proximity_entity)

    def is_anybody_home_or_proximity(self) -> bool | None:
        """Combined home + proximity check. ``None`` if uncertain."""
        home = self.is_anybody_home()
        arrived = self.check_proximity_arrived()
        towards = self.check_proximity_towards()

        if home is True or arrived is True or towards is True:
            return True
        if home is None:
            return None
        return False

    # ── presence scheduler / sensor ──────────────────────────────────────────

    def is_presence_sensor_defined(self) -> bool:
        """Return ``True`` if a presence sensor is configured."""
        return bool(self.config.presence_sensor)

    def is_presence_scheduler_defined(self) -> bool:
        """Return ``True`` if a presence scheduler is configured."""
        return bool(self.config.scheduler_presence)

    def is_presence_scheduler_active(self) -> bool | None:
        """Return presence scheduler state. ``None`` if uncertain."""
        eid = self.config.scheduler_presence
        if not eid:
            return False
        val = self._state_value(eid)
        if val is None:
            return None
        return val == STATE_ON

    def is_presence_sensor_active(self) -> bool | None:
        """Return presence sensor state with reaction-time awareness. ``None`` if uncertain."""
        sensors = self.config.presence_sensor
        if not sensors:
            return False

        if isinstance(sensors, str):
            sensors = [sensors]

        on_delta = self.config.presence_reaction_on
        off_delta = self.config.presence_reaction_off

        now = datetime.now(timezone.utc)
        sensor_active: bool | None = False

        for sid in sensors:
            state = self._get_state(sid)
            if not state or state.state in INVALID_STATES:
                return None

            last_changed = self._ensure_utc(state.last_changed) or now

            if state.state == STATE_ON:
                if now - last_changed >= on_delta:
                    sensor_active = True
            else:
                if now - last_changed < off_delta:
                    sensor_active = True

        return sensor_active

    def is_presence_active(self) -> bool | None:
        """Combined presence scheduler + sensor check. ``None`` if uncertain."""
        sched = self.is_presence_scheduler_active()
        sensor = self.is_presence_sensor_active()
        if sched is None or sensor is None:
            return None
        if sched and sensor:
            return True
        return sensor if not self.is_presence_scheduler_defined() else False

    # ── party mode ───────────────────────────────────────────────────────────

    def check_party_mode(self) -> tuple[bool, float | None]:
        """Return ``(is_active, party_temperature_or_None)``."""
        if self.config.party_mode_switch:
            return True, self.config.party_temperature
        return False, None

    # ── force max / eco ──────────────────────────────────────────────────────

    def is_force_comfort_temp(self) -> bool:
        """Return ``True`` if force-comfort switch is active."""
        return bool(self.config.force_comfort_switch)

    def is_force_eco_temp(self) -> bool:
        """Return ``True`` if force-eco switch is active."""
        return bool(self.config.force_eco_switch)

    # ── away mode ────────────────────────────────────────────────────────────

    def is_away(self) -> bool:
        """Away detection with 4 condition branches."""
        scheduling_mode = self.config.scheduling_mode
        away_sched = self.config.away_scheduler_mode
        away_pres = self.config.away_presence_mode
        ignore_ppl = self.config.away_ignore_people

        if (self.is_person_defined() or self.is_proximity_defined()) and not self.is_anybody_home_or_proximity():
            if away_sched:
                if scheduling_mode == SCHEDULING_MODE_CALENDAR:
                    if self.is_calendar_comfort_active():
                        return True
                else:
                    if self.is_scheduler_active() or self.is_calendar_comfort_active():
                        return True

            if away_pres:
                if self.is_presence_scheduler_active() and not self.is_presence_active():
                    return True

        if ignore_ppl and away_pres:
            return (self.is_presence_scheduler_active()
                    and not self.is_presence_active())

        if (away_pres and (self.is_person_defined() or self.is_proximity_defined())
                and self.is_anybody_home_or_proximity()
                and not ignore_ppl):
            return self.is_presence_scheduler_active() and not self.is_presence_active()

        return False
