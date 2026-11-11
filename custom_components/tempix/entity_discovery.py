"""Send-time entity discovery for the V2 dispatcher.

Registry lookups that resolve, from a TRV entity id, the auxiliary entities V1
finds via the entity/device registry: the calibration ``number`` entity, the
external-sensor select/switch, the valve-position ``number`` entity, and whether
the device is a Tado (offset-service calibration).

Executor-side (needs ``hass`` + registries) — kept out of the pure ``calibration``
math module so that stays HA-free/parity-testable. Faithful ports of V1's
``CalibrationMixin._find_calibration_entity`` / ``_find_external_select`` / Tado
detection and ``ValvePositioner._find_valve_entity``. All share one
``entities.values()`` device walk; ``find_valve_entity`` additionally skips
DISABLED entities to match V1's ``async_entries_for_device`` default
(``include_disabled_entities=False``). The calibration finders deliberately keep
V1's UNFILTERED behaviour — V1's ``_find_calibration_entity`` used
``entities.values()`` too, so a disabled calibration entity is (as in V1) still
resolved. The disabled filter is therefore scoped to the valve lookup only.
Unlike V1, ``find_valve_entity`` also disambiguates several matches (opening vs.
closing degree) instead of taking the first one.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.helpers import (
    device_registry as dr_helper,
    entity_registry as er_helper,
)

_LOGGER = logging.getLogger(__name__)


def is_tado_device(hass: Any, trv_id: str) -> bool:
    """True when *trv_id* belongs to a Tado device (manufacturer match)."""
    try:
        dev_reg = dr_helper.async_get(hass)
        ent_reg = er_helper.async_get(hass)
        entry = ent_reg.async_get(trv_id)
        if entry and entry.device_id:
            dev = dev_reg.async_get(entry.device_id)
            if dev and dev.manufacturer and "tado" in dev.manufacturer.lower():
                return True
    except Exception:  # noqa: BLE001 — registry lookups must never crash a cycle
        pass
    return False


def _entries_on_device(hass: Any, trv_id: str) -> tuple[Any, ...]:
    """All registry entries sharing the TRV's device, or ()."""
    try:
        ent_reg = er_helper.async_get(hass)
        entry = ent_reg.async_get(trv_id)
        if not entry or not entry.device_id:
            return ()
        return tuple(
            e for e in ent_reg.entities.values() if e.device_id == entry.device_id
        )
    except Exception:  # noqa: BLE001
        return ()


def find_calibration_entity(hass: Any, trv_id: str, keyword: str) -> str | None:
    """The calibration ``number`` entity on the same device as *trv_id*."""
    for e in _entries_on_device(hass, trv_id):
        if e.domain == "number" and keyword in e.entity_id:
            return e.entity_id
    return None


def find_valve_entity(hass: Any, trv_id: str, keyword: str) -> str | None:
    """The valve-position ``number`` entity on the same device as *trv_id*.

    Skips DISABLED entities (``disabled_by``) — V1's ValvePositioner resolves via
    ``async_entries_for_device`` (``include_disabled_entities=False``), so a valve
    number the user deliberately disabled must never be written. Scoped here (not
    in ``_entries_on_device``) so the calibration finders keep V1's unfiltered
    behaviour.
    """
    matches = [
        e for e in _entries_on_device(hass, trv_id)
        if e.disabled_by is None and e.domain == "number" and keyword in e.entity_id
    ]
    if "closing" not in keyword.lower():
        # Never write the opening % into a closing degree — also not when the
        # opening degree is disabled and the closing one is the only match.
        matches = [e for e in matches if "closing" not in _valve_descriptor(e)]
    if len(matches) <= 1:
        return matches[0].entity_id if matches else None
    return _pick_valve_entity(trv_id, matches)


# Candidate sets already warned about — one WARNING per ambiguity, not per cycle.
_warned_ambiguous: set[frozenset[str]] = set()


def _valve_descriptor(e: Any) -> str:
    """entity_id + unique_id + translation_key, lowercased. The unique_id keeps
    the expose name (Z2M: ``<ieee>_valve_opening_degree_<topic>``) after the user
    renames the entity_id."""
    parts = (
        e.entity_id,
        getattr(e, "unique_id", None),
        getattr(e, "translation_key", None),
    )
    return " ".join(p for p in parts if p).lower()


def _pick_valve_entity(trv_id: str, matches: list[Any]) -> str | None:
    """Disambiguate several keyword matches on one device.

    Sonoff TRVZB (Z2M) exposes ``valve_opening_degree`` AND
    ``valve_closing_degree``; both contain the default keyword ``valve``. Writing
    an opening percentage into the closing degree leaves the valve partly open
    when the TRV closes it. ``closing`` candidates are already dropped by the
    caller; prefer ``opening`` (Better Thermostat ranks the same way). Still
    ambiguous → None: no valve write beats a wrong one.
    """
    opening = [e for e in matches if "opening" in _valve_descriptor(e)]
    if len(opening) == 1:
        return opening[0].entity_id
    candidates = frozenset(e.entity_id for e in matches)
    if candidates not in _warned_ambiguous:
        _warned_ambiguous.add(candidates)
        _LOGGER.warning(
            "%s: several valve entities match the valve keyword (%s) — none is "
            "written. Set a more specific valve keyword in the room options.",
            trv_id, ", ".join(sorted(candidates)),
        )
    return None


def find_external_select(hass: Any, trv_id: str) -> str | None:
    """The external-temperature select/switch entity for *trv_id*, or None."""
    for e in _entries_on_device(hass, trv_id):
        if e.domain == "select":
            state = hass.states.get(e.entity_id)
            if state and "external" in (state.attributes.get("options", []) or []):
                return e.entity_id
        elif e.domain == "switch" and "external_temperature_sensor" in e.entity_id:
            return e.entity_id
    return None
