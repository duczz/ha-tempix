"""Entity-based calibration — V2 port of V1's CalibrationApplier math.

V1 has TWO calibration paths: the FRITZ *generic* path bakes the sensor offset
into the target temperature (ported in ``hardware_dispatcher.plan()``), and the
*native* / aggressive-calibration path writes an offset on a dedicated entity
(``number.set_value``) or a Tado offset service. This module ports the second —
a 1:1 port of ``CalibrationMixin.calculate_calibration`` /
``_calculate_tado_calibration`` (engine_calibration.py).

The value math (``compute_tado_offset`` / ``compute_entity_calibration``) is PURE
and parity-tested against V1 across a matrix (no calibration hardware is available for testing,
so parity — not a hardware test — is the acceptance, mirroring the valve port).
``plan_calibration`` orchestrates: registry discovery (via ``entity_discovery``)
+ state reads + the pure math → typed ``CalibrationCommand``s. It is THROTTLE-FREE
(computation ≠ sending, exactly like V1's engine/applier split); the per-TRV send
throttle lives in the dispatcher's ``dispatch_calibrations``.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from custom_components.tempix.const import (
    AGGRESSIVE_MODE_CALIBRATION,
    CALIBRATION_MODE_GENERIC,
    CALIBRATION_MODE_OFF,
    DEFAULT_CALIBRATION_KEEPALIVE,
    INVALID_STATES,
    TADO_MAX_OFFSET,
    TADO_MIN_OFFSET,
)
from custom_components.tempix.entity_discovery import (
    find_calibration_entity,
    find_external_select,
    is_tado_device,
)
from custom_components.tempix.models.calibration_command import CalibrationCommand

_LOGGER = logging.getLogger(__name__)


# ── V1-exact rounding (engine_base._round_to_step / _round_half_up) ────────────
# Local copy keeps this module free of a hardware_dispatcher back-import
# (hardware_dispatcher imports calibration, not the other way round).


def _round_half_up(value: float) -> float:
    return float(int(value + 0.5) if value >= 0 else int(value - 0.5))


def _round_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return _round_half_up(value / step) * step


# ── pure value math (parity-tested against V1) ────────────────────────────────


def compute_tado_offset(
    *,
    old_offset: float,
    local_temp: float,
    room_temp: float,
    is_aggressive: bool,
    aggressive_range: float,
    aggressive_offset: float,
    factor: int,
    aggressive_target: float,
    delta: float,
) -> float | None:
    """Tado offset (``tado.set_climate_temperature_offset``) — V1 parity of
    ``_calculate_tado_calibration``. Returns None on deadband (no write)."""
    new_offset = -(local_temp - room_temp) + old_offset
    if is_aggressive:
        temp_diff = float(aggressive_target) - room_temp
        if temp_diff * factor < -aggressive_range:
            new_offset += aggressive_offset * factor
        elif temp_diff * factor > aggressive_range:
            new_offset -= aggressive_offset * factor
    new_offset = max(TADO_MIN_OFFSET, min(TADO_MAX_OFFSET, round(new_offset, 1)))
    if abs(old_offset - new_offset) < delta:
        return None
    return new_offset


def compute_entity_calibration(
    *,
    is_offset: bool,
    old_val: float,
    room_temp: float,
    thermostat_temp: float | None,
    calib_min: float,
    calib_max: float,
    step: float,
    is_aggressive: bool,
    aggressive_range: float,
    aggressive_offset: float,
    factor: int,
    aggressive_target: float,
    delta: float,
    keepalive_elapsed: bool,
) -> float | None:
    """Number-entity calibration value — V1 parity of ``calculate_calibration``
    (non-Tado branch). ``is_offset`` entities compute a delta from the TRV's
    internal sensor; absolute entities write the room temperature. Returns None
    on deadband — except the V1 keepalive: a non-offset entity untouched for
    ``DEFAULT_CALIBRATION_KEEPALIVE`` minutes is re-pushed even inside the band."""
    if is_offset:
        if thermostat_temp is None:
            return None
        new_val = -(float(thermostat_temp) - room_temp) + old_val
    else:
        new_val = room_temp

    if is_aggressive:
        temp_diff = float(aggressive_target) - room_temp
        if temp_diff * factor < -aggressive_range:
            new_val += aggressive_offset * factor
        elif temp_diff * factor > aggressive_range:
            new_val -= aggressive_offset * factor

    # V1: precise/step entities round to step; wide-range (max >= 1000) or
    # >1-step entities encode as centi-degrees (int(x*100)).
    if step <= 1 and calib_max < 1000:
        new_val = _round_to_step(new_val, step)
    else:
        new_val = int(new_val * 100)

    new_val = max(calib_min, min(calib_max, new_val))

    if abs(old_val - new_val) < delta:
        if not (not is_offset and keepalive_elapsed):
            return None
    return new_val


# ── resolution helpers (state-derived, HA-free math inputs) ────────────────────


def _resolve_step(calib_state: Any, trv_state: Any, step_cfg: str) -> float:
    """V1 step resolution: the entity's own ``step`` attr wins; else the
    configured size; else the TRV's ``target_temp_step`` (default 0.5)."""
    step = calib_state.attributes.get("step")
    if step is None:
        if step_cfg == "full":
            step = 1.0
        elif step_cfg == "half":
            step = 0.5
        elif step_cfg == "precise":
            step = 0.1
        else:
            step = trv_state.attributes.get("target_temp_step", 0.5)
    return float(step) if step else 0.5


def _keepalive_elapsed(calib_state: Any, now: datetime) -> bool:
    """True when a (non-offset) calibration entity has been untouched longer
    than the keepalive window — V1 pushes the value again to keep it fresh.
    Any parse failure → False (treated as a fresh entity → deadband holds)."""
    try:
        last_upd = calib_state.last_updated
        if last_upd is None:
            return False
        if last_upd.tzinfo is None:
            last_upd = last_upd.replace(tzinfo=UTC)
        return (now - last_upd) >= timedelta(minutes=DEFAULT_CALIBRATION_KEEPALIVE)
    except Exception:  # noqa: BLE001
        return False


# ── orchestrator (throttle-free planning) ─────────────────────────────────────


def plan_calibration(
    *,
    hass: Any,
    get_state: Callable[[str], Any],
    trvs: tuple[str, ...] | list[str],
    room_temp: float | None,
    target_temp: float | None,
    enabled: bool,
    planner: Any,  # duck-typed PlannerConfig (avoids a hardware_dispatcher import)
    now: datetime,
    warned: set[str] | None = None,
) -> tuple[CalibrationCommand, ...]:
    """Room → per-TRV entity calibration commands (pure planning, no sending).

    ``planner`` supplies calibration_mode / aggressive_mode / aggressive_range /
    aggressive_offset / calibration_delta / calibration_step_size /
    calibration_keyword / is_cooling. ``enabled`` is the adjustment's
    calibration gate, resolved by the caller via
    ``resolve_adjustment_calibration`` (port of V1's ``get_adjustment_calibration``).

    ``warned`` is the caller's per-TRV dedupe set for the "no calibration
    entity" WARNING (V1 ``CalibrationApplier._calib_warned``). Pass ``None`` to
    stay silent — the caller does that pre-flip, where V1's applier still emits
    the warning itself and a second one would only double the log line.
    """
    cal_mode = planner.calibration_mode
    agg_mode = planner.aggressive_mode
    # Mode gate (V1 calculate_calibration): generic → baked into target temp
    # (plan()); off → nothing unless aggressive-calibration is active.
    if cal_mode == CALIBRATION_MODE_GENERIC:
        return ()
    if cal_mode == CALIBRATION_MODE_OFF and agg_mode != AGGRESSIVE_MODE_CALIBRATION:
        return ()
    # Domain gate + room-temp requirement (room-level: no room temp → nothing).
    if not enabled or room_temp is None:
        return ()

    factor = -1 if planner.is_cooling else 1
    is_aggressive = (
        planner.aggressive_offset > 0 and agg_mode == AGGRESSIVE_MODE_CALIBRATION
    )
    delta = planner.calibration_delta
    keyword = planner.calibration_keyword
    step_cfg = planner.calibration_step_size
    aggressive_range = planner.aggressive_range
    aggressive_offset = planner.aggressive_offset

    commands: list[CalibrationCommand] = []
    for trv_id in trvs:
        state = get_state(trv_id)
        if not state:
            continue
        # V1: TRV's own setpoint drives the aggressive nudge; fall back to the
        # room target when the attribute is absent.
        # Ein vorhandenes Attribut mit Wert None (Gerät aus) umgeht den
        # get()-Default und würde in float() werfen.
        aggressive_target = state.attributes.get("temperature")
        if aggressive_target is None:
            aggressive_target = target_temp

        if is_tado_device(hass, trv_id):
            old_offset = float(state.attributes.get("offset_celsius", 0))
            local_temp = float(state.attributes.get("current_temperature", room_temp))
            value = compute_tado_offset(
                old_offset=old_offset, local_temp=local_temp, room_temp=room_temp,
                is_aggressive=is_aggressive, aggressive_range=aggressive_range,
                aggressive_offset=aggressive_offset, factor=factor,
                aggressive_target=aggressive_target, delta=delta,
            )
            if value is None:
                continue
            commands.append(CalibrationCommand(
                trv_id=trv_id, target_entity=trv_id, value=value,
                is_tado=True, old_value=old_offset,
            ))
            continue

        calib_entity = find_calibration_entity(hass, trv_id, keyword)
        if not calib_entity:
            # V1-Port (CalibrationApplier, coordinator_appliers.py): ohne
            # Kalibrier-Entity kann der Offset nicht geschrieben werden — das
            # ist die Diagnose, die V1 bewusst anbietet, und sie darf beim Flip
            # nicht verlorengehen. Zwei Feinheiten aus V1 uebernommen:
            # `generic` faellt schon oben raus (dort DEBUG statt WARNING, der
            # Offset steckt in der Zieltemperatur), und bei `off` + aggressiver
            # Kalibrierung warnt V1 NICHT — nur der Modus-Gate unterscheidet
            # die beiden Wege ueber `cal_mode != CALIBRATION_MODE_OFF` unten.
            if (warned is not None
                    and cal_mode != CALIBRATION_MODE_OFF
                    and trv_id not in warned):
                warned.add(trv_id)
                _LOGGER.warning(
                    "No calibration number entity found for %s – hardware "
                    "sensor offset cannot be written to TRV. Check that a "
                    "calibration number entity exists for this TRV.",
                    trv_id,
                )
            continue
        calib_state = get_state(calib_entity)
        if not calib_state:
            continue

        is_offset = (
            ("offset" in calib_entity or "calibration" in calib_entity)
            and "external" not in calib_entity
        )
        old_val = (
            float(calib_state.state)
            if calib_state.state not in INVALID_STATES else 0
        )
        calib_min = float(calib_state.attributes.get("min", -12 if is_offset else 0))
        calib_max = float(calib_state.attributes.get("max", 12 if is_offset else 1000))
        thermostat_temp = state.attributes.get("current_temperature")
        step = _resolve_step(calib_state, state, step_cfg)
        keepalive_elapsed = _keepalive_elapsed(calib_state, now)

        value = compute_entity_calibration(
            is_offset=is_offset, old_val=old_val, room_temp=room_temp,
            thermostat_temp=thermostat_temp, calib_min=calib_min,
            calib_max=calib_max, step=step, is_aggressive=is_aggressive,
            aggressive_range=aggressive_range, aggressive_offset=aggressive_offset,
            factor=factor, aggressive_target=aggressive_target, delta=delta,
            keepalive_elapsed=keepalive_elapsed,
        )
        if value is None:
            continue
        select_entity = find_external_select(hass, trv_id)
        commands.append(CalibrationCommand(
            trv_id=trv_id, target_entity=calib_entity, value=value,
            is_tado=False, select_entity=select_entity,
            old_value=old_val, is_offset=is_offset,
        ))
    return tuple(commands)
