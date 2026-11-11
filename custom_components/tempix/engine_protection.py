"""
Tempix – Protection Mixin.

Season mode, outside-temperature threshold, automation-active gate,
window detection, and liming protection.

Frost protection lives in V2 as ``FrostConstraint`` (policies/constraints.py):
a heating-only floor on the target. V1's ``is_frost_protection()`` — despite the
name an ABSENCE rule, not a temperature check — was removed; see
CHANGELOG 1.7.0.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, UTC

from homeassistant.const import STATE_ON, STATE_OFF
from homeassistant.util import dt as dt_util

from custom_components.tempix.const import (
    INVALID_STATES,
)

_LOGGER = logging.getLogger(__name__)


class ProtectionMixin:
    """Season mode, window detection, and liming protection."""

    # ── season mode ──────────────────────────────────────────────────────────

    def is_season_mode(self) -> bool:
        """Return ``True`` if season is active (or no entity configured)."""
        entity_id = self.config.season_mode_entity
        if not entity_id:
            return True
        s = self._state_value(entity_id)
        return s == STATE_ON if s is not None else True

    # ── outside temperature threshold ────────────────────────────────────────

    def _evaluate_bootstrap(self, t: float, threshold: float) -> bool:
        """Deterministischer Bootstrap: Berechne den Zustand ohne Bias durch Hysterese.

        Nimmt bewusst weder ``hysteresis`` noch ``fallback`` entgegen — beide
        standen frueher in der Signatur und wurden nie gelesen. Die
        Signatur versprach damit einen hysterese-/fallback-bewussten Bootstrap
        und lieferte einen nackten Schwellenvergleich.
        """
        # Da wir nicht wissen, ob wir von oben oder unten kommen,
        # prüfen wir schlichtweg gegen den Threshold.
        return (t - threshold) * self._factor < 0

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
        outside_ok = self._last_outside_ok

        if self._last_outside_ok is None:
            # First evaluation — no previous state, use plain threshold
            outside_ok = self._evaluate_bootstrap(t, threshold)
        elif self._last_outside_ok:
            # Currently active (heating or cooling)
            if self.is_heating:
                # Heating: only turn OFF if clearly above threshold
                if t >= threshold + hysteresis:
                    outside_ok = False
            elif self.is_cooling:
                # Cooling: only turn OFF if clearly below threshold
                if t <= threshold - hysteresis:
                    outside_ok = False
        else:
            # Currently inactive (heating or cooling)
            if self.is_heating:
                # Heating: only turn ON if clearly below threshold
                if t < threshold - hysteresis:
                    outside_ok = True
            elif self.is_cooling:
                # Cooling: only turn ON if clearly above threshold
                if t > threshold + hysteresis:
                    outside_ok = True

        if self._last_outside_ok != outside_ok:
            self._last_outside_ok = outside_ok
            if self._on_dirty:
                self._on_dirty()

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
        """Gate: season_mode AND outside_temp AND manual_switch.

        Exposed as the climate/sensor entity's
        ``automation_active`` state attribute — same name as the
        automation_active SWITCH, but a different value. The switch is the
        raw manual toggle (``config.automation_active``); this gate is
        false whenever season or outside-temp shuts things down too, even
        with the switch on. Not renamed — the attribute is public API for
        existing dashboards/automations reading it via ``state_attr()``.
        """
        if not self.config.automation_active:
            return False
        season = self.is_season_mode()
        outside = self.check_outside_threshold()
        if outside is None:
            return season
        return season and outside

    # ── window detection ─────────────────────────────────────────────────────

    @contextmanager
    def preserve_window_state(self) -> Iterator[None]:
        """Fenster-Gedaechtnis des Zyklus waehrend eines Aufrufs ausserhalb des
        Zyklus (Diagnose-Export) unveraendert lassen (LT-F12-Diag).

        ``is_window_open`` ist nicht zustandsfrei (Marken, Phasen-Set, Loesch-Liste,
        Reboot-Logmerker) und wird auch von ``calculate_target_temperature`` /
        ``calculate_hvac_mode`` aufgerufen. V2 sieht solche Aufrufe nie; ohne
        Sicherung weicht V1 von V2 ab.
        """
        saved = (
            dict(self._window_close_marks),
            set(self._window_open_phase_confirmed),
            set(self._window_pop_pending),
            set(self._reboot_logged),
        )
        try:
            yield
        finally:
            self._window_close_marks.clear()
            self._window_close_marks.update(saved[0])
            self._window_open_phase_confirmed.clear()
            self._window_open_phase_confirmed.update(saved[1])
            self._window_pop_pending.clear()
            self._window_pop_pending.update(saved[2])
            self._reboot_logged.clear()
            self._reboot_logged.update(saved[3])

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
        confirmed_now: list[str] = []
        pending_reopen: list[tuple[str, datetime]] = []
        phases = self._window_open_phase_confirmed
        marks = self._window_close_marks
        pop_pending = self._window_pop_pending

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

            prev_open = self._window_prev_valid.get(sid) in [STATE_ON, "open", "tilted"]
            if state.state in [STATE_ON, "open", "tilted"]:
                if not prev_open:
                    # LT-F12-Rest (d): neue Oeffnungsphase, noch unbestaetigt
                    phases.discard(sid)
                if last_changed <= on_time:
                    has_open = True
                    confirmed_now.append(sid)
                    phases.add(sid)
                else:
                    # LT-F12: Wiederoeffnen waehrend der laufenden
                    # Schliessverzoegerung gilt lueckenlos als offen; die
                    # Marke bleibt bis zum bestaetigten Oeffnen (Paritaet zu
                    # V2 `_resolve_window`).
                    mark = marks.get(sid)
                    if (
                        close_delta > timedelta(0)
                        and mark is not None
                        and last_changed is not None
                        and timedelta(0) <= last_changed - mark <= close_delta
                    ):
                        has_open = True
                        phases.add(sid)  # Klausel-offen gilt als bestaetigt
                        self.debug_log(f"Window {sid} reopened within close delay – counts as open")
                    else:
                        pending_reopen.append((sid, last_changed))
            elif state.state in [STATE_OFF, "closed"]:
                is_reboot_timestamp = False
                grace_dur = self.config.sensor_retention
                if self._startup_time and last_changed:
                    diff = abs((last_changed - self._startup_time).total_seconds())
                    if diff < grace_dur.total_seconds():
                        is_reboot_timestamp = True
                        if sid not in self._reboot_logged:
                            self.debug_log(f"Reboot detected for window {sid} (diff: {diff:.1f}s). Ignoring close delay.")
                            self._reboot_logged.add(sid)

                # Reboot-safe close-delay: only a REAL open→closed transition
                # counts as recently-closed. A sensor that flapped through
                # 'unavailable' (prev valid state was already closed) keeps a
                # fresh last_changed but was never open — must NOT be treated
                # as open (prod bug: AC off overnight on sensor flap).
                was_open = prev_open
                # Marke des echten offen→zu: ab dem zweiten Zyklus steht
                # `_window_prev_valid` schon auf zu, die Marke haelt den Delay,
                # solange `last_changed` derselbe ist (ein Flap danach bekommt
                # einen neuen Zeitstempel und verlaengert nichts).
                # LT-F12-Rest (d): nur eine bestaetigte Oeffnungsphase setzt
                # eine Marke (kurzes Oeffnen+Schliessen = Prellen).
                if was_open and last_changed and sid in phases:
                    marks[sid] = last_changed
                genuine_close = (
                    last_changed is not None
                    and marks.get(sid) == last_changed
                )
                if not is_reboot_timestamp and genuine_close and last_changed >= off_time:
                    closed_but_recent = True
                    self.debug_log(f"Window {sid} recently closed (delay active until {last_changed + close_delta})")
                if not was_open:
                    phases.discard(sid)

        # LT-F12-Rest (b): Mehrsensor-Raum — Oeffnen kurz nach dem Schliessen
        # eines ANDEREN Sensors des Raums gilt als offen (Paritaet zu V2).
        if pending_reopen and close_delta > timedelta(0):
            room_marks = [
                marks[s] for s in sensors
                if s in self._window_close_marks
            ]
            for sid, lc in pending_reopen:
                if any(timedelta(0) <= lc - m <= close_delta for m in room_marks):
                    has_open = True
                    phases.add(sid)

        # Marken bestaetigter Sensoren NICHT hier loeschen: is_window_open laeuft
        # pro Zyklus mehrfach, ein Loeschen im ersten Aufruf liesse (b) im
        # zweiten ins Leere laufen. Geloescht wird in set_state_snapshot.
        pop_pending.update(confirmed_now)

        # A confirmed-open sensor always wins
        if has_open:
            return True

        # Reachability quorum: require at least half the sensors to be reachable
        valid_count = len(sensors) - invalid_count
        if invalid_count > 0 and valid_count * 2 < len(sensors):
            self.debug_log(f"Window: {invalid_count}/{len(sensors)} sensors unavailable – uncertain")
            return None
        if invalid_count > 0:
            self.debug_log(f"Window: {invalid_count}/{len(sensors)} sensors unavailable – reachable sensors decide")

        return closed_but_recent

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

        # ORTSZEIT, nicht UTC. `liming_day` und `liming_time` traegt
        # der Nutzer als Wanduhrzeit ein — gegen ein UTC-`now` verglichen lief
        # die Kalkschutz-Fahrt in Deutschland 1 h (Winter) bzw. 2 h (Sommer) zu
        # spaet, und um Mitternacht herum am falschen Wochentag. Bei der
        # Default-Dauer von 1 Minute ging das Fenster damit nie zur
        # konfigurierten Zeit auf. Gleiche Korrektur wie bei
        # `persons_force_comfort` (engine_schedule.py) und wie sie
        # engine_calendar.py durchgaengig macht.
        now = dt_util.now()
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
        except Exception:
            # KONFIGURATIONSFEHLER, nicht Unsicherheit: eine unparsbare
            # `liming_time` ist keine Aussage ueber die Welt, sondern ein
            # kaputtes Feld — "kein Kalkschutz" ist die richtige Antwort. Falsch
            # war nur, dass sie STILL blieb: Der Kalkschutz lief nie, und nichts
            # sagte warum. Dieselbe Behandlung wie beim Praesenz-Komfortfenster
            # (1.7.0) und beim Gaeste-Modus — einmal warnen, bei Erholung
            # zuruecksetzen. Eine nackte Warnung wuerde hier jeden Zyklus
            # feuern, weil ein kaputtes Config-Feld bis zur Korrektur kaputt
            # bleibt.
            if not self._liming_time_warned:
                self._liming_time_warned = True
                _LOGGER.warning(
                    "TPX [%s]: Kalkschutz-Uhrzeit '%s' ist unlesbar (erwartet "
                    "HH:MM) — der Kalkschutz laeuft deshalb NIE. Bitte in den "
                    "Optionen korrigieren.",
                    self.config.name, time_str,
                )
            return False

        self._liming_time_warned = False
        return start <= now <= end
