"""Tempix – Switches."""
from __future__ import annotations

import logging
from datetime import datetime, UTC
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.tempix.const import (
    DOMAIN,
    CONF_PARTY_MODE_SWITCH,
    CONF_VACATION_MODE_SWITCH,
    CONF_GUEST_MODE_SWITCH,
    CONF_VACATION_HOME,
    CONF_AUTOMATION_ACTIVE,
    CONF_MANUAL_MODE,
    CONF_SMART_PRECONDITIONING,
    CONF_SUNSHINE_OFFSET,
    CONF_FORCE_COMFORT_SWITCH,
    CONF_FORCE_ECO_SWITCH,
    CONF_LIMING_PROTECTION,
    CONF_FROST_PROTECTION_ENABLED,
)
from custom_components.tempix.device import tempix_device_info

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
            CONF_VACATION_HOME, "mdi:calendar-star"
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
            CONF_MANUAL_MODE, "mdi:hand-back-right-outline"
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


class TempixSwitch(SwitchEntity):
    """Switch that updates Tempix config options.

    Kein ``RestoreEntity``: alle zwölf Keys sind ``bool``-Felder mit
    Default in ``TempixConfig``, ``getattr`` liefert also nie ``None`` und der
    Restore-Zweig war unerreichbar. Persistenz liefert ``entry.options`` —
    ``async_turn_on``/``off`` schreiben dorthin, der Wert ist nach einem
    Neustart schon in der Config, bevor die Entity existiert. Gleiche
    Konstellation wie in ``number.py``; dort ist der Restore-Pfad heute
    ebenfalls unerreichbar (jeder Key hat einen hartkodierten Default) und
    bleibt nur als Absicherung fuer kuenftige Keys.
    """

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
        self._attr_device_info = tempix_device_info(entry)

    @property
    def available(self) -> bool:
        return self.coordinator._updates_enabled

    @property
    def is_on(self) -> bool:
        """Return True if entity is on."""
        return bool(getattr(self.coordinator.config, self.key, False))

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
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
            # Wessen Wert dieser Schalter schreibt, dessen Timer besitzt er auch.
            self.coordinator.cancel_option_timer(sibling_key)
            for entity in self.platform.entities.values():
                if isinstance(entity, TempixSwitch) and entity.key == sibling_key:
                    entity.async_write_ha_state()
                    break

        setattr(self.coordinator.config, self.key, True)
        # Ein Schalter-Klick ist eine DAUERHAFTE Ansage. Lief noch ein Timer aus
        # `set_party_mode`/`set_guest_mode` mit Dauer, wuerde er sie spaeter
        # ueberstimmen — hier faellt er weg.
        self.coordinator.cancel_option_timer(self.key)
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
        # Siehe `async_turn_on`: der Schalter uebernimmt den Key, ein laufender
        # Dauer-Timer darf ihn spaeter nicht mehr anfassen.
        self.coordinator.cancel_option_timer(self.key)
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
        self._attr_device_info = tempix_device_info(entry)

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
        activated = self.coordinator.activate_temporary_manual_override(
            new_temp=temp,
            new_hvac=hvac,
            source="ui",
            now=datetime.now(UTC)
        )
        if not activated:
            # AU-5: Feature 'enable_temporary_manual_override' ist aus. Vorher
            # blieb der Schalter still auf "aus", und das Log nannte nur
            # "Manual TRV change ignored". Jetzt bekommt der Nutzer die Ursache.
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="override_disabled",
                translation_placeholders={"name": self.entry.title},
            )
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Clear the temporary override."""
        self.coordinator.clear_temporary_manual_override()
        # Write state immediately — the listener roundtrip via async_update can
        # end in an uncertainty early-return before listeners are notified,
        # which would leave the switch visually ON.
        self.async_write_ha_state()
