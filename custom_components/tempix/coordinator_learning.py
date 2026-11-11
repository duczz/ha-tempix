"""
Tempix – Climate Rate Learner.

Tracks the temperature change toward target during active heating/cooling
sessions to adaptively learn
the room's climate conditioning rate (°C/h) via exponential moving average.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, UTC
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from .store_migration import MigratingStore

from custom_components.tempix.climate_rate import t1_normalization_factor
from custom_components.tempix.config_model import TempixConfig
from custom_components.tempix.const import CONF_LEARNED_CLIMATE_RATE

_STORAGE_VERSION = 1

_LOGGER = logging.getLogger(__name__)


class ClimateRateLearner:
    """Learns and persists the room heating/cooling rate in °C/h.

    **Der Lerner haelt den gelernten Wert selbst** (``_learned_rate``), die
    Config ist nur die Kopie, aus der die Engine liest. Grund: eine
    Options-Aenderung ohne Struktur-Key (jeder Switch, jede Number-Entity) baut
    in ``async_reload_entry`` eine komplett NEUE ``TempixConfig`` aus
    ``entry.data + entry.options``. Der gelernte Wert steht dort nicht drin — er
    liegt im eigenen Store —, also fiel er auf den Default 1.0 zurueck. Der
    Store ueberlebte das zwar, wurde aber beim naechsten Session-Ende mit der
    EMA aus 1.0 ueberschrieben: der gelernte Wert war dauerhaft weg. Deshalb
    stempelt ``bind_config`` ihn auf jede neue Config zurueck.
    """

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
        self._climate_session: dict[str, Any] | None = None
        self._store: MigratingStore | None = None
        # Quelle der Wahrheit; None = noch nichts geladen/gelernt, dann gilt
        # der Config-Wert (Default oder manuell in den Optionen gesetzt).
        self._learned_rate: float | None = None
        # Letzter in den Optionen gesehener Wert — Aenderung daran = manuelle
        # Eingabe ueber die Number-Entity, siehe bind_config.
        self._last_raw_rate: Any = config._raw.get(CONF_LEARNED_CLIMATE_RATE)

    @property
    def current_rate(self) -> float:
        """Der aktuell gueltige Wert — gelernt, sonst der aus der Config."""
        if self._learned_rate is not None:
            return self._learned_rate
        return self._config.learned_climate_rate

    def bind_config(self, config: TempixConfig) -> None:
        """Swap the config and carry the learned rate over to it.

        Eine manuelle Eingabe in der Number-Entity landet in ``entry.options``
        und kommt hier als geaenderter ``_raw``-Wert an. Die gewinnt — und wird
        sofort persistiert, damit Store und Optionen nicht auseinanderlaufen
        (sonst gewaenne beim naechsten Neustart wieder der Store, und die
        Eingabe waere still verworfen). Ein *unveraenderter* Options-Wert ist
        dagegen keine Eingabe, sondern nur der alte Stand: dann gewinnt der
        gelernte Wert.
        """
        raw_rate = config._raw.get(CONF_LEARNED_CLIMATE_RATE)
        self._config = config

        if raw_rate != self._last_raw_rate:
            self._last_raw_rate = raw_rate
            if self._is_valid_rate(raw_rate):
                self._learned_rate = float(raw_rate)
                self._debug_log(f"Learning: Rate {raw_rate} manuell gesetzt — uebernommen")
                self._persist()
                return

        if self._learned_rate is not None:
            config.learned_climate_rate = self._learned_rate

    def _persist(self) -> None:
        """Schreibt den gelernten Wert verzoegert in den Store."""
        self._get_store().async_delay_save(
            lambda: {CONF_LEARNED_CLIMATE_RATE: self._learned_rate},
            30.0,
        )

    def _get_store(self) -> MigratingStore:
        if self._store is None:
            self._store = MigratingStore(
                Store(self._hass, _STORAGE_VERSION, f"tempix/learning_{self._entry_id}"),
                Store(self._hass, _STORAGE_VERSION, f"tempix.learning.{self._entry_id}"),
                self._config.name,
            )
        return self._store

    async def async_load(self) -> None:
        """Load persisted climate rate from storage and apply to config."""
        try:
            stored = await self._get_store().async_load()
            if stored and CONF_LEARNED_CLIMATE_RATE in stored:
                rate = stored[CONF_LEARNED_CLIMATE_RATE]
                if self._is_valid_rate(rate):
                    self._learned_rate = float(rate)
                    self._config.learned_climate_rate = self._learned_rate
                    self._debug_log(f"Learning: Loaded rate {rate:.2f}°C/h from storage")
        except Exception as exc:
            _LOGGER.warning("%s: Failed to load learning storage: %s", self._config.name, exc)

    def _debug_log(self, msg: str) -> None:
        if self._config.debug_mode:
            _LOGGER.info("TPX Coord [%s]: %s", self._config.name, msg)
        else:
            _LOGGER.debug("TPX Coord [%s]: %s", self._config.name, msg)

    async def update(self, target_temp: float | None, hvac_mode: str) -> None:
        """Track the temperature change toward target during active heating/cooling to learn room climate rate."""
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

        if is_active_mode and temp_away_from_target and self._engine.is_automation_active() and not self._engine.config.manual_mode:
            if self._climate_session is None:
                self._debug_log(
                    f"Learning: Starting climate session "
                    f"(temp={current_temp:.1f}°, target={target_temp:.1f}°)"
                )
                self._climate_session = {
                    "start_temp": current_temp,
                    "start_time": now,
                    "target_temp": target_temp,
                }
            elif abs(self._climate_session["target_temp"] - target_temp) > 0.3:
                # Target changed significantly – restart session
                self._climate_session["start_temp"] = current_temp
                self._climate_session["start_time"] = now
                self._climate_session["target_temp"] = target_temp

        # 2. End session detection & rate calculation
        elif self._climate_session is not None:
            start_temp = self._climate_session["start_temp"]
            start_time = self._climate_session["start_time"]

            duration_hours = (now - start_time).total_seconds() / 3600.0

            target_reached = (
                current_temp is not None
                and abs(current_temp - target_temp) <= 0.2
            )

            if target_reached or not is_active_mode:
                if current_temp is None:
                    # No reading at session end (sensor down/unavailable) — do
                    # not coerce to 0.0 and learn a bogus rate from the gap to
                    # start_temp.
                    self._debug_log(
                        "Learning: Session aborted, no current room temperature available."
                    )
                else:
                    temp_diff = current_temp - start_temp
                    abs_diff = abs(temp_diff)
                    # The room must have moved TOWARD target (heating: up,
                    # cooling: down) — a drift the wrong way is not a climate
                    # rate measurement.
                    moved_correct_direction = temp_diff * factor > 0
                    if duration_hours > 0.25 and abs_diff > 0.5 and moved_correct_direction:
                        calc_rate = abs_diff / duration_hours

                        # Normalise measured rate to a reference outside temp (5 °C)
                        outside_temp = self._engine._resolve_outside_temp()
                        if outside_temp is not None:
                            calc_rate = calc_rate / t1_normalization_factor(
                                outside_temp, self._engine.is_cooling)

                        # Sanity bounds: 0.2 – 10 °C/h
                        if 0.2 <= calc_rate <= 10.0:
                            old_rate = self.current_rate
                            new_rate = (old_rate * 0.8) + (calc_rate * 0.2)  # EMA

                            self._debug_log(
                                f"Learning (T1): Session finished (Tout={outside_temp}°). "
                                f"Calculated normalized rate: {calc_rate:.2f}°C/h. "
                                f"Updating learned base rate: {old_rate:.2f} -> {new_rate:.2f}°C/h"
                            )

                            if self._is_valid_rate(new_rate):
                                self._learned_rate = round(new_rate, 2)
                                self._config.learned_climate_rate = self._learned_rate
                                self._persist()
                            else:
                                self._debug_log(f"Learning: Validation failed for rate {new_rate}")
                        else:
                            self._debug_log(
                                f"Learning: Rate {calc_rate:.2f}°C/h rejected (out of sanity bounds)."
                            )
                    elif not moved_correct_direction and abs_diff > 0.5:
                        self._debug_log(
                            f"Learning: Session rejected, room moved the wrong direction "
                            f"(diff={temp_diff:.1f}°, factor={factor})."
                        )
                    else:
                        self._debug_log(
                            f"Learning: Session too short or insufficient rise "
                            f"(diff={temp_diff:.1f}°, dur={duration_hours:.2f}h)."
                        )

                self._climate_session = None

    @staticmethod
    def _is_valid_rate(value: Any) -> bool:
        """Return True if *value* is a plausible climate rate."""
        return isinstance(value, (int, float)) and math.isfinite(value) and value > 0
