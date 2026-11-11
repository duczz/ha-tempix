"""Welche HVAC-Modi ein Raum als Comfort-/Eco-Modus anbieten darf.

Eine Quelle fuer den Options-Dialog (``config_flow``) und die Select-Entities
(``select``). Vorher kannte nur der Dialog die Geraetefaehigkeiten; die Selects
boten immer die volle Liste an, und ein ``auto`` auf einem ``heat``/``off``-
Thermostat endete im typisierten Fallback des Dispatchers: Geraet aus, Status
"Comfort", keine Warnung.
"""
from __future__ import annotations

from typing import Any

# `dry` und `fan_only` fehlen hier ABSICHTLICH — nicht vergessen.
# V2 kann sie (TEMPERATURE_DRIVEN_HVAC_MODES, temperaturlose Ziele mit
# target_temperature=None), V1 NICHT: dessen Temperaturkette hat keinen
# Zweig für temperaturlose Modi, und calculate_changes wertet ein
# target_temp=None als Uncertainty und bricht ab (engine_calibration.py:52).
# Freigeschaltet werden sie erst, wenn V1 nicht mehr mitrechnet (der Flip
# allein reicht nicht) — dann fehlen noch die Übersetzungen in selector.hvac_mode.options
# (strings.json + de/en), die aktuell nur heat/cool/heat_cool/auto/off kennt.
SCHEDULE_HVAC_MODE_ORDER: tuple[str, ...] = ("heat", "cool", "heat_cool", "auto", "off")

# Wenn kein Thermostat Auskunft gibt (alle unavailable oder nicht konfiguriert).
FALLBACK_SCHEDULE_HVAC_MODES: tuple[str, ...] = ("heat", "cool", "auto", "off")


def common_schedule_hvac_modes(hass: Any, trv_ids: list[str]) -> list[str]:
    """Schnittmenge der Modi aller konfigurierten Thermostate, in Anzeigereihenfolge.

    Ein Thermostat ohne Modus-Angabe (``unavailable``/``unknown`` melden oft keine
    Attribute) zaehlt als "unbekannt" und wird uebersprungen — nicht als "kann
    nichts", das die Schnittmenge leerte und die volle Fallback-Liste zurueckholte.
    Faellt am Ende nichts uebrig, gilt ``FALLBACK_SCHEDULE_HVAC_MODES``.
    """
    supported: set[str] | None = None
    for trv_id in trv_ids or []:
        state = hass.states.get(trv_id) if hass else None
        if state is None:
            continue
        trv_modes = set(state.attributes.get("hvac_modes") or [])
        if not trv_modes:
            continue
        supported = trv_modes if supported is None else supported & trv_modes

    if not supported:
        return list(FALLBACK_SCHEDULE_HVAC_MODES)

    result = [m for m in SCHEDULE_HVAC_MODE_ORDER if m in supported]
    return result or list(FALLBACK_SCHEDULE_HVAC_MODES)
