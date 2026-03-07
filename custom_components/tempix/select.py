"""Tempix – Selects."""
from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.tempix.const import (
    DOMAIN,
    CONF_NAME,
    VERSION,
    CONF_HVAC_MODE_COMFORT,
    CONF_HVAC_MODE_ECO,
)



async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Tempix selects."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]

    selects = [
        TempixSelect(
            coordinator, entry,
            CONF_HVAC_MODE_COMFORT, None,
            ["heat", "cool", "heat_cool", "auto", "off"]
        ),
        TempixSelect(
            coordinator, entry,
            CONF_HVAC_MODE_ECO, None,
            ["heat", "cool", "heat_cool", "auto", "off"]
        ),
    ]

    async_add_entities(selects)


class TempixSelect(SelectEntity, RestoreEntity):
    """Select entity that updates Tempix config options."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator,
        entry: ConfigEntry,
        key: str,
        name: str | None = None,
        options: list[str] | None = None,
        icon: str | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._key = key
        
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        if name:
            self._attr_name = name
        self._attr_translation_key = key
        self._attr_options = options or []
        self._attr_icon = icon

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title,
            manufacturer="panhans / Martin Müller",
            model="Tempix",
            sw_version=VERSION,
        )

    @property
    def current_option(self) -> str | None:
        """Return the current selected option."""
        val = self._entry.options.get(self._key, self._entry.data.get(self._key))
        if val is not None:
            return val
        return self._restored_option

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        new_options = dict(self._entry.options)
        new_options[self._key] = option
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)

    async def async_added_to_hass(self) -> None:
        """Restore last known state on startup."""
        await super().async_added_to_hass()
        self._restored_option: str | None = None
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in ("unknown", "unavailable"):
            if last_state.state in self._attr_options:
                self._restored_option = last_state.state
        self.async_on_remove(
            self._coordinator.async_add_listener(self.async_write_ha_state)
        )
