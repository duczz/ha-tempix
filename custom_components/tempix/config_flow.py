"""Config flow for Tempix.

Single Expert path: 6 focused screens with section headers, shared 1:1
between ConfigFlow (initial setup) and OptionsFlow (edit) via
``TempixCommonFlow`` — same steps, same data keys, no drift.
(A separate "Simple Setup" existed briefly and was removed on purpose:
parallel flows doubled code + tests.)
"""
from __future__ import annotations

import copy
from typing import Any
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult, section
from homeassistant.helpers import selector

from custom_components.tempix.hvac_modes import common_schedule_hvac_modes
from custom_components.tempix.config_model import VALVE_MODES, parse_valve_mode, validate_adjustments
from custom_components.tempix.const import (
    DOMAIN,
    CONF_NAME,
    CONF_TRVS,
    CONF_TEMPERATURE_SENSOR,
    CONF_TEMPERATURE_COMFORT_STATIC,
    CONF_TEMPERATURE_ECO_STATIC,
    CONF_HVAC_MODE_COMFORT,
    CONF_HVAC_MODE_ECO,
    CONF_SCHEDULERS,
    CONF_SCHEDULER_SELECTOR,
    CONF_PERSONS,
    CONF_PEOPLE_ENTERING_DURATION,
    CONF_PEOPLE_LEAVING_DURATION,
    CONF_PERSONS_FORCE_COMFORT,
    CONF_PERSONS_FORCE_COMFORT_START,
    CONF_PERSONS_FORCE_COMFORT_END,
    CONF_GUEST_MODE,
    CONF_PROXIMITY_ENTITY,
    CONF_PROXIMITY_DISTANCE,
    CONF_PROXIMITY_DURATION,
    CONF_PRESENCE_SENSOR,
    CONF_SCHEDULER_PRESENCE,
    CONF_PRESENCE_REACTION_ON,
    CONF_PRESENCE_REACTION_OFF,
    CONF_ADJUSTMENTS,
    CONF_MIN_INSTEAD_OF_OFF,
    CONF_ENABLE_TEMPORARY_MANUAL_OVERRIDE,
    CONF_OFF_IF_ABOVE_ROOM_TEMP,
    CONF_AWAY_BEHAVIOR,
    CONF_HYSTERESIS,
    CONF_AWAY_OFFSET,
    CONF_AWAY_PRESENCE_MODE,
    CONF_AWAY_IGNORE_PEOPLE,
    CONF_WINDOW_SENSORS,
    CONF_WINDOW_REACTION_OPEN,
    CONF_WINDOW_REACTION_CLOSE,
    CONF_WINDOW_OPEN_TEMP,
    CONF_CALIBRATION_MODE,
    CONF_CALIBRATION_KEYWORD,
    CONF_CALIBRATION_TIMEOUT,
    CONF_CALIBRATION_DELTA,
    CONF_CALIBRATION_STEP_SIZE,
    CONF_GENERIC_CALIBRATION_LIMIT,
    CONF_FROST_PROTECTION_ENABLED,
    CONF_FROST_PROTECTION_TEMP,
    CONF_LIMING_PROTECTION,
    CONF_LIMING_DAY,
    CONF_LIMING_TIME,
    CONF_LIMING_DURATION,
    CONF_LIMING_IN_SEASON,
    CONF_SEASON_MODE_ENTITY,
    CONF_OUTSIDE_TEMP_SENSOR,
    CONF_OUTSIDE_TEMP_THRESHOLD,
    CONF_OUTSIDE_TEMP_HYSTERESIS,
    CONF_OUTSIDE_TEMP_FALLBACK,
    CONF_WEATHER_ENTITY,
    CONF_IDLE_TEMPERATURE,
    CONF_ROOM_TEMP_THRESHOLD_ENABLED,
    CONF_ROOM_TEMP_THRESHOLD,
    CONF_VALVE_MODE,
    CONF_VALVE_DIFF,
    CONF_VALVE_STEP,
    CONF_VALVE_MAX,
    CONF_VALVE_MIN,
    CONF_VALVE_KEYWORD,
    CONF_ACTION_DELAY,
    CONF_SENSOR_RETENTION,
    DEFAULT_NAME,
    DEFAULT_FROST_TEMP,
    DEFAULT_OUTSIDE_THRESHOLD,
    DEFAULT_OUTSIDE_HYSTERESIS,
    DEFAULT_ROOM_THRESHOLD,
    DEFAULT_AWAY_OFFSET,
    DEFAULT_HYSTERESIS,
    DEFAULT_VALVE_DIFF,
    DEFAULT_VALVE_MAX,
    DEFAULT_VALVE_MIN,
    DEFAULT_VALVE_STEP,
    DEFAULT_PROXIMITY_DISTANCE,
    DEFAULT_WINDOW_OPEN_TEMP,
    DEFAULT_LIMING_DURATION,
    DEFAULT_AGGRESSIVE_OFFSET,
    DEFAULT_AGGRESSIVE_RANGE,
    DEFAULT_WINDOW_REACTION_OPEN,
    DEFAULT_WINDOW_REACTION_CLOSE,
    DEFAULT_PRESENCE_REACTION_ON,
    DEFAULT_PRESENCE_REACTION_OFF,
    DEFAULT_PEOPLE_ENTERING,
    DEFAULT_PEOPLE_LEAVING,
    DEFAULT_SENSOR_RETENTION,
    DEFAULT_PROXIMITY_DURATION,
    DEFAULT_ACTION_DELAY,
    DEFAULT_COMFORT_TEMP,
    DEFAULT_ECO_TEMP,
    DEFAULT_CALIBRATION_DELTA,
    DEFAULT_CALIBRATION_TIMEOUT,
    DEFAULT_GENERIC_CALIBRATION_LIMIT,
    DEFAULT_IDLE_TEMP,
    CONF_AGGRESSIVE_OFFSET,
    CONF_AGGRESSIVE_RANGE,
    CONF_AGGRESSIVE_MODE_SELECTOR,
    AGGRESSIVE_MODE_OFF,
    AGGRESSIVE_MODES,
    CALIBRATION_MODE_OFF,
    CALIBRATION_MODES,
    CALIBRATION_STEP_SIZE_OPTIONS,
    AWAY_BEHAVIOR_OFFSET,
    AWAY_BEHAVIOR_ECO,
    AWAY_BEHAVIOR_OFF,
    AWAY_BEHAVIOR_IGNORE,
    CONF_DEBUG_MODE,
    CONF_SCHEDULING_MODE,
    SCHEDULING_MODE_HELPER,
    SCHEDULING_MODE_CALENDAR,
    CONF_CALENDAR,
    CONF_CALENDAR_EVENT,
    CONF_CALENDAR_ROOM,
    CONF_CALENDAR_SCAN_INTERVAL,
    DEFAULT_CALENDAR_SCAN_INTERVAL,
    CONF_HOLIDAY_CALENDAR,
    CONF_HOLIDAY_USE_DAY,
    CONF_VACATION_HOME_USE_DAY,
    MIN_CALENDAR_SCAN_INTERVAL,
    MAX_CALENDAR_SCAN_INTERVAL,
)


# ─── Helper: selectors ──────────────────────────────────────────────────────

def _entity_sel(domain: str | list[str] | None = None, multiple: bool = False, device_class: str | None = None):
    cfg: dict[str, Any] = {}
    if domain:
        cfg["domain"] = domain
    if device_class:
        cfg["device_class"] = device_class
    if multiple:
        cfg["multiple"] = True
    return selector.EntitySelector(selector.EntitySelectorConfig(**cfg))


def _number_sel(min_v: float = 0, max_v: float = 100, step: float = 0.5,
                mode: str = "box", unit: str = "°C"):
    return selector.NumberSelector(selector.NumberSelectorConfig(
        min=min_v, max=max_v, step=step, mode=selector.NumberSelectorMode(mode),
        unit_of_measurement=unit,
    ))


def _bool_sel():
    return selector.BooleanSelector()


def _text_sel(multiline: bool = False):
    if multiline:
        return selector.TemplateSelector()
    return selector.TextSelector()


def _duration_sel():
    return selector.DurationSelector()


def _time_sel():
    return selector.TimeSelector()


def _select_sel(options: list[str], translation_key: str | None = None,
                mode: selector.SelectSelectorMode = selector.SelectSelectorMode.LIST):
    return selector.SelectSelector(selector.SelectSelectorConfig(
        options=options,
        mode=mode,
        translation_key=translation_key,
    ))


def _clean_input(user_input: dict[str, Any] | None) -> dict[str, Any]:
    """Remove None values and empty strings for entity selectors. Flattens sections."""
    if user_input is None:
        return {}
    flat_input = {}
    for k, v in user_input.items():
        if isinstance(v, dict) and k.startswith("sec_"):
            for sub_k, sub_v in v.items():
                if sub_v is not None and (not isinstance(sub_v, str) or sub_v.strip() != ""):
                    flat_input[sub_k] = sub_v
        else:
            if v is not None and (not isinstance(v, str) or v.strip() != ""):
                flat_input[k] = v
    return flat_input



# ─── Constants for the menu ─────────────────────────────────────────────────

SETUP_EXPERT = "expert_hardware"


# ─── Common Mixin ───────────────────────────────────────────────────────────

class TempixCommonFlow:
    """Mixin for common step logic shared between ConfigFlow and OptionsFlow."""

    def _get_default(self, key: str, fallback: Any = None) -> Any:
        data = getattr(self, "data", getattr(self, "_options", {}))
        if hasattr(self, "_config_entry") and key not in data:
            entry_opts = getattr(self._config_entry, "options", {})
            entry_data = getattr(self._config_entry, "data", {})
            return entry_opts.get(key, entry_data.get(key, fallback))
        return data.get(key, fallback)

    def _away_behavior_fallback(self) -> str:
        """Vorbelegung, wenn `away_behavior` nicht gespeichert ist.

        Neuer Eintrag: `ignore` (Standard seit 1.6.5). Bestehender Eintrag ohne
        Schlüssel (angelegt vor 1.6.5): `offset` — so fährt ihn `config_model`
        (Bestandsschutz). Vorher stand hier `ignore`, und Speichern des Dialogs
        schrieb es still in die Optionen (LT-F2).
        """
        return AWAY_BEHAVIOR_OFFSET if hasattr(self, "_config_entry") else AWAY_BEHAVIOR_IGNORE

    def _get_list_default(self, key: str) -> list:
        val = self._get_default(key, [])
        if val is None or val == "" or val == "None" or val is vol.UNDEFINED:
            return []
        if isinstance(val, list):
            return val
        return [val]

    def _get_trv_hvac_modes(self) -> list[str]:
        """Return the intersection of HVAC modes supported by all configured TRVs.

        Falls back to ["heat", "cool", "auto", "off"] if TRVs are unavailable or
        the intersection is empty. Die Ermittlung liegt in ``hvac_modes.py`` —
        die Select-Entities fuer Comfort-/Eco-Modus nutzen dieselbe Funktion.
        """
        return common_schedule_hvac_modes(
            getattr(self, "hass", None), self._get_list_default(CONF_TRVS))

    def _get_entity_default(self, key: str) -> Any:
        val = self._get_default(key)
        if val in (None, "", "None"):
            return vol.UNDEFINED
        return val

    def _clearable_schema_key(self, key: str) -> vol.Optional:
        """Return vol.Optional WITHOUT a voluptuous default for clearable fields.

        Using description.suggested_value instead of default= means voluptuous will
        NOT substitute the old value when the field is cleared by the user.
        An absent key → field was cleared. A present key → field has a value.
        Works for any selector, not just entities — used for the entity fields
        below (e.g. outside_temp_sensor, weather_entity).

        party_temperature/vacation_temperature used this too for one day but
        were pulled back out of the options flow entirely: a slider/box selector here always shows a concrete number,
        which disagreed with the Number entity's own "unset" display for the
        same underlying None — two controls for one value that could visibly
        disagree. They're Number-entity-only again now.
        """
        val = self._get_default(key)
        if val in (None, "", "None"):
            return vol.Optional(key)
        return vol.Optional(key, description={"suggested_value": val})

    def _entity_schema_key(self, key: str) -> vol.Optional:
        """Alias of ``_clearable_schema_key`` — kept for the entity call sites below."""
        return self._clearable_schema_key(key)

    def _store_input(
        self,
        user_input: dict[str, Any],
        step_clearable: frozenset[str] = frozenset(),
    ) -> None:
        clean = _clean_input(user_input)
        store = self.data if hasattr(self, "data") else self._options
        store.update(clean)
        # Tombstone each clearable field owned by this step that is absent from the
        # submission.  The entity chip sends its current value when kept, but omits
        # the key entirely when the user clicks X (no voluptuous default → no
        # substitution).  An absent clearable key therefore means "explicitly cleared".
        for key in step_clearable:
            if key not in clean:
                store[key] = None

    async def _save_and_next(
        self,
        user_input: dict[str, Any],
        next_step: str,
        step_clearable: frozenset[str] = frozenset(),
    ) -> FlowResult:
        self._store_input(user_input, step_clearable)
        return await getattr(self, f"async_step_{next_step}")()



    # ── Expert Step 1: Hardware ──────────────────────────────────────────

    def _trv_overlap_errors(self, trvs: Any) -> dict[str, str]:
        """Fehler fuer Thermostate, die ein anderer Eintrag schon steuert.

        Standard: keine Pruefung. Nur der ConfigFlow ueberschreibt das — im
        Options-Flow duerfen bestehende Ueberschneidungen weiter gespeichert
        werden, sonst waeren deren Nutzer ausgesperrt.
        """
        return {}

    async def async_step_expert_hardware(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            if not user_input.get(CONF_TRVS):
                errors[CONF_TRVS] = "no_trvs"
            else:
                errors.update(self._trv_overlap_errors(user_input[CONF_TRVS]))
            hardware_clearable = frozenset({CONF_OUTSIDE_TEMP_SENSOR, CONF_WEATHER_ENTITY})
            if not errors:
                return await self._save_and_next(user_input, "expert_climate", hardware_clearable)
            # Fehler: Eingabe behalten, sonst kommt das Formular leer zurueck und der
            # Nutzer muss alles neu waehlen (gemessen: Auswahl nach
            # trv_in_use leer). Der abgelehnte Wert liegt nur im Flow-Speicher; ein
            # Eintrag entsteht erst, wenn dieser Schritt erneut validiert durchlaufen
            # wurde.
            self._store_input(user_input, hardware_clearable)

        return self.async_show_form(
            step_id="expert_hardware",
            data_schema=vol.Schema({
                # ── Thermostats ──
                vol.Required(CONF_TRVS, default=self._get_list_default(CONF_TRVS)): _entity_sel("climate", multiple=True),
                vol.Optional(CONF_TEMPERATURE_SENSOR, default=self._get_list_default(CONF_TEMPERATURE_SENSOR)): _entity_sel("sensor", device_class="temperature", multiple=True),

                # ── Config Section: Window / Door ──
                vol.Optional("sec_window"): section(vol.Schema({
                    vol.Optional(CONF_WINDOW_SENSORS, default=self._get_list_default(CONF_WINDOW_SENSORS)): _entity_sel("binary_sensor", multiple=True),
                    vol.Optional(CONF_WINDOW_REACTION_OPEN, default=self._get_default(CONF_WINDOW_REACTION_OPEN, DEFAULT_WINDOW_REACTION_OPEN)): _duration_sel(),
                    vol.Optional(CONF_WINDOW_REACTION_CLOSE, default=self._get_default(CONF_WINDOW_REACTION_CLOSE, DEFAULT_WINDOW_REACTION_CLOSE)): _duration_sel(),
                    # Max 30 (nicht 20): Kühlräume nutzen window_open_temp als
                    # Relax-OBERGRENZE bei offenem Fenster (z. B. 28 °C) — der
                    # alte 20er-Deckel sperrte genau diese Werte aus.
                    # Slider wie Comfort/Eco: Range sichtbar, 0 (= AUS) am Anschlag.
                    vol.Optional(CONF_WINDOW_OPEN_TEMP, default=self._get_default(CONF_WINDOW_OPEN_TEMP, DEFAULT_WINDOW_OPEN_TEMP)): _number_sel(0, 30, 0.5, mode="slider"),
                    # `window_legacy_restore` seit 1.7.0 nicht mehr im Dialog (wirkt nur im
                    # V1-Pfad); ein gespeicherter Wert bleibt erhalten, das Lesen bleibt, bis
                    # der V1-Pfad entfernt wird.
                }), {"collapsed": True}),
                
                # ── Config Section: Outside Sensor ──
                vol.Optional("sec_outside"): section(vol.Schema({
                    self._entity_schema_key(CONF_OUTSIDE_TEMP_SENSOR): _entity_sel("sensor"),
                    vol.Optional(CONF_OUTSIDE_TEMP_THRESHOLD, default=self._get_default(CONF_OUTSIDE_TEMP_THRESHOLD, DEFAULT_OUTSIDE_THRESHOLD)): _number_sel(0, 30, 0.5, mode="slider"),
                    vol.Optional(CONF_OUTSIDE_TEMP_HYSTERESIS, default=self._get_default(CONF_OUTSIDE_TEMP_HYSTERESIS, DEFAULT_OUTSIDE_HYSTERESIS)): _number_sel(0, 5, 0.5, mode="slider"),
                    vol.Optional(CONF_OUTSIDE_TEMP_FALLBACK, default=self._get_default(CONF_OUTSIDE_TEMP_FALLBACK, True)): _bool_sel(),
                    self._entity_schema_key(CONF_WEATHER_ENTITY): _entity_sel(["weather"]),
                }), {"collapsed": True}),
            }),
            errors=errors,
        )

    # ── Expert Step 2: Climate & Schedule ────────────────────────────────
    async def async_step_expert_climate(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            clean_input = _clean_input(user_input)
            # Kaputtes Adjustments-JSON wurde bisher still gespeichert und blieb
            # danach wirkungslos, ohne Logzeile (Live-Test).
            if not validate_adjustments(clean_input.get(CONF_ADJUSTMENTS)):
                errors["base"] = "invalid_adjustments"
                # Die uebrigen Eingaben dieses Schritts behalten, sonst springt das
                # ganze Formular zurueck. Das kaputte JSON selbst gehoert NICHT in
                # den Zwischenspeicher: Wuerde der Nutzer das Feld danach leeren,
                # fehlt der Key (`_clean_input` streicht Leerstrings) und der
                # Fehlertext bliebe unbemerkt gespeichert.
                store = self.data if hasattr(self, "data") else self._options
                store.update({k: v for k, v in clean_input.items() if k != CONF_ADJUSTMENTS})
            else:
                mode = clean_input.get(CONF_SCHEDULING_MODE, SCHEDULING_MODE_HELPER)
                next_step = "expert_climate_calendar" if mode == SCHEDULING_MODE_CALENDAR else "expert_climate_schedulers"
                return await self._save_and_next(user_input, next_step)

        trv_modes = self._get_trv_hvac_modes()

        def _resolved_mode(key: str) -> tuple[str, list[str]]:
            """Ein gespeicherter Wert wird nie stillschweigend
            umgeschrieben, nur weil die TRVs ihn gerade nicht als
            unterstuetzt melden (TRV kurz unavailable, Integrations-Update
            verengt hvac_modes). Nur ein wirklich unkonfiguriertes Feld faellt
            auf den TRV-eigenen Default zurueck."""
            stored = self._get_default(key)
            if stored is None:
                return trv_modes[0], trv_modes
            if stored not in trv_modes:
                return stored, [*trv_modes, stored]
            return stored, trv_modes

        _comfort_default, _comfort_modes = _resolved_mode(CONF_HVAC_MODE_COMFORT)
        _eco_default, _eco_modes = _resolved_mode(CONF_HVAC_MODE_ECO)

        return self.async_show_form(
            step_id="expert_climate",
            data_schema=vol.Schema({
                # ── Config Section: Temperatures ──
                vol.Optional("sec_temperatures"): section(vol.Schema({
                    vol.Optional(CONF_TEMPERATURE_COMFORT_STATIC, default=self._get_default(CONF_TEMPERATURE_COMFORT_STATIC, DEFAULT_COMFORT_TEMP)): _number_sel(5, 30, 0.5, mode="slider"),
                    vol.Optional(CONF_HVAC_MODE_COMFORT, default=_comfort_default): _select_sel(_comfort_modes, translation_key="hvac_mode", mode=selector.SelectSelectorMode.DROPDOWN),
                    vol.Optional(CONF_TEMPERATURE_ECO_STATIC, default=self._get_default(CONF_TEMPERATURE_ECO_STATIC, DEFAULT_ECO_TEMP)): _number_sel(5, 30, 0.5, mode="slider"),
                    vol.Optional(CONF_HVAC_MODE_ECO, default=_eco_default): _select_sel(_eco_modes, translation_key="hvac_mode", mode=selector.SelectSelectorMode.DROPDOWN),
                    # party_temperature/vacation_temperature bewusst NICHT hier:
                    # nur ueber die Number-Entities einstellbar (zurueckgebaut,
                    # war kurz im Options-Flow — zwei Bedienstellen fuer denselben
                    # Wert liefen auseinander).
                    # 0.1 statt 0 als Minimum — bei hysteresis=0 fallen
                    # Aus- und Wieder-An-Schwelle exakt zusammen, das Band
                    # kollabiert auf einen Punkt und der Regler oszilliert
                    # bei ref_temp == target garantiert jeden Zyklus.
                    vol.Optional(CONF_HYSTERESIS, default=self._get_default(CONF_HYSTERESIS, DEFAULT_HYSTERESIS)): _number_sel(0.1, 2, 0.1, mode="slider"),
                }), {"collapsed": True}),
                
                # ── Config Section: Scheduling ──
                vol.Optional("sec_scheduling"): section(vol.Schema({
                    vol.Required(CONF_SCHEDULING_MODE, default=self._get_default(CONF_SCHEDULING_MODE, SCHEDULING_MODE_HELPER)): _select_sel([SCHEDULING_MODE_HELPER, SCHEDULING_MODE_CALENDAR], translation_key="scheduling_mode"),
                }), {"collapsed": True}),

                # ── Config Section: Adjustments ── (moved here: belongs
                # next to scheduling_mode, not next to Away/Aggressive Mode — it's
                # a time/day-based override layer on top of whichever scheduler or
                # calendar is selected next.)
                vol.Optional("sec_adjustments"): section(vol.Schema({
                    vol.Optional(CONF_ADJUSTMENTS, default=self._get_default(CONF_ADJUSTMENTS, "[]")): _text_sel(multiline=True),
                }), {"collapsed": True}),

                # ── Config Section: Calibration ──
                vol.Optional("sec_calibration"): section(vol.Schema({
                    vol.Required(CONF_CALIBRATION_MODE, default=self._get_default(CONF_CALIBRATION_MODE, CALIBRATION_MODE_OFF)): _select_sel(CALIBRATION_MODES, translation_key="calibration_mode", mode=selector.SelectSelectorMode.DROPDOWN),
                    vol.Optional(CONF_GENERIC_CALIBRATION_LIMIT, default=self._get_default(CONF_GENERIC_CALIBRATION_LIMIT, DEFAULT_GENERIC_CALIBRATION_LIMIT)): _number_sel(0, 10, 0.1),
                    vol.Optional(CONF_CALIBRATION_DELTA, default=self._get_default(CONF_CALIBRATION_DELTA, DEFAULT_CALIBRATION_DELTA)): _number_sel(0, 2, 0.1, mode="slider"),
                    vol.Required(CONF_CALIBRATION_STEP_SIZE, default=self._get_default(CONF_CALIBRATION_STEP_SIZE, "auto")): _select_sel(CALIBRATION_STEP_SIZE_OPTIONS, translation_key="calibration_step_size", mode=selector.SelectSelectorMode.DROPDOWN),
                    vol.Optional(CONF_CALIBRATION_KEYWORD, default=self._get_default(CONF_CALIBRATION_KEYWORD, "calibration")): _text_sel(),
                    vol.Optional(CONF_CALIBRATION_TIMEOUT, default=self._get_default(CONF_CALIBRATION_TIMEOUT, DEFAULT_CALIBRATION_TIMEOUT)): _duration_sel(),
                }), {"collapsed": True}),
            }),
            errors=errors,
        )

    # ── Expert Step 2b: Calendar (conditional) ───────────────────────────

    async def async_step_expert_climate_calendar(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return await self._save_and_next(
                user_input, "expert_presence",
                frozenset({CONF_HOLIDAY_CALENDAR, CONF_VACATION_HOME_USE_DAY}),
            )

        return self.async_show_form(
            step_id="expert_climate_calendar",
            data_schema=vol.Schema({
                vol.Optional(CONF_CALENDAR, default=self._get_list_default(CONF_CALENDAR)): _entity_sel("calendar", multiple=True),
                vol.Optional(CONF_CALENDAR_EVENT, default=self._get_default(CONF_CALENDAR_EVENT, "")): _text_sel(),
                vol.Optional(CONF_CALENDAR_ROOM, default=self._get_default(CONF_CALENDAR_ROOM, "")): _text_sel(),
                vol.Optional(CONF_CALENDAR_SCAN_INTERVAL, default=self._get_default(CONF_CALENDAR_SCAN_INTERVAL, DEFAULT_CALENDAR_SCAN_INTERVAL)): _number_sel(MIN_CALENDAR_SCAN_INTERVAL, MAX_CALENDAR_SCAN_INTERVAL, 1, mode="slider", unit="min"),
                self._entity_schema_key(CONF_HOLIDAY_CALENDAR): _entity_sel("calendar"),
                vol.Optional(CONF_HOLIDAY_USE_DAY, default=self._get_default(CONF_HOLIDAY_USE_DAY, "sun")): _select_sel(["mon", "tue", "wed", "thu", "fri", "sat", "sun"], translation_key="holiday_use_day", mode=selector.SelectSelectorMode.DROPDOWN),
                self._entity_schema_key(CONF_VACATION_HOME_USE_DAY): _select_sel(["mon", "tue", "wed", "thu", "fri", "sat", "sun"], translation_key="vacation_home_use_day", mode=selector.SelectSelectorMode.DROPDOWN),
            }),
        )

    # ── Expert Step 2c: Schedulers (conditional) ───────────────────────────

    async def async_step_expert_climate_schedulers(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return await self._save_and_next(
                user_input, "expert_presence",
                frozenset({CONF_SCHEDULER_SELECTOR, CONF_HOLIDAY_CALENDAR, CONF_VACATION_HOME_USE_DAY}),
            )

        return self.async_show_form(
            step_id="expert_climate_schedulers",
            data_schema=vol.Schema({
                vol.Optional(CONF_SCHEDULERS, default=self._get_list_default(CONF_SCHEDULERS)): _entity_sel(["schedule", "input_boolean", "binary_sensor"], multiple=True),
                self._entity_schema_key(CONF_SCHEDULER_SELECTOR): _entity_sel(),
                self._entity_schema_key(CONF_HOLIDAY_CALENDAR): _entity_sel("calendar"),
                vol.Optional(CONF_HOLIDAY_USE_DAY, default=self._get_default(CONF_HOLIDAY_USE_DAY, "sun")): _select_sel(["mon", "tue", "wed", "thu", "fri", "sat", "sun"], translation_key="holiday_use_day", mode=selector.SelectSelectorMode.DROPDOWN),
                self._entity_schema_key(CONF_VACATION_HOME_USE_DAY): _select_sel(["mon", "tue", "wed", "thu", "fri", "sat", "sun"], translation_key="vacation_home_use_day", mode=selector.SelectSelectorMode.DROPDOWN),
            }),
        )

    # ── Expert Step 3: Presence & People ─────────────────────────────────

    async def async_step_expert_presence(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return await self._save_and_next(
                user_input, "expert_behavior",
                frozenset({CONF_PROXIMITY_ENTITY, CONF_PRESENCE_SENSOR, CONF_SCHEDULER_PRESENCE}),
            )

        return self.async_show_form(
            step_id="expert_presence",
            data_schema=vol.Schema({
                # ── Config Section: Persons ──
                vol.Optional("sec_persons"): section(vol.Schema({
                    vol.Optional(CONF_PERSONS, default=self._get_list_default(CONF_PERSONS)): _entity_sel("person", multiple=True),
                    vol.Optional(CONF_GUEST_MODE, default=self._get_list_default(CONF_GUEST_MODE)): _entity_sel(["input_boolean", "binary_sensor", "device_tracker", "person"], multiple=True),
                    vol.Optional(CONF_PEOPLE_ENTERING_DURATION, default=self._get_default(CONF_PEOPLE_ENTERING_DURATION, DEFAULT_PEOPLE_ENTERING)): _duration_sel(),
                    vol.Optional(CONF_PEOPLE_LEAVING_DURATION, default=self._get_default(CONF_PEOPLE_LEAVING_DURATION, DEFAULT_PEOPLE_LEAVING)): _duration_sel(),
                    vol.Optional(CONF_PERSONS_FORCE_COMFORT, default=self._get_default(CONF_PERSONS_FORCE_COMFORT, False)): _bool_sel(),
                    vol.Optional(CONF_PERSONS_FORCE_COMFORT_START, default=self._get_default(CONF_PERSONS_FORCE_COMFORT_START, "07:00:00")): _time_sel(),
                    vol.Optional(CONF_PERSONS_FORCE_COMFORT_END, default=self._get_default(CONF_PERSONS_FORCE_COMFORT_END, "22:00:00")): _time_sel(),
                }), {"collapsed": True}),
                
                # ── Config Section: Proximity ──
                vol.Optional("sec_proximity"): section(vol.Schema({
                    self._entity_schema_key(CONF_PROXIMITY_ENTITY): selector.DeviceSelector(
                        selector.DeviceSelectorConfig(
                            integration="proximity",
                        )
                    ),
                    vol.Optional(CONF_PROXIMITY_DISTANCE, default=self._get_default(CONF_PROXIMITY_DISTANCE, DEFAULT_PROXIMITY_DISTANCE)): _number_sel(0, 5000, 10, unit="m"),
                    vol.Optional(CONF_PROXIMITY_DURATION, default=self._get_default(CONF_PROXIMITY_DURATION, DEFAULT_PROXIMITY_DURATION)): _duration_sel(),
                }), {"collapsed": True}),
                
                # ── Config Section: Presence Sensor ──
                vol.Optional("sec_presence_sensor"): section(vol.Schema({
                    self._entity_schema_key(CONF_PRESENCE_SENSOR): _entity_sel("binary_sensor"),
                    self._entity_schema_key(CONF_SCHEDULER_PRESENCE): _entity_sel("schedule"),
                    vol.Optional(CONF_PRESENCE_REACTION_ON, default=self._get_default(CONF_PRESENCE_REACTION_ON, DEFAULT_PRESENCE_REACTION_ON)): _duration_sel(),
                    vol.Optional(CONF_PRESENCE_REACTION_OFF, default=self._get_default(CONF_PRESENCE_REACTION_OFF, DEFAULT_PRESENCE_REACTION_OFF)): _duration_sel(),
                }), {"collapsed": True}),
            }),
        )

    # ── Expert Step 4: Automation & Behavior ─────────────────────────────

    async def async_step_expert_behavior(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return await self._save_and_next(
                user_input, "expert_advanced",
                frozenset({CONF_SEASON_MODE_ENTITY}),
            )

        return self.async_show_form(
            step_id="expert_behavior",
            data_schema=vol.Schema({
                # ── Config Section: Season Mode ──
                vol.Optional("sec_season_mode"): section(vol.Schema({
                    self._entity_schema_key(CONF_SEASON_MODE_ENTITY): _entity_sel(["switch", "input_boolean"]),
                    vol.Optional(CONF_IDLE_TEMPERATURE, default=self._get_default(CONF_IDLE_TEMPERATURE, DEFAULT_IDLE_TEMP)): _number_sel(0, 30, 0.5, mode="slider"),
                    vol.Optional(CONF_ROOM_TEMP_THRESHOLD_ENABLED, default=self._get_default(CONF_ROOM_TEMP_THRESHOLD_ENABLED, False)): _bool_sel(),
                    vol.Optional(CONF_ROOM_TEMP_THRESHOLD, default=self._get_default(CONF_ROOM_TEMP_THRESHOLD, DEFAULT_ROOM_THRESHOLD)): _number_sel(0, 30, 0.5, mode="slider"),
                }), {"collapsed": True}),

                # ── Config Section: Temperature Tweaks ──
                vol.Optional("sec_temperature_tweaks"): section(vol.Schema({
                    vol.Optional(CONF_MIN_INSTEAD_OF_OFF, default=self._get_default(CONF_MIN_INSTEAD_OF_OFF, False)): _bool_sel(),
                    vol.Optional(CONF_ENABLE_TEMPORARY_MANUAL_OVERRIDE, default=self._get_default(CONF_ENABLE_TEMPORARY_MANUAL_OVERRIDE, True)): _bool_sel(),
                    vol.Optional(CONF_OFF_IF_ABOVE_ROOM_TEMP, default=self._get_default(CONF_OFF_IF_ABOVE_ROOM_TEMP, False)): _bool_sel(),
                }), {"collapsed": True}),
                
                # ── Config Section: Away Mode ──
                vol.Optional("sec_away_mode"): section(vol.Schema({
                    vol.Optional(CONF_AWAY_BEHAVIOR, default=self._get_default(CONF_AWAY_BEHAVIOR, self._away_behavior_fallback())): _select_sel([AWAY_BEHAVIOR_OFFSET, AWAY_BEHAVIOR_ECO, AWAY_BEHAVIOR_OFF, AWAY_BEHAVIOR_IGNORE], translation_key="away_behavior", mode=selector.SelectSelectorMode.DROPDOWN),
                    vol.Optional(CONF_AWAY_OFFSET, default=self._get_default(CONF_AWAY_OFFSET, DEFAULT_AWAY_OFFSET)): _number_sel(0, 10, 0.5, mode="slider"),
                    vol.Optional(CONF_AWAY_PRESENCE_MODE, default=self._get_default(CONF_AWAY_PRESENCE_MODE, False)): _bool_sel(),
                    vol.Optional(CONF_AWAY_IGNORE_PEOPLE, default=self._get_default(CONF_AWAY_IGNORE_PEOPLE, False)): _bool_sel(),
                }), {"collapsed": True}),
                
                # ── Config Section: Aggressive Mode ──
                vol.Optional("sec_aggressive_mode"): section(vol.Schema({
                    vol.Required(CONF_AGGRESSIVE_MODE_SELECTOR, default=self._get_default(CONF_AGGRESSIVE_MODE_SELECTOR, AGGRESSIVE_MODE_OFF)): _select_sel(AGGRESSIVE_MODES, translation_key="aggressive_mode", mode=selector.SelectSelectorMode.DROPDOWN),
                    vol.Optional(CONF_AGGRESSIVE_RANGE, default=self._get_default(CONF_AGGRESSIVE_RANGE, DEFAULT_AGGRESSIVE_RANGE)): _number_sel(0, 5, 0.1),
                    vol.Optional(CONF_AGGRESSIVE_OFFSET, default=self._get_default(CONF_AGGRESSIVE_OFFSET, DEFAULT_AGGRESSIVE_OFFSET)): _number_sel(0, 10, 0.1),
                }), {"collapsed": True}),
            }),
        )

    # ── Expert Step 5: Advanced / Valves / Protection ────────────────────

    async def async_step_expert_advanced(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return await self._save_and_finish(user_input)

        return self.async_show_form(
            step_id="expert_advanced",
            data_schema=vol.Schema({
                # ── Config Section: Valve Positioning ──
                vol.Optional("sec_valve_positioning"): section(vol.Schema({
                    # Dropdown, not a toggle: the gates compare against the
                    # STRING "off", so a stored bool silently ENABLED valve
                    # positioning (False != "off"). parse_valve_mode migrates
                    # legacy bools; the four modes match the original blueprint.
                    vol.Optional(CONF_VALVE_MODE, default=parse_valve_mode(self._get_default(CONF_VALVE_MODE, "off"))): _select_sel(list(VALVE_MODES), translation_key="valve_mode", mode=selector.SelectSelectorMode.DROPDOWN),
                    vol.Optional(CONF_VALVE_DIFF, default=self._get_default(CONF_VALVE_DIFF, DEFAULT_VALVE_DIFF)): _number_sel(0, 10, 0.1),
                    vol.Optional(CONF_VALVE_STEP, default=self._get_default(CONF_VALVE_STEP, DEFAULT_VALVE_STEP)): _number_sel(1, 50, 1, unit="%"),
                    vol.Optional(CONF_VALVE_MIN, default=self._get_default(CONF_VALVE_MIN, DEFAULT_VALVE_MIN)): _number_sel(0, 100, 1, unit="%"),
                    vol.Optional(CONF_VALVE_MAX, default=self._get_default(CONF_VALVE_MAX, DEFAULT_VALVE_MAX)): _number_sel(0, 100, 1, unit="%"),
                    vol.Optional(CONF_VALVE_KEYWORD, default=self._get_default(CONF_VALVE_KEYWORD, "valve")): _text_sel(),
                }), {"collapsed": True}),
                
                # ── Config Section: Protection & Liming ──
                vol.Optional("sec_protection"): section(vol.Schema({
                    vol.Optional(CONF_FROST_PROTECTION_ENABLED, default=self._get_default(CONF_FROST_PROTECTION_ENABLED, False)): _bool_sel(),
                    vol.Optional(CONF_FROST_PROTECTION_TEMP, default=self._get_default(CONF_FROST_PROTECTION_TEMP, DEFAULT_FROST_TEMP)): _number_sel(0, 15, 0.5, mode="slider"),
                    vol.Optional(CONF_LIMING_PROTECTION, default=self._get_default(CONF_LIMING_PROTECTION, False)): _bool_sel(),
                    vol.Optional(CONF_LIMING_DAY, default=self._get_default(CONF_LIMING_DAY, "mon")): _select_sel(["mon", "tue", "wed", "thu", "fri", "sat", "sun"], translation_key="liming_day", mode=selector.SelectSelectorMode.DROPDOWN),
                    vol.Optional(CONF_LIMING_TIME, default=self._get_default(CONF_LIMING_TIME, "12:00:00")): _time_sel(),
                    vol.Optional(CONF_LIMING_DURATION, default=self._get_default(CONF_LIMING_DURATION, DEFAULT_LIMING_DURATION)): _number_sel(1, 60, 1, unit="min"),
                    vol.Optional(CONF_LIMING_IN_SEASON, default=self._get_default(CONF_LIMING_IN_SEASON, False)): _bool_sel(),
                }), {"collapsed": True}),
                
                # ── Config Section: Timing ──
                vol.Optional("sec_timing"): section(vol.Schema({
                    vol.Optional(CONF_ACTION_DELAY, default=self._get_default(CONF_ACTION_DELAY, DEFAULT_ACTION_DELAY)): _duration_sel(),
                    vol.Optional(CONF_SENSOR_RETENTION, default=self._get_default(CONF_SENSOR_RETENTION, DEFAULT_SENSOR_RETENTION)): _duration_sel(),
                }), {"collapsed": True}),
            }),
        )

    def _merged_for_save(self, store: dict[str, Any]) -> dict[str, Any]:
        """Was am Ende des Flows als Config geschrieben wird. Neuanlegen: der Flow-Speicher."""
        return store

    async def _save_and_finish(self, user_input: dict[str, Any]) -> FlowResult:
        clean = _clean_input(user_input)
        store = self.data if hasattr(self, "data") else self._options
        store.update(clean)
        final = self._merged_for_save(store)
        title = final.get(CONF_NAME, "")
        return self.async_create_entry(title=title, data=final)


# ─── ConfigFlow ──────────────────────────────────────────────────────────────

class ConfigFlow(TempixCommonFlow, config_entries.ConfigFlow, domain=DOMAIN):
    """Multi-step config flow: name, then the shared expert steps."""

    VERSION = 1

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    # Step 1: Name
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            self.data.update(_clean_input(user_input))
            return await self.async_step_expert_hardware()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_NAME, default=DEFAULT_NAME): _text_sel(),
            }),
        )


    def _trv_overlap_errors(self, trvs: Any) -> dict[str, str]:
        """Neuanlegen: ein Thermostat gehoert zu genau einem Tempix-Eintrag.

        Zwei Eintraege auf demselben Geraet ueberschreiben sich gegenseitig
        (der zuletzt sendende gewinnt, der andere sieht einen physischen
        Eingriff). ``options`` ueberschreiben ``data``, wie in der Laufzeit-Config.
        """
        wanted = set(trvs if isinstance(trvs, list) else [trvs])
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            used = {**entry.data, **entry.options}.get(CONF_TRVS) or []
            if wanted & set(used if isinstance(used, list) else [used]):
                return {CONF_TRVS: "trv_in_use"}
        return {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> OptionsFlowHandler:
        return OptionsFlowHandler(config_entry)


# ─── OptionsFlow ─────────────────────────────────────────────────────────────

class OptionsFlowHandler(TempixCommonFlow, config_entries.OptionsFlow):
    """Options flow – always Expert mode for reconfiguration."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        super().__init__()
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if not hasattr(self, "_options"):
            self._options = dict(self._config_entry.data)
            self._options.update(self._config_entry.options)
            # Stand beim Oeffnen: Bezugspunkt, um beim Speichern zu erkennen, was
            # der Nutzer im Dialog tatsaechlich geaendert hat.
            self._baseline = copy.deepcopy(self._options)
        return await self.async_step_expert_hardware()

    def _merged_for_save(self, store: dict[str, Any]) -> dict[str, Any]:
        """Dialog-Aenderungen auf den AKTUELLEN Stand legen, nicht auf den vom Oeffnen.

        Schalter, Timer, Number-/Select-Entities, Services und der Lerner
        schreiben waehrend der Dialog offen ist in dieselben Options. Frueher
        ging der komplette Stand von vor dem Oeffnen zurueck: ein
        eingeschaltetes Force-Comfort war wieder aus, ein inzwischen
        abgelaufenes Party war wieder an — ohne Timer, also dauerhaft.

        Ein Feld gilt als im Dialog geaendert, wenn sein Wert vom Stand beim
        Oeffnen abweicht (ein geleertes Feld ist ein Tombstone ``None`` und
        zaehlt damit mit). Nur diese Felder gewinnen; alles andere bleibt, wie
        es beim Speichern in der Entry steht — auch Schluessel, die der Dialog
        gar nicht kennt. Aendern beide Seiten dasselbe Feld, gewinnt der Dialog.
        """
        baseline = getattr(self, "_baseline", None)
        if baseline is None:
            return store
        merged = {**self._config_entry.data, **self._config_entry.options}
        for key, value in store.items():
            if baseline.get(key) != value:
                merged[key] = value
        return merged

