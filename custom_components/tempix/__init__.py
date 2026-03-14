"""The Tempix integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from custom_components.tempix.const import (
    DOMAIN, PLATFORMS,
    CONF_GUEST_MODE_SWITCH, CONF_PARTY_MODE_SWITCH,
    CONF_AGGRESSIVE_MODE, CONF_AGGRESSIVE_CALIBRATION_SWITCH, CONF_AGGRESSIVE_MODE_SELECTOR,
    AGGRESSIVE_MODE_OFF, AGGRESSIVE_MODE_TARGET, AGGRESSIVE_MODE_CALIBRATION,
    CONF_CALIBRATION_MODE, CALIBRATION_MODE_OFF, CALIBRATION_MODE_NATIVE, CALIBRATION_MODE_GENERIC,
    CONF_CALIBRATION_ENABLED, CONF_CALIBRATION_GENERIC,
    CONF_TRVS, CONF_TEMPERATURE_SENSOR, CONF_OUTSIDE_TEMP_SENSOR,
    CONF_WINDOW_SENSORS, CONF_PERSONS, CONF_SCHEDULERS,
    CONF_SCHEDULER_SELECTOR, CONF_PRESENCE_SENSOR,
    CONF_SEASON_MODE_ENTITY, CONF_CALENDAR, CONF_PROXIMITY_ENTITY,
)
from custom_components.tempix.config_model import TempixConfig
from custom_components.tempix.engine import TempixEngine
from custom_components.tempix.coordinator import TempixCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tempix from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # 1. Configuration Migration (Phase 44, 49)
    # Check for missing keys or legacy boolean flags before initializing full motor
    # Use merged config (data + options) so new entries (where everything is in data)
    # don't falsely trigger migration and skip platform setup.
    merged_config = {**entry.data, **entry.options}
    changed = False
    new_options = dict(entry.options)
    
    # aggressive mode selector migration – skip if already present in data or options
    if CONF_AGGRESSIVE_MODE_SELECTOR not in merged_config:
        mode = AGGRESSIVE_MODE_OFF
        if merged_config.get(CONF_AGGRESSIVE_CALIBRATION_SWITCH):
            mode = AGGRESSIVE_MODE_CALIBRATION
        elif merged_config.get(CONF_AGGRESSIVE_MODE):
            mode = AGGRESSIVE_MODE_TARGET
        
        new_options[CONF_AGGRESSIVE_MODE_SELECTOR] = mode
        new_options.pop(CONF_AGGRESSIVE_MODE, None)
        new_options.pop(CONF_AGGRESSIVE_CALIBRATION_SWITCH, None)
        changed = True

    # calibration mode selector migration – skip if already in data or options
    if CONF_CALIBRATION_MODE not in merged_config:
        cal_mode = CALIBRATION_MODE_OFF
        if merged_config.get(CONF_CALIBRATION_GENERIC):
            cal_mode = CALIBRATION_MODE_GENERIC
        elif merged_config.get(CONF_CALIBRATION_ENABLED, False):
            cal_mode = CALIBRATION_MODE_NATIVE
        
        new_options[CONF_CALIBRATION_MODE] = cal_mode
        new_options.pop(CONF_CALIBRATION_ENABLED, None)
        new_options.pop(CONF_CALIBRATION_GENERIC, None)
        changed = True
            
    if changed:
        _LOGGER.info("Migrating %s config for new switch keys/defaults. Triggering reload.", entry.entry_id)
        hass.config_entries.async_update_entry(entry, options=new_options)
        # Return True early. HA will reload the entry because options changed, 
        # and we don't want to initialize the full motor with stale data.
        return True

    config = TempixConfig.from_dict({**entry.data, **entry.options})

    # 2. Initialise Engine (pure logic) and Coordinator (state management)
    engine = TempixEngine(hass, config)
    coordinator = TempixCoordinator(hass, config, engine, entry_id=entry.entry_id)

    try:
        await coordinator.async_setup()
    except Exception as err:
        _LOGGER.error("Error setting up Tempix: %s", err)
        raise ConfigEntryNotReady from err

    # Re-load on option change
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    hass.data[DOMAIN][entry.entry_id] = {
        "engine": engine,
        "coordinator": coordinator,
    }

    # Register services (Phase 65)
    async def handle_trigger_update(call: ServiceCall) -> None:
        """Force a recalculation for one or all relevant instances."""
        target_entry = call.data.get("config_entry_id")
        
        for entry_id, data in hass.data[DOMAIN].items():
            if target_entry and entry_id != target_entry:
                continue
            if isinstance(data, dict) and "coordinator" in data:
                _LOGGER.debug("Triggering manual update for coordinator %s", entry_id)
                await data["coordinator"].async_update()

    async def handle_set_status(call: ServiceCall) -> None:
        """Handle set_party_mode and set_guest_mode with optional duration."""
        status = call.data["status"]
        duration = call.data.get("duration")
        service = call.service
        key_map = {
            "set_party_mode": CONF_PARTY_MODE_SWITCH,
            "set_guest_mode": CONF_GUEST_MODE_SWITCH,
        }
        config_key = key_map.get(service)
        if config_key:
            for entry_id, data in hass.data[DOMAIN].items():
                if isinstance(data, dict) and "coordinator" in data:
                    await data["coordinator"].async_set_temporary_option(
                        config_key, status, duration
                    )

    hass.services.async_register(DOMAIN, "trigger_update", handle_trigger_update)
    hass.services.async_register(DOMAIN, "set_party_mode", handle_set_status)
    hass.services.async_register(DOMAIN, "set_guest_mode", handle_set_status)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    _LOGGER.info("Unloading Tempix entry: %s", entry.entry_id)
    data = hass.data[DOMAIN].get(entry.entry_id)
    if data and "coordinator" in data:
        await data["coordinator"].async_unload()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry when options are updated."""
    # Updated config
    new_config = {**entry.data, **entry.options}
    
    # Identify changed keys
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data or "engine" not in data:
        _LOGGER.debug("Reload listener triggered but data not yet initialized for %s", entry.entry_id)
        return

    old_raw = data["engine"].config._raw  # Original dict for comparison

    def _norm(v: Any) -> Any:
        """Treat None, empty string, and missing key as equivalent."""
        return None if v in (None, "", "None") else v

    changed_keys = {
        k for k in (set(new_config) | set(old_raw))
        if not k.startswith("_") and _norm(new_config.get(k)) != _norm(old_raw.get(k))
    }

    # List of keys that REQUIRE a full reload (structural changes)
    RELOAD_REQUIRED_KEYS = {
        CONF_TRVS,
        CONF_TEMPERATURE_SENSOR,
        CONF_OUTSIDE_TEMP_SENSOR,
        CONF_WINDOW_SENSORS,
        CONF_PERSONS,
        CONF_SCHEDULERS,
        CONF_SCHEDULER_SELECTOR,
        CONF_PRESENCE_SENSOR,
        CONF_SEASON_MODE_ENTITY,
        CONF_CALENDAR,
        CONF_PROXIMITY_ENTITY,
    }

    # If no keys actually changed, skip
    if not changed_keys:
        return

    # If no reload-required key was changed, do a dynamic update
    if not (changed_keys & RELOAD_REQUIRED_KEYS):
        _LOGGER.info("Dynamic update for %s. Keys: %s", entry.title, changed_keys)
        typed_config = TempixConfig.from_dict(new_config)
        data["engine"].config = typed_config
        data["coordinator"].config = typed_config
        
        # Force a coordinator update to apply changes immediately
        await data["coordinator"].async_update()
        return

    # Otherwise, full reload
    _LOGGER.warning("Full reload required for %s due to changes in: %s", entry.title, (changed_keys & RELOAD_REQUIRED_KEYS) or changed_keys)
    await hass.config_entries.async_reload(entry.entry_id)
