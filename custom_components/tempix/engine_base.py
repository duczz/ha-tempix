"""
Tempix – Engine Base Mixin.

Provides foundational helpers: state access, parsing, rounding, logging,
and the shared ``__init__`` for the composed engine class.
"""
from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timedelta, timezone, UTC
from typing import TYPE_CHECKING, Any, Callable

from homeassistant.core import HomeAssistant

if TYPE_CHECKING:
    from custom_components.tempix.config_model import TempixConfig
from homeassistant.util import dt as dt_util
from homeassistant.helpers import entity_registry as er_helper
from homeassistant.const import (
    STATE_ON,
    STATE_HOME,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)

from custom_components.tempix.const import (
    INVALID_STATES,
    DEFAULT_ROOM_TEMP_FALLBACK,
    SCHEDULING_MODE_CALENDAR,
)

_LOGGER = logging.getLogger(__name__)


class EngineBaseMixin:
    """Foundational mixin – state access, parsing, rounding, logging."""

    # ── initialisation ───────────────────────────────────────────────────────

    def __init__(self, hass: HomeAssistant, config: TempixConfig, on_dirty: Callable[[], None] | None = None) -> None:
        self.hass: HomeAssistant = hass
        self.config: TempixConfig = config
        self._on_dirty: Callable[[], None] | None = on_dirty
        self._state_snapshot: dict[str, Any] = {}
        self._startup_time: datetime | None = None
        self._smart_preconditioning_active: bool = False
        self._calibration_entity_map: dict[str, str | None] = {}
        self._calendar_events: dict[str, list[dict[str, Any]]] = {}
        self._schedule_slots: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self._last_home_status: bool | None = None  # memory for current session
        self._last_outside_ok: bool | None = None  # hysteresis state for outside threshold
        self._guest_warned: set[str] = set()  # warn once per unavailable guest entity
        # Wie `_guest_warned`: einmal warnen, bei Erholung zuruecksetzen. Eine
        # kaputte `liming_time` bleibt bis zur Korrektur kaputt — eine nackte
        # Warnung wuerde jeden Zyklus feuern.
        self._liming_time_warned: bool = False
        self._reboot_logged: set[str] = set()  # log reboot/initial-state detection only once per entity per boot
        self._calendar_warned: set[str] = set()  # warn once per failed calendar lookup (use_day, use_scheduler, holiday_use_day)
        # Last VALID state per window sensor from the PREVIOUS cycle. Feeds the
        # reboot-safe close-delay in is_window_open: a sensor that flapped
        # `off → unavailable → off` (router/sensor reboot) must NOT count as
        # recently-closed = open. Updated once per cycle in set_state_snapshot.
        self._window_prev_valid: dict[str, str] = {}
        # sid → `last_changed` des letzten ECHTEN offen→zu. Haelt den
        # Close-Delay ueber den ersten Zyklus hinaus, ohne den Flap-Schutz
        # aufzuweichen (Live-Test 2026-11-04, F5; Gegenstueck in V2:
        # `snapshot_builder._resolve_window`, `close_marks`).
        self._window_close_marks: dict[str, datetime] = {}
        # Sensoren mit bestaetigter Oeffnungsphase (LT-F12-Rest d): nur deren
        # Schliessen setzt eine Marke. Gegenstueck V2: `_resolve_window`,
        # `open_phase_confirmed`.
        self._window_open_phase_confirmed: set[str] = set()
        # Bestaetigte Sensoren, deren Marke am naechsten Zyklusanfang faellt.
        self._window_pop_pending: set[str] = set()

    # ── injection ────────────────────────────────────────────────────────────

    def set_startup_time(self, dt: datetime) -> None:
        """Set the startup time (called by coordinator after setup)."""
        self._startup_time = dt

    def set_calendar_events(self, events: dict[str, list[dict[str, Any]]]) -> None:
        """Inject fetched calendar events for deep scanning."""
        self._calendar_events = events

    def set_schedule_slots(self, slots: dict[str, dict[str, list[dict[str, Any]]]]) -> None:
        """Inject fetched schedule slots for start/end time display."""
        self._schedule_slots = slots

    def set_state_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Inject a snapshot of states to be used for this calculation cycle."""
        # Before overwriting, remember the previous cycle's VALID window states
        # for the reboot-safe close-delay. An 'unavailable'/'unknown' reading is
        # skipped so a flap can't erase the last known valid state.
        for sid in self._window_pop_pending:
            self._window_close_marks.pop(sid, None)
        self._window_pop_pending.clear()
        for sid in self.config.window_sensors:
            prev = self._state_snapshot.get(sid)
            if prev is not None and prev.state not in INVALID_STATES:
                self._window_prev_valid[sid] = prev.state
        self._state_snapshot = snapshot

    def set_on_dirty(self, callback: Callable[[], None]) -> None:
        """Inject a callback to be notified when internal state changes."""
        self._on_dirty = callback

    def restore_outside_state(self, is_outside_ok: bool | None) -> None:
        """Restore hysteresis state after reboot."""
        self._last_outside_ok = is_outside_ok

    @property
    def outside_ok(self) -> bool | None:
        """Return the current hysteresis state."""
        return self._last_outside_ok

    # ── logging ──────────────────────────────────────────────────────────────

    def debug_log(self, msg: str) -> None:
        """Log debug message with engine prefix."""
        if self.config.debug_mode:
            _LOGGER.info("TPX Engine [%s]: %s", self.config.name, msg)
        else:
            _LOGGER.debug("TPX Engine [%s]: %s", self.config.name, msg)

    # ── state access ─────────────────────────────────────────────────────────

    def _get_state(self, entity_id: str | list | dict | None) -> Any:
        """Return the HA state object for an entity, preferring snapshot."""
        if not entity_id:
            return None
        if isinstance(entity_id, list):
            entity_id = entity_id[0] if entity_id else None
        if isinstance(entity_id, dict):
            entity_id = entity_id.get("entity_id")
        if not isinstance(entity_id, str):
            return None

        if entity_id in self._state_snapshot:
            return self._state_snapshot[entity_id]

        return self.hass.states.get(entity_id)

    def _is_state_valid(self, entity_id: str | None) -> bool:
        """Check if an entity state is valid (not unknown/unavailable)."""
        state = self._get_state(entity_id)
        return state is not None and state.state not in INVALID_STATES

    def _entity_is_missing(self, entity_id: str | None) -> bool:
        """Ist die Referenz TOT — weder in der State Machine noch in der Registry?

        Abgrenzung zu `unavailable`/`unknown`: dort EXISTIERT die Entity und ist
        nur gerade nicht auswertbar. Das ist eine Laufzeitstoerung, die sich von
        selbst aufloesen kann, und rechtfertigt konservatives Verhalten. Eine
        tote Referenz loest sich nie auf — konservatives Verhalten wird dort zum
        Dauerzustand (Beispiel: ein geloeschter Kalender legte in fuenf von
        sechs Raeumen die Abwesenheitserkennung still lahm).

        **Evidenz, ausdruecklich benannt — keine Zeitheuristik.** `missing` gilt
        genau dann, wenn KEIN State-Objekt UND KEIN Registry-Eintrag existiert.
        Beim HA-Start haben noch nicht geladene Entities ihren Registry-Eintrag
        bereits, der Start faellt also nicht faelschlich hierunter.

        **Restrisiko, bewusst in Kauf genommen:** Entities ohne `unique_id`
        (reine YAML-Helfer) stehen nie in der Registry. Fuer die kann `missing`
        waehrend des Starts kurz zutreffen, obwohl sie spaeter auftauchen.

        Ist die Registry nicht erreichbar, wird NICHT auf `missing` erkannt:
        ohne Beleg keine Behauptung — das entspricht dem Verhalten von vorher.
        """
        if not isinstance(entity_id, str) or not entity_id:
            return False
        if self._get_state(entity_id) is not None:
            return False
        try:
            registry = er_helper.async_get(self.hass)
        except (KeyError, AttributeError, TypeError):
            return False
        return registry.async_get(entity_id) is None

    def get_missing_entity_references(self) -> list[str]:
        """Alle konfigurierten Referenzen, die es nicht (mehr) gibt.

        Bewusst getrennt von `get_uncertainty_reasons()`: die Liste dort behaelt
        ihre Bedeutung („etwas ist gerade nicht auswertbar") fuer Diagnose und
        Statustexte. Hier steht die Teilmenge, die eine KONFIGURATIONSSTOERUNG
        ist und deshalb keinen konservativen Laufzeit-Pfad rechtfertigt.
        """
        kandidaten: list[str] = [
            *self.config.persons,
            *self.config.guest_mode,
            *self.config.schedulers,
            *(self.config.calendar or ()),
            *self.config.window_sensors,
        ]
        presence = self.config.presence_sensor
        if isinstance(presence, list):
            kandidaten.extend(presence)
        elif presence:
            kandidaten.append(presence)
        if self.config.outside_temp_sensor:
            kandidaten.append(self.config.outside_temp_sensor)
        # Einzel-Referenzen, deren Wegfall ebenfalls still wirkt (Live-Test
        # 2026-11-04, F11): ein geloeschtes Wetter nimmt den Sonnenschein-Offset
        # weg, eine geloeschte Saison-Entity laesst die Saison als AN gelten.
        # Gleiche Liste in `domain_resolvers.resolve_missing_entity_references`.
        for eid in (
            self.config.weather_entity,
            self.config.season_mode_entity,
            self.config.holiday_calendar,
            self.config.scheduler_selector,
            # `proximity_entity` bewusst NICHT: das Feld haelt eine DEVICE-ID
            # (DeviceSelector), `_entity_is_missing` prueft Entity-IDs und
            # meldete jede konfigurierte Proximity als tot.
        ):
            if eid:
                kandidaten.append(eid)

        gesehen: set[str] = set()
        tot: list[str] = []
        for eid in kandidaten:
            if not isinstance(eid, str) or eid in gesehen:
                continue
            gesehen.add(eid)
            if self._entity_is_missing(eid):
                tot.append(eid)
        return tot

    def _state_value(self, entity_id: str | None) -> str | None:
        """Return the string state or ``None``."""
        s = self._get_state(entity_id)
        if s is None or s.state in INVALID_STATES:
            return None
        return s.state

    def _float_state(self, entity_id: str | None, default: float | None = None) -> float | None:
        """Return the float state or *default*."""
        v = self._state_value(entity_id)
        if v is None:
            return default
        try:
            return float(v)
        except (ValueError, TypeError):
            return default

    def _temp_state(self, entity_id: str | None, default: float | None = None) -> float | None:
        """Return temperature from state or attributes (weather/climate support).

        ``float()`` parses "nan"/"inf" without raising — reject
        both, same reasoning as ``snapshot_builder.parse_float``.
        """
        state_obj = self._get_state(entity_id)
        if not state_obj or state_obj.state in INVALID_STATES:
            return default

        try:
            value = float(state_obj.state)
            if math.isfinite(value):
                return value
        except (ValueError, TypeError):
            pass

        for attr in ("temperature", "current_temperature"):
            val = state_obj.attributes.get(attr)
            if val is not None:
                try:
                    value = float(val)
                    if math.isfinite(value):
                        return value
                    continue
                except (ValueError, TypeError):
                    continue

        return default

    # ── room temperature ─────────────────────────────────────────────────────

    def _resolve_room_temp(self) -> float | None:
        """Resolve room temperature with optional sensor fusion (median filter)."""
        sensor_id = self.config.temp_sensor
        if not sensor_id:
            return None

        sensors: list[str] = []
        if isinstance(sensor_id, list):
            sensors = [str(s).strip() for item in sensor_id for s in ([item] if not isinstance(item, list) else item)]
        elif isinstance(sensor_id, str):
            sensors = [s.strip() for s in sensor_id.split(",")]
        else:
            sensors = [str(sensor_id)]

        if not sensors:
            return None

        if len(sensors) == 1:
            return self._temp_state(sensors[0])

        vals: list[float] = []
        for s in sensors:
            v = self._temp_state(s)
            if v is not None:
                vals.append(v)

        if not vals:
            return None
        if len(vals) == 1:
            return vals[0]

        vals.sort()
        median = vals[len(vals) // 2]
        max_dev = 3.0
        filtered = [v for v in vals if abs(v - median) <= max_dev]

        return sum(filtered) / len(filtered) if filtered else median

    def _resolve_outside_temp(self) -> float | None:
        """Return outside temperature reading, or ``None``."""
        sensor_id = self.config.outside_temp_sensor
        if not sensor_id:
            return None
        return self._temp_state(sensor_id)

    # ── rounding ─────────────────────────────────────────────────────────────

    def _round_half_up(self, value: float, precision: int = 0) -> float:
        """Round half up (away from zero), matching Jinja2 behaviour."""
        multiplier = 10 ** precision
        val = value * multiplier
        val = int(val + 0.5) if val >= 0 else int(val - 0.5)
        return val / multiplier

    def _round_to_step(self, value: float, step: float) -> float:
        """Round *value* to the nearest hardware *step* size."""
        if step <= 0:
            return value
        return self._round_half_up(value / step) * step

    # ── parsing ──────────────────────────────────────────────────────────────

    def _ensure_utc(self, dt: datetime | None) -> datetime | None:
        """Make a naive datetime aware by assuming UTC; aware values pass through unchanged."""
        if dt is None:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

    def _parse_dt(self, val: Any) -> datetime | None:
        """Robust datetime parsing for various formats (HA, Google, ISO)."""
        if isinstance(val, datetime):
            return self._ensure_utc(val)

        if not isinstance(val, str) or not val.strip():
            return None

        try:
            s = val.strip().replace(" ", "T").replace("z", "+00:00").replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)

            if dt.tzinfo is None:
                tz = dt_util.get_time_zone(self.hass.config.time_zone)
                return dt.replace(tzinfo=tz)
            return dt
        except (ValueError, TypeError):
            return None

    def _parse_duration(self, value: Any, default_seconds: int = 0) -> timedelta:
        """Parse duration from config (dict, int, str) to ``timedelta``."""
        if value is None:
            return timedelta(seconds=default_seconds)
        if isinstance(value, timedelta):
            return value
        if isinstance(value, dict):
            try:
                return timedelta(**value)
            except TypeError:
                _LOGGER.warning(
                    "%s: Invalid duration dict %s, using default.",
                    self.config.name, value,
                )
                return timedelta(seconds=default_seconds)
        if isinstance(value, (int, float)):
            return timedelta(seconds=int(value))
        return timedelta(seconds=default_seconds)

    # ── properties ───────────────────────────────────────────────────────────

    @property
    def _factor(self) -> int:
        """Cooling = −1, Heating = 1. Blueprint: factor variable."""
        return -1 if self.config.hvac_mode_eco == "cool" or self.config.hvac_mode_comfort == "cool" else 1

    @property
    def is_cooling(self) -> bool:
        """True when the TRV operates in cooling mode."""
        return self._factor == -1

    @property
    def is_heating(self) -> bool:
        """True when the TRV operates in heating mode."""
        return self._factor == 1

    # ── uncertainty ──────────────────────────────────────────────────────────

    def get_uncertainty_reasons(self) -> list[str]:
        """Identify all entities currently causing uncertainty (unknown/unavailable)."""
        reasons: list[str | None] = []

        for sid in self.config.window_sensors:
            if not self._is_state_valid(sid):
                reasons.append(sid)

        room_sensors = self.config.temp_sensor
        if isinstance(room_sensors, list):
            for rs in room_sensors:
                if rs and not self._is_state_valid(rs):
                    reasons.append(rs)
        elif room_sensors and not self._is_state_valid(room_sensors):
            reasons.append(room_sensors)

        if self.config.outside_temp_sensor:
            if not self._is_state_valid(self.config.outside_temp_sensor):
                reasons.append(self.config.outside_temp_sensor)

        presence = self.config.presence_sensor
        if isinstance(presence, list):
            for sid in presence:
                if not self._is_state_valid(sid):
                    reasons.append(sid)
        elif presence and not self._is_state_valid(presence):
            reasons.append(presence)

        for pid in self.config.persons:
            if not self._is_state_valid(pid):
                reasons.append(pid)

        for gid in self.config.guest_mode:
            if not self._is_state_valid(gid):
                reasons.append(gid)

        # Proximity ist der einzige Praesenz-Eingang, der kein
        # `entity_id` ist — `proximity_entity` haelt eine DEVICE-ID, deren
        # Entities pro Zyklus ueber die Registry aufgeloest werden. Sie kann
        # deshalb weder im Snapshot noch in `_is_state_valid` stehen und fehlte
        # hier, obwohl sie `is_away()` und damit den Zeitplan-Fingerprint speist.
        # Ergebnis war eine Unsicherheit, die nirgends auftauchte — auch nicht
        # in der Diagnose. Die Pruefung liegt in der Presence-
        # Mixin (`proximity_states`), weil nur die das Device aufloest.
        if self.config.proximity_entity and self.proximity_states() is None:
            reasons.append(f"proximity:{self.config.proximity_entity}")

        for sid in self.config.schedulers:
            if not self._is_state_valid(sid):
                reasons.append(sid)

        if self.config.scheduling_mode == SCHEDULING_MODE_CALENDAR:
            for cid in self.config.calendar:
                if not self._is_state_valid(cid):
                    reasons.append(cid)

        return [r for r in reasons if r is not None]

    # ── minimal config ───────────────────────────────────────────────────────

    def is_minimal_config(self) -> bool:
        """Return ``True`` if no scheduler, persons, guests, presence/proximity or party switch configured."""
        return (
            len(self.config.persons) == 0
            and not self.config.guest_mode
            and len(self.config.schedulers) == 0
            and not self.config.presence_sensor
            and not self.config.proximity_entity
            and not self.config.party_mode_switch
        )
