"""
Tempix – Temperature Mixin.

Comfort / eco / window temperature resolution and the main
``calculate_target_temperature`` chain.
"""
from __future__ import annotations

from custom_components.tempix.const import DEFAULT_PARTY_TEMP, DEFAULT_VACATION_TEMP


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

    def calculate_target_temperature(self, _set_comfort: bool | None = None, _manual_override_temp: float | None = None) -> float | None:
        """Full target-temperature chain. Returns ``None`` if data is uncertain."""
        comfort = self.resolve_comfort_temperature()
        eco = self.resolve_eco_temperature()

        # Manual override takes priority over liming protection (is_liming_time is a
        # maintenance/comfort function, not a safety function like frost protection).
        # An OPEN WINDOW suppresses the override — heating/cooling against an
        # open window wastes energy no matter how deliberate the override was.
        # Suppressed, not cleared: closing the window lets it take effect again.
        # Window uncertainty (None) keeps the override — unavailable sensors must
        # not kill a deliberate user input.
        if _manual_override_temp is not None and self.is_window_open() is not True:
            return _manual_override_temp

        if self.is_liming_time():
            return comfort if comfort is not None else 21.0

        idle_temp = self.config.idle_temperature
        if not self.is_automation_active():
            return idle_temp if idle_temp > 0 else 0.0

        window_open_status = self.is_window_open()
        if window_open_status is None:
            return None

        window_temp = self.resolve_window_open_temperature()
        if window_open_status:
            # Frost protection is a HEATING-only concept (mirrors
            # calculate_hvac_mode): a cooling room must never chase the frost
            # minimum just because frost_protection_enabled happens to be True —
            # that would make the AC actively cool against an open window.
            if self.config.frost_protection_enabled and not self.is_cooling:
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
            # DEFAULT_PARTY_TEMP, not comfort — matches
            # what the Number entity shows when party_temperature is unset.
            return party_temp if party_temp is not None else DEFAULT_PARTY_TEMP

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
            behavior = self.config.away_behavior
            if behavior == "eco":
                target = eff_eco
            elif behavior == "offset":
                away_offset = self.config.away_offset
                if self.is_cooling:
                    target = eff_comfort + away_offset  # Energiesparen: wärmer werden lassen
                else:
                    target = eff_comfort - away_offset  # Energiesparen: kälter werden lassen

        # Sunshine Offset – only in comfort mode, only when sunny.
        # Solar-gain compensation → REDUCE the target in BOTH modes: heating
        # needs less (the sun helps warm the room), cooling needs more (a lower
        # cool setpoint = more cooling, to counter the solar heat). A raised
        # cool target would let the room get warmer exactly when the sun is
        # already heating it — the opposite of the documented "reduced" intent.
        if set_comfort and self.is_sunshine_offset_active():
            offset = self.config.sunshine_offset_value
            target -= offset
            self.debug_log(
                f"Sunshine Offset: target -{offset}°C "
                f"({'Kühlen: mehr kühlen' if self.is_cooling else 'Heizen: weniger heizen'})"
            )

        return target
