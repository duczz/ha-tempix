"""
Tempix – Calendar Mixin.

Event lookup, tag parsing, comfort-activation check, and the
dashboard schedule-period display for calendar-mode rooms.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timedelta, time as dt_time, UTC

time_min = dt_time(0, 0, 0)
from typing import Any

from homeassistant.const import STATE_ON, STATE_OFF, STATE_UNAVAILABLE, STATE_UNKNOWN

def _norm(text: str | None) -> str:
    """Vergleichsform fuer Nutzertext aus Config und Kalender.

    Warum nicht einfach ``.lower()``: „Bueromoebel"-Umlaute koennen in zwei
    Unicode-Formen vorliegen — NFC (ein Codepoint, U+00FC) oder NFD (u +
    kombinierendes Trema). Beide sehen identisch aus, ein Substring-Vergleich
    scheitert aber. Der Config-Flow liefert typischerweise NFC, Google Calendar
    und macOS regelmaessig NFD — genau die Kombination, die hier
    aufeinandertrifft (`calendar_room` gegen `location`/`summary`).

    Der Fehler waere unsichtbar: der Raum bekaeme nie Comfort, und im Log stuende
    nichts. Deshalb beide Seiten vor jedem Vergleich auf NFC ziehen.

    Bewusst ``.lower()`` und NICHT ``.casefold()``: casefold macht aus „ß" ein
    „ss" und wuerde damit Treffer erzeugen, die der Nutzer nicht geschrieben hat.
    Hier geht es nur darum, dass gleich Aussehendes auch gleich vergleicht.

    Reihenfolge (erst ``.lower()``, dann normalisieren): **beide Reihenfolgen
    waeren korrekt.** Nach NFC sind beide Vergleichsseiten derselbe String, und ``.lower()`` ist deterministisch,
    kann auf gleichen Eingaben also keine unterschiedlichen Ausgaben liefern.
    Auch tuerkisch ``"İ".lower()`` (ergibt ``i`` + kombinierender Punkt) fuehrt
    zu keiner Divergenz, weil beide Seiten dieselbe Umformung durchlaufen.
    Diese Variante steht hier, weil sie auch dann traegt, wenn eine kuenftige
    Case-Mapping-Aenderung eine komponierbare Form erzeugt.
    """
    return unicodedata.normalize("NFC", (text or "").strip().lower())



def _is_all_day_event(ev: dict, start_dt: datetime | None, end_dt: datetime | None) -> bool:
    """Ganztagstermin: ``all_day``-Flag oder Mitternacht bis Mitternacht.

    Von ``_score_event`` und der Delegat-Suche genutzt.
    """
    return bool(ev.get("all_day", False) or (
        start_dt and end_dt
        and start_dt.hour == 0 and start_dt.minute == 0
        and end_dt.hour == 0 and end_dt.minute == 0
    ))


def _keyword_prio(ev: dict, event_filters: str | None) -> int | None:
    """Stichwortfilter (``calendar_event``) fuer einen einzelnen Termin.

    Index des ersten passenden Stichworts (999 ohne konfigurierten Filter),
    ``None`` wenn kein Stichwort im Titel steht. Teil von
    ``CalendarMixin._event_filter_prio`` und fuer sich von der Delegat-Suche
    genutzt, die den Raumfilter eigenstaendig (mit Titel-Rueckfall) anwendet.
    """
    found_prio = 999
    if event_filters:
        filters = [_norm(f) for f in re.split(r"[,;]", event_filters) if f.strip()]
        summary = _norm(ev.get("summary", "Termin"))
        for i, f in enumerate(filters):
            if f in summary:
                found_prio = i
                break
        if found_prio == 999:
            return None

    return found_prio


_DAY_LABELS: dict[str, dict[str, str]] = {
    "de": {"mon": "Mo.", "tue": "Di.", "wed": "Mi.", "thu": "Do.", "fri": "Fr.", "sat": "Sa.", "sun": "So."},
    "en": {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu", "fri": "Fri", "sat": "Sat", "sun": "Sun"},
}
from homeassistant.util import dt as dt_util

from custom_components.tempix.const import (
    SCHEDULING_MODE_CALENDAR,
    DEFAULT_MIN_TEMP,
    DEFAULT_MAX_TEMP,
)

_LOGGER = logging.getLogger(__name__)

_SHORT_TO_FULL = {
    "mon": "monday", "tue": "tuesday", "wed": "wednesday",
    "thu": "thursday", "fri": "friday", "sat": "saturday", "sun": "sunday",
}
_WEEKDAY_SHORT = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}


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

    def _get_delegated_event(self, source_cal_id: str | None, day_name: str) -> dict[str, Any] | None:
        """Find delegated event for a weekday.

        Search order: source calendar first, then all room calendars.
        Candidates are narrowed by the keyword filter (``calendar_event``) and the
        room filter; the winner is the longest timed event, an all-day event only
        if nothing else is left.

        Kein Rueckfall auf "alle Termine": findet der Raumfilter nichts, gibt es
        kein Delegat — der Aufrufer meldet "use_day not found" und die Umleitung
        entfaellt (Verhalten wie bei jedem "nicht gefunden"). Der Rueckfall liess einen Raum den Samstagsplan
        eines anderen Raums fahren und einen ganztaegigen Termin den ganzen Tag Eco
        erzeugen. Der Raumfilter selbst ist unveraendert:
        Ort, und nur wenn kein Termin einen Ort trifft, der Titel.
        """
        if not self._calendar_events:
            return None

        # Build search order: source calendar first, then remaining config.calendar entries
        search_order: list[str] = []
        if source_cal_id and source_cal_id in self._calendar_events:
            search_order.append(source_cal_id)
        for cal_id in self.config.calendar:
            if cal_id not in search_order and cal_id in self._calendar_events:
                search_order.append(cal_id)

        # Collect all matching weekday events
        candidates: list[dict] = []
        for cal_id in search_order:
            for ev in self._calendar_events.get(cal_id, []):
                start_dt = self._parse_dt(ev.get("start_time") or ev.get("start"))
                if start_dt and start_dt.strftime("%a").lower() == day_name:
                    ev["calendar_id"] = cal_id
                    candidates.append(ev)

        # Keyword filter: same rule as the normal event selection
        candidates = [ev for ev in candidates if _keyword_prio(ev, self.config.calendar_event) is not None]

        # Apply room filter
        room_filter = self.config.calendar_room
        if room_filter and candidates:
            room_filters = [_norm(rf) for rf in re.split(r"[,;]", room_filter) if rf.strip()]
            filtered = [
                ev for ev in candidates
                if any(
                    rf in loc
                    for rf in room_filters
                    for loc in ([_norm(l) for l in re.split(r"[,;]", ev.get("location") or "") if l.strip()] or [""])
                )
            ]
            if not filtered:
                # Fallback: match against event summary (for calendars without location fields)
                filtered = [
                    ev for ev in candidates
                    if any(rf in _norm(ev.get("summary")) for rf in room_filters)
                ]
            candidates = filtered

        if not candidates:
            return None

        # Timed events beat all-day events; among equals the longest wins
        def _rank(ev: dict) -> tuple[bool, float]:
            s = self._parse_dt(ev.get("start_time") or ev.get("start"))
            e = self._parse_dt(ev.get("end_time") or ev.get("end"))
            duration = (e - s).total_seconds() if s and e else 0.0
            return (not _is_all_day_event(ev, s, e), duration)

        return max(candidates, key=_rank)

    def _score_event(
        self,
        ev: dict,
        is_active: bool,
        found_prio: int,
        now: datetime,
    ) -> tuple[int, float, int, int, int]:
        """Return a scoring tuple for an event (lower = better).

        Dimensions (in priority order):
        1. tier       — active (0) beats inactive (1)
        2. time_dist  — closer in time beats farther; past events penalised by 1e9
        3. found_prio — earlier keyword in filter list beats later
        4. duration_prio — timed event (0) beats all-day (1)
        5. cal_idx    — earlier calendar in config beats later
        """
        start_dt = self._parse_dt(ev.get("start_time"))
        end_dt = self._parse_dt(ev.get("end_time"))

        tier = 0 if is_active else 1

        time_dist = 0.0
        if not is_active:
            if start_dt and start_dt > now:
                time_dist = (start_dt - now).total_seconds()
            elif end_dt and end_dt <= now:
                time_dist = (now - end_dt).total_seconds() + 1_000_000_000.0

        duration_prio = 1 if _is_all_day_event(ev, start_dt, end_dt) else 0

        calendars = self.config.calendar
        cal_id = ev.get("calendar_id")
        cal_idx = calendars.index(cal_id) if cal_id in calendars else 99

        return (tier, time_dist, found_prio, duration_prio, cal_idx)

    def _event_filter_prio(self, ev: dict) -> int | None:
        """Apply this room's location + keyword filters to a single event.

        Returns the keyword priority index (lower = earlier in the
        ``calendar_event`` filter list, 999 if no keyword filter is set) or
        ``None`` if the event does not belong to this room.
        Room/keyword filter for normal event selection and transitions — used
        by ``_process_event_list`` and ``get_filtered_calendar_transitions``.
        ``_get_delegated_event`` applies its own room filter with a summary
        fallback.
        """
        room_filter = self.config.calendar_room
        if room_filter:
            room_filters = [_norm(rf) for rf in re.split(r"[,;]", room_filter) if rf.strip()]
            location = ev.get("location") or ""
            event_locations = [_norm(loc) for loc in re.split(r"[,;]", location) if loc.strip()]
            if not event_locations:
                event_locations = [""]
            if not any(rf in el for rf in room_filters for el in event_locations):
                return None

        return _keyword_prio(ev, self.config.calendar_event)

    def get_filtered_calendar_transitions(self, starts_only: bool = False) -> list[datetime]:
        """Future start/end times of THIS room's fetched calendar events.

        ``starts_only`` leaves out the event ends — the list Optimum Start
        preheats towards (``get_optimum_start_target``).

        Without ``starts_only`` the list also holds the next start and end of
        the active event's daily ``time:`` window, while that event runs.

        A shared calendar ENTITY exposes whichever event is currently
        running in its attributes — possibly another room's — so entity
        attributes are unreliable as transition source. The fetched events,
        reduced to this room via the location/keyword filters, are the only
        room-accurate source for the next phase boundary.
        """
        if not self._calendar_events:
            return []

        now = datetime.now(UTC)
        transitions: list[datetime] = []

        # Redirect-Erkennung: dieselbe "aktive Event"-Auflösung, die auch
        # is_calendar_comfort_active nutzt. Bei aktivem use_day-Redirect
        # (vacation_home_use_day/holiday_use_day) ersetzt die projizierte
        # Heute-Zeitspanne die rohen Grenzen des aktiven Events — sonst
        # meldet diese Funktion die falsche, unumgeleitete Grenze und loescht
        # einen aktiven Override zu frueh.
        active_event = self._get_active_calendar_event(active_only=False)
        redirect_start = redirect_end = None
        redirect_start_next = redirect_end_next = None
        window_edges: list[datetime] = []
        if active_event:
            tags = self.get_calendar_tags(event=active_event, active_only=False)
            delegated_event = tags.get("_delegated_event")
            forced_day = tags.get("use_day")
            if delegated_event and forced_day:
                d_start = self._parse_dt(delegated_event.get("start_time") or delegated_event.get("start"))
                d_end = self._parse_dt(delegated_event.get("end_time") or delegated_event.get("end"))
                if d_start and d_end:
                    local_start, local_end = self._project_delegated_window_onto_today(d_start, d_end)
                    redirect_start = local_start.astimezone(UTC)
                    redirect_end = local_end.astimezone(UTC)

                    # Tages-Luecken-Fix:
                    # _project_delegated_window_onto_today() liefert immer das
                    # zuletzt abgelaufene/gerade laufende Fenster relativ zu
                    # `now`, nie das naechste kommende. Ist dieses Fenster
                    # bereits komplett vorbei (local_end <= now), sind wir in
                    # der Tages-Luecke (z.B. morgens nach dem Ende der letzten
                    # Nacht-Phase) -- das naechste Redirect-Fenster ist dann
                    # exakt einen Tag spaeter. Ohne diesen Zusatzkandidaten
                    # verliert das aktive Event ALLE Kandidaten (weder roh noch
                    # projiziert), und min() greift auf ein viel spaeteres,
                    # falsches Event zurueck (gemessen: Samstag statt heute
                    # Abend). Addition VOR .astimezone(UTC), auf den lokalen
                    # (zonen-bewussten) Zeiten -- sonst ueber eine
                    # Sommer-/Winterzeit-Umstellung hinweg falsch.
                    now_local = dt_util.now()
                    if local_end <= now_local:
                        redirect_start_next = (local_start + timedelta(days=1)).astimezone(UTC)
                        redirect_end_next = (local_end + timedelta(days=1)).astimezone(UTC)

            # Fensterkante: Beginn und Ende des taeglichen
            # `time:`-Fensters sind Phasengrenzen -- der Comfort kippt dort,
            # also endet ein Override dort (ueber sched_next im Fingerprint).
            # Nur solange das effektive Fenster des aktiven Events laeuft
            # (bei use_day-Redirect das umgeleitete) und nur vor dessen Ende.
            # Nicht in starts_only: Optimum Start heizt auf Termin-Beginne vor.
            time_tag = tags.get("time")
            if time_tag:
                eff_start = self._parse_dt(active_event.get("start_time") or active_event.get("start"))
                eff_end = self._parse_dt(active_event.get("end_time") or active_event.get("end"))
                if redirect_start is not None:
                    eff_start, eff_end = redirect_start, redirect_end
                if eff_start and eff_start <= now and (not eff_end or eff_end > now):
                    for edge in self._get_daily_window_edges(time_tag, now):
                        if not eff_end or edge < eff_end:
                            window_edges.append(edge)

        if starts_only:
            keys: tuple[str, ...] = ("start_time", "start")
            redirect_candidates = (redirect_start, redirect_start_next)
        else:
            keys = ("start_time", "start", "end_time", "end")
            redirect_candidates = (redirect_start, redirect_end, redirect_start_next, redirect_end_next)

        for cal_id in self.config.calendar:
            for ev in self._calendar_events.get(cal_id, []):
                if self._event_filter_prio(ev) is None:
                    continue
                if redirect_start is not None and ev is active_event:
                    # Aktives Event ist umgeleitet: die projizierten Grenzen
                    # ERSETZEN die rohen (nicht ergaenzen), sonst bleibt die
                    # falsche rohe Grenze als frueherer Kandidat im min() erhalten.
                    for dt in redirect_candidates:
                        if dt is not None and dt > now:
                            transitions.append(dt)
                    continue
                for key in keys:
                    dt = self._parse_dt(ev.get(key))
                    if dt and dt > now:
                        transitions.append(dt)
        if not starts_only:
            transitions.extend(window_edges)
        return transitions

    def _get_daily_window_edges(self, time_tag: str, now: datetime) -> list[datetime]:
        """Next occurrences (after ``now``, UTC) of the daily window's start and end.

        Built on ``_get_daily_time_window_dt``: an edge that is already past
        moves one day ahead (on the local wall clock, before ``astimezone``).
        An unparsable tag (e.g. ``24:00``) yields no edges.
        """
        start, end = self._get_daily_time_window_dt(time_tag)
        if start is None or end is None:
            return []
        edges: list[datetime] = []
        for edge in (start, end):
            if edge <= now:
                edge += timedelta(days=1)
            edges.append(edge.astimezone(UTC))
        return edges

    def _process_event_list(self, events: list[dict], active_only: bool = True, use_at_state: bool = False) -> dict | None:
        """Find the best matching event from a list (filter + priority)."""
        now = datetime.now(UTC)

        best_event: dict | None = None
        best_score: tuple | None = None

        for ev in events:
            if "start" in ev and "start_time" not in ev:
                ev["start_time"] = ev["start"]
            if "end" in ev and "end_time" not in ev:
                ev["end_time"] = ev["end"]

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

            found_prio = self._event_filter_prio(ev)
            if found_prio is None:
                continue

            score = self._score_event(ev, is_active, found_prio, now)
            if best_score is None or score < best_score:
                best_event = ev
                best_score = score

        return best_event

    # ── dashboard schedule period ────────────────────────────────────────────

    def _format_event_range(self, start_dt: datetime, end_dt: datetime, tz: Any) -> str:
        """`HH:MM - HH:MM`; endet der Termin an einem anderen Tag, mit Wochentag.

        Live-Test 2026-11-04 (F4): ein 24-h-Termin erschien als „10:00 - 10:00".
        Wie die Termin-Detailansicht von HA (`_formatDateRange`): gleicher Tag
        nur Uhrzeiten, sonst beide Enden mit Tag — hier kurz als Wochentag
        (`Di. 10:00 - Mi. 10:00`), damit es in die Kachel passt.
        """
        start_local = start_dt.astimezone(tz)
        end_local = end_dt.astimezone(tz)
        if start_local.date() == end_local.date():
            return f"{start_local.strftime('%H:%M')} - {end_local.strftime('%H:%M')}"
        lang = (self.hass.config.language or "en")[:2].lower()
        labels = _DAY_LABELS.get(lang, _DAY_LABELS["en"])

        def _day(d: datetime) -> str:
            # weekday() statt strftime("%a"): unabhaengig von der Prozess-Locale.
            key = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[d.weekday()]
            return labels.get(key, key)

        return (
            f"{_day(start_local)} {start_local.strftime('%H:%M')} - "
            f"{_day(end_local)} {end_local.strftime('%H:%M')}"
        )

    def get_active_schedule_period(self) -> str:
        """Dashboard: return start–end time of current period."""
        mode = self.config.scheduling_mode

        if mode == SCHEDULING_MODE_CALENDAR:
            event = self._get_active_calendar_event(force_check=True, active_only=False)
            if event:
                tags = self.get_calendar_tags(active_only=False)
                forced_day = tags.get("use_day")
                delegated_event = tags.get("_delegated_event")
                if forced_day:
                    lang = (self.hass.config.language or "en")[:2].lower()
                    labels = _DAY_LABELS.get(lang, _DAY_LABELS["en"])
                    suffix = f" ({labels.get(forced_day, forced_day)})"
                else:
                    suffix = ""

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
                    now = datetime.now(UTC)

                    if delegated_event and forced_day:
                        return f"{self._format_event_range(start_dt, end_dt, tz)}{suffix}"

                    if start_dt > now:
                        # Eco phase: find most recent past event end as eco start
                        latest_end_dt = None
                        calendars = self.config.calendar
                        for cal_id in calendars:
                            for ev in self._calendar_events.get(cal_id, []):
                                ev_end = self._parse_dt(ev.get("end_time") or ev.get("end"))
                                if ev_end and ev_end <= now:
                                    if latest_end_dt is None or ev_end > latest_end_dt:
                                        latest_end_dt = ev_end
                        eco_start_str = latest_end_dt.astimezone(tz).strftime('%H:%M') if latest_end_dt else "00:00"
                        eco_end_str = start_dt.astimezone(tz).strftime('%H:%M')
                        return f"{eco_start_str} - {eco_end_str}{suffix}"

                    return f"{self._format_event_range(start_dt, end_dt, tz)}{suffix}"

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
                    requested = tags["use_scheduler"]
                    warn_key = f"use_scheduler:{requested}"
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
                                    self._calendar_warned.discard(warn_key)
                                    return f"{prefix} {end_str} ({state.attributes.get('friendly_name')}){suffix}"
                    if warn_key not in self._calendar_warned:
                        _LOGGER.warning(
                            "Tempix [%s]: use_scheduler '%s' not resolvable — using fallback display",
                            self.config.name, requested,
                        )
                        self._calendar_warned.add(warn_key)

                if is_all_day:
                    return "Ganztägig"

                return "Keine Zeitspanne"
            return "Keine Zeitspanne"

        # Helper Mode
        # Holiday suffix: show delegated weekday label when holiday override is active
        holiday_suffix = ""
        target_day = None
        effective_day = self._resolve_holiday_target_day()
        if self.is_holiday_today() and effective_day:
            target_day = effective_day.lower()
            lang = (self.hass.config.language or "en")[:2].lower()
            labels = _DAY_LABELS.get(lang, _DAY_LABELS["en"])
            holiday_suffix = f" ({labels.get(target_day, target_day)})"

        sched_id = self.get_active_scheduler()
        if sched_id:
            state = self._get_state(sched_id)

            # Try to get start+end from cached schedule slots
            slot = self._get_active_schedule_slot(sched_id, target_day)
            if slot:
                start_str = slot.get("start", "")
                end_str = slot.get("end", "")
                start_fmt = start_str[:5] if start_str else None
                end_fmt = end_str[:5] if end_str else None
                if start_fmt and end_fmt:
                    if slot.get("is_active", True):
                        # Active Comfort slot → show Comfort timespan
                        return f"{start_fmt} - {end_fmt}{holiday_suffix}"
                    else:
                        # Eco phase → show Eco timespan (last slot end → next slot start)
                        eco = self._get_eco_timespan(sched_id, target_day)
                        if eco:
                            return f"{eco}{holiday_suffix}"
                        return f"{start_fmt} - {end_fmt}{holiday_suffix}"

            # Fallback: bei aktiver Umleitung das umgeleitete Ende, nicht das des echten Wochentags
            redirect = self.get_holiday_redirect_transitions() if holiday_suffix else None
            if redirect:
                prefix = "Heizen bis" if self.is_scheduler_active() else "Sparbetrieb bis"
                tz = dt_util.get_time_zone(self.hass.config.time_zone)
                return f"{prefix} {min(redirect).astimezone(tz).strftime('%H:%M')}{holiday_suffix}"

            # Fallback: next_event attribute
            if state:
                prefix = "Heizen bis" if state.state == STATE_ON else "Sparbetrieb bis"
                for attr in ["next_event", "next_trigger", "next_transition", "next_occurrence"]:
                    next_ev = state.attributes.get(attr)
                    dt = self._parse_dt(next_ev)
                    if dt:
                        tz = dt_util.get_time_zone(self.hass.config.time_zone)
                        return f"{prefix} {dt.astimezone(tz).strftime('%H:%M')}{holiday_suffix}"

        return "Keine Zeitspanne"

    def _get_active_schedule_slot(self, sched_id: str, target_day: str | None = None) -> dict | None:
        """Return the active or next slot for a scheduler, optionally for a specific weekday.

        Schedule response structure: {weekday_full: [{'from': time, 'to': time}]}
        e.g. {'monday': [{'from': time(16,30), 'to': time(21,0)}], 'sunday': [...]}

        target_day: short name e.g. 'sun' → maps to 'sunday'.
        If None: use today's weekday.
        Returns dict with 'start' and 'end' as HH:MM strings.
        """
        schedule = self._schedule_slots.get(sched_id, {})
        if not schedule:
            return None

        tz = dt_util.get_time_zone(self.hass.config.time_zone)
        now_local = datetime.now(tz)

        if target_day:
            full_day = _SHORT_TO_FULL.get(target_day.lower(), target_day.lower())
        else:
            full_day = _SHORT_TO_FULL[_WEEKDAY_SHORT[now_local.weekday()]]

        day_slots = schedule.get(full_day, [])
        if not day_slots:
            return None

        current_time = now_local.time()

        # Find active slot (current time within from–to)
        for slot in day_slots:
            t_from = slot.get("from")
            t_to = slot.get("to")
            if t_from and t_to and t_from <= current_time < t_to:
                return {
                    "start": t_from.strftime("%H:%M"),
                    "end": t_to.strftime("%H:%M"),
                    "is_active": True,
                }

        # No active slot — return next upcoming slot
        future = [s for s in day_slots if s.get("from") and s["from"] > current_time]
        if future:
            s = min(future, key=lambda x: x["from"])
            return {"start": s["from"].strftime("%H:%M"), "end": s["to"].strftime("%H:%M"), "is_active": False}

        # All slots past — return first slot of the day
        s = min(day_slots, key=lambda x: x.get("from", datetime.min.time()))
        return {"start": s["from"].strftime("%H:%M"), "end": s["to"].strftime("%H:%M"), "is_active": False}

    def _get_eco_timespan(self, sched_id: str, target_day: str | None = None) -> str | None:
        """Return the current Eco timespan as 'HH:MM - HH:MM' (last slot end → next slot start).

        Called when no Comfort slot is currently active.
        """
        schedule = self._schedule_slots.get(sched_id, {})
        if not schedule:
            return None

        tz = dt_util.get_time_zone(self.hass.config.time_zone)
        now_local = datetime.now(tz)

        if target_day:
            full_day = _SHORT_TO_FULL.get(target_day.lower(), target_day.lower())
        else:
            full_day = _SHORT_TO_FULL[_WEEKDAY_SHORT[now_local.weekday()]]

        day_slots = schedule.get(full_day, [])
        if not day_slots:
            return None

        current_time = now_local.time()

        # Last slot that ended before now → Eco start
        past = [s for s in day_slots if s.get("to") and s["to"] <= current_time]
        # Next slot that starts after now → Eco end
        future = [s for s in day_slots if s.get("from") and s["from"] > current_time]

        if past:
            eco_start = max(past, key=lambda x: x["to"])["to"].strftime("%H:%M")
        else:
            # No past slot today — look backwards through previous days for last slot end
            eco_start = "00:00"
            for offset in range(1, 7):
                prev_day = _WEEKDAY_SHORT[(now_local.weekday() - offset) % 7]
                prev_full = _SHORT_TO_FULL[prev_day]
                prev_slots = schedule.get(prev_full, [])
                if prev_slots:
                    s = max(prev_slots, key=lambda x: x.get("to", time_min))
                    eco_start = s["to"].strftime("%H:%M")
                    break

        if future:
            eco_end = min(future, key=lambda x: x["from"])["from"].strftime("%H:%M")
        else:
            # No future slot today — look forwards through next days for first slot start
            eco_end = "00:00"
            for offset in range(1, 7):
                next_day = _WEEKDAY_SHORT[(now_local.weekday() + offset) % 7]
                next_full = _SHORT_TO_FULL[next_day]
                next_slots = schedule.get(next_full, [])
                if next_slots:
                    s = min(next_slots, key=lambda x: x.get("from", time_min))
                    eco_end = s["from"].strftime("%H:%M")
                    break

        return f"{eco_start} - {eco_end}"

    # ── Holiday detection ────────────────────────────────────────────────────

    def _resolve_holiday_target_day(self) -> str | None:
        """Return the effective weekday to substitute for today.

        While ``vacation_home`` is on, ``vacation_home_use_day`` takes
        priority (falls back to ``holiday_use_day`` if unset, so existing
        ``vacation_home`` users keep working without configuring the new
        field). Real calendar-detected holidays always use
        ``holiday_use_day``. 1:1 mirrored by
        ``domain_resolvers.resolve_holiday_target_day``.
        """
        if self.config.vacation_home:
            return self.config.vacation_home_use_day or self.config.holiday_use_day
        return self.config.holiday_use_day

    def is_holiday_today(self) -> bool:
        """Return True if today is a holiday according to holiday_calendar,
        or if the 'Vacation at home' switch (vacation_home) is on."""
        if self.config.vacation_home:
            if not self._resolve_holiday_target_day():
                warn_key = "vacation_home_no_use_day"
                if warn_key not in self._calendar_warned:
                    _LOGGER.warning(
                        "Tempix [%s]: vacation_home is on but neither vacation_home_use_day "
                        "nor holiday_use_day is configured — the switch has no effect",
                        self.config.name,
                    )
                    self._calendar_warned.add(warn_key)
            else:
                self._calendar_warned.discard("vacation_home_no_use_day")
            return True
        cal_id = self.config.holiday_calendar
        if not cal_id:
            return False
        # Primary: check entity state directly (works even when get_events returns nothing)
        #
        # `_get_state` statt `hass.states.get`: Dies war einer von fuenf
        # Live-Reads, die am Zyklus-Snapshot vorbeilasen. Post-Flip verschmutzt so ein Wert nicht mehr nur
        # V1s Beobachtung, sondern ueber die Bruecke die EINGABE der fahrenden
        # Engine: der Feiertag wuerde aus einem anderen Zeitpunkt stammen als
        # der Rest des Zyklus. `holiday_calendar` steht im
        # Snapshot (`coordinator.py`, `_get_snapshot_entities`), der Wechsel
        # kostet also nichts.
        state = self._get_state(cal_id)
        if state and state.state == STATE_ON:
            return True
        # Fallback: check fetched events — compare in local timezone to avoid
        # all-day events (midnight local = 22:00 UTC) bleeding into the previous day
        tz = dt_util.get_time_zone(self.hass.config.time_zone)
        local_now = datetime.now(tz)
        today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = local_now.replace(hour=23, minute=59, second=59, microsecond=0)
        for ev in self._calendar_events.get(cal_id, []):
            start = self._parse_dt(ev.get("start_time") or ev.get("start"))
            end = self._parse_dt(ev.get("end_time") or ev.get("end"))
            if start and end and start <= today_end and end > today_start:
                return True
        return False

    # ── calendar overrides / tags ─────────────────────────────────────────────

    def get_calendar_overrides(self, active_only: bool = True) -> dict[str, Any]:
        """Extract comfort, eco and hvac from calendar event description."""
        return self.get_calendar_tags(active_only=active_only)

    def get_calendar_tags(self, event: dict | None = None, active_only: bool = True, depth: int = 0) -> dict[str, Any]:
        """Centralized parsing of all allowed tags in calendar descriptions.

        Supports recursive inheritance if ``use_day`` is present.
        """
        if depth > 2:
            return {}

        if not event:
            event = self._get_active_calendar_event(active_only=active_only)
            if not event and not active_only:
                event = self._get_active_calendar_event(active_only=False)

        if not event:
            return {}

        raw_desc = (event.get("description") or "").strip()
        description = "\n".join(l for l in raw_desc.splitlines() if not l.strip().startswith("#"))
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

            vacation_match = re.search(r"vacation:\s*([\d.]+)", description, re.IGNORECASE)
            if vacation_match:
                try:
                    tags["comfort"] = max(DEFAULT_MIN_TEMP, min(DEFAULT_MAX_TEMP, float(vacation_match.group(1))))
                    tags["vacation"] = True
                except ValueError:
                    pass

            hvac_match = re.search(r"hvac:\s*(\w+)", description, re.IGNORECASE)
            if hvac_match:
                tags["hvac"] = hvac_match.group(1).lower()

            time_match = re.search(r"time:\s*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", description, re.IGNORECASE)
            if time_match:
                tags["time"] = f"{time_match.group(1)} - {time_match.group(2)}"

            day_match = re.search(r"use_day:\s*(\w+)", description, re.IGNORECASE)
            if day_match:
                day_str = day_match.group(1).title()
                day_map = {
                    "Monday": "mon", "Tuesday": "tue", "Wednesday": "wed",
                    "Thursday": "thu", "Friday": "fri", "Saturday": "sat", "Sunday": "sun",
                    "Montag": "mon", "Dienstag": "tue", "Mittwoch": "wed",
                    "Donnerstag": "thu", "Freitag": "fri", "Samstag": "sat", "Sonntag": "sun",
                }
                tags["use_day"] = day_map.get(day_str, day_str[:3].lower())

            sched_match = re.search(r"use_scheduler:\s*([^\n]+)", description, re.IGNORECASE)
            if sched_match:
                tags["use_scheduler"] = sched_match.group(1).strip()

        # Holiday override: replace use_day with the effective holiday target day
        _holiday_target = self._resolve_holiday_target_day()
        if depth == 0 and self.is_holiday_today() and _holiday_target:
            tags["use_day"] = _holiday_target.lower()
            tags.pop("_delegated_event", None)

        # Handle Inheritance
        forced_day = tags.get("use_day")
        if forced_day:
            source_cal_id = event.get("calendar_id") if event else None
            delegated_event = self._get_delegated_event(source_cal_id, forced_day)
            warn_key = f"use_day:{forced_day}"
            if delegated_event:
                parent_tags = self.get_calendar_tags(event=delegated_event, active_only=False, depth=depth + 1)
                for k, v in parent_tags.items():
                    if k not in tags:
                        tags[k] = v
                tags["_delegated_event"] = delegated_event
                self._calendar_warned.discard(warn_key)
            elif warn_key not in self._calendar_warned:
                _LOGGER.warning(
                    "Tempix [%s]: use_day '%s' not found in calendar — falling back to normal schedule",
                    self.config.name, forced_day,
                )
                self._calendar_warned.add(warn_key)

        return tags

    # ── calendar comfort active ──────────────────────────────────────────────

    def is_calendar_comfort_active(self) -> bool | None:
        """Check if any calendar event is active or starting soon (preheat)."""
        if self.config.scheduling_mode != SCHEDULING_MODE_CALENDAR:
            return False

        # Frueher war diese Schleife alles-oder-nichts — EIN
        # ungueltiger Kalender liess die Methode `None` zurueckgeben und
        # vergiftete damit auch die Auswertung der gesunden Kalender. Bei einer
        # TOTEN Referenz wurde daraus ein Dauerzustand: `is_away()` prueft
        # `if not is_scheduled` und lieferte deshalb permanent `False` — die
        # Abwesenheitserkennung des ganzen Raums war still abgeschaltet.
        #
        # Eine tote Referenz wird jetzt uebersprungen (sie ist eine
        # Konfigurationsstoerung, kein Laufzeitzustand). Ein EXISTIERENDER, aber
        # gerade `unavailable`/`unknown` Kalender fuehrt weiterhin zu `None` —
        # dort ist die Vorsicht berechtigt, weil sie sich aufloest.
        #
        # Sind ALLE Kalender tot, bleibt kein Zeitplan uebrig: die Methode
        # liefert dann `False` statt `None`. Das ist sichtbar (der Raum bleibt
        # auf Eco) statt eingefroren, und die tote Referenz wird gemeldet.
        calendars = [cid for cid in self.config.calendar if not self._entity_is_missing(cid)]
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
        # The event's own end and the local projection are needed again for the
        # preheat towards TOMORROW's ``time:`` window under a redirect (below).
        event_end = end_time
        redirect_window: tuple[datetime, datetime] | None = None

        if delegated_event and forced_day:
            d_start = self._parse_dt(delegated_event.get("start_time") or delegated_event.get("start"))
            d_end = self._parse_dt(delegated_event.get("end_time") or delegated_event.get("end"))

            if d_start and d_end:
                start_time, end_time = self._project_delegated_window_onto_today(d_start, d_end)
                redirect_window = (start_time, end_time)

                start_time = start_time.astimezone(UTC)
                end_time = end_time.astimezone(UTC)

        daily_start_found, daily_end_found = self._get_daily_time_window_dt(time_tag if time_tag else "")

        max_dur = self.config.max_smart_preconditioning
        max_mins = max_dur.total_seconds() / 60.0

        precondition_minutes = 0.0
        if self.config.smart_preconditioning:
            room_temp = self._resolve_room_temp()
            target_comfort = self.resolve_comfort_temperature()
            if room_temp is not None and target_comfort is not None:
                # Direction-aware like Optimum Start (should_set_comfort): a
                # cooling room needs lead time when it is WARMER than Comfort.
                diff = (room_temp - target_comfort) if self.is_cooling else (target_comfort - room_temp)
                if diff > 0:
                    conditioning_rate = max(0.1, self._get_effective_conditioning_rate())
                    precondition_minutes = min(max_mins, (diff / conditioning_rate) * 60)

        now = datetime.now(UTC)

        if start_time and start_time <= now:
            if not end_time or end_time > now:
                if daily_start_found and daily_end_found:
                    tz = dt_util.get_time_zone(self.hass.config.time_zone)
                    local_now = datetime.now(tz)

                    if daily_start_found <= local_now < daily_end_found:
                        return True

                    if precondition_minutes > 0:
                        # Preheat (cooling room: pre-cool) towards the NEXT
                        # window start: today's, or tomorrow's once today's
                        # window is over (for an overnight window the start
                        # still to come) — but only if the event is still
                        # running by then.
                        next_daily_start = daily_start_found
                        if next_daily_start <= local_now:
                            next_daily_start += timedelta(days=1)
                        runs_then = not end_time or next_daily_start < end_time
                        if not runs_then and redirect_window:
                            # Redirect: ``end_time`` is the end of TODAY's
                            # projection. It repeats tomorrow as long as the
                            # redirecting event itself is still running then.
                            day = timedelta(days=1)
                            runs_then = (
                                (not event_end or next_daily_start < event_end)
                                and redirect_window[0] + day <= next_daily_start < redirect_window[1] + day
                            )
                        if runs_then:
                            # Lead time in real time (UTC) like Optimum Start:
                            # wall-clock arithmetic stretches or shrinks it by
                            # an hour when it spans a DST change.
                            dt_preheat_start = next_daily_start.astimezone(UTC) - timedelta(minutes=precondition_minutes)
                            if dt_preheat_start <= now:
                                return True

                    return False
                return True
            else:
                return False

        # Towards the event's own START only preheating is decided here;
        # pre-cooling stays with Optimum Start (should_set_comfort), as before.
        if precondition_minutes > 0 and start_time and not self.is_cooling:
            # Real time: a start without offset (calendar entity attributes)
            # carries the local zone, where subtracting is wall-clock time.
            preheat_start = start_time.astimezone(UTC) - timedelta(minutes=precondition_minutes)
            if preheat_start <= now < start_time:
                return True

        return False

    def _is_holiday_comfort_via_calendar(self) -> bool | None:
        """Check comfort for holiday override in helper mode.

        Directly looks up the delegated weekday (effective holiday target
        day, see ``_resolve_holiday_target_day``) in the calendar events and
        checks whether the current time falls within that day's comfort
        window. Does not depend on a today-event being present in the
        calendar — only the target weekday events matter.

        ``None`` = no matching target-day event: the redirect does not apply and
        ``is_scheduler_active`` falls through to the Schedule-Helper.
        """
        day_name = self._resolve_holiday_target_day()
        if not day_name:
            return False

        # Find the reference event for the target weekday across all calendars
        delegated_event = self._get_delegated_event(None, day_name.lower())
        warn_key = f"holiday_use_day:{day_name.lower()}"
        if not delegated_event:
            if warn_key not in self._calendar_warned:
                _LOGGER.warning(
                    "Tempix [%s]: holiday_use_day '%s' not found in calendar — falling back to normal schedule",
                    self.config.name, day_name,
                )
                self._calendar_warned.add(warn_key)
            return None
        self._calendar_warned.discard(warn_key)

        d_start = self._parse_dt(delegated_event.get("start_time") or delegated_event.get("start"))
        d_end = self._parse_dt(delegated_event.get("end_time") or delegated_event.get("end"))

        if not d_start or not d_end:
            return False

        start_time, end_time = self._project_delegated_window_onto_today(d_start, d_end)

        start_utc = start_time.astimezone(UTC)
        end_utc = end_time.astimezone(UTC)
        now = datetime.now(UTC)

        return start_utc <= now < end_utc

    def _is_holiday_comfort_via_scheduler(self) -> bool:
        """Check comfort for holiday override without a calendar configured.

        Uses the active Schedule-Helper's own per-weekday slots
        (``_schedule_slots``, fetched via ``schedule.get_schedule`` —
        see ``_get_active_schedule_slot``) instead of calendar events, so
        ``holiday_use_day``/``vacation_home`` also work for scheduler-only
        setups that have no calendar entity at all.
        """
        day_name = self._resolve_holiday_target_day()
        if not day_name:
            return False

        sched_id = self.get_active_scheduler()
        if not sched_id:
            return False

        slot = self._get_active_schedule_slot(sched_id, day_name.lower())
        warn_key = f"holiday_use_day_scheduler:{day_name.lower()}"
        if slot is None:
            if warn_key not in self._calendar_warned:
                # Den wirksamen Schluessel nennen: bei `vacation_home` gilt
                # `vacation_home_use_day` (Fallback `holiday_use_day`), siehe
                # `_resolve_holiday_target_day`. Frueher nannte
                # die Meldung immer `holiday_use_day`.
                key_name = (
                    "vacation_home_use_day"
                    if self.config.vacation_home and self.config.vacation_home_use_day
                    else "holiday_use_day"
                )
                _LOGGER.warning(
                    "Tempix [%s]: %s '%s' not found in scheduler slots — falling back to normal schedule",
                    self.config.name, key_name, day_name,
                )
                self._calendar_warned.add(warn_key)
            return False
        self._calendar_warned.discard(warn_key)

        return bool(slot.get("is_active"))

    def get_holiday_redirect_transitions(self) -> list[datetime] | None:
        """Naechste Phasengrenzen des Umleitungsziels im Helper-Modus (UTC).

        ``is_scheduler_active()`` entscheidet bei aktiver Umleitung
        (``vacation_home``/``holiday_use_day``) anhand des ZIEL-Tags, waehrend
        ``get_next_schedule_transition()`` im Nicht-Kalender-Zweig nur die
        Attribute der Scheduler-Entity liest (echter Wochentag) — ein Override
        fiel dadurch am rohen statt am umgeleiteten Ende (gemessen:
        17:15 statt 19:20). ``None`` = Umleitung nicht anwendbar, der Aufrufer
        nutzt unveraendert die Entity-Attribute.

        Zwei Pfade, dieselbe Auswahl wie ``is_scheduler_active()``: Kalender
        konfiguriert -> projiziertes Delegat-Event, sonst die Slots des Helpers.
        1:1 gespiegelt von ``domain_resolvers.resolve_holiday_redirect_transitions``.
        """
        day_name = self._resolve_holiday_target_day()
        if not (day_name and self.is_holiday_today()):
            return None

        now = datetime.now(UTC)

        if self.config.calendar:
            delegated_event = self._get_delegated_event(None, day_name.lower())
            if not delegated_event:
                return None
            d_start = self._parse_dt(delegated_event.get("start_time") or delegated_event.get("start"))
            d_end = self._parse_dt(delegated_event.get("end_time") or delegated_event.get("end"))
            if not d_start or not d_end:
                return None
            local_start, local_end = self._project_delegated_window_onto_today(d_start, d_end)
            candidates = [local_start, local_end]
            # Tages-Luecke: das projizierte Fenster ist schon vorbei, das
            # naechste liegt einen Tag spaeter (siehe get_filtered_calendar_transitions).
            if local_end <= dt_util.now():
                candidates += [local_start + timedelta(days=1), local_end + timedelta(days=1)]
            return [c.astimezone(UTC) for c in candidates if c.astimezone(UTC) > now]

        sched_id = self.get_active_scheduler()
        if not sched_id:
            return None
        day_slots = self._schedule_slots.get(sched_id, {}).get(
            _SHORT_TO_FULL.get(day_name.lower(), day_name.lower()), []
        )
        if not day_slots:
            return None

        # Schedule-Helper kennen keine Phase ueber Mitternacht: eine Nachtphase
        # steht als zwei Slots (…-24:00 und 00:00-…). Diese beiden Kanten sind
        # KEINE Phasengrenze (is_scheduler_active() bleibt True) und duerfen kein
        # Kandidat sein — sonst wuerde ein Override jede Nacht um 00:00 geloescht.
        # HA kodiert "24:00" als time.max (homeassistant/components/schedule,
        # deserialize_to_time; im installierten Quelltext
        # belegt); ein `to == 00:00` wertet _get_active_schedule_slot ohnehin
        # nie als aktiv.
        def _is_end_of_day(t: dt_time) -> bool:
            return t >= dt_time(23, 59)

        has_midnight_start = any(s.get("from") == dt_time(0, 0) for s in day_slots)
        edges: list[dt_time] = []
        for slot in day_slots:
            t_from, t_to = slot.get("from"), slot.get("to")
            if t_from and not (t_from == dt_time(0, 0) and any(
                s.get("to") and _is_end_of_day(s["to"]) for s in day_slots
            )):
                edges.append(t_from)
            if t_to and not (_is_end_of_day(t_to) and has_midnight_start):
                edges.append(t_to)

        tz = dt_util.get_time_zone(self.hass.config.time_zone)
        now_local = datetime.now(tz)
        result: list[datetime] = []
        for t in edges:
            dt_local = now_local.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
            if dt_local <= now_local:
                dt_local += timedelta(days=1)
            result.append(dt_local.astimezone(UTC))
        return result

    def _project_delegated_window_onto_today(
        self, d_start: datetime, d_end: datetime
    ) -> tuple[datetime, datetime]:
        """Project a delegated (use_day redirect target) event's time-of-day onto today.

        Exact two-branch midnight-wrap rule, extracted verbatim from
        ``is_calendar_comfort_active`` — do NOT simplify to a
        single branch. A naive ``if end < start: end += 1d`` re-anchors
        overnight windows wrongly once ``now`` has crossed local midnight
        (the window would appear to already be over), which would clear an
        active override every night at 00:00 even without any redirect.

        Returns LOCAL datetimes — same convention as ``_get_daily_time_window_dt``;
        callers convert to UTC as needed.
        """
        now_local = dt_util.now()

        # Ganztagstermin (00:00 bis 00:00): die Uhrzeit-Projektion ergaebe
        # start == end, also ein leeres Fenster und den ganzen Tag Eco. Ein fuer
        # diesen Raum markierter Ganztagstermin heisst: ganzer Tag Comfort.
        if (d_start.hour, d_start.minute, d_end.hour, d_end.minute) == (0, 0, 0, 0) and d_end > d_start:
            day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            return day_start, day_start + timedelta(days=1)

        start_time = now_local.replace(hour=d_start.hour, minute=d_start.minute, second=0, microsecond=0)
        end_time = now_local.replace(hour=d_end.hour, minute=d_end.minute, second=0, microsecond=0)

        if end_time < start_time:
            if now_local >= start_time:
                end_time += timedelta(days=1)
            else:
                start_time -= timedelta(days=1)

        return start_time, end_time

    def _get_daily_time_window_dt(self, time_tag: str) -> tuple[datetime | None, datetime | None]:
        """Parse a daily window ``HH:MM - HH:MM`` and return it as today's datetimes.

        ``time_tag`` is the stored ``tags["time"]`` value, which carries no
        ``time:`` prefix (``get_calendar_tags``); a leading prefix is accepted too.
        """
        time_match = re.search(r"(?:time:\s*)?(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", time_tag, re.IGNORECASE)
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
