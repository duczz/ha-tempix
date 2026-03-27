"""
Tempix – TRV Appliers.

Contains:
  * ``safe_service_call``  – shared async helper with timeout + retry
  * ``CalibrationApplier`` – applies TRV / Tado calibration offsets
  * ``ValvePositioner``    – sets valve-position number entities
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, UTC
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound
from homeassistant.helpers import entity_registry as er_helper

from custom_components.tempix.config_model import TempixConfig
from custom_components.tempix.const import CALIBRATION_MODE_OFF, CALIBRATION_MODE_GENERIC

_LOGGER = logging.getLogger(__name__)


# ── shared service-call helper ────────────────────────────────────────────────

async def safe_service_call(
    hass: HomeAssistant,
    name: str,
    domain: str,
    service: str,
    service_data: dict[str, Any],
    *,
    timeout: int = 30,
    return_response: bool = False,
    max_retries: int = 2,
) -> Any:
    """Wrap hass.services.async_call with timeout and exponential-backoff retry.

    Retries up to *max_retries* times (2 s, 4 s back-off).
    Essential for FRITZ!DECT thermostats where the DECT radio bridge
    can temporarily be unable to reach devices.
    """
    entity_hint = service_data.get("entity_id", "?")
    total_attempts = max_retries + 1

    for attempt in range(1, total_attempts + 1):
        try:
            async with asyncio.timeout(timeout):
                return await hass.services.async_call(
                    domain, service, service_data,
                    blocking=True,
                    return_response=return_response,
                )
        except TimeoutError:
            if attempt < total_attempts:
                backoff = 2 ** attempt  # 2 s, 4 s
                _LOGGER.warning(
                    "%s: %s.%s timed out (attempt %d/%d) for %s – retry in %ds",
                    name, domain, service, attempt, total_attempts, entity_hint, backoff,
                )
                await asyncio.sleep(backoff)
            else:
                _LOGGER.error(
                    "%s: %s.%s timed out after %d attempts for %s – giving up",
                    name, domain, service, total_attempts, entity_hint,
                )
                return None
        except (HomeAssistantError, ServiceNotFound) as exc:
            _LOGGER.warning(
                "%s: %s.%s failed for %s: %s",
                name, domain, service, entity_hint, exc,
            )
            return None
    return None


# ── calibration applier ───────────────────────────────────────────────────────

class CalibrationApplier:
    """Applies TRV calibration (generic number entity or Tado offset)."""

    def __init__(
        self,
        hass: HomeAssistant,
        config: TempixConfig,
        engine: Any,
        name: str,
    ) -> None:
        self._hass = hass
        self._config = config
        self._engine = engine
        self._name = name
        self._last_calibration_time: dict[str, datetime] = {}
        self._calib_warned: set[str] = set()

    async def apply(self) -> dict[str, float]:
        """Apply calibration to all TRVs.

        Returns a mapping of ``trv_entity_id → calibration_value`` for every
        TRV that was actually calibrated in this cycle.
        """
        calibrations: dict[str, float] = {}
        timeout_delta = self._config.calibration_timeout
        now = datetime.now(UTC)

        for trv_id in self._config.trvs:
            last_t = self._last_calibration_time.get(trv_id)
            if last_t and (now - last_t) < timeout_delta:
                continue

            result = self._engine.calculate_calibration(trv_id)
            if not result:
                if (self._config.calibration_mode != CALIBRATION_MODE_OFF
                        and trv_id not in self._calib_warned):
                    if self._config.calibration_mode == CALIBRATION_MODE_GENERIC:
                        _LOGGER.debug(
                            "%s: No calibration entity found for %s – "
                            "entity write skipped (expected for TRVs like FRITZ!DECT). "
                            "Target temperature offset compensation is active via calculate_changes().",
                            self._name, trv_id,
                        )
                    else:
                        _LOGGER.warning(
                            "%s: No calibration number entity found for %s – "
                            "hardware sensor offset cannot be written to TRV. "
                            "Check that a calibration number entity exists for this TRV.",
                            self._name, trv_id,
                        )
                    self._calib_warned.add(trv_id)
                continue

            try:
                if result.get("tado"):
                    try:
                        await safe_service_call(
                            self._hass, self._name,
                            "tado", "set_climate_temperature_offset",
                            {"entity_id": result["entity_id"], "offset": result["value"]},
                        )
                        _LOGGER.debug(
                            "%s: Tado calibration for %s: %s → %s",
                            self._name, trv_id, result.get("old_value"), result["value"],
                        )
                    except (ServiceNotFound, ValueError) as err:
                        _LOGGER.warning(
                            "%s: Tado service call failed (integration missing?): %s",
                            self._name, err,
                        )
                else:
                    calib_eid = result["calibration_entity"]
                    value = result["value"]

                    # Switch external-sensor mode if required
                    select_eid = result.get("select_entity")
                    if select_eid:
                        sel_state = self._engine._get_state(select_eid)
                        if sel_state and sel_state.state != "external":
                            domain = select_eid.split(".")[0]
                            if domain == "select":
                                await safe_service_call(
                                    self._hass, self._name,
                                    "select", "select_option",
                                    {"entity_id": select_eid, "option": "external"},
                                )
                            elif domain == "switch":
                                await safe_service_call(
                                    self._hass, self._name,
                                    "switch", "turn_on",
                                    {"entity_id": select_eid},
                                )

                    await safe_service_call(
                        self._hass, self._name,
                        "number", "set_value",
                        {"entity_id": calib_eid, "value": value},
                    )

                self._last_calibration_time[trv_id] = now
                calibrations[trv_id] = result.get("value", 0)
                _LOGGER.debug(
                    "%s: calibration %s → %.2f (was %.2f)",
                    self._name, trv_id, result["value"], result.get("old_value", 0),
                )

            except Exception as exc:
                _LOGGER.warning(
                    "%s: calibration failed %s: %s", self._name, trv_id, exc
                )

        return calibrations


# ── valve positioner ──────────────────────────────────────────────────────────

class ValvePositioner:
    """Calculates and writes the valve-position percentage for each TRV."""

    def __init__(
        self,
        hass: HomeAssistant,
        config: TempixConfig,
        engine: Any,
        name: str,
    ) -> None:
        self._hass = hass
        self._config = config
        self._engine = engine
        self._name = name

    async def apply(self, target_temp: float | None) -> None:
        """Compute and write the valve position for every configured TRV."""
        if target_temp is None:
            return

        keyword = self._config.valve_keyword

        for trv_id in self._config.trvs:
            position = self._engine.calculate_valve_position(trv_id, target_temp)

            valve_entity = self._find_valve_entity(trv_id, keyword)
            if not valve_entity:
                continue

            cur = self._engine._get_state(valve_entity)
            if cur and cur.state not in (None, "unavailable", "unknown"):
                try:
                    if int(float(cur.state)) == position:
                        continue
                except (ValueError, TypeError):
                    pass

            try:
                await safe_service_call(
                    self._hass, self._name,
                    "number", "set_value",
                    {"entity_id": valve_entity, "value": position},
                )
                _LOGGER.debug(
                    "%s: valve %s → %d%%", self._name, valve_entity, position
                )
            except Exception as exc:
                _LOGGER.warning(
                    "%s: valve failed %s: %s", self._name, valve_entity, exc
                )

    def _find_valve_entity(self, trv_id: str, keyword: str) -> str | None:
        """Return the number entity that controls the valve for *trv_id*, or None."""
        try:
            er = er_helper.async_get(self._hass)
            entry = er.async_get(trv_id)
            if not entry or not entry.device_id:
                return None
            for e in er_helper.async_entries_for_device(er, entry.device_id):
                if e.domain == "number" and keyword in e.entity_id:
                    return e.entity_id
        except Exception:
            pass
        return None
