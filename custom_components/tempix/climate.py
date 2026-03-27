"""Tempix – Virtual Climate entity."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

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
    async_add_entities([TempixClimate(data["coordinator"], entry)])


class TempixClimate(ClimateEntity, RestoreEntity):
    """Virtual climate wrapper presenting the Tempix computed state."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_climate"
        self._attr_name = None  # content: sensor has name, device has name
        self._attr_translation_key = "tempix"

        # Device Info for UI grouping
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": self._entry.title,
            "manufacturer": "panhans / Martin Müller",
            "model": "Tempix",
            "sw_version": VERSION,
        }

        # Supported HVAC modes
        modes = {HVACMode.OFF}
        for attr in ("hvac_mode_comfort", "hvac_mode_eco"):
            m = getattr(coordinator.config, attr, "heat")
            if m in ("heat", "cool", "heat_cool", "auto"):
                modes.add(HVACMode(m))
        self._attr_hvac_modes = list(modes)
        self._attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE

        # Temperature Unit
        self._attr_temperature_unit = coordinator.hass.config.units.temperature_unit
        self._attr_min_temp = 5.0
        self._attr_max_temp = 32.0
        self._attr_target_temperature_step = 0.5

        # Restore placeholders (filled in async_added_to_hass)
        self._restored_hvac_mode: HVACMode | None = None
        self._restored_target_temp: float | None = None
        self._restored_current_temp: float | None = None

    async def async_added_to_hass(self) -> None:
        """Restore last known state on startup to avoid 'Unknown' flash."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            try:
                self._restored_hvac_mode = HVACMode(last_state.state)
            except ValueError:
                pass
            if ATTR_TEMPERATURE in last_state.attributes:
                try:
                    self._restored_target_temp = float(last_state.attributes[ATTR_TEMPERATURE])
                except (ValueError, TypeError):
                    pass
            if "current_temperature" in last_state.attributes:
                try:
                    self._restored_current_temp = float(last_state.attributes["current_temperature"])
                except (ValueError, TypeError):
                    pass

    @property
    def hvac_mode(self) -> HVACMode:
        m = self._coordinator.current_hvac
        if m:
            try:
                return HVACMode(m)
            except ValueError:
                pass
        if self._restored_hvac_mode is not None:
            return self._restored_hvac_mode
        return HVACMode.OFF

    @property
    def target_temperature(self) -> float | None:
        """Return the target temperature, falling back to restored value."""
        return self._coordinator.current_temperature if self._coordinator.current_temperature is not None else self._restored_target_temp

    @property
    def current_temperature(self) -> float | None:
        """Return the actual room temperature, falling back to restored value."""
        temp = self._coordinator.engine._resolve_room_temp()
        return temp if temp is not None else self._restored_current_temp

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # Helper to safely get config values
        cfg = self._coordinator.config
        engine = self._coordinator.engine

        return {
            "reason": self._coordinator.current_reason,
            "target_temperature_calculated": self._coordinator.current_temperature,
            "hvac_mode_calculated": self._coordinator.current_hvac,
            "automation_active": engine.is_automation_active(),
            "manual_override_pause": engine.config.manual_override_pause,
            "season_mode": engine.is_season_mode(),
            "is_away": engine.is_away(),
            "is_party": engine.check_party_mode()[0],
            "outside_temp_ok": engine.check_outside_threshold(),
            "frost_protection_active": engine.is_frost_protection(),
            "calibration_offsets": self._coordinator.last_calibrations,
            "valve_targets": {
                c["entity_id"]: c["temperature"] for c in self._coordinator.last_changes if "temperature" in c
            },
            "next_schedule_transition": engine.get_next_schedule_transition(),
        }



    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Not directly controllable – state is computed."""
        _LOGGER.info("HVAC mode set request ignored – state is computed by Tempix engine.")

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Not directly controllable – state is computed."""
        _LOGGER.info("Temperature set request ignored – state is computed by Tempix engine.")
