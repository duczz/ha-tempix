"""Domain resolvers — native V2 ports of V1 presence/adjustment/schedule/calendar
lookups.

Faithful 1:1 ports of ``ScheduleMixin``/``PresenceMixin``/``CalendarMixin``
methods (engine_schedule.py, engine_presence.py, engine_calendar.py) — used
by the parallel validator so V2's snapshot no longer needs to call into the
V1 engine for these fields. A handful of single-method calendar/proximity/
outside-temp lookups stay bridged from V1 as explicit parameters rather than
being resolved here (see ``resolve_schedule_comfort``'s and
``resolve_calendar_tags``'s docstrings) — each is one V1 method call without
its own decision logic, not a whole unported field.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime, time as dt_time, timedelta
from typing import Any, Callable

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN

from custom_components.tempix.climate_rate import t1_normalization_factor
from custom_components.tempix.const import (
    AWAY_BEHAVIOR_IGNORE,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    INVALID_STATES,
    SCHEDULING_MODE_CALENDAR,
)

_CALENDAR_DAY_MAP = {
    "Monday": "mon", "Tuesday": "tue", "Wednesday": "wed",
    "Thursday": "thu", "Friday": "fri", "Saturday": "sat", "Sunday": "sun",
    "Montag": "mon", "Dienstag": "tue", "Mittwoch": "wed",
    "Donnerstag": "thu", "Freitag": "fri", "Samstag": "sat", "Sonntag": "sun",
}


def _normalize_calendar_text(text: str | None) -> str:
    """1:1 port of ``engine_calendar._norm`` — NFC-normalized, lowercased comparison form."""
    return unicodedata.normalize("NFC", (text or "").strip().lower())


def _is_all_day_event(ev: dict, start_dt: datetime | None, end_dt: datetime | None) -> bool:
    """Ganztagstermin: ``all_day``-Flag oder Mitternacht bis Mitternacht.

    1:1 port of ``engine_calendar._is_all_day_event`` (dieselbe Erkennung wie in
    ``resolve_calendar_event_score``).
    """
    return bool(ev.get("all_day", False) or (
        start_dt and end_dt
        and start_dt.hour == 0 and start_dt.minute == 0
        and end_dt.hour == 0 and end_dt.minute == 0
    ))


def resolve_party_mode(config: Any) -> tuple[bool, float | None]:
    """Return ``(is_active, party_temperature_or_None)``."""
    if config.party_mode_switch:
        return True, config.party_temperature
    return False, None


def resolve_sunshine_offset(
    config: Any, get_state: Callable[[str], Any],
) -> tuple[bool, float]:
    """Return ``(is_active, offset)``. 1:1 port of
    ``TemperatureMixin.is_sunshine_offset_active``/``get_sunshine_offset``
    (engine_temperature.py:37-51). ``offset`` is already ``0.0`` when
    inactive, matching V1's own contract — callers don't need to gate on
    the bool separately to get a safe number.
    """
    if config.sunshine_offset:
        weather_eid = config.weather_entity
        if weather_eid:
            w_state = get_state(weather_eid)
            if w_state and w_state.state == "sunny":
                return True, config.sunshine_offset_value
    return False, 0.0


def resolve_is_season_mode(
    config: Any, state_value: Callable[[str | None], str | None],
) -> bool:
    """1:1 port of ``ProtectionMixin.is_season_mode``
    (engine_protection.py:32-38). Defaults to ``True`` — both when no
    season-mode entity is configured and when its state is unreadable
    (V1's own contract: unconfigured/uncertain
    means "assume season is active", not "unknown").
    """
    entity_id = config.season_mode_entity
    if not entity_id:
        return True
    s = state_value(entity_id)
    return s == STATE_ON if s is not None else True


def resolve_vacation_mode(
    config: Any, calendar_tags: dict
) -> tuple[bool, float | None]:
    """Return ``(is_active, vacation_temperature_or_None)``."""
    if config.vacation_mode_switch:
        return True, config.vacation_temperature
    if calendar_tags.get("vacation"):
        return True, calendar_tags.get("comfort")
    return False, None


# Modi, die V2 aus dem Kalender-Tag `hvac:` uebernimmt.
# V1 reichte jeden `\w+`-Wert ungeprueft durch (der Dispatcher fiel bei einem
# unbekannten Modus auf "off" zurueck) — V2 ignoriert einen Wert ausserhalb der
# Menge und meldet ihn, statt still auszuschalten.
CALENDAR_HVAC_TAG_MODES = frozenset(
    {"heat", "cool", "heat_cool", "auto", "off", "dry", "fan_only"}
)


def resolve_calendar_hvac_mode(
    calendar_tags: dict,
) -> tuple[str | None, str | None]:
    """Return ``(mode, rejected_raw_value)`` for the ``hvac:`` calendar tag.

    ``mode`` is the validated tag value or ``None``; ``rejected_raw_value``
    carries the raw text when a tag was present but not a known mode (the
    caller warns once), ``None`` otherwise.
    """
    raw = calendar_tags.get("hvac")
    if raw is None:
        return None, None
    if raw in CALENDAR_HVAC_TAG_MODES:
        return raw, None
    return None, raw


def resolve_active_adjustment(
    adjustments: list[dict],
    *,
    now: Any,
    calendar_tags: dict,
    scheduler_name: str | None,
) -> dict | None:
    """Return the best matching adjustment entry for the current time.

    1:1 port of ``ScheduleMixin.get_active_adjustment``.
    ``now`` must be HA-local time (not UTC) —
    entries carry wall-clock ``time``/``days`` as the user entered them.
    """
    if not adjustments:
        return None

    forced_day = calendar_tags.get("use_day")

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


def resolve_adjustment_mode(entry: dict | None) -> str:
    """Extract mode override from adjustment. 1:1 port of ``get_adjustment_mode``."""
    if not entry or "mode" not in entry:
        return "auto"
    return entry["mode"]


def resolve_adjustment_comfort(
    entry: dict | None, float_state: Callable[[Any], float | None]
) -> float | None:
    """Extract comfort temp from adjustment entry. 1:1 port of ``get_adjustment_comfort``."""
    if not entry or "comfort" not in entry:
        return None
    v = entry["comfort"]
    try:
        return float(v)
    except (ValueError, TypeError):
        return float_state(v)


def resolve_adjustment_eco(
    entry: dict | None, float_state: Callable[[Any], float | None]
) -> float | None:
    """Extract eco temp from adjustment entry. 1:1 port of ``get_adjustment_eco``."""
    if not entry or "eco" not in entry:
        return None
    v = entry["eco"]
    try:
        return float(v)
    except (ValueError, TypeError):
        return float_state(v)


def resolve_adjustment_calibration(entry: dict | None) -> bool:
    """Extract calibration toggle from adjustment. 1:1 port of ``get_adjustment_calibration``."""
    if not entry or "calibration" not in entry:
        return True
    return entry["calibration"] == "on"


def resolve_active_scheduler(
    config: Any,
    calendar_tags: dict,
    get_state: Callable[[str], Any],
    state_value: Callable[[str], str | None],
) -> str | None:
    """Return the currently active scheduler entity ID.

    1:1 port of ``ScheduleMixin.get_active_scheduler`` (engine_schedule.py:31-89).
    """
    schedulers = config.schedulers
    count = len(schedulers)
    if count == 0:
        return None

    selector_id = config.scheduler_selector

    # Calendar override (v1.5.8)
    forced_sched = calendar_tags.get("use_scheduler")
    if forced_sched:
        if forced_sched in schedulers:
            return forced_sched
        for s_id in schedulers:
            s_state = get_state(s_id)
            if s_state:
                fname = s_state.attributes.get("friendly_name", "")
                if forced_sched.lower() in fname.lower():
                    return s_id

    if count == 1 or not selector_id:
        return schedulers[0]

    sel_val = state_value(selector_id)
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
        s_state = get_state(s_id)
        if s_state:
            fname = s_state.attributes.get("friendly_name", "")
            if fname.lower() == sel_val.lower():
                return s_id

    for s_id in schedulers:
        s_state = get_state(s_id)
        if s_state:
            fname = s_state.attributes.get("friendly_name", "")
            if sel_val.lower() in fname.lower():
                return s_id

    return None


def resolve_scheduler_defined(config: Any, active_scheduler: str | None) -> bool:
    """1:1 port of ``ScheduleMixin.is_scheduler_defined``."""
    if config.scheduling_mode == SCHEDULING_MODE_CALENDAR:
        return bool(config.calendar)
    return active_scheduler is not None


def resolve_scheduler_active(
    scheduler_id: str | None,
    get_state: Callable[[str], Any],
    holiday_comfort_override: bool | None,
) -> bool | None:
    """1:1 port of ``ScheduleMixin.is_scheduler_active``.

    The holiday branch is bridged — pass the caller's precomputed
    ``is_holiday_today() and resolve_holiday_target_day(config)`` result
    (calendar path via ``resolve_holiday_comfort_via_calendar`` when
    ``config.calendar`` is set, else the scheduler-slot path via
    ``resolve_holiday_comfort_via_scheduler``; ``None`` if not applicable)
    as ``holiday_comfort_override``.
    """
    if holiday_comfort_override is not None:
        return holiday_comfort_override
    if scheduler_id is None:
        return False
    state_obj = get_state(scheduler_id)
    if not state_obj or state_obj.state in INVALID_STATES:
        return None
    return state_obj.state == STATE_ON


def resolve_presence_scheduler_active(
    config: Any, state_value: Callable[[str], str | None]
) -> bool | None:
    """1:1 port of ``PresenceMixin.is_presence_scheduler_active``."""
    eid = config.scheduler_presence
    if not eid:
        return False
    val = state_value(eid)
    if val is None:
        return None
    return val == STATE_ON


def resolve_presence_sensor_active(
    config: Any, get_state: Callable[[str], Any], now: Any
) -> bool | None:
    """1:1 port of ``PresenceMixin.is_presence_sensor_active``."""
    sensors = config.presence_sensor
    if not sensors:
        return False
    if isinstance(sensors, str):
        sensors = [sensors]

    on_delta = config.presence_reaction_on
    off_delta = config.presence_reaction_off
    sensor_active: bool | None = False

    for sid in sensors:
        state = get_state(sid)
        if not state or state.state in INVALID_STATES:
            return None

        last_changed = state.last_changed
        if last_changed.tzinfo is None:
            last_changed = last_changed.replace(tzinfo=UTC)

        if state.state == STATE_ON:
            if now - last_changed >= on_delta:
                sensor_active = True
        else:
            if now - last_changed < off_delta:
                sensor_active = True

    return sensor_active


def resolve_presence_active(
    config: Any,
    state_value: Callable[[str], str | None],
    get_state: Callable[[str], Any],
    now: Any,
) -> bool | None:
    """1:1 port of ``PresenceMixin.is_presence_active``."""
    sched = resolve_presence_scheduler_active(config, state_value)
    sensor = resolve_presence_sensor_active(config, get_state, now)
    if sched is None or sensor is None:
        return None
    if sched and sensor:
        return True
    return sensor if not bool(config.scheduler_presence) else False


def resolve_anybody_home_or_proximity(
    home: bool | None,
    proximity_arrived: bool | None,
    proximity_towards: bool | None,
) -> bool | None:
    """1:1 port of the combination logic in
    ``PresenceMixin.is_anybody_home_or_proximity``.

    ``home`` is V2's already-bridged ``anybody_home`` snapshot input;
    ``proximity_arrived``/``proximity_towards`` are
    ``check_proximity_arrived()``/``check_proximity_towards()``, still V1
    calls (Entity-Registry-based proximity-device resolution, out of scope
    for this port).
    """
    if home is True or proximity_arrived is True or proximity_towards is True:
        return True
    if home is None:
        return None
    if proximity_arrived is None or proximity_towards is None:
        return None
    return False


def resolve_effective_conditioning_rate(
    base_rate: float, outside_temp: float | None, is_cooling: bool
) -> float:
    """1:1 port of ``ScheduleMixin._get_effective_conditioning_rate``."""
    if outside_temp is None:
        return base_rate
    return base_rate * t1_normalization_factor(outside_temp, is_cooling)


def resolve_optimum_start(
    *,
    next_schedule_transition: Any,
    room_temperature: float | None,
    comfort_temperature: float | None,
    is_cooling: bool,
    conditioning_rate: float,
    max_smart_preconditioning: timedelta,
    now: Any,
) -> bool:
    """Return ``True`` if Optimum Start should fire now.

    1:1 port of the Optimum Start block in ``should_set_comfort``,
    minus the ``debug_log`` call. Caller is
    responsible for only invoking this when ``config.smart_preconditioning``
    is set and ``comfort_state`` is still ``False``.
    """
    if not next_schedule_transition:
        return False
    if room_temperature is None or comfort_temperature is None:
        return False

    needs_precondition = (
        room_temperature > comfort_temperature if is_cooling
        else room_temperature < comfort_temperature
    )
    if not needs_precondition:
        return False

    rate = max(0.1, conditioning_rate)
    diff = abs(comfort_temperature - room_temperature)
    precondition_minutes = (diff / rate) * 60

    max_minutes = max_smart_preconditioning.total_seconds() / 60
    precondition_minutes = min(max_minutes, precondition_minutes)

    return now + timedelta(minutes=precondition_minutes) >= next_schedule_transition


def resolve_persons_force_comfort(config: Any, now: Any) -> bool | None:
    """Return ``True`` if the force-comfort time window is active now, else ``None``.

    1:1 port of the ``persons_force_comfort`` window check
    in ``should_set_comfort``. ``None`` means "not applicable, fall
    through to ``comfort_state``" — matching V1, which only ever returns
    ``True`` from this stage, never ``False``. Fails safe to ``True`` on an
    unparseable start/end time, exactly like V1 (the warning log itself
    stays with V1's own unchanged ``should_set_comfort()`` call).
    """
    if not config.persons_force_comfort:
        return None

    start_time_str = config.persons_force_comfort_start
    end_time_str = config.persons_force_comfort_end

    try:
        now_time = now.time()
        start_time = datetime.strptime(start_time_str, "%H:%M:%S").time()
        end_time = datetime.strptime(end_time_str, "%H:%M:%S").time()

        if start_time <= end_time:
            is_in_range = start_time <= now_time <= end_time
        else:
            is_in_range = now_time >= start_time or now_time <= end_time

        return True if is_in_range else None
    except (ValueError, TypeError):
        return True


def resolve_schedule_comfort(
    config: Any,
    *,
    entry_mode: str,
    party_active: bool,
    force_eco: bool,
    away: bool,
    now: Any,
    calendar_comfort_active: bool,
    scheduler_defined: bool,
    scheduler_active: bool | None,
    presence_sensor_defined: bool,
    presence_active: bool | None,
    person_or_proximity_defined: bool,
    cached_home_status: bool | None,
    optimum_start_target: Any,
    room_temperature: float | None,
    comfort_temperature: float | None,
    is_cooling: bool,
    conditioning_rate: float,
) -> tuple[bool | None, bool]:
    """Return ``(comfort, smart_preconditioning_fired)``.

    1:1 port of ``ScheduleMixin.should_set_comfort``, all priority
    stages, including the two Optimum-Start veto paths — modeled as a
    return value instead of V1's
    instance-attribute mutation (``self._smart_preconditioning_active``).

    Bridged inputs (single V1 method calls, no own logic — see
    ``parallel_validation.py::_build_snapshot()`` for the call sites):
    ``calendar_comfort_active`` (``is_calendar_comfort_active()``),
    ``scheduler_active``'s holiday override (folded into
    ``resolve_scheduler_active``), ``is_cooling`` (property), and
    ``conditioning_rate``'s ``_resolve_outside_temp()`` input (folded into
    ``resolve_effective_conditioning_rate``). ``cached_home_status`` is
    natively computed via ``resolve_anybody_home_or_proximity`` from the
    already-bridged ``anybody_home`` plus two bridged proximity checks.
    """
    if config.force_comfort_switch:
        return True, False
    if entry_mode == "eco":
        return False, False
    if entry_mode == "comfort":
        return True, False
    if party_active:
        return True, False
    if force_eco:
        return False, False
    if away:
        return True, False

    scheduling_mode_is_calendar = config.scheduling_mode == SCHEDULING_MODE_CALENDAR
    comfort_state = False

    if calendar_comfort_active:
        comfort_state = True

    if not comfort_state:
        if not scheduling_mode_is_calendar:
            if scheduler_defined:
                if scheduler_active is None:
                    return None, False
                if scheduler_active:
                    comfort_state = True

        # Der Praesenzsensor gilt in JEDEM Scheduling-Modus, nicht nur
        # ausserhalb des Kalendermodus. V1 prueft ihn ausserhalb des
        # `!= CALENDAR`-Gates; der "1:1-Port" hatte den Block in
        # das Gate eingerueckt, V2 ignorierte den Sensor im Kalendermodus
        # (V1 Comfort, V2 Eco, DecisionDiff). Die Paritaetstests liefen nur mit
        # scheduling_mode="manual" — daher unbemerkt.
        if not comfort_state and presence_sensor_defined:
            if presence_active is None:
                return None, False
            if presence_active:
                comfort_state = True

        if not scheduling_mode_is_calendar:
            if not comfort_state and not scheduler_defined and not presence_sensor_defined:
                return cached_home_status, False

    smart_preconditioning_fired = False
    if config.smart_preconditioning and not comfort_state:
        fires = resolve_optimum_start(
            next_schedule_transition=optimum_start_target,
            room_temperature=room_temperature,
            comfort_temperature=comfort_temperature,
            is_cooling=is_cooling,
            conditioning_rate=conditioning_rate,
            max_smart_preconditioning=config.max_smart_preconditioning,
            now=now,
        )
        if fires:
            smart_preconditioning_fired = True
            comfort_state = True

    if person_or_proximity_defined:
        if cached_home_status is None:
            # Optimum Start above may have set the flag True before
            # presence turned out to be unresolvable — this branch rejects
            # that decision, so the flag must not survive it.
            return None, False
        if not cached_home_status:
            if config.away_behavior != AWAY_BEHAVIOR_IGNORE:
                # Same reasoning: Optimum Start's "True" is vetoed by
                # away_behavior, the flag must be vetoed with it.
                return False, False

        if config.persons_force_comfort:
            if resolve_persons_force_comfort(config, now):
                return True, smart_preconditioning_fired

        return comfort_state, smart_preconditioning_fired

    return comfort_state, smart_preconditioning_fired


def resolve_holiday_today(
    config: Any,
    calendar_events: dict,
    get_state: Callable[[str], Any],
    parse_dt: Callable[[Any], datetime | None],
    local_now: datetime,
) -> bool:
    """1:1 port of ``CalendarMixin.is_holiday_today``."""
    if config.vacation_home:
        return True
    cal_id = config.holiday_calendar
    if not cal_id:
        return False
    state = get_state(cal_id)
    if state and state.state == STATE_ON:
        return True
    today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = local_now.replace(hour=23, minute=59, second=59, microsecond=0)
    for ev in calendar_events.get(cal_id, []):
        start = parse_dt(ev.get("start_time") or ev.get("start"))
        end = parse_dt(ev.get("end_time") or ev.get("end"))
        if start and end and start <= today_end and end > today_start:
            return True
    return False


def resolve_calendar_event_filter_prio(
    ev: dict, room_filter: str | None, event_filters: str | None,
) -> int | None:
    """1:1 port of ``CalendarMixin._event_filter_prio``."""
    if room_filter:
        room_filters = [_normalize_calendar_text(rf) for rf in re.split(r"[,;]", room_filter) if rf.strip()]
        location = ev.get("location") or ""
        event_locations = [_normalize_calendar_text(loc) for loc in re.split(r"[,;]", location) if loc.strip()]
        if not event_locations:
            event_locations = [""]
        if not any(rf in el for rf in room_filters for el in event_locations):
            return None

    return resolve_calendar_event_keyword_prio(ev, event_filters)


def resolve_calendar_event_keyword_prio(ev: dict, event_filters: str | None) -> int | None:
    """1:1 port of ``engine_calendar._keyword_prio``: Stichwortfilter eines Termins.

    Index des ersten passenden Stichworts (999 ohne Filter), ``None`` wenn kein
    Stichwort im Titel steht.
    """
    found_prio = 999
    if event_filters:
        filters = [_normalize_calendar_text(f) for f in re.split(r"[,;]", event_filters) if f.strip()]
        summary = _normalize_calendar_text(ev.get("summary", "Termin"))
        for i, f in enumerate(filters):
            if f in summary:
                found_prio = i
                break
        if found_prio == 999:
            return None

    return found_prio


def resolve_calendar_event_score(
    ev: dict,
    is_active: bool,
    found_prio: int,
    now: datetime,
    calendars: list[str],
    parse_dt: Callable[[Any], datetime | None],
) -> tuple[int, float, int, int, int]:
    """1:1 port of ``CalendarMixin._score_event``."""
    start_dt = parse_dt(ev.get("start_time"))
    end_dt = parse_dt(ev.get("end_time"))

    tier = 0 if is_active else 1

    time_dist = 0.0
    if not is_active:
        if start_dt and start_dt > now:
            time_dist = (start_dt - now).total_seconds()
        elif end_dt and end_dt <= now:
            time_dist = (now - end_dt).total_seconds() + 1_000_000_000.0

    duration_prio = 1 if _is_all_day_event(ev, start_dt, end_dt) else 0

    cal_id = ev.get("calendar_id")
    cal_idx = calendars.index(cal_id) if cal_id in calendars else 99

    return (tier, time_dist, found_prio, duration_prio, cal_idx)


def resolve_calendar_event_list(
    events: list[dict],
    *,
    room_filter: str | None,
    event_filters: str | None,
    calendars: list[str],
    now: datetime,
    parse_dt: Callable[[Any], datetime | None],
    active_only: bool = True,
    use_at_state: bool = False,
) -> dict | None:
    """Find the best matching event from a list (filter + priority).

    1:1 port of ``CalendarMixin._process_event_list``.
    """
    best_event: dict | None = None
    best_score: tuple | None = None

    for ev in events:
        if "start" in ev and "start_time" not in ev:
            ev["start_time"] = ev["start"]
        if "end" in ev and "end_time" not in ev:
            ev["end_time"] = ev["end"]

        start_dt = parse_dt(ev.get("start_time"))
        end_dt = parse_dt(ev.get("end_time"))

        is_active = False
        if use_at_state:
            is_active = ev.get("is_active", False)
        else:
            if start_dt and start_dt <= now:
                if not end_dt or end_dt > now:
                    is_active = True

        if active_only and not is_active:
            continue

        found_prio = resolve_calendar_event_filter_prio(ev, room_filter, event_filters)
        if found_prio is None:
            continue

        score = resolve_calendar_event_score(ev, is_active, found_prio, now, calendars, parse_dt)
        if best_score is None or score < best_score:
            best_event = ev
            best_score = score

    return best_event


def resolve_active_calendar_event(
    config: Any,
    calendar_events: dict,
    get_state: Callable[[str], Any],
    parse_dt: Callable[[Any], datetime | None],
    now: datetime,
    *,
    force_check: bool = False,
    active_only: bool = True,
) -> dict | None:
    """Find the prioritized active calendar event matching filters.

    1:1 port of ``CalendarMixin._get_active_calendar_event``.
    """
    if not force_check and config.scheduling_mode != SCHEDULING_MODE_CALENDAR:
        return None

    calendars = config.calendar
    if not calendars:
        return None

    if calendar_events:
        combined_events: list[dict] = []
        for cal_id in calendars:
            for ev in calendar_events.get(cal_id, []):
                ev["calendar_id"] = cal_id
                combined_events.append(ev)

        best_event = resolve_calendar_event_list(
            combined_events, room_filter=config.calendar_room, event_filters=config.calendar_event,
            calendars=calendars, now=now, parse_dt=parse_dt, active_only=active_only,
        )
        if best_event:
            return best_event

    state_events: list[dict] = []
    for cal_id in calendars:
        state = get_state(cal_id)
        if not state or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            continue

        summary = (
            state.attributes.get("message") or
            state.attributes.get("summary") or
            (state.state if state.state not in (STATE_ON, STATE_OFF) else "") or
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

    return resolve_calendar_event_list(
        state_events, room_filter=config.calendar_room, event_filters=config.calendar_event,
        calendars=calendars, now=now, parse_dt=parse_dt, active_only=active_only, use_at_state=True,
    )


def resolve_calendar_delegated_event(
    config: Any,
    calendar_events: dict,
    source_cal_id: str | None,
    day_name: str,
    parse_dt: Callable[[Any], datetime | None],
) -> dict | None:
    """Find delegated event for a weekday.

    Search order: source calendar first, then all room calendars. Candidates are
    narrowed by the keyword filter (``calendar_event``) and the room filter; the
    winner is the longest timed event, an all-day event only if nothing else is
    left. 1:1 port of ``CalendarMixin._get_delegated_event`` (engine_calendar.py).

    Kein Rueckfall auf "alle Termine": findet der Raumfilter nichts, gibt es kein
    Delegat (Begruendung dort).
    """
    if not calendar_events:
        return None

    search_order: list[str] = []
    if source_cal_id and source_cal_id in calendar_events:
        search_order.append(source_cal_id)
    for cal_id in config.calendar:
        if cal_id not in search_order and cal_id in calendar_events:
            search_order.append(cal_id)

    candidates: list[dict] = []
    for cal_id in search_order:
        for ev in calendar_events.get(cal_id, []):
            start_dt = parse_dt(ev.get("start_time") or ev.get("start"))
            if start_dt and start_dt.strftime("%a").lower() == day_name:
                ev["calendar_id"] = cal_id
                candidates.append(ev)

    candidates = [
        ev for ev in candidates
        if resolve_calendar_event_keyword_prio(ev, config.calendar_event) is not None
    ]

    room_filter = config.calendar_room
    if room_filter and candidates:
        room_filters = [_normalize_calendar_text(rf) for rf in re.split(r"[,;]", room_filter) if rf.strip()]
        filtered = [
            ev for ev in candidates
            if any(
                rf in loc
                for rf in room_filters
                for loc in (
                    [_normalize_calendar_text(l) for l in re.split(r"[,;]", ev.get("location") or "") if l.strip()]
                    or [""]
                )
            )
        ]
        if not filtered:
            filtered = [
                ev for ev in candidates
                if any(rf in _normalize_calendar_text(ev.get("summary")) for rf in room_filters)
            ]
        candidates = filtered

    if not candidates:
        return None

    def _rank(ev: dict) -> tuple[bool, float]:
        s = parse_dt(ev.get("start_time") or ev.get("start"))
        e = parse_dt(ev.get("end_time") or ev.get("end"))
        duration = (e - s).total_seconds() if s and e else 0.0
        return (not _is_all_day_event(ev, s, e), duration)

    return max(candidates, key=_rank)


def resolve_calendar_tags(
    config: Any,
    calendar_events: dict,
    get_state: Callable[[str], Any],
    parse_dt: Callable[[Any], datetime | None],
    now: datetime,
    local_now: datetime,
    *,
    event: dict | None = None,
    active_only: bool = True,
    depth: int = 0,
) -> dict[str, Any]:
    """Centralized parsing of all allowed tags in calendar descriptions.

    Supports recursive inheritance if ``use_day`` is present. 1:1 port of
    ``CalendarMixin.get_calendar_tags``.

    Does not replicate V1's ``_calendar_warned`` warn-once logging side
    channel — V1's own unchanged call sites (``coordinator.py`` and others)
    keep emitting those warnings independently, same reasoning as
    ``persons_force_comfort``'s warning in ``resolve_persons_force_comfort``.
    """
    if depth > 2:
        return {}

    if not event:
        event = resolve_active_calendar_event(
            config, calendar_events, get_state, parse_dt, now, active_only=active_only,
        )
        if not event and not active_only:
            event = resolve_active_calendar_event(
                config, calendar_events, get_state, parse_dt, now, active_only=False,
            )

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
            tags["use_day"] = _CALENDAR_DAY_MAP.get(day_str, day_str[:3].lower())

        sched_match = re.search(r"use_scheduler:\s*([^\n]+)", description, re.IGNORECASE)
        if sched_match:
            tags["use_scheduler"] = sched_match.group(1).strip()

    # Holiday override: replace use_day with the effective holiday target day.
    _holiday_target = resolve_holiday_target_day(config)
    if depth == 0 and _holiday_target and resolve_holiday_today(
        config, calendar_events, get_state, parse_dt, local_now,
    ):
        tags["use_day"] = _holiday_target.lower()
        tags.pop("_delegated_event", None)

    # Handle inheritance.
    forced_day = tags.get("use_day")
    if forced_day:
        source_cal_id = event.get("calendar_id") if event else None
        delegated_event = resolve_calendar_delegated_event(
            config, calendar_events, source_cal_id, forced_day, parse_dt,
        )
        if delegated_event:
            parent_tags = resolve_calendar_tags(
                config, calendar_events, get_state, parse_dt, now, local_now,
                event=delegated_event, active_only=False, depth=depth + 1,
            )
            for k, v in parent_tags.items():
                if k not in tags:
                    tags[k] = v
            tags["_delegated_event"] = delegated_event

    return tags


def resolve_daily_time_window(
    time_tag: str, local_now: datetime,
) -> tuple[datetime | None, datetime | None]:
    """Parse a daily window ``HH:MM - HH:MM`` and return it as today's datetimes.

    ``time_tag`` is the stored ``tags["time"]`` value, which carries no
    ``time:`` prefix (``resolve_calendar_tags``); a leading prefix is accepted too.

    1:1 port of ``CalendarMixin._get_daily_time_window_dt``.
    """
    time_match = re.search(r"(?:time:\s*)?(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", time_tag, re.IGNORECASE)
    if not time_match:
        return None, None

    try:
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


def resolve_daily_window_edges(time_tag: str, local_now: datetime) -> list[datetime]:
    """Next occurrences (after ``local_now``, UTC) of the daily window's start and end.

    1:1 port of ``CalendarMixin._get_daily_window_edges``.
    """
    start, end = resolve_daily_time_window(time_tag, local_now)
    if start is None or end is None:
        return []
    edges: list[datetime] = []
    for edge in (start, end):
        if edge <= local_now:
            edge += timedelta(days=1)
        edges.append(edge.astimezone(UTC))
    return edges


def resolve_delegated_window_projection(
    d_start: datetime, d_end: datetime, local_now: datetime,
) -> tuple[datetime, datetime]:
    """Project a delegated (use_day redirect target) event's time-of-day onto today.

    1:1 port of ``CalendarMixin._project_delegated_window_onto_today``
    (engine_calendar.py). Exact two-branch midnight-wrap — do not simplify to
    a single branch (see V1 docstring for why: a naive fix re-anchors
    overnight windows wrongly at local midnight).
    """
    # Ganztagstermin (00:00 bis 00:00): die Uhrzeit-Projektion ergaebe start == end,
    # also ein leeres Fenster und den ganzen Tag Eco. Ein fuer diesen Raum
    # markierter Ganztagstermin heisst: ganzer Tag Comfort.
    if (d_start.hour, d_start.minute, d_end.hour, d_end.minute) == (0, 0, 0, 0) and d_end > d_start:
        day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return day_start, day_start + timedelta(days=1)

    start_time = local_now.replace(hour=d_start.hour, minute=d_start.minute, second=0, microsecond=0)
    end_time = local_now.replace(hour=d_end.hour, minute=d_end.minute, second=0, microsecond=0)

    if end_time < start_time:
        if local_now >= start_time:
            end_time += timedelta(days=1)
        else:
            start_time -= timedelta(days=1)

    return start_time, end_time


def resolve_calendar_transitions(
    config: Any,
    calendar_events: dict,
    get_state: Callable[[str], Any],
    parse_dt: Callable[[Any], datetime | None],
    now: datetime,
    local_now: datetime,
    starts_only: bool = False,
) -> list[datetime]:
    """Future start/end times of this room's fetched calendar events.

    1:1 port of ``CalendarMixin.get_filtered_calendar_transitions``,
    including the redirect-aware projection for the active event, the
    ``time:`` window edges and the ``starts_only`` variant for Optimum Start.
    """
    if not calendar_events:
        return []

    transitions: list[datetime] = []

    active_event = resolve_active_calendar_event(
        config, calendar_events, get_state, parse_dt, now, active_only=False,
    )
    redirect_start = redirect_end = None
    redirect_start_next = redirect_end_next = None
    window_edges: list[datetime] = []
    if active_event:
        tags = resolve_calendar_tags(
            config, calendar_events, get_state, parse_dt, now, local_now,
            event=active_event, active_only=False,
        )
        delegated_event = tags.get("_delegated_event")
        forced_day = tags.get("use_day")
        if delegated_event and forced_day:
            d_start = parse_dt(delegated_event.get("start_time") or delegated_event.get("start"))
            d_end = parse_dt(delegated_event.get("end_time") or delegated_event.get("end"))
            if d_start and d_end:
                local_start, local_end = resolve_delegated_window_projection(d_start, d_end, local_now)
                redirect_start = local_start.astimezone(UTC)
                redirect_end = local_end.astimezone(UTC)

                # Tages-Luecken-Fix, 1:1-Port des V1-Fixes in
                # CalendarMixin.get_filtered_calendar_transitions(): das
                # projizierte Fenster kann bereits vorbei sein (Tages-Luecke)
                # -- dann ist das naechste Redirect-Fenster einen Tag spaeter.
                # Addition auf die lokalen Zeiten, vor .astimezone(UTC).
                if local_end <= local_now:
                    redirect_start_next = (local_start + timedelta(days=1)).astimezone(UTC)
                    redirect_end_next = (local_end + timedelta(days=1)).astimezone(UTC)

        # Fensterkante, 1:1-Port aus
        # CalendarMixin.get_filtered_calendar_transitions(): Beginn und Ende
        # des `time:`-Fensters, solange das effektive Fenster laeuft.
        time_tag = tags.get("time")
        if time_tag:
            eff_start = parse_dt(active_event.get("start_time") or active_event.get("start"))
            eff_end = parse_dt(active_event.get("end_time") or active_event.get("end"))
            if redirect_start is not None:
                eff_start, eff_end = redirect_start, redirect_end
            if eff_start and eff_start <= now and (not eff_end or eff_end > now):
                for edge in resolve_daily_window_edges(time_tag, local_now):
                    if not eff_end or edge < eff_end:
                        window_edges.append(edge)

    if starts_only:
        keys: tuple[str, ...] = ("start_time", "start")
        redirect_candidates = (redirect_start, redirect_start_next)
    else:
        keys = ("start_time", "start", "end_time", "end")
        redirect_candidates = (redirect_start, redirect_end, redirect_start_next, redirect_end_next)

    for cal_id in config.calendar:
        for ev in calendar_events.get(cal_id, []):
            if resolve_calendar_event_filter_prio(ev, config.calendar_room, config.calendar_event) is None:
                continue
            if redirect_start is not None and ev is active_event:
                for dt in redirect_candidates:
                    if dt is not None and dt > now:
                        transitions.append(dt)
                continue
            for key in keys:
                dt = parse_dt(ev.get(key))
                if dt and dt > now:
                    transitions.append(dt)
    if not starts_only:
        transitions.extend(window_edges)
    return transitions


def resolve_holiday_redirect_transitions(
    config: Any,
    calendar_events: dict,
    parse_dt: Callable[[Any], datetime | None],
    active_scheduler: str | None,
    schedule_slots: dict,
    holiday_today: bool,
    now: datetime,
    local_now: datetime,
) -> list[datetime] | None:
    """Naechste Phasengrenzen des Umleitungsziels im Helper-Modus (UTC).

    1:1 port of ``CalendarMixin.get_holiday_redirect_transitions``
    (engine_calendar.py) — Begruendung und Mitternachts-Regel dort. ``None`` =
    Umleitung nicht anwendbar (Aufrufer nutzt die Entity-Attribute).
    """
    day_name = resolve_holiday_target_day(config)
    if not (day_name and holiday_today):
        return None

    if config.calendar:
        delegated_event = resolve_calendar_delegated_event(
            config, calendar_events, None, day_name.lower(), parse_dt,
        )
        if not delegated_event:
            return None
        d_start = parse_dt(delegated_event.get("start_time") or delegated_event.get("start"))
        d_end = parse_dt(delegated_event.get("end_time") or delegated_event.get("end"))
        if not d_start or not d_end:
            return None
        local_start, local_end = resolve_delegated_window_projection(d_start, d_end, local_now)
        candidates = [local_start, local_end]
        if local_end <= local_now:
            candidates += [local_start + timedelta(days=1), local_end + timedelta(days=1)]
        return [c.astimezone(UTC) for c in candidates if c.astimezone(UTC) > now]

    if not active_scheduler:
        return None
    day_slots = schedule_slots.get(active_scheduler, {}).get(
        _HOLIDAY_SHORT_TO_FULL_DAY.get(day_name.lower(), day_name.lower()), []
    )
    if not day_slots:
        return None

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

    result: list[datetime] = []
    for t in edges:
        dt_local = local_now.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
        if dt_local <= local_now:
            dt_local += timedelta(days=1)
        result.append(dt_local.astimezone(UTC))
    return result


def resolve_next_schedule_transition(
    config: Any,
    calendar_events: dict,
    get_state: Callable[[str], Any],
    parse_dt: Callable[[Any], datetime | None],
    active_scheduler: str | None,
    now: datetime,
    local_now: datetime,
    schedule_slots: dict | None = None,
    holiday_today: bool = False,
) -> datetime | None:
    """Find the next phase boundary of the active scheduler or calendar.

    1:1 port of ``ScheduleMixin.get_next_schedule_transition``.
    """
    if config.scheduling_mode == SCHEDULING_MODE_CALENDAR:
        transitions = resolve_calendar_transitions(config, calendar_events, get_state, parse_dt, now, local_now)
        return min(transitions) if transitions else None

    redirect_transitions = resolve_holiday_redirect_transitions(
        config, calendar_events, parse_dt, active_scheduler, schedule_slots or {},
        holiday_today, now, local_now,
    )
    if redirect_transitions is not None:
        return min(redirect_transitions) if redirect_transitions else None

    entities = [active_scheduler] if active_scheduler else []
    if not entities:
        return None

    next_transitions: list[datetime] = []
    for eid in entities:
        state = get_state(eid)
        if not state:
            continue
        for attr in ("start_time", "end_time", "next_event", "next_trigger", "next_occurrence", "next_transition"):
            val = state.attributes.get(attr)
            if val:
                dt = parse_dt(val)
                if dt and dt > now:
                    next_transitions.append(dt)

    return min(next_transitions) if next_transitions else None


def resolve_optimum_start_target(
    config: Any,
    calendar_events: dict,
    get_state: Callable[[str], Any],
    parse_dt: Callable[[Any], datetime | None],
    next_schedule_transition: datetime | None,
    now: datetime,
    local_now: datetime,
) -> datetime | None:
    """Next boundary Optimum Start preheats towards.

    1:1 port of ``ScheduleMixin.get_optimum_start_target``: in calendar mode
    the next event start only, otherwise the next schedule transition.
    """
    if config.scheduling_mode == SCHEDULING_MODE_CALENDAR:
        starts = resolve_calendar_transitions(
            config, calendar_events, get_state, parse_dt, now, local_now, starts_only=True,
        )
        return min(starts) if starts else None
    return next_schedule_transition


_HOLIDAY_SHORT_TO_FULL_DAY = {
    "mon": "monday", "tue": "tuesday", "wed": "wednesday", "thu": "thursday",
    "fri": "friday", "sat": "saturday", "sun": "sunday",
}


def resolve_holiday_target_day(config: Any) -> str | None:
    """Return the effective weekday to substitute for today.

    While ``config.vacation_home`` is on, ``vacation_home_use_day`` takes
    priority (falls back to ``holiday_use_day`` if unset, so existing
    ``vacation_home`` users keep working without configuring the new
    field). Real calendar-detected holidays always use ``holiday_use_day``.
    1:1 port of ``CalendarMixin._resolve_holiday_target_day`` (engine_calendar.py).
    """
    if config.vacation_home:
        return config.vacation_home_use_day or config.holiday_use_day
    return config.holiday_use_day


def resolve_holiday_comfort_via_scheduler(
    schedule_slots: dict,
    scheduler_id: str | None,
    day_name: str | None,
    local_now: datetime,
) -> bool:
    """Check comfort for holiday override without a calendar configured.

    Uses the active Schedule-Helper's own per-weekday slots
    (``schedule.get_schedule`` fetch result) instead of calendar events, so
    ``holiday_use_day``/``vacation_home`` also work for scheduler-only
    setups that have no calendar entity at all. 1:1 port of
    ``CalendarMixin._is_holiday_comfort_via_scheduler`` (engine_calendar.py).
    """
    if not day_name or not scheduler_id:
        return False

    schedule = schedule_slots.get(scheduler_id, {})
    if not schedule:
        return False

    full_day = _HOLIDAY_SHORT_TO_FULL_DAY.get(day_name.lower(), day_name.lower())
    day_slots = schedule.get(full_day, [])
    if not day_slots:
        return False

    current_time = local_now.time()
    for slot in day_slots:
        t_from = slot.get("from")
        t_to = slot.get("to")
        if t_from and t_to and t_from <= current_time < t_to:
            return True
    return False


def resolve_holiday_comfort_via_calendar(
    config: Any,
    calendar_events: dict,
    parse_dt: Callable[[Any], datetime | None],
    now: datetime,
    local_now: datetime,
) -> bool | None:
    """Check comfort for holiday override in helper mode.

    ``None`` = no matching target-day event: the redirect does not apply and
    ``resolve_scheduler_active`` falls through to the Schedule-Helper state.

    Directly looks up the delegated weekday (effective holiday target day,
    see ``resolve_holiday_target_day``) in the calendar events and checks
    whether the current time falls within that day's comfort window. 1:1
    port of ``CalendarMixin._is_holiday_comfort_via_calendar``
    (engine_calendar.py).
    """
    day_name = resolve_holiday_target_day(config)
    if not day_name:
        return False

    delegated_event = resolve_calendar_delegated_event(
        config, calendar_events, None, day_name.lower(), parse_dt,
    )
    if not delegated_event:
        return None

    d_start = parse_dt(delegated_event.get("start_time") or delegated_event.get("start"))
    d_end = parse_dt(delegated_event.get("end_time") or delegated_event.get("end"))
    if not d_start or not d_end:
        return False

    start_time, end_time = resolve_delegated_window_projection(d_start, d_end, local_now)

    start_utc = start_time.astimezone(UTC)
    end_utc = end_time.astimezone(UTC)

    return start_utc <= now < end_utc


def resolve_calendar_comfort_active(
    config: Any,
    calendar_events: dict,
    get_state: Callable[[str], Any],
    parse_dt: Callable[[Any], datetime | None],
    live_calendars: list[str],
    now: datetime,
    local_now: datetime,
    room_temperature: float | None,
    comfort_temperature: float | None,
    conditioning_rate: float,
    is_cooling: bool = False,
) -> bool | None:
    """Check if any calendar event is active or starting soon (preheat).

    1:1 port of ``CalendarMixin.is_calendar_comfort_active``.
    ``live_calendars`` is the caller's entity-registry-filtered list of
    ``config.calendar`` (dead references skipped — ``_entity_is_missing()``,
    engine_base.py:137-167 — stays V1-bridged, not ported here).
    ``room_temperature``/``comfort_temperature``/``conditioning_rate`` are
    the same already-computed values ``resolve_schedule_comfort``'s Optimum Start
    uses; V1 recomputes them independently but they're deterministic
    functions of the same cycle's inputs, so reusing them is behaviorally
    identical (same reasoning as reusing ``adj`` for the Adjustments
    calibration flag). ``is_cooling`` is V1's property of the same name: the
    lead time is direction-aware.
    """
    if config.scheduling_mode != SCHEDULING_MODE_CALENDAR:
        return False

    for cid in live_calendars:
        state = get_state(cid)
        if not state or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None

    event = resolve_active_calendar_event(
        config, calendar_events, get_state, parse_dt, now, active_only=False,
    )
    if not event:
        return False

    tags = resolve_calendar_tags(
        config, calendar_events, get_state, parse_dt, now, local_now, active_only=False,
    )
    time_tag = tags.get("time")
    delegated_event = tags.get("_delegated_event")
    forced_day = tags.get("use_day")

    start_time = parse_dt(event.get("start_time") or event.get("start"))
    end_time = parse_dt(event.get("end_time") or event.get("end"))
    # Needed again for the preheat towards TOMORROW's window under a redirect.
    event_end = end_time
    redirect_window: tuple[datetime, datetime] | None = None

    if delegated_event and forced_day:
        d_start = parse_dt(delegated_event.get("start_time") or delegated_event.get("start"))
        d_end = parse_dt(delegated_event.get("end_time") or delegated_event.get("end"))

        if d_start and d_end:
            start_time, end_time = resolve_delegated_window_projection(d_start, d_end, local_now)
            redirect_window = (start_time, end_time)

            start_time = start_time.astimezone(UTC)
            end_time = end_time.astimezone(UTC)

    daily_start_found, daily_end_found = resolve_daily_time_window(time_tag if time_tag else "", local_now)

    max_mins = config.max_smart_preconditioning.total_seconds() / 60.0

    precondition_minutes = 0.0
    if config.smart_preconditioning:
        if room_temperature is not None and comfort_temperature is not None:
            # Direction-aware like Optimum Start (resolve_schedule_comfort).
            diff = (room_temperature - comfort_temperature) if is_cooling else (comfort_temperature - room_temperature)
            if diff > 0:
                precondition_minutes = min(max_mins, (diff / max(0.1, conditioning_rate)) * 60)

    if start_time and start_time <= now:
        if not end_time or end_time > now:
            if daily_start_found and daily_end_found:
                if daily_start_found <= local_now < daily_end_found:
                    return True
                if precondition_minutes > 0:
                    # Next window start: today's, or tomorrow's once today's
                    # window is over — only if the event still runs by then.
                    next_daily_start = daily_start_found
                    if next_daily_start <= local_now:
                        next_daily_start += timedelta(days=1)
                    runs_then = not end_time or next_daily_start < end_time
                    if not runs_then and redirect_window:
                        # Redirect: ``end_time`` ends TODAY's projection; it
                        # repeats tomorrow while the redirecting event runs.
                        day = timedelta(days=1)
                        runs_then = (
                            (not event_end or next_daily_start < event_end)
                            and redirect_window[0] + day <= next_daily_start < redirect_window[1] + day
                        )
                    if runs_then:
                        # Lead time in real time (UTC), not across a DST change in wall-clock time.
                        dt_preheat_start = next_daily_start.astimezone(UTC) - timedelta(minutes=precondition_minutes)
                        if dt_preheat_start <= now:
                            return True
                return False
            return True
        else:
            return False

    # Towards the event's own START only preheating is decided here;
    # pre-cooling stays with Optimum Start (resolve_schedule_comfort).
    if precondition_minutes > 0 and start_time and not is_cooling:
        # Real time, also for a start without offset (calendar entity attributes).
        preheat_start = start_time.astimezone(UTC) - timedelta(minutes=precondition_minutes)
        if preheat_start <= now < start_time:
            return True

    return False


def resolve_missing_entity_references(
    config: Any,
    entity_is_missing: Callable[[str], bool],
) -> tuple[str, ...]:
    """All configured entity references that no longer exist.

    1:1 port of ``EngineBase.get_missing_entity_references``
    (engine_base.py:169-200). ``entity_is_missing`` is the same
    registry-backed check other resolvers leave V1-bridged (see
    ``resolve_calendar_comfort_active``'s docstring) — bridged here
    too rather than reimplemented, since it needs the entity registry,
    not domain logic.

    Built and wired into the
    Snapshot (``TempixSnapshot.missing_entity_references``), but NO
    ``coordinator.py`` call site reads it yet —
    ``_track_dead_references()`` is unsafe to migrate: it must
    keep firing every cycle regardless of ``manual_mode``/
    ``_should_run_v2()`` (a dead reference appearing mid-pause must
    still be reported, same reasoning as its own "not gated on
    success" docstring), but ``_v2_last_snapshot`` freezes for the
    whole pause once ``_should_run_v2()`` stops running. Migrating
    would have made a pause-appearing dead reference go unreported for
    the pause's entire duration, not just one cycle. Stays on the
    live ``engine.get_missing_entity_references()`` call until a
    per-cycle-fresh source exists independent of ``_should_run_v2``.
    (Since 2026-11-10 the pause path rebuilds the snapshot every cycle,
    ``coordinator._refresh_paused_snapshot``; migrating is still not done
    and would need its own check of cycle order and the refresh's error path.)
    ``_presence_inputs_uncertain`` (feeds
    ``_build_schedule_fingerprint``'s override-clearing presence-uncertainty
    guard) also stays live, unrelated reason: entangled-with-override-lifecycle
    caution — ``_build_schedule_fingerprint()`` runs before the block that
    sets ``_v2_last_snapshot`` for the cycle, so a snapshot source would
    carry the previous cycle's data.

    All five list-valued config fields are guarded with ``or ()`` even
    though the real ``TempixConfig`` defaults them to ``[]``, never
    ``None`` (unlike V1's own method, which only guards ``calendar`` —
    it's bound to a real dataclass instance and never sees the others
    as ``None``). This resolver is a plain function callers may invoke
    with looser test doubles; a shared test config fixture sets
    ``guest_mode=None``, for one.
    """
    kandidaten: list[str] = [
        *(config.persons or ()),
        *(config.guest_mode or ()),
        *(config.schedulers or ()),
        *(config.calendar or ()),
        *(config.window_sensors or ()),
    ]
    presence = config.presence_sensor
    if isinstance(presence, list):
        kandidaten.extend(presence)
    elif presence:
        kandidaten.append(presence)
    if config.outside_temp_sensor:
        kandidaten.append(config.outside_temp_sensor)
    # Gleiche Einzel-Referenzen wie `EngineBase.get_missing_entity_references`
    # (Live-Test 2026-11-04, F11). `getattr`, weil Aufrufer lockere
    # Test-Doubles uebergeben duerfen (siehe Docstring).
    for attr in (
        "weather_entity",
        "season_mode_entity",
        "holiday_calendar",
        "scheduler_selector",
        # `proximity_entity` bewusst nicht — Device-ID, siehe V1.
    ):
        eid = getattr(config, attr, None)
        if eid:
            kandidaten.append(eid)

    gesehen: set[str] = set()
    tot: list[str] = []
    for eid in kandidaten:
        if not isinstance(eid, str) or eid in gesehen:
            continue
        gesehen.add(eid)
        if entity_is_missing(eid):
            tot.append(eid)
    return tuple(tot)
