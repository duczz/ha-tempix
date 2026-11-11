"""HardwareDispatcher — the executor.

Owns the mutable CommandLedger (ExpectedHardwareState with TTL +
``confirmed_at`` tracking) and the **Device Command Planning** stage: the
per-DEVICE adaptation of the room ``TargetState`` into ``HardwareCommand``s
(generic calibration, aggressive mode, ±6 °C clamp, step rounding,
``min_instead_of_off``, ``off_if_above_room_temp`` hysteresis — V1-parity
port of ``engine_calibration.calculate_changes``). Hardware compensation,
never decision logic.

Guarantees:
- Incompatible modes fall back (e.g. dry → off) via typed
  ``DeviceDispatchResult`` — no exceptions cross device boundaries.
- A lost command (HA accepted the call, the radio frame never arrived) is
  repaired by the REGULAR cycle: the entry expires unconfirmed, ``plan()``
  still sees the stale device value and re-plans it. ``resend_expired_sweep()``
  exists for the same case but is deliberately NOT wired up — see its
  docstring before calling it.
- The ``min_delta`` guard acts ONLY here (suppresses noise sends
  against the ledger value; mode changes always send) — never on the
  engine result.
- UNSAFE decisions are not dispatched (hard hardware safeguard).
- Dry-run mode (pre-flip parallel validation): plans, sends nothing,
  writes NOTHING to the ledger.
- ``RoomHealth`` is derived from FAILED dispatches.

Valve positioning (``number.set_value`` command kind) is a separate
command type, not squeezed in.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from custom_components.tempix.clock import EngineClock
from custom_components.tempix.entity_discovery import find_valve_entity
from custom_components.tempix.models.calibration_command import CalibrationCommand
from custom_components.tempix.models.capability_map import (
    DeviceCapability,
    DeviceCapabilityMap,
)
from custom_components.tempix.models.command_ledger import (
    LedgerEntry,
    LedgerEntryState,
    LedgerView,
    _parse_dt,
)
from custom_components.tempix.service_send import safe_service_call
from custom_components.tempix.models.decision_result import DecisionResult
from custom_components.tempix.models.dispatch_result import (
    DeviceDispatch,
    DeviceDispatchResult,
    DispatchSummary,
)
from custom_components.tempix.models.hardware_command import HardwareCommand
from custom_components.tempix.models.health import SnapshotValidity
from custom_components.tempix.models.valve_command import ValveCommand
from custom_components.tempix.valve import calculate_valve_position
from custom_components.tempix.models.snapshot import TempixSnapshot
from custom_components.tempix.models.target_state import HVAC_OFF, TargetState
from custom_components.tempix.models.telemetry import RoomHealth
from custom_components.tempix.snapshot_builder import TEMP_TOLERANCE, _temps_match

_LOGGER = logging.getLogger(__name__)

# V1 constants (engine_calibration)
ROOM_TEMP_FALLBACK = 20.0
MAX_TOTAL_OFFSET = 6.0

# Circuit-breaker backoff ladder, per-TRV exponential, capped.
#
# The base was 15 (→ 15/30/60/120 min), carried over from V1 for cutover
# parity (V1-parity, coordinator ``_apply_with_semaphore``, moved here
# because the target architecture keeps no circuit-breaker logic in the
# coordinator). That first stage is far more conservative than how HA core
# itself backs off a temporarily unreachable integration (``ConfigEntryNotReady``:
# ``min(2**(tries-1), 3600)``, i.e. starting at 1s) or general circuit-breaker
# practice (first stage in the seconds range) — a single transient radio
# timeout (FRITZ!DECT) parked a device for a quarter hour with zero further
# attempts. Deliberate deviation from V1-parity, not an oversight: base
# lowered to 2 min, same doubling, same cap.
_CB_BACKOFF_BASE_MINUTES = 2
_CB_BACKOFF_CAP_MINUTES = 120


@dataclass(frozen=True)
class PlannerConfig:
    """Device-Command-Planning knobs (reload-static, V1 semantics).

    Defaults are neutral (all modes off) and do NOT mirror the V1 config
    defaults: production never relies on them, because
    ``ParallelValidator._planner_config`` sets every field from the room
    config before each ``plan()``. A bare ``PlannerConfig()`` only appears in
    tests and as the long-lived dispatcher's placeholder before the first
    ``configure()``.
    The mode string literals below are the values the V1 select entities
    store (const.AGGRESSIVE_MODE_TARGET/AGGRESSIVE_MODE_CALIBRATION,
    CALIBRATION_MODE_GENERIC) — kept as literals only to preserve the
    HA-free boundary of this module; keep them in sync with const.py.
    """

    aggressive_mode: str = "off"           # V1: "target_temp" | "calibration" | "off"
    aggressive_range: float = 0.0
    aggressive_offset: float = 0.0
    calibration_mode: str = "off"          # V1: "generic" | "native" | "off"
    calibration_delta: float = 0.0         # deadband against offset flapping
    generic_calibration_limit: float = 5.0  # const.DEFAULT_GENERIC_CALIBRATION_LIMIT
    calibration_step_size: str = "auto"    # const.CALIBRATION_STEP_SIZE_AUTO ("full"/"half"/"precise"/else→device step)
    # Entity-based calibration. ``calibration_keyword`` finds the
    # calibration ``number`` entity on the TRV's device; ``calibration_timeout``
    # throttles re-writes (send side). Only used for the "native" path — generic
    # bakes into the target temp above, off does nothing.
    calibration_keyword: str = "calibration"
    calibration_timeout: timedelta = timedelta(minutes=1)
    min_instead_of_off: bool = False
    off_if_above_room_temp: bool = False
    hysteresis: float = 0.3                 # const.DEFAULT_HYSTERESIS
    is_cooling: bool = False
    window_open_temp: float = 0.0          # dont_turn_off context (V1)
    # Mirrors FrostConstraint's own config so
    # plan() can recognise an already-computed frost-floor correction on an
    # external-sensor failure (see plan() below) without re-running the
    # constraint itself.
    frost_enabled: bool = False
    frost_temp: float = 5.0
    # Valve positioning. Off by default → no valve command.
    valve_mode: str = "off"                # V1: "off" | "regular" | "pessimistic" | "optimistic"
    valve_diff: float = 1.0
    valve_max: int = 100
    valve_min: int = 0                     # closing point; 0 = no minimum
    valve_step: int = 10
    valve_keyword: str = "valve"           # finds the valve ``number`` entity


def _round_half_up(value: float) -> float:
    """Round half AWAY from zero (V1 `_round_half_up`, Jinja2 behaviour).

    Python's builtin ``round()`` is banker's rounding (0.5 → 0) — using it
    would diverge from V1 at every .25/.75 step boundary.
    """
    return float(int(value + 0.5) if value >= 0 else int(value - 0.5))


def _round_to_step(value: float, step: float) -> float:
    """V1-exact step rounding (``_round_to_step``): half-up, NO extra
    rounding of the product — float dust flows on exactly like in V1 and
    is cleaned by the final ``round(x, 1)`` clamp."""
    if step <= 0:
        return value
    return _round_half_up(value / step) * step


class HardwareDispatcher:
    """Per-room executor with CommandLedger and command planning."""

    def __init__(
        self,
        hass: Any,
        clock: EngineClock,
        capability_map: DeviceCapabilityMap,
        *,
        planner: PlannerConfig | None = None,
        command_ttl: timedelta = timedelta(seconds=60),
        # ``min_delta`` — ENTSCHIEDEN: bleibt bei 0.0, wird
        # NICHT verdrahtet, und das ist kein vergessener Rest. Drei Gruende,
        # jeder am Code geprueft:
        #
        # 1. Jede Zieltemperatur wird vor dem Senden auf die Geraete-Schritt-
        #    weite gerundet (``_round_to_step``, siehe ``plan()``). Die Differenz
        #    zweier Kommandos ist damit immer ein Vielfaches des Steps — bei den
        #    ueblichen 0.5 K greift ein ``min_delta <= 0.5`` nie, und ein
        #    groesseres wuerde einen ECHTEN Einzelschritt unterdruecken.
        # 2. Ein Wirkbereich entsteht nur bei ``calibration_step_size=precise``
        #    (0.1). Genau dieses Rauschen bremst aber schon ``calibration_delta``
        #    (Default 0.5) direkt an der Quelle, per Geraet und eine Ebene
        #    frueher — ``min_delta`` waere ein zweites Deadband hinter einem
        #    bestehenden.
        # 3. Bei identischen Werten ist die Differenz 0 und damit IMMER
        #    ``< min_delta``. Scharf geschaltet wuerde der Guard unten deshalb
        #    die TTL-Divergenzkorrektur aushebeln — also genau den Mechanismus,
        #    der ein von Hand verstelltes Thermostat zurueckholt.
        #
        # Wer ihn doch aktivieren will, braucht zuerst einen Test fuer Punkt 3;
        # der bestehende Test deckt nur die Unterdrueckung ab, nicht die
        # Wechselwirkung.
        min_delta: float = 0.0,
        dry_run: bool = False,
        name: str = "",
        action_delay: timedelta = timedelta(0),
        on_cb_change: Callable[[], None] | None = None,
        on_send: Callable[[HardwareCommand], None] | None = None,
        # Abbruch-Hook fuer die Sendeschleifen. Der Coordinator uebergibt
        # `not self._updates_enabled` — Unload wartet nur 5 s auf einen laufenden
        # Zyklus; ein Mehrgeraete-Zyklus (Geraete x `action_delay`) kann laenger
        # brauchen und sendete danach weiter, waehrend die Integration abgebaut
        # wird. Geprueft wird AUSSCHLIESSLICH an Geraete-Grenzen (Schleifenkopf):
        # ein Abbruch zwischen `set_hvac_mode` und `set_temperature` desselben
        # Geraets hinterliesse ein Teil-Kommando ohne Ledger-Eintrag.
        # Default `None` = nie abbrechen, Verhalten wie bisher.
        should_abort: Callable[[], bool] | None = None,
    ) -> None:
        self._should_abort = should_abort
        self._hass = hass
        self._clock = clock
        self._capabilities = capability_map
        self._planner = planner if planner is not None else PlannerConfig()
        self._command_ttl = command_ttl
        self._min_delta = min_delta
        self._dry_run = dry_run
        # Send-path knobs. ``name`` is the room label for
        # ``safe_service_call`` logs; ``action_delay`` is the FRITZ!DECT
        # breathing gap between the mode and the temperature call (V1 parity).
        self._name = name
        self._action_delay = action_delay
        # Fires immediately BEFORE each real service call, with the command as
        # it will actually go out (capability fallback already applied). Every
        # suppression path — repeat guard, min_delta, backoff, unsupported mode
        # without ``off``, dry-run — returns before this, so a caller doing echo
        # bookkeeping sees exactly the sends that happen. Deliberately pre-send
        # and not post-dispatch: a device echo can arrive faster than the send
        # await returns (V1 tracks before its call for the same reason).
        self._on_send = on_send
        self._ledger: dict[str, LedgerEntry] = {}
        # One-shot repeat-guard bypass (resync): a device returning from
        # `unavailable` has a stale-but-fresh ledger entry from before the
        # outage, which would otherwise swallow the re-assert that restores
        # its real target for a full `command_ttl`.
        self._resync_bypass: set[str] = set()
        self._last_generic_offsets: dict[str, float] = {}
        # Entity-based calibration send throttle (V1 parity:
        # CalibrationApplier._last_calibration_time). Per-TRV last successful
        # write; a fresh write is suppressed until ``calibration_timeout`` has
        # elapsed. ``_calib_no_entity_warned`` dedups the "no calibration entity"
        # warning (V1 CalibrationApplier._calib_warned).
        self._last_calibration_time: dict[str, datetime] = {}
        self._calib_no_entity_warned: set[str] = set()
        self._room_health = RoomHealth.HEALTHY
        # Circuit breaker: per-TRV consecutive-failure count
        # + backoff deadline. ``on_cb_change`` fires when the state mutates so
        # the coordinator persists it.
        # { entity_id: {"failures": int, "retry_after": datetime | None} }
        self._cb_state: dict[str, dict[str, Any]] = {}
        self._on_cb_change = on_cb_change

    # ── public state ──────────────────────────────────────────────────────

    @property
    def room_health(self) -> RoomHealth:
        return self._room_health

    def ledger_view(self) -> LedgerView:
        """Immutable copy for the SnapshotBuilder."""
        return LedgerView(entries=tuple(self._ledger.values()))

    # ── runtime configuration ─────────────────────────────────────────────

    def configure(
        self, capability_map: DeviceCapabilityMap, planner: PlannerConfig,
    ) -> None:
        """Inject the live device capabilities + planning config into the
        long-lived dispatcher. Pre-flip it runs with an empty map + default
        planner (only its ledger side is used); at the flip the validator feeds
        the real values each cycle so plan()/dispatch() match what V1 would do.
        Capabilities are refreshed per cycle (a TRV can come online with
        different modes); the planner is reload-static but re-set here too for
        symmetry. Never touches the ledger/CB — those persist across configure
        calls."""
        self._capabilities = capability_map
        self._planner = planner

    # ── ledger persistence ────────────────────────────────────────────────

    def ledger_to_storage(self) -> dict[str, Any]:
        """Serialize the ledger for the Store. The Store I/O is owned by the
        coordinator."""
        return {
            "schema_version": 1,
            "entries": [e.to_dict() for e in self._ledger.values()],
        }

    def restore_ledger(self, data: dict[str, Any] | None, now: datetime) -> None:
        """Rehydrate the ledger from Store after a reboot.

        Every restored entry is forced STALE: its last-sent command is kept
        for value-based echo matching (the first TRV event after a
        reboot must not be misread as a user override) but is never
        re-confirmed and never resent (STALE stays out of the reconcile loop).
        Malformed entries are skipped, never fatal. ``now`` is accepted for
        symmetry/future age-capping; the STALE marking already makes the
        entries inert.
        """
        self._ledger = {}
        if not isinstance(data, dict):
            return
        entries = data.get("entries")
        if not isinstance(entries, (list, tuple)):
            return
        for raw in entries:
            entry = LedgerEntry.from_dict(raw, force_stale=True)
            if entry is not None:
                self._ledger[entry.entity_id] = entry

    # ── circuit-breaker persistence ───────────────────────────────────────

    def cb_to_storage(self) -> dict[str, Any]:
        """Serialize the circuit-breaker state for the Store. Like the ledger,
        the Store I/O is owned by the long-lived coordinator. ``retry_after``
        is stored as isoformat."""
        return {
            entity_id: {
                "failures": st["failures"],
                "retry_after": (
                    st["retry_after"].isoformat() if st["retry_after"] else None
                ),
            }
            for entity_id, st in self._cb_state.items()
        }

    def restore_cb(self, data: dict[str, Any] | None) -> None:
        """Rehydrate the circuit-breaker state after a reboot. Malformed entries
        are skipped, never fatal; ``retry_after`` is coerced to aware-UTC (shared
        ``_parse_dt``) so the later ``now < retry_after`` comparison cannot raise
        on a naive/garbage value."""
        self._cb_state = {}
        if not isinstance(data, dict):
            return
        for entity_id, st in data.items():
            if not isinstance(entity_id, str) or not isinstance(st, dict):
                continue
            raw_failures = st.get("failures", 0)
            failures = int(raw_failures) if isinstance(raw_failures, (int, float)) else 0
            self._cb_state[entity_id] = {
                "failures": failures,
                "retry_after": _parse_dt(st.get("retry_after")),
            }

    def record_sent(
        self, entity_id: str, hvac_mode: str | None,
        target_temperature: float | None, now: datetime,
        ttl: timedelta | None = None,
    ) -> None:
        """Record a command in the ledger WITHOUT sending.

        In observe-only the coordinator records V1's ACTUAL sends here, so the
        ledger reflects what the hardware was last told — the state ``confirm()``
        and the override detection compare against. At the flip the dispatcher's
        own ``_send`` records instead. A fresh record supersedes any prior
        (un)confirmed entry for the device."""
        self._ledger[entity_id] = LedgerEntry(
            entity_id=entity_id,
            hvac_mode=hvac_mode,
            target_temperature=target_temperature,
            sent_at=now,
            ttl=ttl if ttl is not None else self._command_ttl,
        )

    def bypass_repeat_guard_once(self, entity_id: str) -> None:
        """Skip the repeat-guard (and ``min_delta``) for this entity's NEXT
        dispatch only, then self-clear. See ``_resync_bypass``."""
        self._resync_bypass.add(entity_id)

    def confirm(self, entity_id: str, hvac_mode: str | None,
                target_temperature: float | None, at: datetime) -> None:
        """Value-based ack from an HA state event (two-phase)."""
        entry = self._ledger.get(entity_id)
        if entry is None or entry.confirmed_at is not None:
            return
        mode_ok = entry.hvac_mode is None or entry.hvac_mode == hvac_mode
        temp_ok = entry.target_temperature is None or _temps_match(
            target_temperature, entry.target_temperature, TEMP_TOLERANCE)
        if mode_ok and temp_ok:
            self._ledger[entity_id] = LedgerEntry(
                entity_id=entry.entity_id, hvac_mode=entry.hvac_mode,
                target_temperature=entry.target_temperature,
                sent_at=entry.sent_at, ttl=entry.ttl, confirmed_at=at,
            )

    # ── Device Command Planning (V1 parity) ───────────────────────────────

    def plan_valves(
        self, target: TargetState, snapshot: TempixSnapshot,
    ) -> tuple[ValveCommand, ...]:
        """Plan the valve opening per TRV. Pure — the
        ``number.set_value`` send is flip-time. The position is room-level (same
        for every TRV given a room sensor), one command per configured device.

        V1 parity (the subtle part): V1 and V2 represent "off" DIFFERENTLY —
        V1's ``calculate_target_temperature`` returns ``0.0`` for an off/idle
        room (a number), so V1's ValvePositioner runs and drives the valve
        CLOSED (``calculate_valve_position(…, 0.0)`` → 0). V2 represents off as
        ``target_temperature = None``. So OFF must emit an explicit 0 (closed)
        command here — otherwise V2 would leave the valve at its last position
        when the room switches off (real divergence, surfaced by the pre-flip
        valve diff). Other target-None modes (dry/fan_only, hvac_mode != off)
        carry no temperature and V1's target is None there too, so it skips the
        valve — we skip as well.

        The reference temperature falls back to the DEVICE's own sensor when the
        room temperature is unresolvable — V1's ``calculate_valve_position``
        does exactly that (engine_calibration.py). The value handed in here is
        V1's ``_resolve_room_temp()``, which is ``None`` for every room WITHOUT a
        configured external sensor; without the fallback
        ``calculate_valve_position`` takes its ``room_temp is None`` branch and
        returns a flat 100 — the valve wide open instead of modulated, and wide
        open again whenever a configured external sensor merely drops out.
        Per device, because V1's fallback is per TRV — which is
        why the position is no longer computed once for the whole room."""
        p = self._planner
        if p.valve_mode == "off":
            return ()
        if target.hvac_mode != HVAC_OFF and target.target_temperature is None:
            return ()  # dry/fan_only — no temperature, V1 skips the valve too

        # Fenster schlaegt Force Comfort auch an der Ventil-Entity — dieselbe
        # Regel wie in `plan()`. Ohne das fuhr `calculate_valve_position` im
        # Band 0 < Ziel - Raum < valve_diff auf valve_max, obwohl das Fenster
        # das Ziel bereits reduziert hatte. Quelle ist `window_open_resolved`
        # (wie WindowConstraint), nicht das V1-gebrueckte `window_open`.
        # BEWUSSTE V1-ABWEICHUNG: V1 (engine_calibration.py:252) uebergibt
        # force_comfort ohne Fenster-Pruefung.
        valve_force = snapshot.force_comfort and snapshot.window_open_resolved is not True

        commands: list[ValveCommand] = []
        for entity_id in self._capabilities.entity_ids:
            if target.hvac_mode == HVAC_OFF:
                position = 0  # V1 parity: close the valve when the room is off
            else:
                position = calculate_valve_position(
                    mode=p.valve_mode,
                    max_diff=p.valve_diff,
                    valve_max=p.valve_max,
                    step=p.valve_step,
                    room_temp=self._valve_reference_temp(entity_id, snapshot),
                    target_temp=target.target_temperature,
                    force_comfort=valve_force,
                    valve_min=p.valve_min,
                )
            commands.append(ValveCommand(trv_id=entity_id, position=position))
        return tuple(commands)

    @staticmethod
    def _valve_reference_temp(
        entity_id: str, snapshot: TempixSnapshot,
    ) -> float | None:
        """Room temperature for the valve curve, with V1's per-TRV fallback."""
        if snapshot.room_temperature is not None:
            return snapshot.room_temperature
        device = snapshot.device_state_for(entity_id)
        return device.current_temperature if device is not None else None

    def plan(self, target: TargetState, snapshot: TempixSnapshot) -> tuple[HardwareCommand, ...]:
        """Room TargetState → per-device commands (pure, no sending)."""
        p = self._planner
        # V1-parity (engine_calibration.calculate_changes): a configured
        # external room sensor that is unavailable freezes ALL valves —
        # emit no commands, exactly like V1's early return [].
        #
        # ONE narrow exception. `room_sensor_
        # failed` only means the EXTERNAL sensor(s) are gone — it says
        # nothing about `room_temperature_resolved`, which independently
        # falls back to a TRV's own reading (`_resolve_room_temp`) and is
        # exactly what `FrostConstraint`'s OFF-facet already reads for this
        # reason (policies/constraints.py, "needs to see the device's own
        # fallback reading to fire at all"). `target` here already carries
        # that constraint's correction if it fired — freezing unconditionally
        # would silently discard an already-computed, safety-relevant frost
        # target. Every OTHER command (comfort/eco accuracy) stays frozen,
        # matching the deliberate V1-parity decision
        # unchanged; this only lets through a target that independently
        # re-verifies as the frost floor.
        room = snapshot.room_temperature_resolved
        frost_floor_active = (
            p.frost_enabled and not p.is_cooling and target.is_off
            and room is not None and room < p.frost_temp
            and target.target_temperature == p.frost_temp
        )
        if snapshot.room_sensor_failed and not frost_floor_active:
            return ()

        factor = -1 if p.is_cooling else 1
        # Force Comfort zieht bei offenem Fenster nie auf max_temp — auch
        # nicht bei `window_open_temp == 0` mit Frostschutz, wo
        # WindowConstraint (comfort, frost_temp) statt OFF liefert und
        # `window_holds_on` unten deshalb False ist. Gemessen: Ziel heat/5.0,
        # an das Geraet ging heat/30.0. Nur die Ventilschutz-Fahrt darf das
        # Fenster uebergehen (WindowConstraint ist unter `liming_active`
        # bewusst nicht anwendbar). Die Regel gilt hier unabhaengig davon, ob
        # WindowConstraint das Ziel veraendert hat — also auch bei Idle
        # (GoalClass.SYSTEM) und bei OFF-Zielen, die `min_instead_of_off`
        # bzw. ein Geraet ohne OFF am Laufen haelt: dort bleibt es beim
        # Idle-Wert bzw. dem schwaechsten Sollwert statt max_temp. Fuer
        # `window_open_temp > 0` galt das ueber `window_holds_on` schon.
        force = (
            (snapshot.force_comfort and snapshot.window_open_resolved is not True)
            or snapshot.liming_active
        )
        # NUR die Ventilschutz-Fahrt haelt ein Geraet gegen ein OFF-Ziel
        # am Laufen — nicht Force Comfort. `WindowConstraint` entscheidet bei
        # `window_open_temp == 0` bewusst OFF und nennt die Force-Comfort-
        # Ausnahme von V1 ausdruecklich "NOT reproduced"
        # (policies/constraints.py). `force` an dieser Stelle hat genau diese
        # Entscheidung eine Ebene tiefer wieder aufgehoben: in einer
        # Messung stand der Status auf `Window Open`, waehrend der TRV mit
        # `heat/28.0` ins offene Fenster heizte. `is_applicable` dort schliesst
        # ebenfalls nur `liming_active` aus — die beiden Listen decken sich jetzt.
        keep_running = snapshot.liming_active
        # Mirror WindowConstraint's own source (window_open_resolved,
        # not the V1-bridged window_open) — otherwise this guard would stay
        # blind exactly when the hold-last-valid-state fallback kicks in, and `force` above
        # would push max_temp into the window the constraint just inhibited.
        window_holds_on = snapshot.window_open_resolved is True and p.window_open_temp > 0
        room_temp = snapshot.room_temperature

        commands: list[HardwareCommand] = []
        for cap in self._capabilities.devices:
            device = snapshot.device_state_for(cap.entity_id)
            if device is None or device.hvac_mode is None:
                continue  # unavailable → circuit-breaker territory, skip

            valve_mode = target.hvac_mode
            valve_temp = target.target_temperature
            cur_mode = device.hvac_mode
            cur_temp = device.target_temperature
            cur_valve_temp = device.current_temperature
            # V1-parity (engine_calibration.py:108): FALSY check, not
            # `is not None` — a TRV reporting the common glitch value 0.0
            # must fall back to ROOM_TEMP_FALLBACK, exactly like V1, or the
            # aggressive/hysteresis math diverges.
            ref_temp = room_temp if room_temp is not None else (
                cur_valve_temp if cur_valve_temp else ROOM_TEMP_FALLBACK)
            step = self._step_for(cap)
            has_off = cap.supports(HVAC_OFF) if cap.hvac_modes else True

            if valve_temp is not None:
                # Aggressive mode (target variant)
                if (p.aggressive_mode == "target_temp" and p.aggressive_offset > 0
                        and valve_mode != HVAC_OFF):
                    diff = (valve_temp - ref_temp) * factor
                    if diff < -p.aggressive_range:
                        valve_temp -= p.aggressive_offset * factor
                    elif diff > p.aggressive_range:
                        valve_temp += p.aggressive_offset * factor

                # Generic calibration (per device, stateful deadband)
                if p.calibration_mode == "generic" and cur_valve_temp is not None:
                    offset = cur_valve_temp - ref_temp
                    if p.aggressive_mode == "calibration":
                        diff = (valve_temp - ref_temp) * factor
                        if diff < -p.aggressive_range:
                            offset -= p.aggressive_offset * factor
                        elif diff > p.aggressive_range:
                            offset += p.aggressive_offset * factor
                    limit = p.generic_calibration_limit
                    offset = max(-limit, min(limit, offset))
                    last = self._last_generic_offsets.get(cap.entity_id, 0.0)
                    if abs(offset - last) < p.calibration_delta:
                        offset = last  # deadband against flapping
                    offset = _round_to_step(offset, step)
                    self._last_generic_offsets[cap.entity_id] = offset
                    valve_temp += offset

                # Total offset clamp ±6 °C against the ROOM target
                deviation = valve_temp - target.target_temperature
                if abs(deviation) > MAX_TOTAL_OFFSET:
                    valve_temp = target.target_temperature + (
                        MAX_TOTAL_OFFSET if deviation > 0 else -MAX_TOTAL_OFFSET)

                valve_temp = _round_to_step(valve_temp, step)

                # off_if_above_room_temp hysteresis (direction-aware, V1-exact:
                # switching OFF happens at >= target+hyst, switching back ON
                # only below target-hyst — the band prevents flapping)
                if p.off_if_above_room_temp and valve_mode != HVAC_OFF:
                    is_currently_off = cur_mode == HVAC_OFF or (
                        cur_temp is not None
                        and cur_temp <= cap.min_temp + 0.1
                        and p.min_instead_of_off
                    )
                    room_target = target.target_temperature
                    if not p.is_cooling:
                        if not is_currently_off:
                            if ref_temp >= room_target + p.hysteresis - 1e-9:
                                valve_mode = HVAC_OFF  # > or isclose (V1)
                        elif ref_temp > room_target - p.hysteresis + 1e-9:
                            valve_mode = HVAC_OFF      # strictly above re-on point
                    else:
                        if not is_currently_off:
                            if ref_temp <= room_target - p.hysteresis + 1e-9:
                                valve_mode = HVAC_OFF
                        elif ref_temp < room_target + p.hysteresis - 1e-9:
                            valve_mode = HVAC_OFF

            # dont_turn_off (V1): devices that must keep running
            if valve_mode == HVAC_OFF and (
                not has_off or p.min_instead_of_off or window_holds_on or keep_running
            ):
                # For the liming exception, `cur_mode` defeats the very
                # purpose of `keep_running` when the device is already OFF —
                # the room's own decision (`target.hvac_mode`) is what the
                # valve should actually drive to, not the state it's stuck in.
                # The other three reasons keep `cur_mode` (correct: the
                # device recovers on the next real heat demand).
                if keep_running and cur_mode == HVAC_OFF and target.hvac_mode != HVAC_OFF:
                    valve_mode = target.hvac_mode
                else:
                    valve_mode = cur_mode
                # Same direction bug as in the `force` branch below,
                # opposite sign. `min_temp` is "barely on" for heating —
                # weakest heat demand, keeps the device alive without
                # overheating. For cooling the weakest-demand extreme is
                # `max_temp` (least cooling); unconditional min_temp meant
                # this branch drove cooling devices to maximum cooling
                # instead of the minimum "keep it running" nudge it intends.
                # BEWUSSTE V1-ABWEICHUNG: V1 (engine_calibration.py:196) setzt
                # unveraendert `min_temp`. Bewusst als V2-Korrektur gesetzt.
                valve_temp = cap.max_temp if p.is_cooling else cap.min_temp

            # Force (comfort/liming) must not override a target the
            # window has already reduced — WindowConstraint's own rule is
            # "window beats even manual override" (constraints.py), force
            # driving straight to max_temp here reversed that a layer down.
            if force and valve_temp is not None and not window_holds_on:
                # "Ventil ganz auf" ist richtungsabhaengig. Beim Heizen
                # ist das `max_temp` (V1). Beim Kuehlen ist `max_temp` die
                # GERINGSTE Kuehlung — Force Comfort bewirkte dort das
                # Gegenteil seines Namens. Gemessen an einem
                # Klimageraet, mehrfach reproduziert: Ziel 23.0, an das
                # Geraet ging 30.0; mit comfort=21.0 waren es 9 K Abweichung.
                # Dieselbe Funktion berechnet oben `factor` und nutzt
                # `p.is_cooling` in den Hysterese-Zweigen — nur hier nicht.
                # `FrostConstraint` begruendet dieselbe Unterscheidung
                # ausdruecklich (constraints.py:55-57).
                # BEWUSSTE V1-ABWEICHUNG: V1 (engine_calibration.py:198) setzt
                # unveraendert `max_temp`. Bewusst als V2-Korrektur gesetzt.
                valve_temp = cap.min_temp if p.is_cooling else cap.max_temp

            if valve_mode != HVAC_OFF and valve_temp is not None:
                valve_temp = max(cap.min_temp, min(cap.max_temp, round(valve_temp, 1)))

            # Change detection against the DEVICE state (V1). Off never
            # carries a temperature (Comet-DECT bounce, V1 lesson).
            temp_to_send = None if valve_mode == HVAC_OFF else valve_temp
            if cur_mode != valve_mode or (
                valve_mode != HVAC_OFF
                and temp_to_send is not None
                and cur_temp != temp_to_send
            ):
                commands.append(HardwareCommand(
                    entity_id=cap.entity_id,
                    hvac_mode=valve_mode,
                    target_temperature=temp_to_send,
                ))
        return tuple(commands)

    def _step_for(self, cap: DeviceCapability) -> float:
        sizes = {"full": 1.0, "half": 0.5, "precise": 0.1}
        return sizes.get(self._planner.calibration_step_size, cap.target_temp_step)

    # ── dispatch ──────────────────────────────────────────────────────────

    async def dispatch(self, result: DecisionResult, snapshot: TempixSnapshot) -> DispatchSummary:
        """Plan + send. UNSAFE results trigger the hardware safeguard."""
        if result.validity is SnapshotValidity.UNSAFE:
            _LOGGER.warning(
                "decision %s is UNSAFE — hardware safeguard: nothing dispatched",
                result.snapshot_id,
            )
            return DispatchSummary()
        return await self.dispatch_commands(self.plan(result.target_state, snapshot))

    async def dispatch_commands(
        self, commands: tuple[HardwareCommand, ...],
    ) -> DispatchSummary:
        """Send an ALREADY-planned command tuple. The
        cutover flow plans once — over this dispatcher, so the calibration
        deadband (``_last_generic_offsets``) advances exactly once — uses those
        commands for the parity diff, then hands the SAME tuple here to send.
        Re-planning would advance the deadband a second time and diverge, so the
        two concerns (plan vs send) are split. The UNSAFE safeguard lives at the
        plan stage in the caller.
        """
        now = self._clock.now()
        results: list[DeviceDispatch] = []
        for idx, command in enumerate(commands):
            if self._abort_requested("commands", len(commands) - idx):
                break
            dispatch = await self._dispatch_one(command, now)
            if dispatch is not None:
                results.append(dispatch)

        summary = DispatchSummary(results=tuple(results))
        self._update_room_health(summary)
        return summary

    def _abort_requested(self, what: str, remaining: int) -> bool:
        """True, wenn der Abbruch-Hook die Sendeschleife beenden will.

        `remaining` zaehlt die NICHT BEARBEITETEN Eintraege inkl. des aktuellen —
        darunter koennen Geraete sein, die Repeat-Guard, Backoff oder `min_delta`
        ohnehin uebersprungen haetten; es ist keine Zahl nicht gesendeter Befehle.
        """
        if self._should_abort is None or not self._should_abort():
            return False
        _LOGGER.info(
            "%s: %s dispatch stopped — integration is unloading; %d entr%s not processed",
            self._name, what, remaining, "y" if remaining == 1 else "ies",
        )
        return True

    # ── valve + calibration dispatch ──────────────────────────────────────

    async def dispatch_valves(self, commands: tuple[ValveCommand, ...]) -> None:
        """Send planned valve positions. Per
        TRV: resolve the valve ``number`` entity, skip when the current position
        already matches (V1 ValvePositioner idempotency), else ``number.set_value``.
        Best-effort — NO ledger, NO circuit breaker (V1's ValvePositioner has
        neither; a missed valve write self-corrects next cycle). Dry-run sends
        nothing, so pre-flip parity is untouched."""
        if self._dry_run:
            return
        keyword = self._planner.valve_keyword
        for idx, cmd in enumerate(commands):
            if self._abort_requested("valves", len(commands) - idx):
                break
            entity = find_valve_entity(self._hass, cmd.trv_id, keyword)
            if not entity:
                continue
            cur = self._hass.states.get(entity)
            if cur is not None and cur.state not in (None, "unavailable", "unknown"):
                try:
                    if int(float(cur.state)) == cmd.position:
                        continue  # already at target — no write (V1 parity)
                except (ValueError, TypeError):
                    pass
            await safe_service_call(
                self._hass, self._name, "number", "set_value",
                {"entity_id": entity, "value": cmd.position},
            )
            _LOGGER.debug("%s: valve %s → %d%%", self._name, entity, cmd.position)

    async def dispatch_calibrations(
        self, commands: tuple[CalibrationCommand, ...], now: datetime,
    ) -> dict[str, float]:
        """Send planned entity calibration writes. Per-TRV throttle
        (``calibration_timeout``) mirrors V1's CalibrationApplier — a command
        planned this cycle but still inside the throttle window is skipped (and
        the throttle is NOT advanced, so it retries once due). Tado → offset
        service; else ``number.set_value`` after flipping the external-sensor
        select when required. Best-effort. Dry-run sends nothing / no throttle
        mutation, so pre-flip parity is untouched.

        Returns ``{trv_id: value}`` for every TRV that was ACTUALLY calibrated
        this cycle — gedrosselte Kommandos fehlen darin. Gleiche Semantik wie
        V1s ``CalibrationApplier.apply()``, damit der Coordinator post-Flip
        ``last_calibrations`` genauso fuellen kann wie vorher (vorher
        fror die Anzeige nach dem Flip ein).
        """
        applied: dict[str, float] = {}
        if self._dry_run:
            return applied
        timeout = self._planner.calibration_timeout
        for idx, cmd in enumerate(commands):
            if self._abort_requested("calibrations", len(commands) - idx):
                break
            last_t = self._last_calibration_time.get(cmd.trv_id)
            if last_t is not None and (now - last_t) < timeout:
                continue
            if cmd.is_tado:
                await safe_service_call(
                    self._hass, self._name, "tado",
                    "set_climate_temperature_offset",
                    {"entity_id": cmd.target_entity, "offset": cmd.value},
                )
            else:
                if cmd.select_entity:
                    await self._toggle_external_sensor(cmd.select_entity)
                await safe_service_call(
                    self._hass, self._name, "number", "set_value",
                    {"entity_id": cmd.target_entity, "value": cmd.value},
                )
            # V1 parity: throttle advances whenever the calls didn't raise —
            # safe_service_call swallows service errors, so a silently-failed
            # write still throttles (retried on the next due cycle).
            self._last_calibration_time[cmd.trv_id] = now
            applied[cmd.trv_id] = cmd.value
            _LOGGER.debug(
                "%s: calibration %s → %s (was %s)",
                self._name, cmd.trv_id, cmd.value, cmd.old_value,
            )
        return applied

    async def _toggle_external_sensor(self, select_entity: str) -> None:
        """Flip an external-temperature select/switch to 'external' before a
        calibration write — only when it isn't already (V1 CalibrationApplier)."""
        state = self._hass.states.get(select_entity)
        if state is None or state.state == "external":
            return
        domain = select_entity.split(".")[0]
        if domain == "select":
            await safe_service_call(
                self._hass, self._name, "select", "select_option",
                {"entity_id": select_entity, "option": "external"},
            )
        elif domain == "switch":
            await safe_service_call(
                self._hass, self._name, "switch", "turn_on",
                {"entity_id": select_entity},
            )

    async def _dispatch_one(
        self, command: HardwareCommand, now: datetime,
    ) -> DeviceDispatch | None:
        entity_id = command.entity_id
        outcome = DeviceDispatchResult.SUCCESS
        detail: str | None = None

        # Capability fallback: unsupported mode → off, typed, no exception.
        if command.hvac_mode is not None and not self._capabilities.supports(
            entity_id, command.hvac_mode
        ):
            fallback = HVAC_OFF if self._capabilities.supports(entity_id, HVAC_OFF) else None
            if fallback is None:
                # No send — ``command`` stays None so echo bookkeeping does not
                # record a phantom.
                return DeviceDispatch(
                    entity_id, DeviceDispatchResult.FALLBACK_APPLIED,
                    f"mode '{command.hvac_mode}' unsupported, no off — skipped",
                )
            detail = f"fallback {fallback} (no {command.hvac_mode})"
            outcome = DeviceDispatchResult.FALLBACK_APPLIED
            command = HardwareCommand(entity_id, hvac_mode=fallback, target_temperature=None)

        entry = self._ledger.get(entity_id)
        if entity_id in self._resync_bypass:
            self._resync_bypass.discard(entity_id)
        elif entry is not None:
            same_values = (
                entry.hvac_mode == command.hvac_mode
                and _temps_match(entry.target_temperature, command.target_temperature, 1e-9)
            )
            # Repeat-send guard, bounded by the COMMAND TTL — deliberately NOT
            # by the ledger's CONFIRMED window.
            #
            # ``plan()`` has already compared against the DEVICE state, so an
            # identical command arriving here means the device is NOT at the
            # value we last sent. Two different situations:
            #   * inside the TTL — repeating would hammer the radio, and one
            #     cycle of patience costs at most ``command_ttl``. Two sub-cases
            #     share that treatment deliberately: an UNCONFIRMED entry may
            #     simply not have reported back yet (our own send is still
            #     settling), and a CONFIRMED-then-diverged one is a real
            #     divergence we choose to sit out for the rest of the TTL rather
            #     than re-send every cycle at a hand-turned dial. The guard does
            #     NOT key on ``confirmed_at`` for that reason (pinned by a
            #     test that asserts silence inside the TTL and a correction after);
            #   * past the TTL — the device genuinely drifted away from our
            #     command: a hand-turned dial with the temporary override OFF,
            #     a dropped FRITZ!DECT frame, a foreign automation. V1 corrects
            #     that on the next heartbeat and V2 must too.
            #
            # The old gate keyed on ``LedgerEntryState`` and let CONFIRMED
            # suppress — but CONFIRMED lasts ``staleness_timeout`` (300 s), so a
            # confirmed-then-diverged device stayed uncorrected for up to five
            # minutes while the planner re-emitted the command every cycle
            # (measured). STALE/EXPIRED were never suppressed, so the CONFIRMED
            # window is the only behaviour that changes here.
            if same_values and now - entry.sent_at < self._command_ttl:
                # Inside the TTL — no repeat. Reported as SKIPPED instead of
                # dropped so the diagnostics can tell "suppressed" from
                # "nothing planned".
                return DeviceDispatch(
                    entity_id, DeviceDispatchResult.SKIPPED,
                    f"repeat guard — same command sent "
                    f"{(now - entry.sent_at).total_seconds():.0f}s ago, "
                    f"TTL {self._command_ttl.total_seconds():.0f}s",
                )
            # min_delta: suppress noise; mode changes always send.
            if (
                self._min_delta > 0
                and entry.hvac_mode == command.hvac_mode
                and entry.target_temperature is not None
                and command.target_temperature is not None
                and abs(command.target_temperature - entry.target_temperature) < self._min_delta
            ):
                return DeviceDispatch(
                    entity_id, DeviceDispatchResult.SKIPPED,
                    f"min_delta {self._min_delta} — change too small",
                )

        if self._dry_run:
            # Pre-flip: compute, send nothing, ledger untouched —
            # and no ``command``, so nothing downstream counts this as sent.
            return DeviceDispatch(entity_id, outcome, "dry_run")

        # Circuit breaker: a device still inside its backoff
        # window is skipped — no send, health left untouched (V1 parity). The
        # dry-run branch above returns first, so pre-flip parity is unaffected.
        if self._in_backoff(entity_id, now):
            _LOGGER.debug("%s: circuit breaker skipping %s (backoff)", self._name, entity_id)
            # SKIPPED, not dropped: this is the one suppression that can last
            # 2–120 min and survives a restart (``restore_cb``), while the
            # RoomHealth that would explain it does not — the room then looks
            # healthy while nothing is being sent to it. ``retry_after``
            # is carried in the detail because it is the datum that names the
            # cause; it is NOT in the diagnostics export otherwise.
            cb = self._cb_state.get(entity_id) or {}
            retry_after = cb.get("retry_after")
            return DeviceDispatch(
                entity_id, DeviceDispatchResult.SKIPPED,
                f"circuit breaker backoff — {cb.get('failures', 0)} failure(s), "
                f"retry after {retry_after.isoformat() if retry_after else '?'}",
            )

        # From here the send is ATTEMPTED, so ``command`` is carried on both
        # exits. A FAILED call may still have partially landed on the device —
        # V1 tracks its echo before the call for exactly that reason, and the
        # entry ages out via the command TTL if no echo arrives.
        if self._on_send is not None:
            self._on_send(command)
        error = await self._send(command, now, self._command_ttl)
        if error is not None:
            self._register_cb_failure(entity_id, now)
            return DeviceDispatch(
                entity_id, DeviceDispatchResult.FAILED, error, command=command)
        self._register_cb_success(entity_id)
        return DeviceDispatch(entity_id, outcome, detail, command=command)

    async def _send(
        self, command: HardwareCommand, now: datetime, ttl: timedelta,
    ) -> str | None:
        """Send one command to HA and record it in the ledger. Returns an
        error string on failure (device failures must not cascade), else
        None. Shared by the regular dispatch and the resend sweep so both
        paths keep identical service-call/ledger semantics.

        The two calls go through ``safe_service_call``
        (30 s timeout + exponential retry, FRITZ!DECT radio parity), with the
        configured ``action_delay`` gap between the mode and the temperature
        call — exactly like V1's separate-call branch. A persistent failure
        raises out of ``safe_service_call`` (``raise_on_failure``) and is
        caught here so a dead device never cascades; the caller trips the
        circuit breaker. The off-command carries no temperature (Comet-DECT
        bounce lesson, preserved)."""
        entity_id = command.entity_id
        sends_temperature = (
            command.target_temperature is not None and command.hvac_mode != HVAC_OFF
        )
        try:
            if command.hvac_mode is not None:
                await safe_service_call(
                    self._hass, self._name, "climate", "set_hvac_mode",
                    {"entity_id": entity_id, "hvac_mode": command.hvac_mode},
                    raise_on_failure=True,
                )
                if sends_temperature and self._action_delay.total_seconds() > 0:
                    await asyncio.sleep(self._action_delay.total_seconds())
            if sends_temperature:
                await safe_service_call(
                    self._hass, self._name, "climate", "set_temperature",
                    {"entity_id": entity_id, "temperature": command.target_temperature},
                    raise_on_failure=True,
                )
        except Exception as err:  # noqa: BLE001 — device failures must not cascade
            _LOGGER.warning("dispatch to %s failed: %s", entity_id, err)
            return str(err)

        self._ledger[entity_id] = LedgerEntry(
            entity_id=entity_id,
            hvac_mode=command.hvac_mode,
            target_temperature=command.target_temperature,
            sent_at=now, ttl=ttl,
        )
        return None

    # ── circuit breaker (V1 parity) ───────────────────────────────────────

    def _in_backoff(self, entity_id: str, now: datetime) -> bool:
        cb = self._cb_state.get(entity_id)
        return cb is not None and cb["retry_after"] is not None and now < cb["retry_after"]

    def _register_cb_failure(self, entity_id: str, now: datetime) -> None:
        """A send failed → grow the per-TRV backoff (2 → 4 → 8 → … → 120 min)."""
        cb = self._cb_state.setdefault(entity_id, {"failures": 0, "retry_after": None})
        cb["failures"] += 1
        backoff_min = min(
            _CB_BACKOFF_BASE_MINUTES * (2 ** (cb["failures"] - 1)),
            _CB_BACKOFF_CAP_MINUTES,
        )
        cb["retry_after"] = now + timedelta(minutes=backoff_min)
        _LOGGER.warning(
            "%s: %s failed (%d) — circuit breaker pausing for %dmin",
            self._name, entity_id, cb["failures"], backoff_min,
        )
        if self._on_cb_change is not None:
            self._on_cb_change()

    def _register_cb_success(self, entity_id: str) -> None:
        """A send succeeded → reset the breaker for this TRV (only when it was
        actually tripped, so a healthy device never triggers a persist)."""
        cb = self._cb_state.get(entity_id)
        if cb is None or cb["failures"] == 0:
            return
        _LOGGER.info("%s: %s recovered — circuit breaker reset", self._name, entity_id)
        cb["failures"] = 0
        cb["retry_after"] = None
        if self._on_cb_change is not None:
            self._on_cb_change()

    def _capability_staleness(self, entity_id: str) -> float:
        cap = self._capabilities.for_entity(entity_id)
        return cap.staleness_timeout if cap is not None else 300.0

    # ── reconciliation support ───────────────────────────────────────────

    async def resend_expired_sweep(self) -> DispatchSummary:
        """Re-send all EXPIRED ledger entries — explicitly NO re-evaluation:
        this method never touches the engine, it only repeats the failed
        transmission.

        **NOT WIRED UP — nothing calls this today.**
        Kept deliberately; read this before wiring it in.

        The case it was written for — a service call that HA accepted but whose
        radio frame never reached the device, so no exception fires and the
        circuit breaker stays blind — is covered by the REGULAR cycle instead:
        ``plan()`` compares against the DEVICE state, so a lost command still
        shows the old value and gets re-planned, and ``_dispatch_one`` lets it
        through because the entry is past its TTL. The repair lands one
        heartbeat after the TTL elapses.

        Wiring the sweep in on top would DIVERGE from V1 in two places, because
        it sources the command from the ledger rather than from the planner:

        * ``room_sensor_failed`` — ``plan()`` returns ``()`` and freezes every
          valve (V1 parity, ``calculate_changes`` does the same). The sweep does
          not know that flag and would keep re-sending the last command.
        * an ``unavailable`` device — ``plan()`` skips it (no device state, no
          command), exactly like V1. The sweep would send anyway, HA would
          raise, and the circuit breaker would back off a device that is simply
          offline — which the ``trv_unavailable`` repair issue already covers.

        So this is not a gap waiting to be closed; deciding its fate belongs to
        the V1 removal, when V1's apply loop disappears and the reconcile story is
        rewritten as a whole.
        """
        now = self._clock.now()
        results: list[DeviceDispatch] = []
        for entity_id, entry in list(self._ledger.items()):
            staleness = timedelta(seconds=self._capability_staleness(entity_id))
            if entry.state_at(now, staleness) is not LedgerEntryState.EXPIRED:
                continue
            if self._dry_run:
                results.append(DeviceDispatch(
                    entity_id, DeviceDispatchResult.SUCCESS, "dry_run"))
                continue
            # Circuit breaker: a device inside its backoff
            # window is skipped by the resend sweep too — the breaker must not
            # be bypassed via the reconcile path.
            if self._in_backoff(entity_id, now):
                continue
            command = HardwareCommand(
                entity_id, hvac_mode=entry.hvac_mode,
                target_temperature=entry.target_temperature,
            )
            error = await self._send(command, now, entry.ttl)
            if error is not None:
                self._register_cb_failure(entity_id, now)
                results.append(DeviceDispatch(
                    entity_id, DeviceDispatchResult.FAILED, error))
                continue
            self._register_cb_success(entity_id)
            results.append(DeviceDispatch(
                entity_id, DeviceDispatchResult.SUCCESS, "resend"))
        summary = DispatchSummary(results=tuple(results))
        self._update_room_health(summary)
        return summary

    # ── health ────────────────────────────────────────────────────────────

    def _update_room_health(self, summary: DispatchSummary) -> None:
        # Health is derived from ATTEMPTED dispatches. SKIPPED entries
        # are suppressions, not outcomes — counting them would let a cycle in
        # which every device was suppressed report HEALTHY and thereby erase a
        # preceding OFFLINE (earlier they were dropped from ``results``
        # and this filter was implicit). The same is true for a FALLBACK_APPLIED with no ``off`` to
        # fall back to (or a dry-run) — ``command is None`` means nothing left
        # the dispatcher, so it must not count as a healthy attempt either.
        attempted = tuple(
            r for r in summary.results
            if r.result is not DeviceDispatchResult.SKIPPED and r.command is not None
        )
        if not attempted:
            return  # nothing attempted → health unchanged
        failed = len(summary.failed_entities)
        if failed == 0:
            self._room_health = RoomHealth.HEALTHY
        elif failed == len(attempted):
            self._room_health = RoomHealth.OFFLINE
        else:
            self._room_health = RoomHealth.DEGRADED
