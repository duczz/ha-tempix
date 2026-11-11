"""The Tempix integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from custom_components.tempix.const import (
    DOMAIN, PLATFORMS,
    CONF_GUEST_MODE_SWITCH, CONF_PARTY_MODE_SWITCH,
    CONF_AGGRESSIVE_MODE, CONF_AGGRESSIVE_CALIBRATION_SWITCH, CONF_AGGRESSIVE_MODE_SELECTOR,
    AGGRESSIVE_MODE_OFF, AGGRESSIVE_MODE_TARGET, AGGRESSIVE_MODE_CALIBRATION,
    CONF_CALIBRATION_MODE, CALIBRATION_MODE_OFF, CALIBRATION_MODE_NATIVE, CALIBRATION_MODE_GENERIC,
    CONF_CALIBRATION_ENABLED, CONF_CALIBRATION_GENERIC,
    CONF_ENABLE_TEMPORARY_MANUAL_OVERRIDE,
    CONF_MANUAL_MODE, LEGACY_CONF_MANUAL_OVERRIDE,
    CONF_TRVS, CONF_TEMPERATURE_SENSOR, CONF_OUTSIDE_TEMP_SENSOR,
    CONF_WINDOW_SENSORS, CONF_PERSONS, CONF_SCHEDULERS,
    CONF_SCHEDULER_SELECTOR, CONF_PRESENCE_SENSOR,
    CONF_SEASON_MODE_ENTITY, CONF_CALENDAR, CONF_PROXIMITY_ENTITY,
    CONF_GUEST_MODE, CONF_SCHEDULER_PRESENCE, CONF_WEATHER_ENTITY,
    CONF_HOLIDAY_CALENDAR, CONF_SCHEDULING_MODE, CONF_CALENDAR_SCAN_INTERVAL,
    CONF_ADJUSTMENTS,
)
from custom_components.tempix.config_model import TempixConfig
from custom_components.tempix.config_validation import ISSUE_TRANSLATION_KEYS
from custom_components.tempix.engine import TempixEngine
from custom_components.tempix.coordinator import TempixCoordinator

_LOGGER = logging.getLogger(__name__)

# Keys that REQUIRE a full reload (structural changes).
#
# Die Regel dahinter, damit die Liste nicht wieder auseinanderlaeuft:
# **jeder Key, den `_register_listeners` oder
# `_start_coordinator` liest, gehoert hier hinein.** Beide laufen ausschliesslich
# aus `async_setup` heraus und sind per `_updates_enabled` gegen einen zweiten
# Lauf gesperrt — der dynamische Pfad tauscht also die Config, aber nie die
# Ableitungen daraus. Ein Key, der nur dort gelesen wird, bleibt sonst bis zum
# naechsten Neustart wirkungslos.
#
# Die untere Gruppe fehlte genau deshalb — beschrieben nach URSACHE, weil das
# die Regel oben ist, nicht nach Symptom: `holiday_calendar` bekam weder Fetcher
# noch Listener (nachtraeglich gesetzt also komplett inert), `scheduling_mode`
# liess den jeweils anderen Fetcher unregistriert, `calendar_scan_interval`
# erreichte den bereits laufenden Timer nicht, und `guest_mode`/
# `scheduler_presence`/`weather_entity`/`adjustments` (dessen comfort/eco
# entity_ids sein duerfen) bauten ihre State-Change-Subscription nicht auf.
# Sichtbar wurde Letzteres als „Aenderung loest keinen Zyklus aus" — der WERT
# selbst wird pro Zyklus frisch gelesen, verloren ging nur der Ausloeser, es
# blieb der 1-Minuten-Heartbeat (bis zu 60 s spaet).
#
# Auf Modulebene statt lokal in `async_reload_entry`, damit
# ein Test die Liste gegen die beiden Funktionen halten kann.
RELOAD_REQUIRED_KEYS = {
    CONF_TRVS,
    CONF_TEMPERATURE_SENSOR,
    CONF_OUTSIDE_TEMP_SENSOR,
    CONF_WINDOW_SENSORS,
    CONF_PERSONS,
    CONF_SCHEDULERS,
    CONF_SCHEDULER_SELECTOR,
    CONF_PRESENCE_SENSOR,
    CONF_SEASON_MODE_ENTITY,
    CONF_CALENDAR,
    CONF_PROXIMITY_ENTITY,
    CONF_GUEST_MODE,
    CONF_SCHEDULER_PRESENCE,
    CONF_WEATHER_ENTITY,
    CONF_HOLIDAY_CALENDAR,
    CONF_SCHEDULING_MODE,
    CONF_CALENDAR_SCAN_INTERVAL,
    CONF_ADJUSTMENTS,
}


_TRIGGER_UPDATE_SCHEMA = vol.Schema({
    vol.Optional("config_entry_id"): str,
})

_SET_STATUS_SCHEMA = vol.Schema({
    vol.Optional("config_entry_id"): str,
    vol.Required("status"): bool,
    vol.Optional("duration"): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
})


def _require_loaded_entry(hass: HomeAssistant, target_entry: str | None) -> None:
    """Eine gesetzte ``config_entry_id`` muss auf einen geladenen Raum zeigen.

    Vorher traf eine unbekannte ID (Tippfehler, geloeschter oder deaktivierter
    Raum) keinen Eintrag, und der Aufruf endete ohne Fehler und ohne Logzeile
    (Live-Test). Eine leere oder fehlende ID bleibt
    bewusst haus-weit (dokumentiertes Verhalten).
    """
    if not target_entry:
        return
    data = hass.data[DOMAIN].get(target_entry)
    if not (isinstance(data, dict) and "coordinator" in data):
        raise ServiceValidationError(
            f"Unknown or not loaded Tempix config_entry_id: {target_entry!r}"
        )


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Register die drei Domain-Services genau einmal.

    HAs Quality-Scale-Regel ``action-setup`` verlangt Registrierung hier statt
    in ``async_setup_entry`` — die lief bisher pro Config Entry, also mit N
    Raeumen N-mal, jedes Mal dieselbe Closure ueberschreibend (keine der
    ueberschriebenen Closures war je falsch, aber ``has_service`` fehlte als
    Guard), und blieb nach Entfernen des letzten Entries fuer immer in
    Developer Tools -> Aktionen stehen, weil nichts sie je abmeldet.
    Domain-Services in ``async_setup`` ist stattdessen der Standardfall: HA
    ruft die Funktion genau einmal auf, unabhaengig von der Zahl der Entries.
    """
    hass.data.setdefault(DOMAIN, {})

    async def handle_trigger_update(call: ServiceCall) -> None:
        """Force a recalculation for one or all relevant instances."""
        target_entry = call.data.get("config_entry_id")
        _require_loaded_entry(hass, target_entry)

        # A room's own async_update() can park on its _update_lock for
        # seconds to minutes while this loop holds no lock at all —
        # another entry setting up, unloading, or reloading in that window
        # mutates hass.data[DOMAIN] mid-iteration (RuntimeError: dictionary
        # changed size during iteration) and would abort this house-wide
        # call halfway, with entries after the mutation silently skipped.
        # Snapshot the id list, then re-fetch each entry's CURRENT data
        # right before use — a reload swaps in a new coordinator instance,
        # and the stale one from the snapshot must not be awaited.
        for entry_id in list(hass.data[DOMAIN]):
            if target_entry and entry_id != target_entry:
                continue
            data = hass.data[DOMAIN].get(entry_id)
            if not (isinstance(data, dict) and "coordinator" in data):
                continue
            _LOGGER.debug("Triggering manual update for coordinator %s", entry_id)
            await data["coordinator"].async_update()

    async def handle_set_status(call: ServiceCall) -> None:
        """Handle set_party_mode and set_guest_mode with optional duration.

        Optional ``config_entry_id`` targets a single room (mirrors
        handle_trigger_update). Without it the status is set house-wide, exactly
        as before — backward-compatible for existing automations.
        """
        status = call.data["status"]
        duration = call.data.get("duration")
        target_entry = call.data.get("config_entry_id")
        service = call.service
        key_map = {
            "set_party_mode": CONF_PARTY_MODE_SWITCH,
            "set_guest_mode": CONF_GUEST_MODE_SWITCH,
        }
        config_key = key_map.get(service)
        if config_key:
            _require_loaded_entry(hass, target_entry)
            # Same reasoning as handle_trigger_update above.
            for entry_id in list(hass.data[DOMAIN]):
                if target_entry and entry_id != target_entry:
                    continue
                data = hass.data[DOMAIN].get(entry_id)
                if not (isinstance(data, dict) and "coordinator" in data):
                    continue
                await data["coordinator"].async_set_temporary_option(
                    config_key, status, duration
                )

    hass.services.async_register(
        DOMAIN, "trigger_update", handle_trigger_update, schema=_TRIGGER_UPDATE_SCHEMA)
    hass.services.async_register(
        DOMAIN, "set_party_mode", handle_set_status, schema=_SET_STATUS_SCHEMA)
    hass.services.async_register(
        DOMAIN, "set_guest_mode", handle_set_status, schema=_SET_STATUS_SCHEMA)

    return True


def _raw_config(entry: ConfigEntry) -> dict[str, Any]:
    """``entry.data + entry.options``, mit ``entry.title`` als Namensquelle.

    ``data["name"]`` friert beim Anlegen ein — der Options-Flow fuehrt
    ``CONF_NAME`` nicht, nur ``ConfigFlow.async_step_user`` setzt ihn. Wer den
    Eintrag ueber HAs Kontextmenue umbenennt, aendert deshalb nur den Titel,
    und ``config.name`` zeigte danach dauerhaft den Anlagenamen — sichtbar in
    Log-Praefixen (``TPX Coord [...]``) und in den Repair-Issue-Texten.

    Der Titel gewinnt hier, damit beides der Umbenennung folgt. Zweiter Effekt:
    weil ``_raw`` den Titel jetzt enthaelt, sieht ``async_reload_entry`` eine
    Umbenennung ueberhaupt erst als ``changed_key`` — vorher verglich es nur
    data+options gegen ``config._raw``, ein Titelwechsel war dort unsichtbar
    und lief in den ``no changed keys``-Return.

    Gefahrlos, weil ``config.name`` nirgends Identitaet traegt: alle Storage-
    Keys haengen an ``entry_id``, und ``MigratingStore``/``SceneManager``
    benutzen den Namen ausschliesslich als Log-Label (geprueft).
    """
    raw = {**entry.data, **entry.options}
    if entry.title:
        raw["name"] = entry.title
    return raw


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tempix from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # 1. Configuration Migration
    # Check for missing keys or legacy boolean flags before initializing full motor
    # Use merged config (data + options) so new entries (where everything is in data)
    # don't falsely trigger migration and skip platform setup.
    merged_config = {**entry.data, **entry.options}
    changed = False
    new_options = dict(entry.options)
    
    # aggressive mode selector migration – skip if already present in data or options
    if CONF_AGGRESSIVE_MODE_SELECTOR not in merged_config:
        mode = AGGRESSIVE_MODE_OFF
        if merged_config.get(CONF_AGGRESSIVE_CALIBRATION_SWITCH):
            mode = AGGRESSIVE_MODE_CALIBRATION
        elif merged_config.get(CONF_AGGRESSIVE_MODE):
            mode = AGGRESSIVE_MODE_TARGET
        
        new_options[CONF_AGGRESSIVE_MODE_SELECTOR] = mode
        new_options.pop(CONF_AGGRESSIVE_MODE, None)
        new_options.pop(CONF_AGGRESSIVE_CALIBRATION_SWITCH, None)
        changed = True

    # calibration mode selector migration – skip if already in data or options
    if CONF_CALIBRATION_MODE not in merged_config:
        cal_mode = CALIBRATION_MODE_OFF
        if merged_config.get(CONF_CALIBRATION_GENERIC):
            cal_mode = CALIBRATION_MODE_GENERIC
        elif merged_config.get(CONF_CALIBRATION_ENABLED, False):
            cal_mode = CALIBRATION_MODE_NATIVE
        
        new_options[CONF_CALIBRATION_MODE] = cal_mode
        new_options.pop(CONF_CALIBRATION_ENABLED, None)
        new_options.pop(CONF_CALIBRATION_GENERIC, None)
        changed = True

    # Temporary manual override migration – existing users get opt-in default (False)
    if CONF_ENABLE_TEMPORARY_MANUAL_OVERRIDE not in merged_config:
        new_options[CONF_ENABLE_TEMPORARY_MANUAL_OVERRIDE] = False
        changed = True

    # Pause switch rename (1.7.0): manual_override -> manual_mode. Der Wert (laufende
    # Pause) wandert mit; ist der neue Key schon gesetzt, gewinnt er.
    if LEGACY_CONF_MANUAL_OVERRIDE in new_options:
        legacy_value = new_options.pop(LEGACY_CONF_MANUAL_OVERRIDE)
        new_options.setdefault(CONF_MANUAL_MODE, legacy_value)
        changed = True

    # Die unique_id des Schalters zieht mit, sonst legt HA eine neue Entity an und
    # die alte (samt Entity-ID, Verlauf, Dashboard-Verweisen) bleibt verwaist.
    ent_reg = er.async_get(hass)
    legacy_entity_id = ent_reg.async_get_entity_id(
        "switch", DOMAIN, f"{entry.entry_id}_{LEGACY_CONF_MANUAL_OVERRIDE}"
    )
    if legacy_entity_id:
        ent_reg.async_update_entity(
            legacy_entity_id, new_unique_id=f"{entry.entry_id}_{CONF_MANUAL_MODE}"
        )

    if changed:
        _LOGGER.info("Migrating %s config for new switch keys/defaults.", entry.entry_id)
        hass.config_entries.async_update_entry(entry, options=new_options)
        # NO early return here. async_update_entry only notifies REGISTERED
        # update listeners (verified against HA 2026.6: _async_save_and_notify)
        # — ours is registered further down, so nothing would reload the entry
        # and it would sit LOADED but without platforms until the next restart.
        # entry.options is updated synchronously, so setup continues below with
        # the migrated values.

    config = TempixConfig.from_dict(_raw_config(entry))

    # 2. Initialise Engine (pure logic) and Coordinator (state management)
    engine = TempixEngine(hass, config)
    coordinator = TempixCoordinator(hass, config, engine, entry_id=entry.entry_id)

    try:
        await coordinator.async_setup()
    except Exception as err:
        _LOGGER.error("Error setting up Tempix: %s", err)
        # AU-3: HA ruft async_unload_entry für einen nicht geladenen Eintrag nicht
        # auf. Ohne Aufräumen blieben die in async_setup/_start_coordinator
        # registrierten Listener und Timer stehen, und der nächste Versuch legte
        # einen zweiten Koordinator daneben.
        try:
            await coordinator.async_unload()
        except Exception:  # noqa: BLE001 – der ursprüngliche Fehler zählt
            _LOGGER.debug("Cleanup after failed setup also failed", exc_info=True)
        raise ConfigEntryNotReady from err

    # Re-load on option change
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    hass.data[DOMAIN][entry.entry_id] = {
        "engine": engine,
        "coordinator": coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    _LOGGER.info("Unloading Tempix entry: %s", entry.entry_id)
    data = hass.data[DOMAIN].get(entry.entry_id)
    if data and "coordinator" in data:
        await data["coordinator"].async_unload()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Config-Befunde des entfernten Eintrags aus den Repairs nehmen.

    Beim Entfernen ist der Coordinator schon entladen, daher direkt ueber die
    feste Code-Menge statt ueber dessen Gedaechtnis. Die uebrigen Repair-Issues
    (`paused_`, `update_failing_`, `calibration_unavailable_`, `trv_unavailable_`,
    `degraded_`, `dead_reference_`) raeumt diese Funktion nicht ab, das muss sie
    auch nicht: Das Coordinator-Unload loescht sie, solange sie aktiv sind
    (`_*_issue_active`; live gemessen 2026-11-04). Ein Issue von vor einem
    Neustart, das danach nicht wieder aktiv wurde, ist nicht persistent
    (`is_persistent` wird nie gesetzt) und laedt HA mit `active=False`; die
    Repairs-Liste zeigt es nicht (HA-Quelle 2026.10 gelesen:
    `helpers/issue_registry.py`, `components/repairs/websocket_api.py`).
    """
    from homeassistant.helpers import issue_registry as ir

    for code in ISSUE_TRANSLATION_KEYS:
        ir.async_delete_issue(hass, DOMAIN, f"config_{code}_{entry.entry_id}")


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry when options are updated."""
    new_config = _raw_config(entry)

    # Identify changed keys
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data or "engine" not in data:
        _LOGGER.debug("Reload listener triggered but data not yet initialized for %s", entry.entry_id)
        return

    old_raw = data["coordinator"].config._raw  # Original dict for comparison

    def _norm(v: Any) -> Any:
        """Treat None, empty string, and missing key as equivalent."""
        return None if v in (None, "", "None") else v

    changed_keys = {
        k for k in (set(new_config) | set(old_raw))
        if not k.startswith("_") and _norm(new_config.get(k)) != _norm(old_raw.get(k))
    }

    if not changed_keys:
        _LOGGER.debug("Reload listener for %s: no changed keys, skipping", entry.title)
        return

    # If no reload-required key was changed, do a dynamic update
    if not (changed_keys & RELOAD_REQUIRED_KEYS):
        _LOGGER.info("Dynamic update for %s. Keys: %s", entry.title, changed_keys)
        typed_config = TempixConfig.from_dict(new_config)
        # apply_config swaps the config on the coordinator, the ENGINE and all
        # helper objects (CalibrationApplier, ValvePositioner,
        # ClimateRateLearner, SceneManager, V2-Dispatcher) — eine schlichte
        # Attributzuweisung liesse sie auf der alten Instanz stehen.
        data["coordinator"].apply_config(typed_config)

        # Ein reines Umbenennen ist KEINE Regeländerung. Seit `_raw_config` den
        # Titel führt, taucht es hier als `changed_key` auf — gewollt, damit die
        # Log-Label mitziehen. Ein erzwungener Regelzyklus ist für eine
        # Umbenennung aber schlicht nicht bestellt.
        if changed_keys == {"name"}:
            _LOGGER.debug(
                "Rename only for %s — Konfiguration getauscht, "
                "Regelzyklus unberührt", entry.title)
            return

        # KEIN Verwerfen von `_last_outside_ok`/`_last_home_status` mehr
        # (war seit 1.6.3 drin). Jede Bedienung — Schalter,
        # Number-Entity, Options-Änderung — läuft durch diesen Pfad, und das
        # Verwerfen liess die nächste Auswertung im Bootstrap-Zweig gegen die
        # nackte Schwelle rechnen: in der Totzone kippte der Raum bei einem
        # Klick auf Party-Modus ab, und `_on_dirty` schrieb den gekippten Wert
        # in den Store (überlebte also auch den Neustart).
        #
        # Ein Verwerfen ist auch für die Keys nicht nötig, für die es intuitiv
        # richtig scheint: die Hysterese-Zweige in `check_outside_threshold`
        # rechnen jeden Zyklus gegen die AKTUELLE Schwelle. Gemessen für
        # Schwellen-, Hysterese- und heat↔cool-Wechsel (letzterer dreht
        # `_factor` und damit die Bedeutung des Flags): ausserhalb der Totzone
        # liefern „behalten" und „Bootstrap" dasselbe Ergebnis, innerhalb der
        # Totzone ist Kontinuität per Definition die gewollte Antwort. Ein
        # Wechsel des Aussensensors ist ohnehin RELOAD_REQUIRED.
        #
        # `async_request_refresh` statt `async_update`: die Switch-Entities
        # rufen nach ihrem `async_update_entry` selbst refresh an — beide Wege
        # laufen damit durch denselben Coalescer statt nur durch `_update_lock`.
        _LOGGER.debug("Dynamic update for %s: calling async_request_refresh", entry.title)
        await data["coordinator"].async_request_refresh()
        _LOGGER.debug("Dynamic update for %s: refresh complete", entry.title)
        return

    # Otherwise, full reload
    _LOGGER.info("Full reload required for %s due to changes in: %s", entry.title, (changed_keys & RELOAD_REQUIRED_KEYS) or changed_keys)
    await hass.config_entries.async_reload(entry.entry_id)
