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

from .const import (
    DOMAIN,
    VERSION,
)

TO_REDACT = {"persons", "active_calendar_event", "presence_sensor"}


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


def _redact_calendar_events(events: dict | None) -> dict | None:
    """Remove location and calendar_id from each event; anonymize calendar keys."""
    if not events:
        return events
    result = {}
    for i, cal_events in enumerate(events.values()):
        cleaned = [
            {k: v for k, v in ev.items() if k not in ("location", "calendar_id")}
            for ev in (cal_events or [])
        ]
        result[f"calendar_{i}"] = cleaned
    return result


def _redact_person_states(person_states: dict) -> dict:
    """Anonymize person entity IDs and reduce attributes to state only."""
    return {
        f"person_{i}": {"state": state.get("state")} if state else None
        for i, state in enumerate(person_states.values())
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    engine = data["engine"]

    # Redact sensitive keys from engine config
    redacted_config = {
        k: v for k, v in engine.config.to_dict().items()
        if not k.startswith("_") and k not in ["api_key", "password", "token", "username"]
    }

    # ── TRV states ───────────────────────────────────────────────────────────
    trv_states = {}
    for trv_id in engine.config.trvs:
        trv_states[trv_id] = _safe_state(hass, trv_id)

    # ── Sensor states ────────────────────────────────────────────────────────
    sensor_states = {}
    temp_sensor = engine.config.temp_sensor
    if temp_sensor:
        sensor_states["temperature_sensor"] = {
            "entity_id": temp_sensor,
            **(_safe_state(hass, temp_sensor) or {"state": "not_found"}),
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
            "frost_protection": engine.is_frost_protection(),
            "is_liming_active": engine.is_liming_time(),
            "force_comfort": engine.is_force_comfort_temp(),
            "force_eco": engine.is_force_eco_temp(),
            "is_optimum_start_active": engine.is_optimum_start_active(),
            "is_weather_anticipation_active": engine.is_weather_anticipation_active(),
            "weather_offset": engine.get_weather_offset(),
            "uncertainty_reasons": engine.get_uncertainty_reasons(),
        },
        "decision_logic": {
            "should_set_comfort": engine.should_set_comfort(),
            "resolved_room_temp": engine._resolve_room_temp(),
            "comfort_temp": engine.resolve_comfort_temperature(),
            "eco_temp": engine.resolve_eco_temperature(),
            "target_temperature": engine.calculate_target_temperature(),
            "hvac_mode": engine.calculate_hvac_mode(),
        },
        "scheduler_info": {
            "active_scheduler": engine.get_active_scheduler(),
            "active_calendar_event": engine._get_active_calendar_event(active_only=False),
            "calendar_tags": engine.get_calendar_tags(),
            "fetched_calendar_events": _redact_calendar_events(engine._calendar_events),
            "schedule_period": engine.get_active_schedule_period(),
            "next_schedule_transition": engine.get_next_schedule_transition(),
        },
        "trv_states": trv_states,
        "sensor_states": sensor_states,
        "person_states": _redact_person_states(person_states),
        "coordinator_state": {
            "current_temperature": coordinator.current_temperature,
            "current_hvac": coordinator.current_hvac,
            "current_reason": coordinator.current_reason,
            "last_calibrations": coordinator.last_calibrations,
            "last_generic_offsets": getattr(coordinator, "last_generic_offsets", {}),
            "last_changes": coordinator.last_changes,
        },
        "timing": {
            "engine_startup": startup.isoformat() if startup else None,
            "coordinator_ready": ready.isoformat() if ready else None,
            "uptime_seconds": (now - startup).total_seconds() if startup else None,
            "last_update": last_update if isinstance(last_update, str) else str(last_update) if last_update else None,
        },
    }

    return async_redact_data(diag, TO_REDACT)
