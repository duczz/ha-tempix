"""Tempix – Numbers."""
from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from datetime import timedelta

from custom_components.tempix.device import tempix_device_info
from custom_components.tempix.const import (
    DOMAIN,
    CONF_TEMPERATURE_COMFORT_STATIC,
    CONF_TEMPERATURE_ECO_STATIC,
    CONF_PARTY_TEMPERATURE,
    CONF_VACATION_TEMPERATURE,
    CONF_SUNSHINE_OFFSET_VALUE,
    CONF_MAX_SMART_PRECONDITIONING,
    CONF_LEARNED_CLIMATE_RATE,
    DEFAULT_VACATION_TEMP,
    DEFAULT_PARTY_TEMP,
    DEFAULT_COMFORT_TEMP,
    DEFAULT_ECO_TEMP,
    DEFAULT_SUNSHINE_OFFSET_VALUE,
    DEFAULT_CLIMATE_RATE,
    DEFAULT_MAX_SMART_PRECONDITIONING,
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
            CONF_TEMPERATURE_COMFORT_STATIC, "mdi:thermometer",
            5.0, 30.0, 0.5, NumberDeviceClass.TEMPERATURE, temp_unit
        ),
        TempixNumber(
            coordinator, entry,
            CONF_TEMPERATURE_ECO_STATIC, "mdi:thermometer-low",
            5.0, 30.0, 0.5, NumberDeviceClass.TEMPERATURE, temp_unit
        ),
        TempixNumber(
            coordinator, entry,
            CONF_PARTY_TEMPERATURE, "mdi:party-popper",
            5.0, 30.0, 0.5, NumberDeviceClass.TEMPERATURE, temp_unit
        ),
        TempixNumber(
            coordinator, entry,
            CONF_VACATION_TEMPERATURE, "mdi:airplane",
            5.0, 30.0, 0.5, NumberDeviceClass.TEMPERATURE, temp_unit
        ),
        TempixNumber(
            coordinator, entry,
            CONF_SUNSHINE_OFFSET_VALUE, "mdi:weather-sunny",
            0.0, 5.0, 0.1, NumberDeviceClass.TEMPERATURE, temp_unit
        ),
        TempixNumber(
            coordinator, entry,
            CONF_MAX_SMART_PRECONDITIONING, "mdi:clock-end",
            0, 240, 5, None, "min"
        ),
        TempixNumber(
            coordinator, entry,
            CONF_LEARNED_CLIMATE_RATE, "mdi:heating-coil",
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
        self._attr_translation_key = key
        self._attr_icon = icon
        
        self._attr_native_min_value = min_value
        self._attr_native_max_value = max_value
        self._attr_native_step = step
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit

        self._restored_native_value: float | None = None
        self._attr_device_info = tempix_device_info(self._entry)

    @property
    def available(self) -> bool:
        return self._coordinator._updates_enabled

    def _hardcoded_default(self) -> float | None:
        """Der Wert, mit dem die ENGINE rechnet, solange die Option nicht
        gesetzt ist. `None` = fuer diesen Key ist keiner hinterlegt."""
        if self._key == CONF_VACATION_TEMPERATURE:
            return float(DEFAULT_VACATION_TEMP)
        if self._key == CONF_PARTY_TEMPERATURE:
            return float(DEFAULT_PARTY_TEMP)
        if self._key == CONF_TEMPERATURE_COMFORT_STATIC:
            return float(DEFAULT_COMFORT_TEMP)
        if self._key == CONF_TEMPERATURE_ECO_STATIC:
            return float(DEFAULT_ECO_TEMP)
        if self._key == CONF_SUNSHINE_OFFSET_VALUE:
            return float(DEFAULT_SUNSHINE_OFFSET_VALUE)
        if self._key == CONF_LEARNED_CLIMATE_RATE:
            return float(DEFAULT_CLIMATE_RATE)
        if self._key == CONF_MAX_SMART_PRECONDITIONING:
            return float(DEFAULT_MAX_SMART_PRECONDITIONING.get("hours", 0) * 60 + DEFAULT_MAX_SMART_PRECONDITIONING.get("minutes", 0))
        return None

    @property
    def native_value(self) -> float:
        """Return the current value."""
        val = getattr(self._coordinator.config, self._key, None)
        if val is None:
            # Der hartkodierte Default schlaegt den restaurierten Wert.
            # Umgekehrt war es zwischenzeitlich, weil
            # `_restored_native_value` befuellt, aber nie gelesen wurde — die
            # Annahme dabei war, dass hier ein vom Nutzer gesetzter Wert
            # verlorengeht. Das kann nicht passieren: `async_set_native_value`
            # schreibt in `entry.options`, ein gesetzter Wert kommt also immer
            # oben als `val` an und dieser Zweig laeuft gar nicht erst.
            #
            # Damit ist der restaurierte Wert fuer diese Keys IMMER der Default
            # des Vorstarts — identisch mit dem heutigen (dann egal) oder
            # veraltet, sobald sich eine DEFAULT_*-Konstante in einem Update
            # aendert. Genau dann zeigte die Entity den alten Wert an, waehrend
            # die Engine mit dem neuen rechnete.
            default = self._hardcoded_default()
            if default is not None:
                return default
            # Kein Default hinterlegt (heute hat jeder Key einen; Absicherung
            # fuer kuenftige Keys): hier ist der zuletzt gesehene Wert
            # tatsaechlich die beste verfuegbare Quelle.
            if self._restored_native_value is not None:
                return self._restored_native_value
            return 0.0
        if isinstance(val, timedelta):
            return val.total_seconds() / 60.0
        return float(val)

    async def async_set_native_value(self, value: float) -> None:
        """Set new value."""
        new_options = dict(self._entry.options)
        
        # Convert minutes back to duration dict for compatibility
        if self._key == CONF_MAX_SMART_PRECONDITIONING:
            new_options[self._key] = {"minutes": int(value)}
        else:
            new_options[self._key] = value
            
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)

    async def async_added_to_hass(self) -> None:
        """Restore last known state on startup."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in ("unknown", "unavailable"):
            try:
                self._restored_native_value = float(last_state.state)
            except (ValueError, TypeError):
                pass
        self.async_on_remove(
            self._coordinator.async_add_listener(self.async_write_ha_state)
        )
