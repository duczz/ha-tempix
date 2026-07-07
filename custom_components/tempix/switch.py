"""Tempix – Switches."""
from __future__ import annotations

import logging
from datetime import datetime, UTC
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
    CONF_VACATION_MODE_SWITCH,
    CONF_GUEST_MODE_SWITCH,
    CONF_AUTOMATION_ACTIVE,
    CONF_MANUAL_OVERRIDE,
    CONF_SMART_PRECONDITIONING,
    CONF_SUNSHINE_OFFSET,
    CONF_FORCE_COMFORT_SWITCH,
    CONF_FORCE_ECO_SWITCH,
    CONF_LIMING_PROTECTION,
    CONF_FROST_PROTECTION_ENABLED,
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
            CONF_GUEST_MODE_SWITCH, "mdi:account-star"
        ),
        TempixSwitch(
            coordinator, entry,
            CONF_PARTY_MODE_SWITCH, "mdi:party-popper"
        ),
        TempixSwitch(
            coordinator, entry,
            CONF_VACATION_MODE_SWITCH, "mdi:airplane"
        ),
        TempixSwitch(
            coordinator, entry,
            CONF_LIMING_PROTECTION, "mdi:valve"
        ),
        TempixSwitch(
            coordinator, entry,
            CONF_FROST_PROTECTION_ENABLED, "mdi:snowflake"
        ),
        TempixSwitch(
            coordinator, entry,
            CONF_AUTOMATION_ACTIVE, "mdi:robot-outline"
        ),
        TempixSwitch(
            coordinator, entry,
            CONF_MANUAL_OVERRIDE, "mdi:hand-back-right-outline"
        ),
        TempixSwitch(
            coordinator, entry,
            CONF_SMART_PRECONDITIONING, "mdi:clock-fast"
        ),
        TempixSwitch(
            coordinator, entry,
            CONF_SUNSHINE_OFFSET, "mdi:weather-sunny-alert"
        ),
        TempixSwitch(
            coordinator, entry,
            CONF_FORCE_ECO_SWITCH, "mdi:leaf"
        ),
        TempixSwitch(
            coordinator, entry,
            CONF_FORCE_COMFORT_SWITCH, "mdi:fire"
        ),
        TempixOverrideSwitch(coordinator, entry),
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
        icon: str,
    ) -> None:
        """Initialize the switch."""
        self.coordinator = coordinator
        self.entry = entry
        self.key = key
        self._attr_translation_key = key
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Martin Müller",
        )
        self._restored_is_on: bool = False

    @property
    def available(self) -> bool:
        return self.coordinator._updates_enabled

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
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in ("unknown", "unavailable"):
            self._restored_is_on = last_state.state == "on"
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )

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

        setattr(self.coordinator.config, self.key, True)
        _LOGGER.debug("TPX Switch [%s]: turn_on key=%s, updating entry options", self.entry.title, self.key)
        self.hass.config_entries.async_update_entry(self.entry, options=new_options)
        self.async_write_ha_state()
        _LOGGER.debug("TPX Switch [%s]: turn_on key=%s, calling async_request_refresh", self.entry.title, self.key)
        await self.coordinator.async_request_refresh()
        _LOGGER.debug("TPX Switch [%s]: turn_on key=%s, refresh complete", self.entry.title, self.key)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        new_options = dict(self.entry.options)
        new_options[self.key] = False
        setattr(self.coordinator.config, self.key, False)
        _LOGGER.debug("TPX Switch [%s]: turn_off key=%s, updating entry options", self.entry.title, self.key)
        self.hass.config_entries.async_update_entry(self.entry, options=new_options)
        self.async_write_ha_state()
        _LOGGER.debug("TPX Switch [%s]: turn_off key=%s, calling async_request_refresh", self.entry.title, self.key)
        await self.coordinator.async_request_refresh()
        _LOGGER.debug("TPX Switch [%s]: turn_off key=%s, refresh complete", self.entry.title, self.key)

class TempixOverrideSwitch(SwitchEntity):
    """Switch reflecting the state of the active TemporaryManualOverride."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "temporary_manual_override"
    _attr_icon = "mdi:hand-back-right"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        self.coordinator = coordinator
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_temporary_manual_override"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Martin Müller",
        )

    @property
    def available(self) -> bool:
        return self.coordinator._updates_enabled

    @property
    def is_on(self) -> bool:
        override = self.coordinator._temporary_manual_override
        return override is not None and override.active

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Lock in the current computed target temperature as an override.
        
        No-op if the coordinator state is uncertain (temp or hvac is None),
        because we'd create a ghost override that shows as ON but does nothing.
        """
        temp = self.coordinator.current_temperature
        hvac = self.coordinator.current_hvac
        if temp is None or hvac is None:
            _LOGGER.warning(
                "TPX OverrideSwitch [%s]: Cannot activate override – coordinator state is uncertain "
                "(temp=%s, hvac=%s). Is a sensor unavailable?",
                self.entry.title, temp, hvac,
            )
            return
        self.coordinator.activate_temporary_manual_override(
            new_temp=temp,
            new_hvac=hvac,
            source="ui",
            now=datetime.now(UTC)
        )
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Clear the temporary override."""
        self.coordinator.clear_temporary_manual_override()
        # Write state immediately — the listener roundtrip via async_update can
        # end in an uncertainty early-return before listeners are notified,
        # which would leave the switch visually ON.
        self.async_write_ha_state()
