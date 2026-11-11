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
    AWAY_BEHAVIOR_IGNORE,
)


class PresenceMixin:
    """Presence, guest mode, proximity, away logic, party mode."""

    # ── guest mode ───────────────────────────────────────────────────────────

    def is_guest_mode(self) -> bool | None:
        """Return guest-mode state. ``None`` if no entity reports guests and at least one is uncertain."""
        if self.config.guest_mode_switch:
            return True

        val = self.config.guest_mode
        if not val:
            return False

        entities = val if isinstance(val, list) else [val]

        uncertain = False
        for entity_id in entities:
            # `_get_state` statt `hass.states.get`: als einzige
            # Resolver-Methode las diese hier live am Zyklus-Snapshot vorbei,
            # obwohl `guest_mode` in `_get_snapshot_entities` steht.
            state = self._get_state(entity_id)
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

            if is_reboot and p not in self._reboot_logged:
                self.debug_log(f"Initial state/Reboot detected for {p}. Ignoring delays.")
                self._reboot_logged.add(p)

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
                # Grace period after start, persons uncertain → assume someone is home
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

    def _proximity_confirmed(self, state: Any) -> bool:
        """Gate a proximity trigger on ``config.proximity_duration``.

        The state must have persisted for at least the configured duration
        (via ``last_changed``) before it counts — same confirmation pattern
        as ``presence_reaction_on``. Duration 0 (default) → instant, which
        keeps pre-1.7.0 behavior.
        Fail-open on missing/broken timestamps, matching the window logic.
        """
        duration = self.config.proximity_duration
        if duration.total_seconds() <= 0:
            return True
        try:
            last_changed = self._ensure_utc(getattr(state, "last_changed", None))
            if not last_changed:
                return True
            return last_changed <= datetime.now(timezone.utc) - duration
        except Exception:
            return True

    def proximity_states(self) -> list[tuple[str, Any]] | None:
        """Brauchbare ``(entity_id, state)``-Paare hinter dem Proximity-**Device**.

        ``None`` bedeutet UNSICHER — in drei Faellen: das Device loest auf keine
        Entity auf, **ein Kanal, auf den sich die Auswertung stuetzt**, fehlt
        oder ist ``unavailable``/``unknown`` (siehe ``_is_proximity_channel``),
        oder die Aufloesung selbst wirft. Eine leere Liste gibt es bewusst
        nicht: sie waere von "geprueft, niemand in der Naehe" nicht zu
        unterscheiden.

        Der Massstab ist bewusst NICHT "das ganze Geraet ist weg": Die
        HA-Proximity-Integration exponiert Richtung und Distanz als getrennte
        Entities, und ein Ausfall nur der Richtung haette sonst wieder ein
        stilles ``False`` ergeben.

        **Enge Definition.** Ein Device, dessen Entities
        gesund sind, aber nicht auf den ``towards``/``distance``-Filter passen
        (abweichende Benennung, anderes Integrations-Layout), ist NICHT
        unsicher — es ist eine Aussage: niemand naehert sich. Andernfalls
        stuende ein voellig gesundes Setup dauerhaft auf ``None``, und aus dem
        Ausfallschutz wuerde Dauer-Eco.

        Nicht konfiguriert (`[]`) ist ebenfalls nicht unsicher.
        """
        prox_id = self.config.proximity_entity
        if not prox_id:
            return []
        try:
            ent_reg = er_helper.async_get(self.hass)
            entries = er_helper.async_entries_for_device(ent_reg, prox_id)
        except Exception:
            return None

        # `entry.entity_id` bleibt die Quelle der ID — nicht `state.entity_id`.
        # Die Registry ist hier die Autoritaet, und der State muss die ID nicht
        # tragen (Testdoubles tun es nicht).
        states: list[tuple[str, Any]] = []
        channel_missing = False
        for entry in entries:
            eid = entry.entity_id
            state = self.hass.states.get(eid)
            if state is None or str(state.state).lower() in INVALID_STATES:
                # Teilausfall zaehlt: ein ausgefallener Kanal macht das Device
                # unsicher (siehe Docstring).
                if self._is_proximity_channel(eid, state):
                    channel_missing = True
                continue
            states.append((eid, state))

        if channel_missing or not states:
            return None
        return states

    @staticmethod
    def _is_proximity_channel(entity_id: str, state: Any) -> bool:
        """Ist diese Entity einer der Kanaele, auf die sich der Filter stuetzt?

        Spiegelt bewusst die Erkennung aus ``check_proximity_towards``:
        ``device_class`` ``enum``/``distance``, ersatzweise ``direction``/
        ``distance`` im Namen. Der Namensweg ist noetig, weil ein
        ``unavailable``-State seine Attribute verloren haben kann.

        Eine Entity, die WEDER Form noch Namen nach zum Filter gehoert, macht
        das Device nicht unsicher — sonst zoege jede beliebige Zusatz-Entity
        des Geraets (Batterie, Update-Sensor) den ganzen Raum in Dauer-Eco.
        """
        device_class = None
        if state is not None:
            try:
                device_class = state.attributes.get("device_class")
            except Exception:
                device_class = None
        if device_class in ("enum", "distance"):
            return True
        return "direction" in entity_id or "distance" in entity_id

    @staticmethod
    def _proximity_stem(entity_id: str) -> str:
        """Der Entity-Stamm — alles vor dem letzten ``_``.

        Ueber diesen Stamm paart ``check_proximity_towards`` Richtungs- und
        Distanzkanal desselben Geraets. Eigene Methode, damit die Diagnose
        dieselbe Bildung benutzt statt einer zweiten Regex daneben.
        """
        return re.sub(r'_(?=[^_]*$).*', '', entity_id)

    @staticmethod
    def _proximity_channel_flags(entity_id: str, state: Any) -> tuple[bool, bool]:
        """``(is_towards, is_distance)`` — reine FORM-Erkennung eines Kanals.

        Bewusst ohne das ``proximity_duration``-Gate: die Form einer Entity
        ("das ist der Richtungskanal") ist unabhaengig davon, ob der Trigger
        lange genug stabil ist. Das Gate bleibt in ``check_proximity_towards``.

        Extrahiert, damit die Diagnose die Kanal-Einordnung nicht
        nachprogrammieren muss.
        """
        device_class = state.attributes.get("device_class")

        is_towards = device_class == "enum" and state.state == "towards"
        is_towards = is_towards or (
            not device_class and "direction" in entity_id and state.state == "towards"
        )

        is_dist = device_class == "distance"
        is_dist = is_dist or (not device_class and "distance" in entity_id)

        return is_towards, is_dist

    def check_proximity_arrived(self) -> bool | None:
        """``True`` bei bestaetigtem 'arrived', ``None`` wenn nicht pruefbar."""
        if not self.config.proximity_entity:
            return False
        states = self.proximity_states()
        if states is None:
            return None

        for _eid, state in states:
            if (state.attributes.get("device_class") == "enum" and
                    state.state == "arrived" and
                    self._proximity_confirmed(state)):
                return True
        return False

    def check_proximity_towards(self) -> bool | None:
        """``True`` bei 'towards' innerhalb der Distanzgrenze, ``None`` wenn
        nicht pruefbar."""
        if not self.config.proximity_entity:
            return False
        states = self.proximity_states()
        if states is None:
            return None
        distance_limit = self.config.proximity_distance

        towards_stems: set[str] = set()
        distance_stems: set[str] = set()

        for eid, state in states:
            stem = self._proximity_stem(eid)
            is_towards, is_dist = self._proximity_channel_flags(eid, state)

            if is_towards and self._proximity_confirmed(state):
                towards_stems.add(stem)
            elif is_dist:
                try:
                    d = int(float(state.state))
                    if d <= distance_limit:
                        distance_stems.add(stem)
                except (ValueError, TypeError):
                    continue

        return len(towards_stems.intersection(distance_stems)) > 0

    def get_next_proximity_event(self) -> datetime | None:
        """Earliest future moment a pending proximity confirmation completes.

        Feeds ``get_next_duration_event()`` so the coordinator wakes up
        exactly when a currently-held-back 'arrived'/'towards' state passes
        its ``proximity_duration`` gate — instead of waiting for the next
        heartbeat. ``None`` when no gate is pending.
        """
        prox_id = self.config.proximity_entity
        duration = self.config.proximity_duration
        if not prox_id or duration.total_seconds() <= 0:
            return None

        now = datetime.now(timezone.utc)
        candidates: list[datetime] = []
        try:
            ent_reg = er_helper.async_get(self.hass)
            for entry in er_helper.async_entries_for_device(ent_reg, prox_id):
                state = self.hass.states.get(entry.entity_id)
                if not state or state.state not in ("arrived", "towards"):
                    continue
                device_class = state.attributes.get("device_class")
                if not (device_class == "enum" or (not device_class and "direction" in entry.entity_id)):
                    continue
                last_changed = self._ensure_utc(getattr(state, "last_changed", None))
                if last_changed and last_changed + duration > now:
                    candidates.append(last_changed + duration)
        except Exception:
            return None
        return min(candidates) if candidates else None

    def is_proximity_defined(self) -> bool:
        """Return ``True`` if a proximity entity is configured."""
        return bool(self.config.proximity_entity)

    def is_anybody_home_or_proximity(self) -> bool | None:
        """Combined home + proximity check. ``None`` if uncertain.

        Proximity reicht seine Unsicherheit jetzt durch. Vorher
        las sich ein Ausfall des Proximity-Devices als "niemand in der Naehe" —
        eine Aussage, die niemand geprueft hatte. In Setups, deren Away-Zustand
        allein an Proximity haengt (Personen abwesend, oder gar keine Personen
        konfiguriert: ``is_anybody_home()`` liefert dann konstruktionsbedingt
        ``False``, siehe ``is_minimal_config``), kippte damit bei jedem Funkloch
        ``is_away()`` auf True — Override geloescht, und bei
        ``away_behavior="off"`` das Geraet aus.

        Reihenfolge: irgendetwas sicher ``True`` gewinnt (jemand ist da bzw.
        naehert sich), danach zaehlt Unsicherheit, erst dann ``False``.
        """
        home = self.is_anybody_home()
        arrived = self.check_proximity_arrived()
        towards = self.check_proximity_towards()

        if home is True or arrived is True or towards is True:
            return True
        if home is None:
            return None
        if arrived is None or towards is None:
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

    def is_vacation_mode(self) -> tuple[bool, float | None]:
        """Return ``(is_active, vacation_temperature_or_None)``."""
        if self.config.vacation_mode_switch:
            return True, self.config.vacation_temperature
        tags = self.get_calendar_tags()
        if tags.get("vacation"):
            return True, tags.get("comfort")
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
        """Away detection. Returns True if a schedule is active but nobody is home/in the room."""
        scheduling_mode = self.config.scheduling_mode
        away_pres = self.config.away_presence_mode
        ignore_ppl = self.config.away_ignore_people

        if self.config.away_behavior == AWAY_BEHAVIOR_IGNORE:
            return False

        # 1. We only consider "Away mode" during active schedules
        is_scheduled = False
        if scheduling_mode == SCHEDULING_MODE_CALENDAR:
            is_scheduled = self.is_calendar_comfort_active()
        else:
            is_scheduled = self.is_scheduler_active() or self.is_calendar_comfort_active()

        if not is_scheduled:
            return False

        # 2. Check Room Presence (if enabled for Away Mode)
        if away_pres:
            if self.is_presence_scheduler_active() and not self.is_presence_active():
                return True
            if ignore_ppl:
                return False

        # 3. Check House Presence (Persons / Proximity)
        if self.is_person_defined() or self.is_proximity_defined():
            if self.is_anybody_home_or_proximity() is False:
                return True

        return False
