"""
Tempix – Temperature Mixin.

Comfort / eco / window temperature resolution and the main
``calculate_target_temperature`` chain.
"""
from __future__ import annotations

from custom_components.tempix.const import DEFAULT_VACATION_TEMP


class TemperatureMixin:
    """Temperature resolution – comfort, eco, window, target chain."""

    # ── simple resolvers ─────────────────────────────────────────────────────

    def resolve_comfort_temperature(self) -> float:
        """Return the active comfort temperature (calendar override → static)."""
        overrides = self.get_calendar_overrides()
        if "comfort" in overrides:
            return overrides["comfort"]
        return self.config.temp_comfort_static

    def resolve_eco_temperature(self) -> float:
        """Return the active eco temperature (calendar override → static)."""
        overrides = self.get_calendar_overrides()
        if "eco" in overrides:
            return overrides["eco"]
        return self.config.temp_eco_static

    def resolve_window_open_temperature(self) -> float:
        """Return the configured window-open temperature."""
        return self.config.window_open_temp

    # ── sunshine offset ──────────────────────────────────────────────────────

    def is_sunshine_offset_active(self) -> bool:
        """Return ``True`` if the weather state currently qualifies for sunshine offset."""
        if self.config.sunshine_offset:
            weather_eid = self.config.weather_entity
            if weather_eid:
                w_state = self._get_state(weather_eid)
                if w_state and w_state.state == "sunny":
                    return True
        return False

    def get_sunshine_offset(self) -> float:
        """Return the current sunshine offset (0.0 if inactive)."""
        if self.is_sunshine_offset_active():
            return self.config.sunshine_offset_value
        return 0.0

    # ── target temperature chain ─────────────────────────────────────────────

    def calculate_target_temperature(self, _set_comfort: bool | None = None) -> float | None:
        """Full target-temperature chain. Returns ``None`` if data is uncertain."""
        comfort = self.resolve_comfort_temperature()
        eco = self.resolve_eco_temperature()

        if self.is_frost_protection():
            return self.config.frost_protection_temp

        idle_temp = self.config.idle_temperature
        if not self.is_automation_active():
            return idle_temp if idle_temp > 0 else 0.0

        window_open_status = self.is_window_open()
        if window_open_status is None:
            return None

        window_temp = self.resolve_window_open_temperature()
        if window_open_status:
            if self.config.frost_protection_enabled:
                frost_min = self.config.frost_protection_temp
                return max(frost_min, window_temp) if window_temp > 0 else frost_min
            return window_temp if window_temp > 0 else 0.0

        is_vacation, vacation_temp = self.is_vacation_mode()
        if is_vacation:
            vt = vacation_temp if vacation_temp is not None else DEFAULT_VACATION_TEMP
            if self.config.frost_protection_enabled:
                return max(self.config.frost_protection_temp, vt)
            return vt

        is_party, party_temp = self.check_party_mode()
        if is_party:
            return party_temp if party_temp is not None else comfort

        adj = self.get_active_adjustment()
        adj_comfort = self.get_adjustment_comfort(adj)
        adj_eco = self.get_adjustment_eco(adj)
        entry_mode = self.get_adjustment_mode(adj)

        eff_comfort = adj_comfort if adj_comfort is not None else comfort
        eff_eco = adj_eco if adj_eco is not None else eco

        set_comfort = _set_comfort if _set_comfort is not None else self.should_set_comfort(entry_mode)
        if set_comfort is None:
            return None

        target = eff_comfort if set_comfort else eff_eco

        if set_comfort and self.is_away():
            away_offset = self.config.away_offset
            target = eff_comfort - away_offset

        # Sunshine Offset – only in comfort mode, only when sunny
        if set_comfort and self.is_sunshine_offset_active():
            offset = self.config.sunshine_offset_value
            target -= offset
            self.debug_log(f"Sunshine Offset: target -{offset}°C")

        return target
