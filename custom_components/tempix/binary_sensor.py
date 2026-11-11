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

from custom_components.tempix.const import (
    DOMAIN,
)
from custom_components.tempix.device import tempix_device_info

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
            "season_mode",
            "mdi:snowflake", None,
            # season_mode ist ein eigenes Snapshot-Feld (Default True bei fehlendem Snapshot,
            # V1s eigener Vertrag — siehe domain_resolvers.resolve_is_season_mode).
            lambda e, c=coordinator: (
                c._v2_last_snapshot.season_mode if c._v2_last_snapshot else True
            )
        ),
        TempixBinarySensor(
            coordinator, engine, entry,
            "anybody_home",
            "mdi:home-account", None,
            # 1:1-Snapshot-Äquivalent, durch einen Paritätstest festgehalten.
            lambda e, c=coordinator: bool(
                c._v2_last_snapshot and c._v2_last_snapshot.anybody_home)
        ),
        TempixBinarySensor(
            coordinator, engine, entry,
            "window_open",
            "mdi:window-open", BinarySensorDeviceClass.WINDOW,
            # snapshot.window_open (der rohe, an den
            # Builder übergebene Wert) — NICHT window_open_resolved (das ist
            # der Builder-eigene, reboot-sichere Close-Delay-Wert und
            # bewusst ein anderer Wert, siehe `window_open_resolved` in
            # snapshot_builder.py).
            lambda e, c=coordinator: bool(
                c._v2_last_snapshot and c._v2_last_snapshot.window_open)
        ),
        TempixBinarySensor(
            coordinator, engine, entry,
            "away_mode",
            "mdi:home-export-outline", None,
            # 1:1-Snapshot-Äquivalent, durch einen Paritätstest festgehalten.
            lambda e, c=coordinator: bool(
                c._v2_last_snapshot and c._v2_last_snapshot.away)
        ),
        TempixBinarySensor(
            coordinator, engine, entry,
            "presence_active",
            "mdi:motion-sensor", None,
            # Snapshot-Feld (Snapshot.presence_active), in V2 nativ aufgelöst.
            lambda e, c=coordinator: bool(
                c._v2_last_snapshot and c._v2_last_snapshot.presence_active)
        ),

        TempixBinarySensor(
            coordinator, engine, entry,
            "proximity_arrived",
            "mdi:map-marker-radius", None,
            # Snapshot-Feld (Snapshot.proximity_arrived) — der rohe, weiterhin V1-gebrückte
            # Wert, nur jetzt über den Snapshot statt live gelesen.
            lambda e, c=coordinator: bool(
                c._v2_last_snapshot and c._v2_last_snapshot.proximity_arrived)
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
        self._attr_translation_key = key
        self._attr_icon = icon
        self._attr_device_class = device_class

        # Device Info for UI grouping
        self._attr_device_info = tempix_device_info(self._entry)

    @property
    def available(self) -> bool:
        return self._coordinator._updates_enabled

    @property
    def is_on(self) -> bool | None:
        try:
            return self._val_func(self._engine)
        except Exception as err:
            # AU-2: nicht still schlucken (DEBUG, läuft bei jedem State-Write).
            _LOGGER.debug(
                "TPX binary_sensor %s: is_on failed: %r",
                self._attr_translation_key, err,
            )
            return None

    async def async_added_to_hass(self) -> None:
        """Register callbacks."""
        self.async_on_remove(
            self._coordinator.async_add_listener(self.async_write_ha_state)
        )
