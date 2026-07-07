"""Tempix – Virtual Climate entity."""
from __future__ import annotations

import logging
from datetime import datetime, UTC
from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
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
        self._attr_name = None

        # Device Info for UI grouping
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": self._entry.title,
            "manufacturer": "Martin Müller",
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
        # TURN_OFF/TURN_ON are mandatory (HA 2024.2+) once OFF is in hvac_modes
        # and set_hvac_mode is implemented — without them HA logs a deprecation
        # warning that will become an error in future releases.
        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.TURN_ON
        )

        # Temperature Unit
        self._attr_temperature_unit = coordinator.hass.config.units.temperature_unit
        self._attr_min_temp = 5.0
        self._attr_max_temp = 32.0
        self._attr_target_temperature_step = 0.5

        # Restore placeholders (filled in async_added_to_hass)
        self._restored_hvac_mode: HVACMode | None = None
        self._restored_target_temp: float | None = None
        self._restored_current_temp: float | None = None

        # Optimistic UI State for Passthrough Mode
        self._optimistic_hvac_mode: HVACMode | None = None
        self._optimistic_target_temp: float | None = None

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
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # B1: Outside the pause/passthrough mode the coordinator IS the
        # authority — drop optimistic values on every update. Keeping them
        # until equality left the UI showing a stale wish forever when the
        # engine overruled it (frost clamp) or after the pause ended. This
        # also produces the ADR-019 "constraint bouncing": the user briefly
        # sees their input, then the engine's correction becomes visible.
        # DURING the pause the coordinator values are computed-but-ignored —
        # keep the optimistic state, it mirrors what the user last sent to
        # the hardware; it gets dropped on the first update after the pause.
        if not getattr(self._coordinator.config, "manual_override", False):
            self._optimistic_target_temp = None
            self._optimistic_hvac_mode = None

        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._coordinator._updates_enabled

    @property
    def hvac_mode(self) -> HVACMode:
        if self._optimistic_hvac_mode is not None:
            return self._optimistic_hvac_mode

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
        if self._optimistic_target_temp is not None:
            return self._optimistic_target_temp
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

        status = self._coordinator.current_reason
        if engine.config.manual_override:
            status = "Paused (Passthrough Mode Active)"

        return {
            "reason": status,
            "target_temperature_calculated": self._coordinator.current_temperature,
            "hvac_mode_calculated": self._coordinator.current_hvac,
            "automation_active": engine.is_automation_active(),
            "manual_override": engine.config.manual_override,
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
        """Handle HVAC mode changes from the UI."""
        if getattr(self._coordinator.config, "manual_override", False):
            # PASSTHROUGH MODE: Forward directly to ALL hardware TRVs.
            # Filter self-references to prevent an infinite loop.
            import asyncio
            target_trvs = [t for t in self._coordinator.config.trvs if t != self.entity_id]
            _LOGGER.debug(f"Manual Override active - Passthrough HVAC mode {hvac_mode} to {target_trvs}")
            tasks = [
                self.hass.services.async_call(
                    "climate", "set_hvac_mode", {"entity_id": trv, "hvac_mode": hvac_mode}, blocking=False
                )
                for trv in target_trvs
            ]
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for trv, result in zip(target_trvs, results):
                    if isinstance(result, Exception):
                        _LOGGER.warning("Passthrough set_hvac_mode failed for %s: %s", trv, result)
            # Optimistic UI update
            self._optimistic_hvac_mode = hvac_mode
            # B2 / ADR-019 deadlock guard: switching OFF→HEAT/COOL while no
            # target is known would make HA hide the temperature slider
            # permanently. Inject a fallback so the slider stays usable.
            if hvac_mode != HVACMode.OFF and self.target_temperature is None:
                self._optimistic_target_temp = 20.0
            self.async_write_ha_state()
            return

        if not getattr(self._coordinator.config, "enable_temporary_manual_override", False):
            _LOGGER.debug(
                "TPX [%s]: set_hvac_mode ignored – 'Enable Temporary Manual Override' is OFF. hvac_mode=%s",
                self._coordinator.config.name, hvac_mode,
            )
            return

        self._coordinator.activate_temporary_manual_override(
            new_temp=None,
            new_hvac=str(hvac_mode),
            source="ui",
            now=datetime.now(UTC)
        )
        # Optimistic UI update for immediate feedback
        self._optimistic_hvac_mode = hvac_mode
        # B2 / ADR-019 deadlock guard (see passthrough branch above)
        if hvac_mode != HVACMode.OFF and self.target_temperature is None:
            self._optimistic_target_temp = 20.0
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Handle temperature changes from the UI."""
        if getattr(self._coordinator.config, "manual_override", False):
            # PASSTHROUGH MODE: Forward directly to ALL hardware TRVs.
            # - Strip entity_id from kwargs: **kwargs would override the per-TRV entity_id
            #   we set explicitly, causing the service call to re-target the wrong entity.
            # - Filter self-references: prevents an infinite loop if this Tempix entity is
            #   accidentally listed in config.trvs.
            import asyncio
            target_trvs = [t for t in self._coordinator.config.trvs if t != self.entity_id]
            fwd_kwargs = {k: v for k, v in kwargs.items() if k != "entity_id"}
            _LOGGER.debug(f"Manual Override active - Passthrough temperature {fwd_kwargs} to {target_trvs}")
            tasks = [
                self.hass.services.async_call(
                    "climate", "set_temperature", {"entity_id": trv, **fwd_kwargs}, blocking=False
                )
                for trv in target_trvs
            ]
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for trv, result in zip(target_trvs, results):
                    if isinstance(result, Exception):
                        _LOGGER.warning("Passthrough set_temperature failed for %s: %s", trv, result)
            # Optimistic UI update
            if ATTR_TEMPERATURE in kwargs:
                self._optimistic_target_temp = float(kwargs[ATTR_TEMPERATURE])
            if ATTR_HVAC_MODE in kwargs:
                self._optimistic_hvac_mode = HVACMode(kwargs[ATTR_HVAC_MODE])
            self.async_write_ha_state()
            return

        if not getattr(self._coordinator.config, "enable_temporary_manual_override", False):
            _LOGGER.debug(
                "TPX [%s]: set_temperature ignored – 'Enable Temporary Manual Override' is OFF. kwargs=%s",
                self._coordinator.config.name, kwargs,
            )
            return
            
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return

        hvac_mode = kwargs.get(ATTR_HVAC_MODE)

        self._coordinator.activate_temporary_manual_override(
            new_temp=float(temp), 
            new_hvac=str(hvac_mode) if hvac_mode else None, 
            source="ui", 
            now=datetime.now(UTC)
        )
        # Optimistic UI update for immediate feedback
        self._optimistic_target_temp = float(temp)
        if hvac_mode:
            self._optimistic_hvac_mode = HVACMode(hvac_mode)
        self.async_write_ha_state()
