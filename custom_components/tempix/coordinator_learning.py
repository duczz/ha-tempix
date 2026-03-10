"""
Tempix – Heating Rate Learner.

Tracks temperature rise during comfort phases to adaptively learn
the room's heating rate (°C/h) via exponential moving average.
"""
from __future__ import annotations

import logging
from datetime import datetime, UTC
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.tempix.config_model import TempixConfig
from custom_components.tempix.const import CONF_LEARNED_HEATING_RATE

_LOGGER = logging.getLogger(__name__)


class HeatingRateLearner:
    """Learns and persists the room heating/cooling rate in °C/h."""

    def __init__(
        self,
        hass: HomeAssistant,
        config: TempixConfig,
        engine: Any,
        entry_id: str,
    ) -> None:
        self._hass = hass
        self._config = config
        self._engine = engine
        self._entry_id = entry_id
        self._heating_session: dict[str, Any] | None = None

    def _debug_log(self, msg: str) -> None:
        if self._config.debug_mode:
            _LOGGER.info("TPX Coord [%s]: %s", self._config.name, msg)
        else:
            _LOGGER.debug("TPX Coord [%s]: %s", self._config.name, msg)

    async def update(self, target_temp: float | None, hvac_mode: str) -> None:
        """Track temperature rise during comfort phases to learn room heating rate."""
        if target_temp is None:
            return

        now = datetime.now(UTC)
        current_temp = self._engine._resolve_room_temp()

        # 1. Start session detection
        is_active_mode = hvac_mode in ("heat", "cool")
        factor = self._engine._factor  # 1 = heating, -1 = cooling
        temp_away_from_target = (
            current_temp is not None
            and (target_temp - current_temp) * factor > 0.5
        )

        if is_active_mode and temp_away_from_target and self._engine.is_automation_active() and not self._engine.config.manual_override_pause:
            if self._heating_session is None:
                self._debug_log(
                    f"Learning: Starting heating session "
                    f"(temp={current_temp:.1f}°, target={target_temp:.1f}°)"
                )
                self._heating_session = {
                    "start_temp": current_temp,
                    "start_time": now,
                    "target_temp": target_temp,
                }
            elif abs(self._heating_session["target_temp"] - target_temp) > 0.3:
                # Target changed significantly – restart session
                self._heating_session["start_temp"] = current_temp
                self._heating_session["start_time"] = now
                self._heating_session["target_temp"] = target_temp

        # 2. End session detection & rate calculation
        elif self._heating_session is not None:
            start_temp = self._heating_session["start_temp"]
            start_time = self._heating_session["start_time"]

            duration_hours = (now - start_time).total_seconds() / 3600.0
            temp_diff = (current_temp or 0.0) - start_temp

            target_reached = (
                current_temp is not None
                and abs(current_temp - target_temp) <= 0.2
            )

            if target_reached or not is_active_mode:
                abs_diff = abs(temp_diff)
                if duration_hours > 0.25 and abs_diff > 0.5:
                    calc_rate = abs_diff / duration_hours

                    # Normalise measured rate to a reference outside temp (5 °C)
                    outside_temp = self._engine._resolve_outside_temp()
                    if outside_temp is not None:
                        ref_temp = 5.0
                        sensitivity = 0.04
                        norm_factor = 1.0 - (ref_temp - outside_temp) * sensitivity
                        norm_factor = max(0.3, min(2.0, norm_factor))
                        calc_rate = calc_rate / norm_factor

                    # Sanity bounds: 0.2 – 10 °C/h
                    if 0.2 <= calc_rate <= 10.0:
                        old_rate = self._config.learned_heating_rate
                        new_rate = (old_rate * 0.8) + (calc_rate * 0.2)  # EMA

                        self._debug_log(
                            f"Learning (T1): Session finished (Tout={outside_temp}°). "
                            f"Calculated normalized rate: {calc_rate:.2f}°C/h. "
                            f"Updating learned base rate: {old_rate:.2f} -> {new_rate:.2f}°C/h"
                        )

                        entry = self._hass.config_entries.async_get_entry(self._entry_id)
                        if entry and self._is_valid_rate(new_rate):
                            new_options = dict(entry.options)
                            new_options[CONF_LEARNED_HEATING_RATE] = round(new_rate, 2)
                            self._hass.config_entries.async_update_entry(
                                entry, options=new_options
                            )
                        elif entry:
                            self._debug_log(f"Learning: Validation failed for rate {new_rate}")
                    else:
                        self._debug_log(
                            f"Learning: Rate {calc_rate:.2f}°C/h rejected (out of sanity bounds)."
                        )
                else:
                    self._debug_log(
                        f"Learning: Session too short or insufficient rise "
                        f"(diff={temp_diff:.1f}°, dur={duration_hours:.2f}h)."
                    )

                self._heating_session = None

    @staticmethod
    def _is_valid_rate(value: Any) -> bool:
        """Return True if *value* is a plausible heating rate."""
        return isinstance(value, (int, float)) and value > 0
