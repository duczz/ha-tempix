"""Diagnostics support for Tempix."""
from __future__ import annotations

import sys
import logging
from datetime import datetime, UTC
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er_helper

from .const import (
    ACTIVE_ENGINE,
    DOMAIN,
    INVALID_STATES,
    VERSION,
)
from .snapshot_builder import _as_temp_sensor_tuple

TO_REDACT = {"persons", "active_calendar_event", "presence_sensor"}

# Kanal-Suffixe, die im Klartext stehen dürfen. Der Rest eines
# Proximity-Entity-Namens wird pseudonymisiert, weil die HA-Proximity-
# Integration den PERSONENNAMEN in die Entity-ID schreibt
# (`sensor.<proximity>_<person>_distance`) — dieselbe Person, die `persons`
# oben redigiert. Ohne Whitelist wäre der Suffix ein Schlupfloch: ein Gerät
# ohne Kanal-Suffix (`sensor.home_user`) trägt den Namen genau dort.
_PROXIMITY_SUFFIX_ALLOWED = frozenset({
    "azimuth", "dir", "direction", "distance", "nearest", "of",
    "proximity", "travel",
})

# Die einzigen zwei Zustaende, die ein Person-Entity haben kann, ohne einen ORT
# zu nennen. Alles andere ist ein benutzerdefinierter Zonenname — in einem
# realen Export stand dort ein Ortsname, also Person + Aufenthaltsort. Fuer die
# Diagnose zaehlt nur die Kategorie (zuhause / nicht zuhause / unsicher).
_PERSON_STATES_ALLOWED = frozenset({"home", "not_home"})

# HA-Domains, deren Entity-ID per Definition eine PERSON bezeichnet — und deren
# Objekt-ID daher fast immer der Name ist (`person.max_mustermann`,
# `device_tracker.max_iphone`). Anders als eine Namensliste ist diese Menge
# endlich und aufzaehlbar: es sind die beiden Domains, mit denen HA Personen
# modelliert. Alle uebrigen Entity-IDs bleiben lesbar — sie sind das Rueckgrat
# jeder Diagnose, und der Nutzer hat sie selbst benannt.
_PERSONAL_ENTITY_DOMAINS = frozenset({"person", "device_tracker"})


def _safe_state(hass: HomeAssistant, entity_id: str | list | None) -> dict[str, Any] | None:
    """Safely get an entity state for diagnostics."""
    if isinstance(entity_id, list):
        entity_id = entity_id[0] if entity_id else None
    if not isinstance(entity_id, str):
        return None

    state = hass.states.get(entity_id)
    if not state:
        return None
    return {
        "state": state.state,
        "attributes": dict(state.attributes),
        "last_changed": state.last_changed.isoformat() if state.last_changed else None,
        "last_updated": state.last_updated.isoformat() if state.last_updated else None,
    }


def _calendar_aliases(config: Any) -> dict[str, str]:
    """Stabile Pseudonyme fuer alle Kalender-Entity-IDs des Raums.

    Grund: eine Kalender-Entity-ID ist oft die Mailadresse des Nutzers
    (``calendar.vorname_nachname_gmail_com``) — im Klartext im Export, obwohl
    ``persons`` daneben redigiert wird und der Dump an GitHub-Issues haengt.

    Der Schluessel kommt aus der **Position in ``config.calendar``**, nicht aus
    der Aufzaehlung der gefetchten Events. Das ist der Unterschied, der zaehlt:
    ``_async_fetch_calendar_events`` ueberspringt Kalender ohne State und bei
    Fetch-Fehlern (``continue``), ein positionsbasiertes ``enumerate`` ueber das
    Ergebnis-Dict haette also je nach Verfuegbarkeit andere Kalender auf
    ``calendar_0`` gelegt — und die Zuordnung zwischen Config und
    ``fetched_calendar_events`` still verschoben.
    """
    aliases: dict[str, str] = {}
    for i, cal_id in enumerate(getattr(config, "calendar", None) or []):
        aliases[cal_id] = f"calendar_{i}"
    holiday = getattr(config, "holiday_calendar", None)
    if holiday and holiday not in aliases:
        aliases[holiday] = "holiday_calendar"
    return aliases


def _redact_calendar_events(
    events: dict | None, engine: Any = None, aliases: dict[str, str] | None = None
) -> dict | None:
    """Redact each event via ``_redact_event`` (title, location, description,
    calendar ID); anonymize calendar keys.

    Adds ``passes_room_filter`` per event. The list is UNFILTERED —
    with a calendar shared across rooms it otherwise cannot be told which event
    counts for THIS room, and the redaction strips exactly the two fields
    (``summary``, ``location``) the filter runs on. That gap caused a
    misdiagnosis: a foreign room's event read like an overlapping
    comfort slot, while the room filtering was working correctly.

    The flag mirrors ``engine._event_filter_prio`` — i.e. location AND keyword
    filter, the same rule the engine uses for normal event selection and
    transitions. The delegated-event search (``_get_delegated_event``) applies
    its own room filter with a summary fallback, so a delegated event found via
    its title can show ``passes_room_filter: false`` here.
    """
    if not events:
        return events

    aliases = aliases or {}
    result = {}
    for i, (cal_id, cal_events) in enumerate(events.items()):
        # Fallback nur fuer den Fall, dass ein gefetchter Kalender in KEINER
        # Config-Liste mehr steht (Options-Flow-Aenderung zwischen Fetch und
        # Download) — dann lieber ein sichtbar unzugeordneter Schluessel als
        # eine erfundene Zuordnung.
        key = aliases.get(cal_id) or f"calendar_unmapped_{i}"
        result[key] = [_redact_event(ev, engine) for ev in (cal_events or [])]
    return result


def _redact_event(ev: dict, engine: Any = None) -> dict:
    """Ein Kalender-Event ohne Titel, Ort, Beschreibung und Kalender-ID.

    Einzige Stelle, die entscheidet, welche Event-Felder den Export verlassen —
    ``fetched_calendar_events`` und das Umleitungs-Event in ``calendar_tags``
    gehen beide hier durch. Das Umleitungs-Event lag vorher im Klartext im
    Export (Titel, Ort, Kalender-ID), waehrend die gefetchte Liste daneben
    redigiert war.
    """
    # Evaluate BEFORE redaction — the filter needs summary + location.
    passes: bool | None = None
    if engine is not None:
        try:
            passes = engine._event_filter_prio(ev) is not None
        except Exception:  # diagnostics must never break the download
            passes = None
    entry = {
        k: v for k, v in ev.items()
        if k not in ("location", "calendar_id", "summary", "description", "message")
    }
    entry["passes_room_filter"] = passes
    return entry


def _redact_calendar_tags(
    tags: dict | None, engine: Any = None, aliases: dict[str, str] | None = None
) -> dict | None:
    """``calendar_tags`` mit redigiertem Umleitungs-Event (``_delegated_event``).

    Alle anderen Tags (``comfort``, ``use_day``, ``time`` ...) sind Werte aus
    der Tag-Syntax, keine Event-Texte, und bleiben unveraendert. Die
    Kalender-ID des Delegats wird wie bei ``fetched_calendar_events``
    pseudonymisiert (``calendar_<n>``); ist sie in keiner Config-Liste mehr
    zu finden, steht ``calendar_unmapped`` da.
    """
    delegated = (tags or {}).get("_delegated_event")
    if not isinstance(delegated, dict):
        return tags
    entry = _redact_event(delegated, engine)
    cal_id = delegated.get("calendar_id")
    entry["calendar"] = (
        ((aliases or {}).get(cal_id) or "calendar_unmapped") if cal_id else None
    )
    return {**tags, "_delegated_event": entry}


def _override_detail(tmo: Any | None, now: datetime) -> dict[str, Any]:
    """Provenance of the active temporary manual override.

    ``override_active: true`` alone left the first question unanswerable — WHO
    set it WHEN. It came up twice and had to be inferred from
    the *value* both times: a self-echo mirrors what the engine sent, a real
    intervention does not. The override object has carried ``source`` and
    ``timestamp`` all along; they just never reached the export.

    ``tmo`` is None when no override is active — every field is then None
    rather than absent, so the shape of the export stays stable.
    """
    ts = getattr(tmo, "timestamp", None) if tmo is not None else None
    dated = isinstance(ts, datetime)
    # `isinstance` alone is not enough: a NAIVE datetime is still a datetime,
    # and `now - naive` raises TypeError — which would fail the whole
    # diagnostics download, for the one user who most needs it (a corrupted or
    # hand-edited domain store is exactly how a naive timestamp gets in). The
    # restore path normally rejects those earlier, so this is belt and braces.
    aware = dated and ts.tzinfo is not None and ts.utcoffset() is not None
    return {
        "source": getattr(tmo, "source", None) if tmo is not None else None,
        "since": ts.isoformat() if dated else None,
        "age_seconds": round((now - ts).total_seconds()) if aware else None,
        "target_temperature": getattr(tmo, "target_temperature", None) if tmo is not None else None,
        "hvac_mode": getattr(tmo, "hvac_mode", None) if tmo is not None else None,
        "schedule_revision": getattr(tmo, "schedule_revision", None) if tmo is not None else None,
    }


def _person_state_value(value: Any) -> Any:
    """Der Personen-Zustand, aber ohne Zonennamen.

    ``home``/``not_home`` und die Invalid-States bleiben stehen — feste Woerter,
    und genau die Kategorie, auf die die Anwesenheitslogik reagiert. Jeder andere
    Wert ist ein benutzerdefinierter Zonenname, also ein ORT: ein realer Export
    nannte dort einen Ortsnamen. Das ist dieselbe Klasse von Angabe, die die
    Kalender-Redaktion seit v1.6.3 als ``location`` entfernt.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in _PERSON_STATES_ALLOWED or text in INVALID_STATES:
        return value
    return "<zone>"


def _mask_personal_entity(value: Any) -> Any:
    """Reduziert Entity-IDs personenbezogener Domains auf ``<domain>.<redacted>``.

    Betrifft die Felder, die eine Person *referenzieren* koennen, ohne dafuer
    gedacht zu sein: ``guest_mode`` nimmt laut Config-Flow auch ``person`` und
    ``device_tracker``, ``scheduler_selector`` sogar jede Domain. ``persons``
    daneben wird komplett redigiert — die Inkonsistenz war der Befund.

    Die Domain bleibt sichtbar, weil sie diagnostisch traegt (ein
    ``input_boolean`` verhaelt sich anders als ein ``device_tracker``) und selbst
    nichts verraet.
    """
    if isinstance(value, list):
        return [_mask_personal_entity(v) for v in value]
    if not isinstance(value, str) or "." not in value:
        return value
    domain = value.split(".", 1)[0]
    if domain in _PERSONAL_ENTITY_DOMAINS:
        return f"{domain}.<redacted>"
    return value


def _redact_person_states(person_states: dict) -> dict:
    """Anonymize person entity IDs and reduce attributes to state only."""
    return {
        f"person_{i}": {"state": _person_state_value(state.get("state"))} if state else None
        for i, state in enumerate(person_states.values())
    }


def _proximity_suffix(entity_id: str) -> str:
    """Der Kanal-Suffix, falls er zum Vokabular gehoert — sonst ``<other>``."""
    tail = entity_id.rsplit("_", 1)[-1] if "_" in entity_id else entity_id.split(".")[-1]
    return tail if tail in _PROXIMITY_SUFFIX_ALLOWED else "<other>"


# Das Vokabular des HA-Richtungskanals (``direction_of_travel``). Feste
# Wortliste, daher unbedenklich im Klartext — egal, wer das Geraet konfiguriert.
_DIRECTION_VALUES = frozenset({"towards", "away_from", "arrived", "stationary"})


def _proximity_state_value(channel_type: str | None, state: Any) -> Any:
    """Der State eines Kanals — aber nur, wenn seine FORM erwartet wird.

    Whitelist, nicht Blacklist. Die Vorgaengerfassung maskierte gegen die Namen
    aus ``persons`` und liess alles andere durch. Ein realer Export
    trug daraufhin einen Personennamen im Klartext: die Proximity-Integration gab einen
    Namen aus, der in dieser Liste nicht stand (der ``nearest``-Kanal nennt die
    naechste Person). Gerade bei einem Kanal, dessen Typ nicht erkannt wird,
    weiss niemand, was im State steht — dort ist "durchlassen, ausser es ist
    bekannt schlecht" die falsche Richtung.

    Es geht dabei nichts verloren: auf Kanaele ohne erkannten Typ stuetzt sich
    die Auswertung ohnehin nicht (``counts_as: null``,
    ``is_filter_channel: false``). ``unavailable``/``unknown`` bleiben immer
    lesbar — feste Woerter, und der Ausfall eines Kanals ist der diagnostisch
    wichtigste Fall ueberhaupt.
    """
    if state is None:
        return None
    raw = state.state
    text = str(raw).strip().lower()

    if text in INVALID_STATES:
        return raw
    if channel_type == "distance":
        try:
            float(text)
        except (TypeError, ValueError):
            return "<masked>"
        return raw
    if channel_type == "direction" and text in _DIRECTION_VALUES:
        return raw
    return "<masked>"


def _proximity_channel_type(entity_id: str, state: Any) -> str | None:
    """``direction`` | ``distance`` | ``None`` — Formerkennung OHNE den State-Wert.

    Absichtlich getrennt von ``engine._proximity_channel_flags``: jene Funktion
    ist die Live-Auswertung und verlangt fuer "towards" auch den Zustandswert
    ``towards``. Fuer die Frage "sind Richtungs- und Distanzkanal ueberhaupt
    paarbar" (``stem_pairing``) darf der momentane Wert keine Rolle spielen —
    ein Kanal auf ``away_from`` bleibt der Richtungskanal.
    """
    device_class = None
    if state is not None:
        try:
            device_class = state.attributes.get("device_class")
        except Exception:
            device_class = None
    if device_class == "distance":
        return "distance"
    if device_class == "enum":
        return "direction"
    if "distance" in entity_id:
        return "distance"
    if "direction" in entity_id:
        return "direction"
    return None


def _proximity_diagnostics(hass: HomeAssistant, engine: Any) -> dict[str, Any]:
    """Der Proximity-Pfad im Klartext.

    Vorher stand im Export ueber Proximity genau ein Feld: ``is_proximity_active``
    — und das ist ``is_anybody_home_or_proximity()``, das schon auf ``True``
    steht, sobald eine PERSON zuhause ist. Ein Dump aus einem Setup mit
    anwesenden Personen sagte damit nichts darueber, was das Proximity-Geraet
    geliefert hat. Genau die Unterscheidung, die die Fixes (Unsicherheit
    statt stillem "niemand da") herstellen, war im Export nicht nachweisbar.

    ``proximity_entity`` ist eine **Device**-ID; sie loest auf mehrere Entities
    auf (Richtung und Distanz getrennt). Der Block zeigt daher jeden Kanal des
    Geraets — auch die ausgefallenen, die ``proximity_states()`` herausfiltert,
    denn deren Fehlen IST das Ereignis.

    Pseudonymisiert an zwei Stellen, beide als Whitelist:
    - die Entity-ID traegt den Personennamen, es bleiben Stamm-GRUPPE und
      Kanal-Suffix (``_PROXIMITY_SUFFIX_ALLOWED``) — zusammen genau das, was die
      Paarung in ``check_proximity_towards`` entscheidet, ohne die Person zu
      nennen;
    - der STATE-Wert kann den Namen ebenfalls tragen (der ``nearest``-Kanal
      nennt die naechste Person), daher gibt ``_proximity_state_value`` nur
      Werte aus, deren Form zum Kanaltyp passt.
    """
    if not getattr(engine.config, "proximity_entity", None):
        return {"configured": False}

    prox_id = engine.config.proximity_entity
    section: dict[str, Any] = {"configured": True}

    def _safe(fn: Any, default: Any = "error") -> Any:
        try:
            return fn()
        except Exception:  # diagnostics must never break the download
            return default

    try:
        resolved = engine.proximity_states()
        section["verdict"] = "uncertain" if resolved is None else "usable"
    except Exception:
        section["verdict"] = "error"

    # Beide Verdikte sind dreiwertig: True / False / None (= unsicher).
    section["arrived"] = _safe(engine.check_proximity_arrived)
    section["towards"] = _safe(engine.check_proximity_towards)

    try:
        ent_reg = er_helper.async_get(hass)
        entries = er_helper.async_entries_for_device(ent_reg, prox_id)
    except Exception:
        section["channels"] = None
        return section

    stem_groups: dict[str, int] = {}
    channels: list[dict[str, Any]] = []
    direction_stems: set[int] = set()
    distance_stems: set[int] = set()

    for reg_entry in entries:
        eid = reg_entry.entity_id
        state = hass.states.get(eid)
        stem = _safe(lambda eid=eid: engine._proximity_stem(eid), eid)
        group = stem_groups.setdefault(stem, len(stem_groups))
        healthy = state is not None and str(state.state).lower() not in INVALID_STATES
        ch_type = _proximity_channel_type(eid, state)

        counts_as = None
        if healthy:
            flags = _safe(lambda eid=eid, s=state: engine._proximity_channel_flags(eid, s), None)
            if flags:
                is_towards, is_dist = flags
                counts_as = "towards" if is_towards else ("distance" if is_dist else None)

        channels.append({
            "stem_group": f"stem_{group}",
            "suffix": _proximity_suffix(eid),
            "channel_type": ch_type,
            "state": _proximity_state_value(ch_type, state),
            "device_class": (
                state.attributes.get("device_class") if state is not None else None
            ),
            "last_changed": (
                state.last_changed.isoformat()
                if state is not None and state.last_changed else None
            ),
            "healthy": healthy,
            # Zaehlt dieser Kanal fuer die Unsicherheits-Regel? Ein Kanal, der
            # WEDER Form noch Namen nach zum Filter gehoert (Batterie, Update),
            # macht das Geraet nicht unsicher.
            "is_filter_channel": _safe(
                lambda eid=eid, s=state: engine._is_proximity_channel(eid, s), None
            ),
            # Wie die LIVE-Auswertung diesen Kanal in diesem Moment einordnet.
            "counts_as": counts_as,
        })

        if ch_type == "direction":
            direction_stems.add(group)
        elif ch_type == "distance":
            distance_stems.add(group)

    section["channels"] = channels

    # ``check_proximity_towards`` verlangt Richtung UND Distanz auf DEMSELBEN
    # Stamm (alles vor dem letzten ``_``). Sind beide Kanaltypen vorhanden, aber
    # auf verschiedenen Staemmen, ist die Schnittmenge leer und das Ergebnis
    # konstruktionsbedingt ``False`` — ohne Fehler, ohne Logzeile. Dieses Feld
    # macht den Fall sichtbar, statt ihn als "niemand naehert sich" zu lesen.
    if direction_stems and distance_stems:
        section["stem_pairing"] = (
            "paired" if direction_stems & distance_stems else "unpaired"
        )
    else:
        section["stem_pairing"] = "n/a"

    return section


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    engine = data["engine"]

    # Der Export ist opt-out: alles ausser `_`-Feldern geht raus, gefiltert wird
    # unten gezielt. Der frueher hier stehende Namensfilter
    # (`api_key`/`password`/`token`/`username`) ist entfernt
    # worden — keines dieser Felder existiert in `TempixConfig`, eine
    # Klimaintegration hat sie nicht. Er sah nach Schutz aus und war nie einer.
    # Was ein neues Feld wirklich aufhaelt, ist ein Guard-Test.
    redacted_config = {
        k: v for k, v in engine.config.to_dict().items()
        if not k.startswith("_")
    }

    # Felder, die eine Person referenzieren KOENNEN, ohne dafuer gedacht zu sein
    # (s. `_mask_personal_entity`). `persons`/`presence_sensor` laufen daneben
    # ueber TO_REDACT — diese zwei fehlten dort.
    for key in ("guest_mode", "scheduler_selector"):
        if redacted_config.get(key):
            redacted_config[key] = _mask_personal_entity(redacted_config[key])

    # Kalender-Entity-IDs pseudonymisieren — die IDs sind haeufig Mailadressen.
    # Dieselben Pseudonyme benennen unten die `fetched_calendar_events`, die
    # Zuordnung Config ↔ Events bleibt also lesbar.
    calendar_aliases = _calendar_aliases(engine.config)
    if redacted_config.get("calendar"):
        redacted_config["calendar"] = [
            calendar_aliases.get(c, "calendar_unmapped")
            for c in redacted_config["calendar"]
        ]
    if redacted_config.get("holiday_calendar"):
        redacted_config["holiday_calendar"] = calendar_aliases.get(
            redacted_config["holiday_calendar"], "holiday_calendar"
        )

    # ── TRV states ───────────────────────────────────────────────────────────
    trv_states = {}
    for trv_id in engine.config.trvs:
        trv_states[trv_id] = _safe_state(hass, trv_id)

    # ── Sensor states ────────────────────────────────────────────────────────
    sensor_states = {}
    # A multi-sensor temp_sensor used to collapse to one
    # merged entry — the FULL configured list as "entity_id", paired with
    # only sensor [0]'s state/attributes (_safe_state's own collapse). A
    # remote diagnosis couldn't tell which of several sensors that state
    # belonged to, and never saw [1]/[2] at all. One entry per sensor now,
    # same normalization the fusion itself uses (engine_base._resolve_room_temp).
    temp_sensors = _as_temp_sensor_tuple(engine.config.temp_sensor)
    if len(temp_sensors) == 1:
        sensor_states["temperature_sensor"] = {
            "entity_id": temp_sensors[0],
            **(_safe_state(hass, temp_sensors[0]) or {"state": "not_found"}),
        }
    else:
        for sid in temp_sensors:
            sensor_states[f"temperature_sensor_{sid}"] = {
                "entity_id": sid,
                **(_safe_state(hass, sid) or {"state": "not_found"}),
            }
    outside_sensor = engine.config.outside_temp_sensor
    if outside_sensor:
        sensor_states["outside_temperature"] = {
            "entity_id": outside_sensor,
            **(_safe_state(hass, outside_sensor) or {"state": "not_found"}),
        }
    for sid in engine.config.window_sensors:
        sensor_states[f"window_{sid}"] = _safe_state(hass, sid)
    presence = engine.config.presence_sensor
    if presence:
        sensor_states["presence_sensor"] = {
            "entity_id": presence[0] if isinstance(presence, list) else presence,
            **(_safe_state(hass, presence[0] if isinstance(presence, list) else presence) or {"state": "not_found"}),
        }

    # ── Person states ────────────────────────────────────────────────────────
    person_states = {}
    for pid in engine.config.persons:
        person_states[pid] = _safe_state(hass, pid)

    # ── Timing info ──────────────────────────────────────────────────────────
    now = datetime.now(UTC)
    startup = getattr(engine, "_startup_time", None)
    ready = getattr(coordinator, "_ready_time", None)
    last_update = getattr(coordinator, "_last_update", None)

    # A temporary manual override (and force/frost/window) makes the actually
    # driven output diverge from the raw schedule layer. The decision_logic
    # block below therefore separates the two: `schedule_*` = what the schedule
    # would do on its own (override-free, mirrors the V2 SCHEDULE goal),
    # `effective_*` = what is really driving the hardware. Without this split
    # the block read as a contradiction during an override (schedule eco/off
    # vs. driven cool/25).
    _tmo = getattr(coordinator, "_temporary_manual_override", None)
    override_active = bool(_tmo and getattr(_tmo, "active", False))

    override_detail = _override_detail(_tmo if override_active else None, now)

    # LT-F12-Diag: der Export ruft is_window_open() auch indirekt auf
    # (calculate_target_temperature / calculate_hvac_mode). Das V1-Fenster-
    # Gedaechtnis darf dadurch nicht wandern, V2 sieht diese Aufrufe nie.
    with engine.preserve_window_state():
        diag = {
            "system": {
                "ha_version": HA_VERSION,
                "python_version": sys.version,
                "integration_version": VERSION,
                "diagnostic_timestamp": now.isoformat(),
            },
            "info": {
                "entry_id": entry.entry_id,
                "version": entry.version,
                "title": entry.title,
            },
            "config": redacted_config,
            "engine_state": {
                "automation_active": engine.is_automation_active(),
                "season_mode": engine.is_season_mode(),
                "outside_temp_ok": engine.check_outside_threshold(),
                "window_open": engine.is_window_open(),
                "anybody_home": engine.is_anybody_home(),
                "is_proximity_active": engine.is_anybody_home_or_proximity(),
                "away": engine.is_away(),
                "guest_mode": engine.is_guest_mode(),
                "is_party_mode": engine.check_party_mode()[0],
                "party_temperature": engine.check_party_mode()[1],
                "is_scheduler_active": engine.is_scheduler_active(),
                "is_presence_active": engine.is_presence_active(),
                # Fixed `False` so the shape of the export stays stable. V1's
                # absence rule has been removed (CHANGELOG 1.7.0); whether frost
                # protection intervenes is shown by `decision_trace`
                # (FrostConstraint).
                "frost_protection": False,
                "is_liming_active": engine.is_liming_time(),
                "force_comfort": engine.is_force_comfort_temp(),
                "force_eco": engine.is_force_eco_temp(),
                "is_smart_preconditioning_active": engine.is_smart_preconditioning_active(),
                "is_sunshine_offset_active": engine.is_sunshine_offset_active(),
                "sunshine_offset": engine.get_sunshine_offset(),
                "uncertainty_reasons": engine.get_uncertainty_reasons(),
            },
            "decision_logic": {
                "should_set_comfort": engine.should_set_comfort(),
                "resolved_room_temp": engine._resolve_room_temp(),
                "comfort_temp": engine.resolve_comfort_temperature(),
                "eco_temp": engine.resolve_eco_temperature(),
                # Schedule layer only — computed WITHOUT the active override, so it
                # shows what the schedule alone would do (may differ from effective).
                "schedule_target_temperature": engine.calculate_target_temperature(),
                "schedule_hvac_mode": engine.calculate_hvac_mode(),
                # What is actually driving the hardware this cycle.
                "override_active": override_active,
                "override": override_detail,
                "effective_target_temperature": coordinator.current_temperature,
                "effective_hvac_mode": coordinator.current_hvac,
                "effective_reason": coordinator.current_reason,
            },
            "scheduler_info": {
                "active_scheduler": engine.get_active_scheduler(),
                "active_calendar_event": engine._get_active_calendar_event(active_only=False),
                "calendar_tags": _redact_calendar_tags(
                    engine.get_calendar_tags(), engine, calendar_aliases
                ),
                "fetched_calendar_events": _redact_calendar_events(
                    engine._calendar_events, engine, calendar_aliases
                ),
                "fetched_schedule_slots": engine._schedule_slots,
                "schedule_period": engine.get_active_schedule_period(),
                "next_schedule_transition": engine.get_next_schedule_transition(),
            },
            "trv_states": trv_states,
            "sensor_states": sensor_states,
            # Eigener Block statt einer Zeile in `sensor_states`: `proximity_entity`
            # ist eine Device-ID mit N Kanaelen, und die Verdikte darueber sind
            # dreiwertig. Beides passt nicht in das Ein-Entity-Schema oben.
            "proximity": _proximity_diagnostics(hass, engine),
            "person_states": _redact_person_states(person_states),
            "coordinator_state": {
                "current_temperature": coordinator.current_temperature,
                "current_hvac": coordinator.current_hvac,
                "current_reason": coordinator.current_reason,
                "last_calibrations": coordinator.last_calibrations,
                "last_generic_offsets": getattr(coordinator, "last_generic_offsets", {}),
                # Der PLAN des letzten Zyklus (V1-Parität, speist `valve_targets`).
                "last_changes": coordinator.last_changes,
                # Was daraus wirklich rausging — inklusive der Geräte, die der
                # Dispatcher unterdrückt hat. Die stehen als
                # `result: "skipped"` mit Grund im `detail` drin und tragen
                # `sent: false`.
                # Ein leeres `last_dispatch` heißt jetzt: es war nichts geplant.
                # Weicht bewusst von `last_changes` ab (Plan vs. Bilanz).
                "last_dispatch": getattr(coordinator, "_last_dispatch", []),
                # Sperrzeiten — siehe `_circuit_breakers`.
                "circuit_breaker": _circuit_breakers(coordinator),
            },
            "timing": {
                "engine_startup": startup.isoformat() if startup else None,
                "coordinator_ready": ready.isoformat() if ready else None,
                "uptime_seconds": (now - startup).total_seconds() if startup else None,
                "last_update": last_update,
            },
            "v2_parallel_validation": _v2_diagnostics(coordinator),
        }

    return async_redact_data(diag, TO_REDACT)


def _circuit_breakers(coordinator: Any) -> dict[str, Any]:
    """Sperrzeiten beider Circuit Breaker — der einzige Zustand, der ein
    erreichbares Gerät 2–120 min lang von jedem Kommando abschneidet.

    Stand früher in KEINEM Diagnose-Feld. Genau deshalb blieb ein Fall
    (Plan und Gerät divergieren nach einem Neustart, ohne jedes Signal)
    unbestimmbar: Der Breaker wird über den Neustart persistiert
    (``restore_cb``), die ``room_health``, die ihn erklären würde, nicht —
    sie startet als HEALTHY. Ohne dieses Feld ist der Zustand von "alles in
    Ordnung, nichts zu tun" nicht zu unterscheiden.

    Beide Breaker stehen hier, weil der Export nicht wissen muss, welche Engine
    fährt: ``dispatcher`` ist V2s (post-Flip der wirksame), ``v1`` der des
    Coordinators (pre-Flip). ``retry_after: null`` heißt "nicht gesperrt".
    """
    def _serialize(state: Any) -> dict[str, Any]:
        if not isinstance(state, dict):
            return {}
        out: dict[str, Any] = {}
        for entity_id, cb in state.items():
            if not isinstance(cb, dict):
                continue
            retry_after = cb.get("retry_after")
            out[entity_id] = {
                "failures": cb.get("failures", 0),
                "retry_after": (
                    retry_after.isoformat()
                    if hasattr(retry_after, "isoformat") else retry_after
                ),
            }
        return out

    dispatcher = getattr(coordinator, "_v2_dispatcher", None)
    return {
        "dispatcher": _serialize(getattr(dispatcher, "_cb_state", None)),
        "v1": _serialize(getattr(coordinator, "_cb_state", None)),
    }


def _room_health(coordinator: Any) -> str | None:
    """RoomHealth des V2-Dispatchers als Klartext (None, wenn kein Dispatcher)."""
    dispatcher = getattr(coordinator, "_v2_dispatcher", None)
    health = getattr(dispatcher, "room_health", None) if dispatcher else None
    return getattr(health, "value", None)


def _v2_diagnostics(coordinator: Any) -> dict[str, Any]:
    """DecisionTrace + DecisionDiffs of the V2 evaluation (post-flip: V2 drives, V1 observes)."""
    result = getattr(coordinator, "v2_last_result", None)
    section: dict[str, Any] = {
        # WELCHE Engine faehrt — die erste Frage bei jedem Cutover-Support-Fall
        # und der Beleg fuer die Rollback-Probe. Steht bewusst AUCH hier
        # und nicht nur als INFO-Logzeile: der HA-Log-Viewer zeigt per Default
        # nur WARNING und hoeher, INFO findet man erst in den vollen Logs.
        "active_engine": ACTIVE_ENGINE,
        "evaluations": getattr(coordinator, "v2_eval_count", 0),
        "diff_count": getattr(coordinator, "v2_diff_count", 0),
        # error_count > 0: the V2 evaluation is throwing. Post-flip the room
        # is then uncontrolled, and "0 diffs" does NOT mean parity (see
        # coordinator WARNING).
        "error_count": getattr(coordinator, "v2_error_count", 0),
        # RoomHealth: aus FEHLGESCHLAGENEN Dispatches abgeleitet
        # (HEALTHY / DEGRADED = ein Teil der Geraete / OFFLINE = alle). Wurde
        # berechnet, aber von niemandem gelesen — post-Flip ist das
        # die einzige Stelle, an der "welches Ventil nimmt nichts mehr an"
        # sichtbar wird, ohne das Log zu durchsuchen.
        "room_health": _room_health(coordinator),
    }
    if result is not None:
        section["last_result"] = {
            "snapshot_id": result.snapshot_id,
            "goal": result.goal_class.name if result.goal_class else None,
            "hvac_mode": result.target_state.hvac_mode,
            "target_temperature": result.target_state.target_temperature,
            "validity": result.validity.value,
            # Ohne Klartext ist `validity` eine Sackgasse: "degraded" allein
            # zwingt zum Rueckwaertsschliessen ueber die Sensorzustaende.
            "validity_reasons": list(getattr(result, "validity_reasons", ())),
            "expired_override_id": result.expired_override_id,
            "next_evaluation_at": (
                result.next_evaluation_at.isoformat()
                if result.next_evaluation_at else None
            ),
            "planned_commands": [
                {
                    "entity_id": c.entity_id,
                    "hvac_mode": c.hvac_mode,
                    "temperature": c.target_temperature,
                }
                for c in result.hardware_commands
            ],
            # The gap-free reasoning chain of the last decision.
            "decision_trace": [
                {
                    "policy": e.policy,
                    "input": e.input_value,
                    "output": e.output_value,
                    "reason": e.reason,
                }
                for e in result.trace.entries
            ],
        }
    recent = getattr(coordinator, "v2_recent_diffs", None)
    if recent:
        section["recent_diffs"] = [d.to_log_dict() for d in recent]
    return section
