"""
Tempix – Calibration Mixin.

Per-valve changes, calibration calculations (generic, Tado, offset),
valve positioning, and related entity discovery.
"""
from __future__ import annotations

import math
import logging
from datetime import datetime, timedelta, UTC
from typing import Any, cast

from homeassistant.helpers import (
    entity_registry as er_helper,
    device_registry as dr_helper,
)

from custom_components.tempix.const import (
    CALIBRATION_MODE_OFF,
    CALIBRATION_MODE_GENERIC,
    AGGRESSIVE_MODE_TARGET,
    AGGRESSIVE_MODE_CALIBRATION,
    DEFAULT_MIN_TEMP,
    DEFAULT_MAX_TEMP,
    DEFAULT_ROOM_TEMP_FALLBACK,
    DEFAULT_CALIBRATION_KEEPALIVE,
    TADO_MIN_OFFSET,
    TADO_MAX_OFFSET,
    INVALID_STATES,
)

_LOGGER = logging.getLogger(__name__)


class CalibrationMixin:
    """Per-valve changes, calibration, valve positioning."""

    # ── per-valve changes ────────────────────────────────────────────────────

    def calculate_changes(
        self,
        last_generic_offsets: dict[str, float] | None = None,
        _target_temp: float | None = None,
        _hvac_mode: str | None = None,
    ) -> tuple[list[dict], dict[str, float]]:
        """Compute per-valve target with aggressive + generic calibration."""
        trvs = self.config.trvs
        target_temp = _target_temp if _target_temp is not None else self.calculate_target_temperature()
        hvac_mode = _hvac_mode if _hvac_mode is not None else self.calculate_hvac_mode()

        if target_temp is None or hvac_mode is None:
            reasons = self.get_uncertainty_reasons()
            self.debug_log(f"Uncertainty detected ({reasons}). Aborting changes.")
            return [], last_generic_offsets or {}

        target_temp = cast(float, target_temp)
        hvac_mode = cast(str, hvac_mode)

        is_force_comfort = self.is_force_comfort_temp() or self.is_liming_time()
        min_not_off = self.config.min_instead_of_off
        window_temp = self.resolve_window_open_temperature()
        aggressive_range: float = self.config.aggressive_range
        aggressive_offset: float = self.config.aggressive_offset
        agg_mode = self.config.aggressive_mode_selector
        is_aggressive = aggressive_offset > 0 and agg_mode == AGGRESSIVE_MODE_TARGET
        is_aggressive_calib = agg_mode == AGGRESSIVE_MODE_CALIBRATION
        cal_mode = self.config.calibration_mode
        delta: float = self.config.calibration_delta
        generic_calib = cal_mode == CALIBRATION_MODE_GENERIC
        generic_limit: float = self.config.generic_calibration_limit
        off_above = self.config.off_if_above_room_temp
        room_temp = self._resolve_room_temp()
        ref_room_temp = room_temp if room_temp is not None else DEFAULT_ROOM_TEMP_FALLBACK

        changes: list[dict] = []
        generic_offsets: dict[str, float] = {}

        room_sensor_id = self.config.temp_sensor
        if room_sensor_id:
            room_state = self._get_state(room_sensor_id)
            if not room_state or room_state.state in INVALID_STATES:
                self.debug_log(f"Skipping changes because room sensor {room_sensor_id} is {room_state.state if room_state else 'None'}")
                return [], last_generic_offsets or {}

        self.debug_log(f"Calculating changes. Target: {target_temp}, Mode: {hvac_mode}")

        for trv_id in trvs:
            state = self._get_state(trv_id)
            if not state:
                continue
            if state.state in INVALID_STATES:
                self.debug_log(f"Skipping {trv_id} because state is {state.state}")
                continue

            cur_mode = state.state
            cur_temp = state.attributes.get("temperature")
            cur_valve_temp = state.attributes.get("current_temperature")
            min_temp = state.attributes.get("min_temp", DEFAULT_MIN_TEMP)
            max_temp = state.attributes.get("max_temp", DEFAULT_MAX_TEMP)
            hvac_modes = state.attributes.get("hvac_modes", [])

            valve_temp = target_temp
            valve_mode = hvac_mode

            has_off = "off" in [m.lower() for m in hvac_modes] if hvac_modes else True

            ref_temp = room_temp if room_temp is not None else (cur_valve_temp if cur_valve_temp else DEFAULT_ROOM_TEMP_FALLBACK)

            # Aggressive mode (non-calibration)
            if is_aggressive and hvac_mode != "off":
                temp_diff = valve_temp - ref_temp
                limit_neg = 0 - aggressive_range
                self.debug_log(f"Aggressive active. valve={valve_temp}, ref={ref_temp}, diff={temp_diff}, range={aggressive_range}")
                if temp_diff * self._factor < limit_neg:
                    valve_temp -= aggressive_offset * self._factor
                elif temp_diff * self._factor > aggressive_range:
                    valve_temp += aggressive_offset * self._factor
                self.debug_log(f"Final valve_temp={valve_temp}")

            # Step size
            calib_step = self.config.calibration_step_size
            if calib_step == "full":
                step = 1.0
            elif calib_step == "0.5":
                step = 0.5
            elif calib_step == "0.1":
                step = 0.1
            else:
                step = state.attributes.get("target_temp_step", 0.5)
                step = float(step) if step else 0.5

            decimal_places = len(str(step).split(".")[-1]) if "." in str(step) else 0

            # Generic calibration
            if generic_calib and cur_valve_temp is not None:
                offset = cur_valve_temp - ref_temp

                if is_aggressive_calib:
                    temp_diff = valve_temp - ref_temp
                    if temp_diff * self._factor < -aggressive_range:
                        offset -= aggressive_offset * self._factor
                    elif temp_diff * self._factor > aggressive_range:
                        offset += aggressive_offset * self._factor

                limit_neg_gen = 0 - generic_limit
                offset = max(limit_neg_gen, min(generic_limit, offset))

                last_off = last_generic_offsets.get(trv_id, 0) if last_generic_offsets else 0
                if abs(offset - last_off) < delta:
                    offset = last_off

                offset = self._round_to_step(offset, step)
                generic_offsets[trv_id] = offset
                valve_temp += offset

            # Total Offset Clamping
            max_offset = 6.0
            total_dev = valve_temp - target_temp
            if abs(total_dev) > max_offset:
                valve_temp = target_temp + (max_offset if total_dev > 0 else -max_offset)

            # Final Rounding
            valve_temp = self._round_to_step(valve_temp, step)

            # Mode Overrides
            if off_above and hvac_mode != "off":
                hysteresis = self.config.hysteresis
                is_currently_off = cur_mode == "off" or (cur_temp is not None and cur_temp <= min_temp + 0.1 and min_not_off)

                if not is_currently_off:
                    if self._factor == 1 and (ref_temp > (target_temp + hysteresis) or math.isclose(ref_temp, target_temp + hysteresis)):
                        valve_mode = "off"
                    elif self._factor == -1 and (ref_temp < (target_temp - hysteresis) or math.isclose(ref_temp, target_temp - hysteresis)):
                        valve_mode = "off"
                else:
                    if self._factor == 1 and (ref_temp > (target_temp - hysteresis) and not math.isclose(ref_temp, target_temp - hysteresis)):
                        valve_mode = "off"
                    elif self._factor == -1 and (ref_temp < (target_temp + hysteresis) and not math.isclose(ref_temp, target_temp + hysteresis)):
                        valve_mode = "off"

            dont_turn_off = (
                not has_off or min_not_off
                or (self.is_window_open() and window_temp > 0)
                or is_force_comfort
            )

            if valve_mode == "off" and dont_turn_off:
                valve_mode = cur_mode
                valve_temp = min_temp

            if is_force_comfort:
                valve_temp = max_temp

            valve_temp = max(min_temp, min(max_temp, round(valve_temp, 1)))

            if cur_mode != valve_mode or cur_temp != valve_temp:
                changes.append({
                    "entity_id": trv_id,
                    "hvac_mode": valve_mode,
                    "temperature": valve_temp,
                })

        return changes, generic_offsets

    # ── valve positioning ────────────────────────────────────────────────────

    def calculate_valve_position(self, trv_id: str, target_temp: float) -> int:
        """Compute valve opening percentage for positioning mode."""
        mode = self.config.valve_mode
        if mode == "off":
            return 100

        room_temp = self._resolve_room_temp()
        if room_temp is None:
            state = self._get_state(trv_id)
            room_temp = state.attributes.get("current_temperature") if state else None
        if room_temp is None or target_temp is None:
            return 100

        diff = target_temp - room_temp
        max_diff = self.config.valve_diff
        valve_max = self.config.valve_max
        step = self.config.valve_step

        if diff >= max_diff:
            return valve_max
        if diff <= 0:
            return 0
        if max_diff <= 0:
            return valve_max

        opening_regular = (100 / max_diff) * diff
        if mode == "pessimistic":
            opening = math.sqrt(abs(opening_regular)) * 10
        elif mode == "optimistic":
            opening = (opening_regular ** 2) / 100
        else:
            opening = opening_regular

        opening_abs = (opening / 100) * valve_max
        if self.is_force_comfort_temp():
            return valve_max

        return int(((opening_abs + step / 2) // step) * step)

    # ── calibration calculation ──────────────────────────────────────────────

    def calculate_calibration(self, trv_id: str) -> dict | None:
        """Calibration changes for a single TRV. Returns dict with entity + value."""
        agg_mode = self.config.aggressive_mode_selector
        is_aggressive_calib = agg_mode == AGGRESSIVE_MODE_CALIBRATION
        cal_mode = self.config.calibration_mode

        if cal_mode == CALIBRATION_MODE_OFF and not is_aggressive_calib:
            return None
        if cal_mode == CALIBRATION_MODE_GENERIC:
            return None

        adj = self.get_active_adjustment()
        if not self.get_adjustment_calibration(adj):
            return None

        room_temp = self._resolve_room_temp()
        if room_temp is None:
            return None

        keyword = self.config.calibration_keyword
        delta = self.config.calibration_delta
        step_cfg = self.config.calibration_step_size
        aggressive_range = self.config.aggressive_range
        aggressive_offset = self.config.aggressive_offset
        is_aggressive = aggressive_offset > 0 and agg_mode == AGGRESSIVE_MODE_CALIBRATION

        state = self._get_state(trv_id)
        if not state:
            return None

        # Check if Tado device
        try:
            dev_reg = dr_helper.async_get(self.hass)
            ent_reg = er_helper.async_get(self.hass)
            entry = ent_reg.async_get(trv_id)
            if entry and entry.device_id:
                dev = dev_reg.async_get(entry.device_id)
                if dev and dev.manufacturer and "tado" in dev.manufacturer.lower():
                    return self._calculate_tado_calibration(
                        trv_id, state, room_temp, delta,
                        is_aggressive, aggressive_range, aggressive_offset,
                    )
        except Exception:
            pass

        # Non-Tado: find calibration number entity
        calib_entity = self._find_calibration_entity(trv_id, keyword)
        if not calib_entity:
            return None

        calib_state = self._get_state(calib_entity)
        if not calib_state:
            return None

        is_offset = ("offset" in calib_entity or "calibration" in calib_entity) and "external" not in calib_entity
        old_val = float(calib_state.state) if calib_state.state not in INVALID_STATES else 0
        calib_min = float(calib_state.attributes.get("min", -12 if is_offset else 0))
        calib_max = float(calib_state.attributes.get("max", 12 if is_offset else 1000))

        if is_offset:
            thermostat_temp = state.attributes.get("current_temperature")
            if thermostat_temp is None:
                return None
            new_val = -(float(thermostat_temp) - room_temp) + old_val
        else:
            new_val = room_temp

        # Aggressive calibration
        if is_aggressive:
            target = state.attributes.get("temperature", self.calculate_target_temperature())
            temp_diff = float(target) - room_temp
            if temp_diff * self._factor < -aggressive_range:
                new_val += aggressive_offset * self._factor
            elif temp_diff * self._factor > aggressive_range:
                new_val -= aggressive_offset * self._factor

        # Rounding
        step = calib_state.attributes.get("step")
        if step is None:
            if step_cfg == "full":
                step = 1.0
            elif step_cfg == "0.5":
                step = 0.5
            elif step_cfg == "0.1":
                step = 0.1
            else:
                step = state.attributes.get("target_temp_step", 0.5)

        step = float(step) if step else 0.5

        if step <= 1 and calib_max < 1000:
            new_val = self._round_to_step(new_val, step)
        else:
            new_val = int(new_val * 100)

        new_val = max(calib_min, min(calib_max, new_val))

        if abs(old_val - new_val) < delta:
            if not is_offset:
                try:
                    last_upd = self._ensure_utc(calib_state.last_updated)
                    if datetime.now(UTC) - last_upd >= timedelta(minutes=DEFAULT_CALIBRATION_KEEPALIVE):
                        pass
                    else:
                        return None
                except Exception:
                    return None
            else:
                return None

        select_entity = self._find_external_select(trv_id)

        return {
            "calibration_entity": calib_entity,
            "value": new_val,
            "old_value": old_val,
            "is_offset": is_offset,
            "select_entity": select_entity,
        }

    def _calculate_tado_calibration(
        self, trv_id: str, state: Any, room_temp: float, delta: float,
        is_aggressive: bool, aggressive_range: float, aggressive_offset: float,
    ) -> dict | None:
        """Tado-specific calibration via ``tado.set_climate_temperature_offset``."""
        old_offset = float(state.attributes.get("offset_celsius", 0))
        local_temp = float(state.attributes.get("current_temperature", room_temp))
        new_offset = -(local_temp - room_temp) + old_offset

        if is_aggressive:
            target = state.attributes.get("temperature", self.calculate_target_temperature())
            temp_diff = float(target) - room_temp
            if temp_diff * self._factor < -aggressive_range:
                new_offset += aggressive_offset * self._factor
            elif temp_diff * self._factor > aggressive_range:
                new_offset -= aggressive_offset * self._factor

        new_offset = max(TADO_MIN_OFFSET, min(TADO_MAX_OFFSET, round(new_offset, 1)))

        if abs(old_offset - new_offset) < delta:
            return None

        return {
            "tado": True,
            "entity_id": trv_id,
            "value": new_offset,
            "old_value": old_offset,
        }

    def _find_calibration_entity(self, trv_id: str, keyword: str) -> str | None:
        """Find the calibration number entity on the same device as *trv_id*."""
        try:
            ent_reg = er_helper.async_get(self.hass)
            entry = ent_reg.async_get(trv_id)
            if not entry or not entry.device_id:
                return None
            for e in ent_reg.entities.values():
                if (e.device_id == entry.device_id
                        and e.domain == "number"
                        and keyword in e.entity_id):
                    return e.entity_id
        except Exception:
            pass
        return None

    def _find_external_select(self, trv_id: str) -> str | None:
        """Find external temperature sensor select/switch entity for calibration."""
        try:
            ent_reg = er_helper.async_get(self.hass)
            entry = ent_reg.async_get(trv_id)
            if not entry or not entry.device_id:
                return None
            for e in ent_reg.entities.values():
                if e.device_id != entry.device_id:
                    continue
                if e.domain == "select":
                    state = self.hass.states.get(e.entity_id)
                    if state and "external" in (state.attributes.get("options", []) or []):
                        return e.entity_id
                elif e.domain == "switch" and "external_temperature_sensor" in e.entity_id:
                    return e.entity_id
        except Exception:
            pass
        return None
