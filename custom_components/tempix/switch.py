"""Tempix – Switches."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.tempix.const import (
    DOMAIN,
    CONF_PARTY_MODE_SWITCH,
    CONF_GUEST_MODE_SWITCH,
    CONF_AUTOMATION_ACTIVE,
    CONF_OPTIMUM_START,
    CONF_WEATHER_ANTICIPATION,
    CONF_FORCE_COMFORT_SWITCH,
    CONF_FORCE_ECO_SWITCH,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Tempix switches."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]

    async_add_entities([
        TempixSwitch(
            coordinator, entry,
            CONF_GUEST_MODE_SWITCH, "Guest Mode", "mdi:account-star"
        ),
        TempixSwitch(
            coordinator, entry,
            CONF_PARTY_MODE_SWITCH, "Party Mode", "mdi:party-popper"
        ),
        TempixSwitch(
            coordinator, entry,
            CONF_AUTOMATION_ACTIVE, "Automation Active", "mdi:robot"
        ),

        TempixSwitch(
            coordinator, entry,
            CONF_OPTIMUM_START, "Smart Preheating", "mdi:clock-fast"
        ),
        TempixSwitch(
            coordinator, entry,
            CONF_WEATHER_ANTICIPATION, "Weather Anticipation", "mdi:weather-sunny-alert"
        ),
        TempixSwitch(
            coordinator, entry,
            CONF_FORCE_ECO_SWITCH, "Force Eco Temperatur", "mdi:leaf"
        ),
        TempixSwitch(
            coordinator, entry,
            CONF_FORCE_COMFORT_SWITCH, "Force Comfort Temperatur", "mdi:fire-alert"
        ),
    ])


class TempixSwitch(SwitchEntity, RestoreEntity):
    """Switch that updates Tempix config options."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator,
        entry: ConfigEntry,
        key: str,
        name: str,
        icon: str,
    ) -> None:
        """Initialize the switch."""
        self.coordinator = coordinator
        self.entry = entry
        self.key = key
        self._attr_name = name
        self._attr_translation_key = key
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="panhans / Martin Müller",
        )

    @property
    def is_on(self) -> bool:
        """Return True if entity is on."""
        val = getattr(self.coordinator.config, self.key, None)
        if val is not None:
            return bool(val)
        return self._restored_is_on

    async def async_added_to_hass(self) -> None:
        """Restore last known state on startup."""
        await super().async_added_to_hass()
        self._restored_is_on: bool = False
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in ("unknown", "unavailable"):
            self._restored_is_on = last_state.state == "on"

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the entity on."""
        new_options = dict(self.entry.options)
        new_options[self.key] = True

        # Mutual exclusion: force_comfort and force_eco cannot both be active
        sibling_key = None
        if self.key == CONF_FORCE_COMFORT_SWITCH:
            sibling_key = CONF_FORCE_ECO_SWITCH
        elif self.key == CONF_FORCE_ECO_SWITCH:
            sibling_key = CONF_FORCE_COMFORT_SWITCH

        if sibling_key:
            new_options[sibling_key] = False
            setattr(self.coordinator.config, sibling_key, False)
            for entity in self.platform.entities.values():
                if isinstance(entity, TempixSwitch) and entity.key == sibling_key:
                    entity.async_write_ha_state()
                    break

        self.hass.config_entries.async_update_entry(self.entry, options=new_options)
        setattr(self.coordinator.config, self.key, True)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        new_options = dict(self.entry.options)
        new_options[self.key] = False
        self.hass.config_entries.async_update_entry(self.entry, options=new_options)
        setattr(self.coordinator.config, self.key, False)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
