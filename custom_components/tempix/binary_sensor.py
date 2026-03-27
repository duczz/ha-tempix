"""Tempix – Binary Sensors."""
from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo

from custom_components.tempix.const import (
    DOMAIN,
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

    sensors = [
        TempixBinarySensor(
            coordinator, engine, entry,
            "season_mode", "Season Mode",
            "mdi:snowflake", None,
            lambda e: e.is_season_mode()
        ),
        TempixBinarySensor(
            coordinator, engine, entry,
            "anybody_home", "Persons & Devices", # anybody home
            "mdi:home-account", None,
            lambda e: e.is_anybody_home()
        ),
        TempixBinarySensor(
            coordinator, engine, entry,
            "window_open", "Window Open",
            "mdi:window-open", BinarySensorDeviceClass.WINDOW,
            lambda e: e.is_window_open()
        ),
        TempixBinarySensor(
            coordinator, engine, entry,
            "away_mode", "Away Mode",
            "mdi:bag-suitcase", None,
            lambda e: e.is_away()
        ),
        TempixBinarySensor(
            coordinator, engine, entry,
            "presence_active", "Presence Detection",
            "mdi:motion-sensor", None,
            lambda e: e.is_presence_active()
        ),

        TempixBinarySensor(
            coordinator, engine, entry,
            "proximity_arrived", "Proximity Arrived",
            "mdi:map-marker-radius", None,
            lambda e: e.check_proximity_arrived()
        ),
    ]

    async_add_entities(sensors)


class TempixBinarySensor(BinarySensorEntity):
    """Generic Binary Sensor for Tempix engine states."""

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
        device_class: BinarySensorDeviceClass | None,
        val_func,
    ) -> None:
        self._coordinator = coordinator
        self._engine = engine
        self._entry = entry
        self._key = key
        self._val_func = val_func
        
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_translation_key = key
        self._attr_icon = icon
        self._attr_device_class = device_class

        # Device Info for UI grouping
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title,
            manufacturer="panhans / Martin Müller",
            model="Tempix",
            sw_version=VERSION,
        )

    @property
    def is_on(self) -> bool | None:
        try:
            return self._val_func(self._engine)
        except Exception:
            return None

    async def async_added_to_hass(self) -> None:
        """Register callbacks."""
        self.async_on_remove(
            self._coordinator.async_add_listener(self.async_write_ha_state)
        )
