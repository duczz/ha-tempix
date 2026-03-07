"""Tempix – Numbers."""
from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity

from datetime import timedelta

from custom_components.tempix.const import (
    DOMAIN,
    VERSION,
    CONF_TEMPERATURE_COMFORT_STATIC,
    CONF_TEMPERATURE_ECO_STATIC,
    CONF_PARTY_TEMPERATURE,
    CONF_WEATHER_OFFSET,
    CONF_MAX_OPTIMUM_START,
    CONF_LEARNED_HEATING_RATE,
)



async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Tempix numbers."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]

    temp_unit = hass.config.units.temperature_unit

    numbers = [
        TempixNumber(
            coordinator, entry,
            CONF_TEMPERATURE_COMFORT_STATIC, "Comfort Temperature", "mdi:thermometer",
            5.0, 35.0, 0.5, NumberDeviceClass.TEMPERATURE, temp_unit
        ),
        TempixNumber(
            coordinator, entry,
            CONF_TEMPERATURE_ECO_STATIC, "Eco Temperature", "mdi:thermometer-low",
            5.0, 35.0, 0.5, NumberDeviceClass.TEMPERATURE, temp_unit
        ),
        TempixNumber(
            coordinator, entry,
            CONF_PARTY_TEMPERATURE, "Party Temperature", "mdi:party-popper",
            5.0, 35.0, 0.5, NumberDeviceClass.TEMPERATURE, temp_unit
        ),
        TempixNumber(
            coordinator, entry,
            CONF_WEATHER_OFFSET, "Weather Anticipation Offset", "mdi:weather-sunny",
            0.0, 5.0, 0.1, NumberDeviceClass.TEMPERATURE, temp_unit
        ),
        TempixNumber(
            coordinator, entry,
            CONF_MAX_OPTIMUM_START, "Smart Preheating Max. Duration", "mdi:clock-end",
            0, 240, 5, None, "min"
        ),
        TempixNumber(
            coordinator, entry,
            CONF_LEARNED_HEATING_RATE, "Smart Preheating Learning Rate", "mdi:heating-coil",
            0.1, 5.0, 0.1, None, "°C/h"
        ),
    ]

    async_add_entities(numbers)


class TempixNumber(NumberEntity, RestoreEntity):
    """Number entity that updates Tempix config options."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator,
        entry: ConfigEntry,
        key: str,
        name: str,
        icon: str,
        min_value: float,
        max_value: float,
        step: float,
        device_class: NumberDeviceClass | None,
        unit: str | None,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._key = key
        
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_translation_key = key
        self._attr_icon = icon
        
        self._attr_native_min_value = min_value
        self._attr_native_max_value = max_value
        self._attr_native_step = step
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title,
            manufacturer="panhans / Martin Müller",
            model="Tempix",
            sw_version=VERSION,
        )

    @property
    def native_value(self) -> float:
        """Return the current value."""
        val = getattr(self._coordinator.config, self._key, 0.0)
        if isinstance(val, timedelta):
            return val.total_seconds() / 60.0
        return float(val)

    async def async_set_native_value(self, value: float) -> None:
        """Set new value."""
        new_options = dict(self._entry.options)
        
        # Convert minutes back to duration dict for compatibility
        if self._key == CONF_MAX_OPTIMUM_START:
            new_options[self._key] = {"minutes": int(value)}
        else:
            new_options[self._key] = value
            
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)

    async def async_added_to_hass(self) -> None:
        """Restore last known state on startup."""
        await super().async_added_to_hass()
        self._restored_native_value: float | None = None
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in ("unknown", "unavailable"):
            try:
                self._restored_native_value = float(last_state.state)
            except (ValueError, TypeError):
                pass
        self.async_on_remove(
            self._coordinator.async_add_listener(self.async_write_ha_state)
        )
