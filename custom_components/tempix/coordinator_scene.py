"""
Tempix – Scene Manager.

Saves and restores TRV states for window-open and party-mode scenarios.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

_STORAGE_VERSION = 1
_STORAGE_KEY = "tempix.scenes"


class SceneManager:
    """Persists and restores TRV scenes (window open / party mode)."""

    def __init__(
        self,
        hass: HomeAssistant,
        trvs: list[str],
        action_delay_secs: float,
        name: str,
    ) -> None:
        self._hass = hass
        self._trvs = trvs
        self._action_delay_secs = action_delay_secs
        self._name = name
        self._scenes: dict[str, dict] = {}
        self._store = Store(hass, _STORAGE_VERSION, _STORAGE_KEY)

    async def async_load(self) -> None:
        """Load previously persisted scenes from storage."""
        try:
            stored = await self._store.async_load()
            if stored:
                self._scenes = stored
        except Exception as exc:
            _LOGGER.warning("%s: Failed to load persistent scenes: %s", self._name, exc)

    def has_scene(self, key: str) -> bool:
        """Return True if a scene is saved under *key*."""
        return key in self._scenes

    @property
    def scenes(self) -> dict[str, dict]:
        """Read-only view of all saved scenes."""
        return self._scenes

    async def save(self, key: str) -> None:
        """Snapshot current TRV states under *key*."""
        snapshot: dict[str, dict[str, Any]] = {}
        for trv_id in self._trvs:
            state = self._hass.states.get(trv_id)
            if state:
                snapshot[trv_id] = {
                    "hvac_mode": state.state,
                    "temperature": state.attributes.get("temperature"),
                }
        self._scenes[key] = snapshot
        self._store.async_delay_save(lambda: self._scenes, 1.0)
        _LOGGER.debug("%s: saved scene '%s' with %d TRVs", self._name, key, len(snapshot))

    def clear(self, key: str) -> None:
        """Discard a saved scene without restoring it."""
        if self._scenes.pop(key, None) is not None:
            self._store.async_delay_save(lambda: self._scenes, 1.0)
            _LOGGER.debug("%s: cleared scene '%s' (discarded)", self._name, key)

    async def restore(self, key: str, service_caller) -> None:
        """Restore saved TRV states and remove the scene entry.

        Args:
            key: Scene key to restore (e.g. ``"window"``).
            service_caller: Async callable with signature
                ``(domain, service, service_data)`` – typically a bound
                partial of ``safe_service_call``.
        """
        snapshot = self._scenes.pop(key, {})
        self._store.async_delay_save(lambda: self._scenes, 1.0)

        for trv_id, data in snapshot.items():
            try:
                if data.get("hvac_mode"):
                    await service_caller(
                        "climate", "set_hvac_mode",
                        {"entity_id": trv_id, "hvac_mode": data["hvac_mode"]},
                    )
                if data.get("temperature") is not None:
                    await service_caller(
                        "climate", "set_temperature",
                        {"entity_id": trv_id, "temperature": data["temperature"]},
                    )
                if self._action_delay_secs > 0:
                    await asyncio.sleep(self._action_delay_secs)
            except Exception as exc:
                _LOGGER.warning(
                    "%s: restore '%s' failed for %s: %s",
                    self._name, key, trv_id, exc,
                )

        _LOGGER.debug("%s: restored scene '%s'", self._name, key)
