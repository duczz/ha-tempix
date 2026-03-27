"""Tempix – Diagnostic sensor."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    UnitOfTemperature,
    CONF_NAME,
    STATE_ON,
    STATE_OFF,
    STATE_UNKNOWN,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.tempix.const import (
    DOMAIN,
    SCHEDULING_MODE_CALENDAR,
    VERSION,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    engine = data["engine"]

    temp_unit = hass.config.units.temperature_unit

    sensors = [
        TempixStatusSensor(coordinator, engine, entry),
        TempixSensor(
            coordinator, engine, entry,
            "target_temperature", "Target Temperature",
            "mdi:thermometer-auto", SensorDeviceClass.TEMPERATURE,
            temp_unit,
            lambda e: e.calculate_target_temperature()
        ),
        TempixSensor(
            coordinator, engine, entry,
            "external_temperature", "External Room Temperature",
            "mdi:thermometer-bluetooth", SensorDeviceClass.TEMPERATURE,
            temp_unit,
            lambda e: e._resolve_room_temp()
        ),
        TempixSensor(
            coordinator, engine, entry,
            "outside_temperature", "Outside Temperature",
            "mdi:thermometer-minus", SensorDeviceClass.TEMPERATURE,
            temp_unit,
            lambda e: e._temp_state(e.config.outside_temp_sensor)
        ),
        TempixSensor(
            coordinator, engine, entry,
            "active_adjustment", "Active Adjustment",
            "mdi:tune", None,
            None,
            lambda e: _get_adjustment_name(e)
        ),
        TempixSensor(
            coordinator, engine, entry,
            "active_scheduler", "Active Scheduler",
            "mdi:calendar-clock", None,
            None,
            lambda e: _get_scheduler_name(e)
        ),
        TempixSensor(
            coordinator, engine, entry,
            "active_schedule_period", "Active Scheduler Time Period",
            "mdi:clock-outline", None,
            None,
            lambda e: e.get_active_schedule_period()
        ),
        TempixSensor(
            coordinator, engine, entry,
            "calibration_offset", "Calibration Offset",
            "mdi:arrow-collapse-vertical", None,
            None,
            lambda e: _get_calibration_offset(coordinator, engine)
        ),
    ]
    
    # Per-TRV diagnostic sensors
    for trv_id in coordinator.config.trvs:
        short_id = trv_id.split(".")[-1]
        sensors.append(
            TempixSensor(
                coordinator, engine, entry,
                f"trv_temp_{short_id}", f"TRV Temp {short_id}",
                "mdi:thermometer-check", SensorDeviceClass.TEMPERATURE,
                temp_unit,
                lambda e, tid=trv_id: e._get_state(tid).attributes.get("current_temperature") if e._get_state(tid) else None
            )
        )
        sensors.append(
            TempixSensor(
                coordinator, engine, entry,
                f"trv_target_{short_id}", f"TRV Target {short_id}",
                "mdi:thermometer-auto", SensorDeviceClass.TEMPERATURE,
                temp_unit,
                lambda e, tid=trv_id, c=coordinator: _get_trv_target_temperature(c, tid)
            )
        )

    # Cleanup orphan entities (Phase 52)
    from homeassistant.helpers import entity_registry as er
    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    
    # Get current expected unique IDs
    expected_unique_ids = {s.unique_id for s in sensors}
    
    for entity in entities:
        # Only cleanup sensors that look like our TRV sensors
        if entity.unique_id.startswith(f"{entry.entry_id}_trv_") and entity.unique_id not in expected_unique_ids:
            _LOGGER.info("Removing orphan sensor entity: %s (unique_id: %s)", entity.entity_id, entity.unique_id)
            registry.async_remove(entity.entity_id)

    async_add_entities(sensors)


def _get_calibration_offset(coordinator, engine) -> str:
    """Format the last known calibration values, including generic offset."""
    # Start with physical offsets (e.g. Tado)
    offsets = dict(coordinator.last_calibrations)
    
    # Add generic offsets (target manipulation)
    generic_offsets = getattr(coordinator, "last_generic_offsets", {})
    for eid, offset in generic_offsets.items():
        if offset != 0:
            # Check if this entity already has a physical offset to avoid double labeling
            if eid in offsets and offsets[eid] == offset:
                continue
            offsets[f"{eid} (gen)"] = offset

    if not offsets:
        return "0.0"
        
    values = set(offsets.values())
    if len(values) == 1:
        return str(list(values)[0])
    return ", ".join([f"{k}: {v}" for k, v in offsets.items()])


def _get_adjustment_name(engine) -> str:
    adj = engine.get_active_adjustment()
    if not adj:
        return "None"
    # Try to find a name or description, fallback to ID/Time
    return str(adj.get("name", adj.get("time", "Unknown")))


def _get_scheduler_name(engine) -> str:
    in_cal_mode = engine.config.scheduling_mode == SCHEDULING_MODE_CALENDAR

    # Im Kalender-Modus: Kalender hat Priorität
    if in_cal_mode:
        cal_event = engine._get_active_calendar_event(force_check=True, active_only=False)
        if cal_event:
            from datetime import datetime, UTC
            now = datetime.now(UTC)
            start_dt = engine._parse_dt(cal_event.get("start_time") or cal_event.get("start"))
            end_dt = engine._parse_dt(cal_event.get("end_time") or cal_event.get("end"))
            is_active = bool(start_dt and end_dt and start_dt <= now < end_dt)
            prefix = "Kalender" if is_active else "Kalender (nächster)"
            summary = cal_event.get("summary")
            if summary and summary != "none":
                return f"{prefix}: {summary}"

    # Scheduler-Helper-Modus (oder Kalender ohne aktives Event)
    sched_id = engine.get_active_scheduler()
    if sched_id:
        state = engine._get_state(sched_id)
        if state:
            if state.attributes.get("friendly_name"):
                return state.attributes.get("friendly_name")
            if state.state not in [STATE_UNKNOWN, STATE_UNAVAILABLE]:
                return f"{sched_id} ({state.state})"
        return str(sched_id)

    return "Kein Termin/Scheduler"


def _get_trv_target_temperature(coordinator, trv_id: str) -> float | None:
    """Get target temperature for TRV from last known changes or state."""
    # Check last changes recorded by coordinator
    if coordinator.last_changes:
        for change in coordinator.last_changes:
            if change.get("entity_id") == trv_id:
                temp = change.get("temperature")
                if temp is not None:
                    return float(temp)
    
    # Fallback to current state
    state = coordinator.hass.states.get(trv_id)
    if state:
        # Most TRVs use 'temperature' attribute for target
        temp = state.attributes.get("temperature")
        if temp is not None:
            return float(temp)
    return None


class TempixStatusSensor(SensorEntity, RestoreEntity):
    """Sensor that exposes the full Tempix engine state for dashboards."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator, engine, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._engine = engine
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_status"
        self._attr_name = "Status"
        self._attr_icon = "mdi:fire"
        self._attr_translation_key = "status"

        # Device Info for UI grouping
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=self._entry.title,
            manufacturer="panhans / Martin Müller",
            model="Tempix",
            sw_version=VERSION,
        )

    @property
    def native_value(self) -> str:
        val = self._coordinator.current_reason
        if val:
            return val
        return self._restored_native_value or "Idle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        engine = self._engine
        is_party, _ = engine.check_party_mode()
        adj = engine.get_active_adjustment()

        attrs: dict[str, Any] = {
            "hvac_mode": self._coordinator.current_hvac,
            "target_temperature": self._coordinator.current_temperature,
            "external_room_temperature": engine._resolve_room_temp(),
            "season_mode": engine.is_season_mode(),
            "automation_active": engine.is_automation_active(),
            "manual_override_pause": engine.config.manual_override_pause,
            "outside_temperature": engine._float_state(engine.config.outside_temp_sensor),
            "anybody_home": engine.is_anybody_home(),
            "scheduler_active": engine.is_scheduler_active(),
            "presence_active": engine.is_presence_active(),
            "proximity_arrived": engine.check_proximity_arrived(),
            "proximity_towards": engine.check_proximity_towards(),
            "window_open": engine.is_window_open(),
            "party_mode": is_party,
            "guest_mode": engine.is_guest_mode(),
            "away": engine.is_away(),
            "force_comfort": engine.is_force_comfort_temp(),
            "force_eco": engine.is_force_eco_temp(),
            "optimum_start_active": getattr(engine, "is_optimum_start_active", lambda: False)(),
            "set_comfort": engine.should_set_comfort(),
            "adjustment": adj.get("name") if adj else None,
            "last_changes": self._coordinator.last_changes,
            "calibration_offsets": self._coordinator.last_calibrations,
            "heating_state": self._coordinator.current_state.value,
        }
        return attrs

    async def async_added_to_hass(self) -> None:
        """Restore last known state on startup."""
        await super().async_added_to_hass()
        self._restored_native_value: str | None = None
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in ("unknown", "unavailable"):
            self._restored_native_value = last_state.state
        self.async_on_remove(
            self._coordinator.async_add_listener(self.async_write_ha_state)
        )


class TempixSensor(SensorEntity, RestoreEntity):
    """Generic Sensor for Tempix numeric/string values."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator,
        engine,
        entry,
        key: str,
        name: str,
        icon: str,
        device_class: SensorDeviceClass | None,
        native_unit_of_measurement: str | None,
        val_func,
    ) -> None:
        self._coordinator = coordinator
        self._engine = engine
        self._entry = entry
        self._val_func = val_func
        
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_translation_key = key
        self._attr_icon = icon
        if device_class:
            self._attr_device_class = device_class
        if native_unit_of_measurement:
            self._attr_native_unit_of_measurement = native_unit_of_measurement

        # Device Info for UI grouping
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=coordinator.config.name,
            manufacturer="panhans / Martin Müller",
            model="Virtual Thermostat",
            sw_version=VERSION,
        )

    @property
    def native_value(self) -> str | int | float | None:
        try:
            val = self._val_func(self._engine)
            if val is not None:
                return val
        except Exception:
            pass
        return self._restored_native_value

    async def async_added_to_hass(self) -> None:
        """Restore last known state on startup."""
        await super().async_added_to_hass()
        self._restored_native_value: str | float | None = None
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in ("unknown", "unavailable"):
            # Try to preserve numeric type for temperature sensors
            if getattr(self, "_attr_device_class", None) == SensorDeviceClass.TEMPERATURE:
                try:
                    self._restored_native_value = float(last_state.state)
                except (ValueError, TypeError):
                    self._restored_native_value = last_state.state
            else:
                self._restored_native_value = last_state.state
        self.async_on_remove(
            self._coordinator.async_add_listener(self.async_write_ha_state)
        )
