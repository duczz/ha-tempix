"""
Tempix – Engine Base Mixin.

Provides foundational helpers: state access, parsing, rounding, logging,
and the shared ``__init__`` for the composed engine class.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone, UTC
from typing import TYPE_CHECKING, Any

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

    def __init__(self, hass: HomeAssistant, config: TempixConfig) -> None:
        self.hass: HomeAssistant = hass
        self.config: TempixConfig = config
        self._state_snapshot: dict[str, Any] = {}
        self._startup_time: datetime | None = None
        self._optimum_start_active: bool = False
        self._calibration_entity_map: dict[str, str | None] = {}
        self._calendar_events: dict[str, list[dict[str, Any]]] = {}
        self._last_home_status: bool | None = None  # memory for current session
        self._last_outside_ok: bool | None = None  # hysteresis state for outside threshold
        self._guest_warned: set[str] = set()  # O-5: warn once per unavailable guest entity

    # ── injection ────────────────────────────────────────────────────────────

    def set_calendar_events(self, events: dict[str, list[dict[str, Any]]]) -> None:
        """Inject fetched calendar events for deep scanning."""
        self._calendar_events = events

    def set_state_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Inject a snapshot of states to be used for this calculation cycle."""
        self._state_snapshot = snapshot

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
        """Return temperature from state or attributes (weather/climate support)."""
        state_obj = self._get_state(entity_id)
        if not state_obj or state_obj.state in INVALID_STATES:
            return default

        try:
            return float(state_obj.state)
        except (ValueError, TypeError):
            pass

        for attr in ("temperature", "current_temperature"):
            val = state_obj.attributes.get(attr)
            if val is not None:
                try:
                    return float(val)
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
        """Ensure a datetime object is timezone-aware natively in UTC."""
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
        """Return ``True`` if no scheduler, persons or presence configured."""
        return (
            len(self.config.persons) == 0
            and not self.config.guest_mode
            and len(self.config.schedulers) == 0
            and not self.config.presence_sensor
            and not self.config.proximity_entity
            and not self.config.party_mode_switch
        )
