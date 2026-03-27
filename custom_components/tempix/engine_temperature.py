"""
Tempix – Temperature Mixin.

Comfort / eco / window temperature resolution and the main
``calculate_target_temperature`` chain.
"""
from __future__ import annotations



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

    # ── weather anticipation ─────────────────────────────────────────────────

    def is_weather_anticipation_active(self) -> bool:
        """Return ``True`` if the weather state currently qualifies for anticipation."""
        if self.config.weather_anticipation:
            weather_eid = self.config.weather_entity
            if weather_eid:
                w_state = self._get_state(weather_eid)
                if w_state and w_state.state in ["sunny", "clear"]:
                    return True
        return False

    def get_weather_offset(self) -> float:
        """Return the current weather offset (0.0 if inactive)."""
        if self.is_weather_anticipation_active():
            return self.config.weather_offset
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

        # Weather Anticipation – only in comfort mode, only daytime states
        if self.config.weather_anticipation and set_comfort:
            weather_eid = self.config.weather_entity
            if weather_eid:
                w_state = self._get_state(weather_eid)
                if w_state and w_state.state in ["sunny", "clear"]:
                    offset = self.config.weather_offset
                    target -= offset
                    self.debug_log(f"Weather Anticipation: target -{offset}°C (state={w_state.state})")

        return target
