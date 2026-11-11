"""Tempix – Selects."""
from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.tempix.const import (
    DOMAIN,
    CONF_NAME,
    CONF_HVAC_MODE_COMFORT,
    CONF_HVAC_MODE_ECO,
)
from custom_components.tempix.device import tempix_device_info
from custom_components.tempix.hvac_modes import (
    SCHEDULE_HVAC_MODE_ORDER,
    common_schedule_hvac_modes,
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
        TempixHvacModeSelect(
            coordinator, entry,
            CONF_HVAC_MODE_COMFORT, None,
            list(SCHEDULE_HVAC_MODE_ORDER)
        ),
        TempixHvacModeSelect(
            coordinator, entry,
            CONF_HVAC_MODE_ECO, None,
            list(SCHEDULE_HVAC_MODE_ORDER)
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
        self._restored_option: str | None = None

        self._attr_device_info = tempix_device_info(self._entry)

    @property
    def available(self) -> bool:
        return self._coordinator._updates_enabled

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
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in ("unknown", "unavailable"):
            if last_state.state in self._attr_options:
                self._restored_option = last_state.state
        self.async_on_remove(
            self._coordinator.async_add_listener(self.async_write_ha_state)
        )


class TempixHvacModeSelect(TempixSelect):
    """Comfort-/Eco-Modus: nur Modi, die die Thermostate des Raums koennen.

    Die Liste war fest ``heat/cool/heat_cool/auto/off``. ``auto`` auf einem
    ``heat``/``off``-Thermostat fuehrte im Dispatcher zum typisierten Fallback
    auf ``off`` — Geraet aus, Status "Comfort", keine Warnung. Der Options-Dialog
    filterte schon nach Geraetefaehigkeit; hier gilt jetzt dieselbe Funktion
    (``hvac_modes.common_schedule_hvac_modes``).

    Ein bereits gespeicherter Wert bleibt immer waehlbar: ein kurz
    ``unavailable``es Geraet oder ein Update, das ``hvac_modes`` verengt, darf
    ihn nicht aus der Liste werfen — HA zeigt eine Auswahl, deren Wert nicht in
    ``options`` steht, als "unbekannt".

    ``options`` ist eine Property und wird bei jedem State-Write neu berechnet;
    nach einem Neustart sind die Thermostate oft noch nicht da.
    """

    @property
    def options(self) -> list[str]:
        modes = common_schedule_hvac_modes(
            self.hass, list(self._coordinator.config.trvs or []))
        current = self.current_option
        if current and current not in modes:
            modes = [*modes, current]
        # Anzeigereihenfolge wie im Dialog; Unbekanntes (alter Wert) ans Ende.
        order = {m: i for i, m in enumerate(SCHEDULE_HVAC_MODE_ORDER)}
        return sorted(modes, key=lambda m: order.get(m, len(order)))
