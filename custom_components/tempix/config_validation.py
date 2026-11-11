"""Semantische Config-Validierung: rein, nicht-fatal, ohne hass.

`validate_config` liefert strukturierte Befunde (Code + Details), keine
Freitexte: Repair-Issues brauchen einen `translation_key`, die UI ist de/en.
Die Details enthalten nur sprachneutrale Tokens (Feldnamen, Werte, Indizes).

Die Funktion wirft nie. Tempix muss auch mit unsinniger Config starten; jede
Regel ist einzeln abgesichert, ein Fehler in einer Regel verschluckt nur diese.

Pruefungen, die `hass`/Registry brauchen (zwei Eintraege auf einem Geraet,
`trvs`-Selbstreferenz), sind bewusst nicht hier.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Eine Repair-ID pro Code und Eintrag: f"config_{code}_{entry_id}".
CODE_TEMP_ORDER = "temp_order"
CODE_MODE_CONFLICT = "mode_conflict"
CODE_FROST_ABOVE_ECO = "frost_above_eco"
CODE_ADJUSTMENTS = "adjustments"
CODE_HOLIDAY_NO_DAY = "holiday_no_day"
CODE_AGGRESSIVE_NO_OFFSET = "aggressive_no_offset"
CODE_PRECONDITIONING_ZERO = "preconditioning_zero"
CODE_PROXIMITY_ZERO = "proximity_zero"
CODE_VALVE_MIN_NOT_BELOW_MAX = "valve_min_not_below_max"

# Code -> translation_key in strings.json / translations/*.json (`issues`).
ISSUE_TRANSLATION_KEYS: dict[str, str] = {
    CODE_TEMP_ORDER: "config_temp_order",
    CODE_MODE_CONFLICT: "config_mode_conflict",
    CODE_FROST_ABOVE_ECO: "config_frost_above_eco",
    CODE_ADJUSTMENTS: "config_adjustments",
    CODE_HOLIDAY_NO_DAY: "config_holiday_no_day",
    CODE_AGGRESSIVE_NO_OFFSET: "config_aggressive_no_offset",
    CODE_PRECONDITIONING_ZERO: "config_preconditioning_zero",
    CODE_PROXIMITY_ZERO: "config_proximity_zero",
    CODE_VALVE_MIN_NOT_BELOW_MAX: "config_valve_min_not_below_max",
}

# Wie `engine_schedule.get_active_adjustment` die Tage vergleicht:
# `current_day not in str(days)` mit `strftime("%a")` — Substring auf der
# String-Darstellung, case-sensitiv. `"Saturday"` greift also, `"sat"` und `[]`
# greifen nie.
_WEEKDAY_TOKENS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_ADJUSTMENT_MODES = ("comfort", "eco", "off", "auto")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class ConfigFinding:
    """Ein Befund: `code` aus ISSUE_TRANSLATION_KEYS, `details` sprachneutral."""

    code: str
    details: str


def _adjustment_problems(adjustments: Any) -> list[str]:
    """Tokens je Adjustment-Eintrag, z. B. ``[1] time``; leer = unauffaellig."""
    if not adjustments:
        return []
    if not isinstance(adjustments, list):
        return [f"adjustments ({type(adjustments).__name__}, list expected)"]
    problems: list[str] = []
    for idx, entry in enumerate(adjustments):
        if not isinstance(entry, dict):
            problems.append(f"[{idx}] entry")
            continue
        bad: list[str] = []
        # `time` fehlt -> die Engine ignoriert den Eintrag still; die Engine
        # vergleicht Strings, `6:00` sortiert falsch, `25:99` ist unmoeglich.
        t = entry.get("time")
        if not isinstance(t, str) or not _TIME_RE.match(t):
            bad.append("time")
        mode = entry.get("mode")
        if mode is not None and (not isinstance(mode, str) or mode not in _ADJUSTMENT_MODES):
            bad.append("mode")
        days = entry.get("days")
        if days is not None and not any(tok in str(days) for tok in _WEEKDAY_TOKENS):
            bad.append("days")
        if bad:
            problems.append(f"[{idx}] " + ", ".join(bad))
    return problems


def validate_config(config: Any) -> list[ConfigFinding]:
    """Semantische Pruefung einer `TempixConfig`. Wirft nie."""
    findings: list[ConfigFinding] = []

    def rule(fn) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 - nie fatal, nur diese Regel entfaellt
            _LOGGER.debug("config validation rule %s failed", fn.__name__, exc_info=True)

    comfort_mode = getattr(config, "hvac_mode_comfort", None)
    eco_mode = getattr(config, "hvac_mode_eco", None)

    def temp_order() -> None:
        # Nur bei eindeutiger Richtung (heat/heat oder cool/cool). `off`,
        # `heat_cool` und `auto` zaehlen nicht: dort ist die Eco-Temperatur
        # unbenutzt bzw. die Richtung offen.
        if comfort_mode != eco_mode or comfort_mode not in ("heat", "cool"):
            return
        comfort, eco = config.temp_comfort_static, config.temp_eco_static
        wrong = comfort < eco if comfort_mode == "heat" else comfort > eco
        if wrong:
            findings.append(ConfigFinding(
                CODE_TEMP_ORDER,
                f"{comfort_mode}: comfort {comfort} / eco {eco}"))

    def mode_conflict() -> None:
        # Echter Konflikt: eine Seite heat, die andere cool. `cool`/`off` ist
        # keiner (Klimaanlagen mit Eco `off`). `engine_base._factor` entscheidet
        # bei heat/cool still zugunsten Kuehlen.
        if {comfort_mode, eco_mode} == {"heat", "cool"}:
            findings.append(ConfigFinding(
                CODE_MODE_CONFLICT, f"comfort {comfort_mode} / eco {eco_mode}"))

    def frost() -> None:
        # Nur Heizen: bei Eco `heat` ist die Eco-Temperatur die Referenz.
        if (config.frost_protection_enabled and eco_mode == "heat"
                and config.frost_protection_temp > config.temp_eco_static):
            findings.append(ConfigFinding(
                CODE_FROST_ABOVE_ECO,
                f"frost {config.frost_protection_temp} / eco {config.temp_eco_static}"))

    def adjustments() -> None:
        problems = _adjustment_problems(getattr(config, "adjustments", None))
        if problems:
            findings.append(ConfigFinding(CODE_ADJUSTMENTS, "; ".join(problems)))

    def holiday() -> None:
        # Kalender-Feiertage nutzen immer `holiday_use_day`
        # (`resolve_holiday_target_day`); ohne Tag ist der Override tot.
        if config.holiday_calendar and not config.holiday_use_day:
            findings.append(ConfigFinding(CODE_HOLIDAY_NO_DAY, str(config.holiday_calendar)))

    def aggressive() -> None:
        # Beide aktiven Modi brauchen `offset > 0`: `target_temp`
        # (`engine_calibration.py` is_aggressive, `hardware_dispatcher.py`
        # Aggressive-Zweig der Target-Variante) und `calibration`
        # (`calibration.py` is_aggressive). Bei Offset 0 ist der Boost
        # ein No-op.
        if (config.aggressive_mode_selector in ("target_temp", "calibration")
                and config.aggressive_offset <= 0):
            findings.append(ConfigFinding(
                CODE_AGGRESSIVE_NO_OFFSET,
                f"mode {config.aggressive_mode_selector}, offset {config.aggressive_offset}"))

    def preconditioning() -> None:
        if (config.smart_preconditioning
                and config.max_smart_preconditioning.total_seconds() <= 0):
            findings.append(ConfigFinding(CODE_PRECONDITIONING_ZERO, "max 0"))

    def proximity() -> None:
        # `distance <= limit` mit Limit 0 trifft praktisch nie.
        if config.proximity_entity and config.proximity_distance <= 0:
            findings.append(ConfigFinding(
                CODE_PROXIMITY_ZERO, f"distance {config.proximity_distance}"))

    def valve_min() -> None:
        # `valve.py` bildet (0, max] auf [min, max] ab; bei min >= max bleibt
        # nur noch zu/max (An/Aus). `valve_max = 0` ist "Ventil sperren"
        # (gewollt, test_config_model) und hier kein Befund.
        if (config.valve_mode != "off" and config.valve_max > 0
                and config.valve_min >= config.valve_max):
            findings.append(ConfigFinding(
                CODE_VALVE_MIN_NOT_BELOW_MAX,
                f"min {config.valve_min} / max {config.valve_max}"))

    for fn in (temp_order, mode_conflict, frost, adjustments, holiday,
               aggressive, preconditioning, proximity, valve_min):
        rule(fn)
    return findings
